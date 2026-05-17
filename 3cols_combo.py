RATES_URL = "https://www.ameren.com/api/ameren/promotion/RtpHourlyPricesbyDate"
CACHE_FILENAME = "cached_rates.json"
RATE_THOUGHTS_FILENAME = "rate_thoughts.json"
RATE_PROMPT_FILENAME = "PROCESS_RATE_PROMPT.md"
HIGH_PRICE_THRESHOLD = 10
DEFAULT_bar = 5

import argparse
from contextlib import contextmanager
import json
import math
import os
import re
import select
import shutil
import sys
import termios
import textwrap
import threading
import time
import tty
from datetime import datetime, timedelta
from pathlib import Path

import requests

from hour_utils import detect_hour_offset, normalize_hour, hour_to_time, shift_hours_if_last_zero
from ptc_pull import PTC_FAILURE_VALUE, get_cached_price_to_compare

# ANSI escape sequences for terminal colors
BOLD = '\033[1m'
BLACK = '\033[30m'
WHITE = '\033[97m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
BG_GREEN = '\033[42m'
BG_YELLOW = '\033[43m'
BG_RED = '\033[41m'
BG_WHITE = '\033[47m'
BG_BLUE = '\033[44m'
RESET = '\033[0m'
CENT = '¢'
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CLEAR_SCREEN = "\033[2J\033[H"
CLEAR_LINE = "\033[2K"
REFRESH_SECONDS = 60
BASE_DIR = Path(__file__).resolve().parent
RATE_ANALYSIS_LOCK = threading.Lock()
RATE_ANALYSIS_IN_FLIGHT = set()


def _color_for_price(price, thresholds, is_max=False):
	if is_max:
		return RESET
	if price > HIGH_PRICE_THRESHOLD or price > thresholds[1]:
		return RED
	if price <= thresholds[0]:
		return GREEN
	return YELLOW


def _bar_length(price, max_price_cents, bar):
	if price < 0:
		return 1
	if max_price_cents > 0:
		length = int(round((price / max_price_cents) * bar))
	else:
		length = 0
	return max(1, length)


def _bar_palette(price, thresholds, is_max=False):
	color = _color_for_price(price, thresholds, is_max=is_max)
	if is_max:
		return color, BLACK, BG_WHITE
	if color == RED:
		return color, WHITE, BG_RED
	if color == GREEN:
		return color, BLACK, BG_GREEN
	return color, BLACK, BG_YELLOW


def _overflow_text_color(price, thresholds, is_max=False):
	if is_max:
		return WHITE
	return _color_for_price(price, thresholds)


def _render_positive_bar(
	text,
	width,
	text_color,
	background_color,
	overflow_color,
):
	render_width = max(width, len(text))
	parts = []

	for index in range(render_width):
		char = text[index] if index < len(text) else " "
		if index < width:
			parts.append(f"{background_color}{text_color}{char}{RESET}")
		elif char != " ":
			parts.append(f"{overflow_color}{char}{RESET}")
		else:
			parts.append(char)

	return "".join(parts).rstrip()


def colorize_price(
	price,
	thresholds,
	max_price_cents,
	should_highlight=False,
	is_max=False,
	bar=DEFAULT_bar,
):
	if bar <= 0:
		raise ValueError("bar must be greater than 0")

	price_text = f"{CENT}{price:.1f}"

	if price < 0:
		color = _color_for_price(price, thresholds, is_max=is_max)
		combined = f"{color}▒{price_text}{RESET}"
	else:
		fill_length = _bar_length(price, max_price_cents, bar)
		_, text_color, background_color = _bar_palette(price, thresholds, is_max=is_max)
		overflow_color = _overflow_text_color(price, thresholds, is_max=is_max)
		combined = _render_positive_bar(
			price_text,
			fill_length,
			text_color,
			background_color,
			overflow_color,
		)

	prefix = f"{BOLD}" if should_highlight else ""
	postfix = "<" if should_highlight else ""
	return f"{prefix}{combined}{RESET}{postfix}"


def fetch_or_load_rates():
	# Use ISO date for cache key, but send "Month DD, YYYY" to the API per new contract
	today_iso = datetime.now().date().isoformat()
	today_display = datetime.now().strftime("%B %d, %Y")

	# Try to read cache for today's data
	if os.path.exists(CACHE_FILENAME):
		with open(CACHE_FILENAME, "r") as f:
			try:
				cached = json.load(f)
				if cached.get("date") == today_iso and cached.get("data"):
					return cached.get("data")
			except json.JSONDecodeError:
				pass

	# Fetch fresh data: try new date format first, then fallback to old format if needed
	payload_new = {"SelectedDate": today_display}
	response = requests.post(RATES_URL, json=payload_new)

	if response.status_code != 200:
		# Fallback to previous ISO format if the new format fails for any reason
		payload_old = {"SelectedDate": today_iso}
		response = requests.post(RATES_URL, json=payload_old)

	if response.status_code == 200:
		data = response.json()
		with open(CACHE_FILENAME, "w") as f:
			json.dump({
				"date": today_iso,
				"requestedDate": today_display,
				"data": data,
			}, f)
		get_cached_price_to_compare(today_iso)
		return data

	raise RuntimeError(f"Failed to fetch rates: {response.status_code} - {response.text}")


def apply_price_to_compare_threshold():
	global HIGH_PRICE_THRESHOLD

	today_iso = datetime.now().date().isoformat()
	cached_price = get_cached_price_to_compare(today_iso)
	if cached_price == PTC_FAILURE_VALUE:
		HIGH_PRICE_THRESHOLD = 10
		return False

	try:
		HIGH_PRICE_THRESHOLD = float(cached_price)
	except (TypeError, ValueError):
		HIGH_PRICE_THRESHOLD = 10
		return False

	return True


def _load_env_file(path=None):
	path = path or BASE_DIR / ".env"
	if not path.exists():
		return

	with path.open("r", encoding="utf-8") as f:
		for raw_line in f:
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			key = key.strip()
			value = value.strip().strip('"').strip("'")
			if key and key not in os.environ:
				os.environ[key] = value


def _env_bool(name, default=True):
	value = os.getenv(name)
	if value is None:
		return default
	return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name, default):
	try:
		return float(os.getenv(name, default))
	except (TypeError, ValueError):
		return default


def _env_int(name, default):
	try:
		return int(os.getenv(name, default))
	except (TypeError, ValueError):
		return default


def load_rate_ai_config():
	_load_env_file()
	model = os.getenv("RATE_OPENAI_MODEL_ID") or os.getenv("OPENAI_MODEL_ID", "")
	return {
		"enabled": _env_bool("RATE_ANALYSIS_ENABLED", default=True),
		"endpoint": os.getenv("RATE_OPENAI_ENDPOINT") or os.getenv("OPENAI_ENDPOINT", "http://127.0.0.1:6767/v1"),
		"api_key": os.getenv("RATE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
		"model": model,
		"temperature": _env_float("RATE_ANALYSIS_TEMPERATURE", 0.2),
		"max_tokens": _env_int("RATE_ANALYSIS_MAX_TOKENS", 700),
		"extra_prompt": os.getenv("RATE_EXTRA_PROMPT") or os.getenv("EXTRA_PROMPT", ""),
	}


def rate_ai_is_configured(config=None):
	config = config or load_rate_ai_config()
	return bool(config["enabled"] and config["model"])


def load_rate_prompt_template():
	prompt_path = BASE_DIR / RATE_PROMPT_FILENAME
	if prompt_path.exists():
		return prompt_path.read_text(encoding="utf-8")
	return (
		"You are an electricity rate analyst. Analyze the provided Ameren hourly "
		"rate data and return concise practical guidance.\n\n"
		"Analysis kind: {{ANALYSIS_KIND}}\n\n"
		"{{EXTRA_PROMPT}}\n\n"
		"RATE DATA:\n{{RATE_DATA}}\n"
	)


def _clean_llm_response(raw):
	text = raw.strip()
	if text.startswith("{"):
		try:
			obj = json.loads(text)
			if isinstance(obj, dict) and "message" in obj:
				text = obj["message"]
		except (json.JSONDecodeError, TypeError):
			pass
	return text.replace("\\n", "\n").replace("\\t", "\t")


def query_rate_llm(prompt, config):
	from openai import OpenAI

	client = OpenAI(
		base_url=config["endpoint"],
		api_key=config["api_key"] or "not-needed",
	)
	response = client.chat.completions.create(
		model=config["model"],
		messages=[{"role": "user", "content": prompt}],
		temperature=config["temperature"],
		max_tokens=config["max_tokens"],
	)
	return _clean_llm_response(response.choices[0].message.content or "")


def _read_json_file(path, default):
	if not path.exists():
		return default
	try:
		with path.open("r", encoding="utf-8") as f:
			data = json.load(f)
		return data if isinstance(data, dict) else default
	except (OSError, json.JSONDecodeError):
		return default


def load_rate_thoughts():
	return _read_json_file(BASE_DIR / RATE_THOUGHTS_FILENAME, {})


def save_rate_thoughts(thoughts):
	path = BASE_DIR / RATE_THOUGHTS_FILENAME
	with path.open("w", encoding="utf-8") as f:
		json.dump(thoughts, f, indent=2, ensure_ascii=False)
		f.write("\n")


def _analysis_keys(now=None):
	now = now or datetime.now()
	return now.date().isoformat(), now.strftime("%Y-%m-%dT%H")


def _rate_records(hourly_details):
	shifted = shift_hours_if_last_zero(hourly_details)
	if not shifted:
		return []

	hour_offset = detect_hour_offset(shifted)
	records = []
	for item in shifted:
		normalized_hour = normalize_hour(item["hour"], hour_offset)
		records.append({
			"hour": f"{normalized_hour:02d}:00",
			"display": hour_to_time(item["hour"], hour_offset),
			"price_cents": round(item["price"] * 100, 3),
		})
	return records


def build_rate_analysis_context(hourly_details, now=None):
	now = now or datetime.now()
	records = _rate_records(hourly_details)
	prices = [record["price_cents"] for record in records]
	current_record = next((record for record in records if record["hour"] == f"{now.hour:02d}:00"), None)
	upcoming = [
		record for record in records
		if int(record["hour"].split(":", 1)[0]) >= now.hour
	][:6]

	return {
		"date": now.date().isoformat(),
		"hour_key": now.strftime("%Y-%m-%dT%H"),
		"generated_at": now.isoformat(timespec="seconds"),
		"high_price_threshold_cents": HIGH_PRICE_THRESHOLD,
		"current_hour": current_record,
		"upcoming_hours": upcoming,
		"daily": {
			"min_cents": min(prices) if prices else None,
			"max_cents": max(prices) if prices else None,
			"average_cents": round(sum(prices) / len(prices), 3) if prices else None,
		},
		"hourly_prices": records,
	}


def build_rate_prompt(template, analysis_kind, context, config):
	extra_prompt = config.get("extra_prompt") or ""
	template = template.replace("{{ANALYSIS_KIND}}", analysis_kind)
	template = template.replace("{{RATE_DATA}}", json.dumps(context, indent=2))
	if extra_prompt:
		template = template.replace("{{EXTRA_PROMPT}}", extra_prompt)
	else:
		template = template.replace("{{EXTRA_PROMPT}}\n\n", "").replace("{{EXTRA_PROMPT}}", "")
	return template


def _needed_analysis_jobs(thoughts, now=None):
	date_key, hour_key = _analysis_keys(now)
	jobs = []
	if thoughts.get("date") != date_key or not thoughts.get("daily_statement"):
		jobs.append(("daily", f"daily:{date_key}"))
	if thoughts.get("hour_key") != hour_key or not thoughts.get("hourly_statement"):
		jobs.append(("hourly", f"hourly:{hour_key}"))
	return jobs


def ensure_rate_analysis_background(hourly_details, now=None):
	now = now or datetime.now()
	config = load_rate_ai_config()
	if not hourly_details or not rate_ai_is_configured(config):
		return False

	with RATE_ANALYSIS_LOCK:
		thoughts = load_rate_thoughts()
		jobs = [
			job for job in _needed_analysis_jobs(thoughts, now)
			if job[1] not in RATE_ANALYSIS_IN_FLIGHT
		]
		if not jobs:
			return False
		for _, job_key in jobs:
			RATE_ANALYSIS_IN_FLIGHT.add(job_key)

	thread = threading.Thread(
		target=_run_rate_analysis_jobs,
		args=(list(hourly_details), now, jobs, config),
		daemon=True,
	)
	thread.start()
	return True


def _run_rate_analysis_jobs(hourly_details, now, jobs, config):
	date_key, hour_key = _analysis_keys(now)
	job_keys = [job_key for _, job_key in jobs]

	try:
		template = load_rate_prompt_template()
		context = build_rate_analysis_context(hourly_details, now=now)
		with RATE_ANALYSIS_LOCK:
			thoughts = load_rate_thoughts()
			if thoughts.get("date") != date_key:
				thoughts.pop("daily_statement", None)
				thoughts.pop("daily_generated_at", None)
			if thoughts.get("hour_key") != hour_key:
				thoughts.pop("hourly_statement", None)
				thoughts.pop("hourly_generated_at", None)
			thoughts.update({
				"date": date_key,
				"hour_key": hour_key,
				"analysis_status": "running",
				"analysis_error": "",
				"model": config["model"],
			})
			save_rate_thoughts(thoughts)

		for analysis_kind, _ in jobs:
			statement = query_rate_llm(
				build_rate_prompt(template, analysis_kind, context, config),
				config,
			)
			with RATE_ANALYSIS_LOCK:
				thoughts = load_rate_thoughts()
				thoughts["date"] = date_key
				thoughts["hour_key"] = hour_key
				thoughts["model"] = config["model"]
				thoughts["analysis_status"] = "running"
				if analysis_kind == "daily":
					thoughts["daily_statement"] = statement
					thoughts["daily_generated_at"] = datetime.now().isoformat(timespec="seconds")
				else:
					thoughts["hourly_statement"] = statement
					thoughts["hourly_generated_at"] = datetime.now().isoformat(timespec="seconds")
				save_rate_thoughts(thoughts)

		with RATE_ANALYSIS_LOCK:
			thoughts = load_rate_thoughts()
			thoughts["analysis_status"] = "ready"
			thoughts["analysis_error"] = ""
			save_rate_thoughts(thoughts)
	except Exception as exc:
		with RATE_ANALYSIS_LOCK:
			thoughts = load_rate_thoughts()
			thoughts.update({
				"date": date_key,
				"hour_key": hour_key,
				"analysis_status": "error",
				"analysis_error": str(exc),
			})
			save_rate_thoughts(thoughts)
	finally:
		with RATE_ANALYSIS_LOCK:
			for job_key in job_keys:
				RATE_ANALYSIS_IN_FLIGHT.discard(job_key)


def _visible_width(value):
	return len(ANSI_RE.sub("", value))


def _pad_cell(value, width):
	return f"{value}{' ' * max(0, width - _visible_width(value))}"


def _format_plain_table(rows, headers):
	widths = []
	for index, header in enumerate(headers):
		cell_widths = [_visible_width(row[index]) for row in rows]
		widths.append(max([_visible_width(header)] + cell_widths))

	formatted = []
	for row in [headers] + rows:
		cells = []
		for index, value in enumerate(row):
			if index == len(row) - 1:
				cells.append(value)
			else:
				cells.append(_pad_cell(value, widths[index]))
		formatted.append(" ".join(cells).rstrip())

	return "\n".join(formatted)


def build_table(hourly_details, now=None, bar=DEFAULT_bar):
	if bar <= 0:
		raise ValueError("bar must be greater than 0")

	hourly_details = shift_hours_if_last_zero(hourly_details)
	if not hourly_details:
		raise ValueError("No hourly price data available.")

	hour_offset = detect_hour_offset(hourly_details)

	all_prices = [item["price"] for item in hourly_details]
	sorted_prices = sorted(all_prices)
	n = len(sorted_prices)
	lower_third = sorted_prices[n // 3]
	upper_third = sorted_prices[(2 * n) // 3]
	max_price = max(all_prices)

	first_half = []
	second_half = []
	now = now or datetime.now()
	current_hour = now.hour
	current_time_marker = f">{BOLD}{now.hour:02}:{now.minute:02}{RESET}<"

	for index, item in enumerate(hourly_details):
		time_label = hour_to_time(item["hour"], hour_offset)
		normalized_hour = normalize_hour(item["hour"], hour_offset)

		highlight_price = normalized_hour == current_hour
		if highlight_price or (normalized_hour + 12) % 24 == current_hour:
			time_label = current_time_marker

		price = round(item["price"] * 100, 1)
		combined_cell = colorize_price(
			price,
			(lower_third * 100, upper_third * 100),
			max_price * 100,
			should_highlight=highlight_price,
			is_max=(item["price"] == max_price),
			bar=bar,
		)

		if index < 12:
			first_half.append([time_label, combined_cell])
		else:
			second_half.append([combined_cell])

	while len(second_half) < len(first_half):
		second_half.append([""])

	return [left + right for left, right in zip(first_half, second_half)]


def _positive_int(value):
	parsed = int(value)
	if parsed <= 0:
		raise argparse.ArgumentTypeError("max bar width must be greater than 0")
	return parsed


def seconds_until_next_minute(now=None):
	now = now or datetime.now()
	next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
	return max(0.0, (next_minute - now).total_seconds())


def _format_countdown(seconds_remaining):
	total_seconds = max(0, int(math.ceil(seconds_remaining)))
	minutes, seconds = divmod(total_seconds, 60)
	return f"{minutes:02}:{seconds:02}"


def _format_refresh_seconds(seconds_remaining):
	total_seconds = max(0, int(math.ceil(seconds_remaining)))
	return f":{min(99, total_seconds):02d}"


def _terminal_size():
	size = shutil.get_terminal_size(fallback=(80, 24))
	return size.columns, size.lines


def _bottom_bar_plain_text(next_refresh_at, now=None, width=None):
	now = now or datetime.now()
	remaining = (next_refresh_at - now).total_seconds()
	width = width or _terminal_size()[0]
	left = " [i] Analysis "
	right = f" {_format_refresh_seconds(remaining)} "
	padding = max(1, width - len(left) - len(right))
	return (left + (" " * padding) + right)[:width]


def _bottom_bar_line(next_refresh_at, now=None, width=None):
	return f"{BG_BLUE}{WHITE}{_bottom_bar_plain_text(next_refresh_at, now=now, width=width)}{RESET}"


def _write_bottom_bar(next_refresh_at, output=None, now=None):
	output = output or sys.stdout
	width, height = _terminal_size()
	output.write(f"\033[{height};1H\r{CLEAR_LINE}{_bottom_bar_line(next_refresh_at, now=now, width=width)}")
	output.flush()


def render_screen(
	bar=DEFAULT_bar,
	output=None,
	clear_screen=False,
	next_refresh_at=None,
	now=None,
	start_analysis=True,
):
	output = output or sys.stdout
	now = now or datetime.now()

	if clear_screen:
		print(CLEAR_SCREEN, end="", file=output)

	data = fetch_or_load_rates()
	ptc_loaded = apply_price_to_compare_threshold()
	hourly_details = data.get("hourlyPriceDetails") or []
	table = build_table(hourly_details, now=now, bar=bar)
	if start_analysis:
		ensure_rate_analysis_background(hourly_details, now=now)
	print(_format_plain_table(table, headers=["Hour", "AM", "PM"]), file=output)
	if not ptc_loaded:
		print(f"{RED}PTC FETCH FAILURE{RESET}", file=output)
	if next_refresh_at is not None:
		_write_bottom_bar(next_refresh_at, output=output, now=now)
	output.flush()


def _update_countdown_line(next_refresh_at, output=None):
	_write_bottom_bar(next_refresh_at, output=output)


def _wrap_analysis_text(text, width):
	lines = []
	for paragraph in text.splitlines():
		if not paragraph.strip():
			lines.append("")
		else:
			lines.extend(textwrap.wrap(paragraph, width=max(20, width)) or [""])
	return lines


def _analysis_display_text(now=None):
	now = now or datetime.now()
	config = load_rate_ai_config()
	thoughts = load_rate_thoughts()
	date_key, hour_key = _analysis_keys(now)

	if not rate_ai_is_configured(config):
		return (
			"Analysis is not configured yet.\n\n"
			"Copy rate_analysis.env.example to .env and set RATE_OPENAI_MODEL_ID, "
			"RATE_OPENAI_ENDPOINT, and RATE_OPENAI_API_KEY if your endpoint requires one."
		)

	status = thoughts.get("analysis_status", "idle")
	error = thoughts.get("analysis_error", "")
	daily = thoughts.get("daily_statement") if thoughts.get("date") == date_key else ""
	hourly = thoughts.get("hourly_statement") if thoughts.get("hour_key") == hour_key else ""

	sections = []
	if hourly:
		sections.append(f"Hourly action ({hour_key})\n{hourly}")
	else:
		sections.append(f"Hourly action ({hour_key})\nAnalysis is warming up.")

	if daily:
		sections.append(f"Daily analysis ({date_key})\n{daily}")
	else:
		sections.append(f"Daily analysis ({date_key})\nAnalysis is warming up.")

	if status == "running":
		sections.append("Status\nGenerating in the background.")
	elif status == "error" and error:
		sections.append(f"Status\nLLM error: {error}")
	elif thoughts.get("model"):
		sections.append(f"Status\nReady from {thoughts['model']}.")

	return "\n\n".join(sections)


def render_analysis_screen(output=None, clear_screen=False, next_refresh_at=None, now=None):
	output = output or sys.stdout
	now = now or datetime.now()
	width, height = _terminal_size()
	body_height = max(1, height - 2)
	lines = _wrap_analysis_text(_analysis_display_text(now=now), width - 1)

	if clear_screen:
		print(CLEAR_SCREEN, end="", file=output)

	for line in lines[:body_height]:
		print(line[:width - 1], file=output)

	if next_refresh_at is not None:
		_write_bottom_bar(next_refresh_at, output=output, now=now)
	output.flush()


@contextmanager
def terminal_key_reader(input_stream=None):
	input_stream = input_stream or sys.stdin
	if not input_stream.isatty():
		def no_key(timeout=0):
			if timeout:
				time.sleep(timeout)
			return ""
		yield no_key
		return

	fd = input_stream.fileno()
	old_settings = termios.tcgetattr(fd)
	tty.setcbreak(fd)
	try:
		def read_key(timeout=0):
			ready, _, _ = select.select([input_stream], [], [], timeout)
			if not ready:
				return ""
			return input_stream.read(1)

		yield read_key
	finally:
		termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _refresh_screen(view_mode, args, output, next_refresh_at):
	if view_mode == "analysis":
		data = fetch_or_load_rates()
		apply_price_to_compare_threshold()
		hourly_details = data.get("hourlyPriceDetails") or []
		ensure_rate_analysis_background(hourly_details)
		render_analysis_screen(
			output=output,
			clear_screen=True,
			next_refresh_at=next_refresh_at,
		)
	else:
		render_screen(
			bar=args.bar,
			output=output,
			clear_screen=True,
			next_refresh_at=next_refresh_at,
		)


def run_refresh_loop(args, output=None):
	output = output or sys.stdout
	next_delay = seconds_until_next_minute()
	view_mode = "rates"

	with terminal_key_reader() as read_key:
		while True:
			next_refresh_at = datetime.now() + timedelta(seconds=next_delay)
			refresh_ready = threading.Event()
			timer = threading.Timer(next_delay, refresh_ready.set)
			timer.daemon = True
			timer.start()

			try:
				_refresh_screen(view_mode, args, output, next_refresh_at)
				while not refresh_ready.is_set():
					_update_countdown_line(next_refresh_at, output=output)
					key = read_key(timeout=1)
					if key in {"i", "I"}:
						view_mode = "analysis" if view_mode == "rates" else "rates"
						_refresh_screen(view_mode, args, output, next_refresh_at)
					elif key in {"q", "Q"}:
						return
				_update_countdown_line(next_refresh_at, output=output)
			finally:
				timer.cancel()

			next_delay = REFRESH_SECONDS


def parse_args(argv=None):
	parser = argparse.ArgumentParser(description="Show Ameren hourly rates with prices overlaid on bars.")
	parser.add_argument(
		"-bar",
		type=_positive_int,
		default=DEFAULT_bar,
		help=f"Maximum width used to scale the colored bars (default: {DEFAULT_bar})",
	)
	parser.add_argument(
		"--once",
		action="store_true",
		help="Render once and exit instead of refreshing every minute.",
	)
	return parser.parse_args(argv)


def main(argv=None):
	args = parse_args(argv)
	if args.once:
		render_screen(bar=args.bar, start_analysis=False)
		return
	run_refresh_loop(args)


if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		print("\nStopped.")
	except Exception as e:
		print(f"Error: {e}")
