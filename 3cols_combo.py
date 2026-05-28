RATES_URL = "https://www.ameren.com/api/ameren/promotion/RtpHourlyPricesbyDate"
CACHE_FILENAME = "cached_rates.json"
TOMORROW_CACHE_FILENAME = "tomorrow_cached.json"
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
BG_CYAN = '\033[46m'
RESET = '\033[0m'
CENT = '¢'
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CLEAR_SCREEN = "\033[2J\033[H"
CLEAR_LINE = "\033[2K"
KEY_UP = "UP"
KEY_DOWN = "DOWN"
KEY_PAGE_UP = "PAGE_UP"
KEY_PAGE_DOWN = "PAGE_DOWN"
KEY_MOUSE_UP = "MOUSE_UP"
KEY_MOUSE_DOWN = "MOUSE_DOWN"
MOUSE_ENABLE = "\033[?1000h\033[?1006h"
MOUSE_DISABLE = "\033[?1000l\033[?1006l"
REFRESH_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 50
BASE_DIR = Path(__file__).resolve().parent
RATE_ANALYSIS_LOCK = threading.RLock()
RATE_ANALYSIS_IN_FLIGHT = set()
RATE_ANALYSIS_QUEUE = []
RATE_ANALYSIS_WORKER = None
UNSET = object()


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


def _cache_path(filename):
	path = Path(filename)
	return path if path.is_absolute() else BASE_DIR / path


def _target_day_parts(target_date):
	return target_date.isoformat(), target_date.strftime("%B %d, %Y")


def _selected_date_payloads(target_date):
	target_iso, target_display = _target_day_parts(target_date)
	return [
		("display", target_display),
		("iso", target_iso),
	]


def _hour_key_for_time(now=None):
	return (now or datetime.now()).strftime("%Y-%m-%dT%H")


def _selected_date_to_iso(value):
	if not value:
		return ""
	try:
		return datetime.strptime(value, "%B %d, %Y").date().isoformat()
	except ValueError:
		return ""


def _rate_day_iso(data):
	if not isinstance(data, dict):
		return ""

	selected_date = _selected_date_to_iso(data.get("selectedDate"))
	if selected_date:
		return selected_date

	hourly_details = data.get("hourlyPriceDetails") or []
	for item in hourly_details:
		date_value = item.get("date")
		if isinstance(date_value, str) and date_value:
			return date_value.split("T", 1)[0]
	return ""


def _hour_values(hourly_details):
	hours = set()
	for item in hourly_details or []:
		hour_value = str(item.get("hour", "")).strip()
		if not hour_value:
			continue
		if hour_value.isdigit():
			hours.add(f"{int(hour_value):02d}")
		else:
			hours.add(hour_value)
	return hours


def _has_complete_next_day_rates(data, target_date_iso):
	if not isinstance(data, dict):
		return False
	if data.get("isErrorFetchingData"):
		return False
	if not data.get("isNextDay"):
		return False

	hourly_details = data.get("hourlyPriceDetails") or []
	if len(hourly_details) != 24:
		return False
	if _hour_values(hourly_details) != {f"{hour:02d}" for hour in range(1, 25)}:
		return False

	data_date_iso = _rate_day_iso(data)
	if data_date_iso and data_date_iso != target_date_iso:
		return False
	return True


def _load_rate_cache(filename):
	return _read_json_file(_cache_path(filename), {})


def _save_rate_cache(filename, payload):
	path = _cache_path(filename)
	with path.open("w", encoding="utf-8") as f:
		json.dump(payload, f, indent=2, ensure_ascii=False)
		f.write("\n")


def _rate_fetch_failure_payload(
	target_date_iso,
	target_display,
	checked_at,
	last_checked_hour,
	attempted_formats,
	error,
	error_kind,
	require_next_day=False,
):
	payload = {
		"date": target_date_iso,
		"requestedDate": target_display,
		"checked_at": checked_at,
		"last_checked_hour": last_checked_hour,
		"attempted_formats": attempted_formats,
		"data": None,
		"error": error,
		"error_kind": error_kind,
	}
	if require_next_day:
		payload["available"] = False
	return payload


def _fetch_rates_for_date(target_date, request_format):
	payloads = dict(_selected_date_payloads(target_date))
	selected_date = payloads[request_format]
	response = requests.post(
		RATES_URL,
		json={"SelectedDate": selected_date},
		timeout=REQUEST_TIMEOUT_SECONDS,
	)
	return response, selected_date


def load_cached_rates(target_date=None, cache_filename=None, require_next_day=False, now=None):
	now = now or datetime.now()
	target_date = target_date or now.date()
	cache_filename = cache_filename or (TOMORROW_CACHE_FILENAME if require_next_day else CACHE_FILENAME)
	target_date_iso, _ = _target_day_parts(target_date)
	cached = _load_rate_cache(cache_filename)
	if cached.get("date") != target_date_iso:
		return None
	data = cached.get("data")
	if require_next_day:
		return data if _has_complete_next_day_rates(data, target_date_iso) else None
	return data if data is not None else None


def _load_rate_cache_entry(target_date=None, cache_filename=None, require_next_day=False, now=None):
	now = now or datetime.now()
	target_date = target_date or now.date()
	cache_filename = cache_filename or (TOMORROW_CACHE_FILENAME if require_next_day else CACHE_FILENAME)
	target_date_iso, _ = _target_day_parts(target_date)
	cached = _load_rate_cache(cache_filename)
	return cached if cached.get("date") == target_date_iso else {}


def _rate_unavailable_text(day_label, error_kind="", require_next_day=False):
	if error_kind == "network":
		return "NO INTERNET - no cached {} rates available.".format(day_label)
	if require_next_day:
		return "Tomorrow rates are not available yet."
	return "{} rates are not available.".format(day_label.capitalize())


def _rate_unavailable_notice(day_label, error_kind="", require_next_day=False):
	return "{}{}{}".format(RED, _rate_unavailable_text(day_label, error_kind, require_next_day=require_next_day), RESET)


def fetch_or_load_rates(target_date=None, cache_filename=None, require_next_day=False, now=None):
	now = now or datetime.now()
	target_date = target_date or now.date()
	cache_filename = cache_filename or (TOMORROW_CACHE_FILENAME if require_next_day else CACHE_FILENAME)
	target_date_iso, target_display = _target_day_parts(target_date)
	cached = _load_rate_cache(cache_filename)
	attempted_formats = list(cached.get("attempted_formats") or [])

	if cached.get("date") == target_date_iso:
		cached_data = cached.get("data")
		if require_next_day:
			if _has_complete_next_day_rates(cached_data, target_date_iso):
				return cached_data
			if cached.get("last_checked_hour") == _hour_key_for_time(now) and "iso" in attempted_formats:
				return None
		elif cached_data is not None:
			return cached_data

	checked_at = now.isoformat(timespec="seconds")
	last_checked_hour = _hour_key_for_time(now)
	attempted_formats = []
	last_response = None
	last_error = ""
	last_error_kind = ""

	for request_format, _ in _selected_date_payloads(target_date):
		attempted_formats.append(request_format)
		try:
			response, requested_date = _fetch_rates_for_date(target_date, request_format)
		except requests.RequestException as exc:
			last_error = "{}".format(exc)
			last_error_kind = "network"
			continue

		last_response = response

		if response.status_code != 200:
			last_error = f"{response.status_code} - {response.text}"
			last_error_kind = "service"
			continue

		try:
			data = response.json()
		except ValueError as exc:
			last_error = "{}".format(exc)
			last_error_kind = "service"
			continue

		if not require_next_day:
			_save_rate_cache(cache_filename, {
				"date": target_date_iso,
				"requestedDate": target_display,
				"checked_at": checked_at,
				"last_checked_hour": last_checked_hour,
				"attempted_formats": attempted_formats,
				"data": data,
			})
			get_cached_price_to_compare(target_date_iso)
			return data

		if _has_complete_next_day_rates(data, target_date_iso):
			_save_rate_cache(cache_filename, {
				"date": target_date_iso,
				"requestedDate": target_display,
				"checked_at": checked_at,
				"last_checked_hour": last_checked_hour,
				"attempted_formats": attempted_formats,
				"successful_format": request_format,
				"selectedDateSent": requested_date,
				"available": True,
				"data": data,
			})
			return data

		last_error = "No valid next-day rates in response"
		last_error_kind = "unavailable"

	if require_next_day:
		_save_rate_cache(
			cache_filename,
			_rate_fetch_failure_payload(
				target_date_iso,
				target_display,
				checked_at,
				last_checked_hour,
				attempted_formats,
				last_error,
				last_error_kind or "unavailable",
				require_next_day=True,
			),
		)
		return None

	if not last_error:
		if last_response is None:
			last_error = "no response received"
			last_error_kind = "network"
		else:
			last_error = f"{last_response.status_code} - {last_response.text}"
			last_error_kind = "service"

	_save_rate_cache(
		cache_filename,
		_rate_fetch_failure_payload(
			target_date_iso,
			target_display,
			checked_at,
			last_checked_hour,
			attempted_formats,
			last_error,
			last_error_kind or "service",
		),
	)
	return None


def fetch_or_load_tomorrow_rates(now=None):
	now = now or datetime.now()
	return fetch_or_load_rates(
		target_date=now.date() + timedelta(days=1),
		cache_filename=TOMORROW_CACHE_FILENAME,
		require_next_day=True,
		now=now,
	)


def load_cached_tomorrow_rates(now=None):
	now = now or datetime.now()
	return load_cached_rates(
		target_date=now.date() + timedelta(days=1),
		cache_filename=TOMORROW_CACHE_FILENAME,
		require_next_day=True,
		now=now,
	)


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


def _llm_stats(response, elapsed):
	tokens = 0
	if getattr(response, "usage", None) and response.usage.completion_tokens:
		tokens = response.usage.completion_tokens
	tok_per_sec = tokens / elapsed if elapsed > 0 and tokens else 0.0
	return {"tokens": tokens, "tok_per_sec": tok_per_sec, "elapsed": round(elapsed, 1)}


def query_rate_llm(prompt, config):
	from openai import OpenAI

	client = OpenAI(
		base_url=config["endpoint"],
		api_key=config["api_key"] or "not-needed",
	)
	t0 = time.monotonic()
	response = client.chat.completions.create(
		model=config["model"],
		messages=[{"role": "user", "content": prompt}],
		temperature=config["temperature"],
		max_tokens=config["max_tokens"],
	)
	elapsed = time.monotonic() - t0
	return _clean_llm_response(response.choices[0].message.content or ""), _llm_stats(response, elapsed)


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
	with RATE_ANALYSIS_LOCK:
		return _read_json_file(BASE_DIR / RATE_THOUGHTS_FILENAME, {})


def save_rate_thoughts(thoughts):
	path = BASE_DIR / RATE_THOUGHTS_FILENAME
	with RATE_ANALYSIS_LOCK:
		with path.open("w", encoding="utf-8") as f:
			json.dump(thoughts, f, indent=2, ensure_ascii=False)
			f.write("\n")


def _analysis_keys(now=None):
	now = now or datetime.now()
	return now.date().isoformat(), now.strftime("%Y-%m-%dT%H")


def _tomorrow_analysis_date(now=None):
	now = now or datetime.now()
	return (now.date() + timedelta(days=1)).isoformat()


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


def _needed_analysis_jobs(thoughts, now=None, tomorrow_details=None):
	date_key, hour_key = _analysis_keys(now)
	jobs = []
	if thoughts.get("date") != date_key or not thoughts.get("daily_statement"):
		jobs.append(("daily", f"daily:{date_key}"))
	if thoughts.get("hour_key") != hour_key or not thoughts.get("hourly_statement"):
		jobs.append(("hourly", f"hourly:{hour_key}"))
	if tomorrow_details:
		tomorrow_date_key = _tomorrow_analysis_date(now)
		if thoughts.get("tomorrow_date") != tomorrow_date_key or not thoughts.get("tomorrow_daily_statement"):
			jobs.append(("tomorrow_daily", f"tomorrow_daily:{tomorrow_date_key}"))
	return jobs


def _clear_stale_analysis_fields(thoughts, date_key, hour_key, tomorrow_date_key=None):
	if thoughts.get("date") != date_key:
		for key in ("daily_statement", "daily_generated_at", "daily_stats"):
			thoughts.pop(key, None)
	if thoughts.get("hour_key") != hour_key:
		for key in ("hourly_statement", "hourly_generated_at", "hourly_stats"):
			thoughts.pop(key, None)
	if thoughts.get("tomorrow_date") != tomorrow_date_key:
		for key in ("tomorrow_date", "tomorrow_daily_statement", "tomorrow_daily_generated_at", "tomorrow_daily_stats"):
			thoughts.pop(key, None)


def _start_rate_analysis_worker_locked():
	global RATE_ANALYSIS_WORKER
	if RATE_ANALYSIS_WORKER and RATE_ANALYSIS_WORKER.is_alive():
		return
	RATE_ANALYSIS_WORKER = threading.Thread(target=_analysis_worker_loop, daemon=True)
	RATE_ANALYSIS_WORKER.start()


def _analysis_worker_loop():
	global RATE_ANALYSIS_WORKER
	while True:
		with RATE_ANALYSIS_LOCK:
			if not RATE_ANALYSIS_QUEUE:
				RATE_ANALYSIS_WORKER = None
				return
			args = RATE_ANALYSIS_QUEUE.pop(0)
		_run_rate_analysis_jobs(*args)


def ensure_rate_analysis_background(hourly_details, tomorrow_hourly_details=None, now=None):
	now = now or datetime.now()
	config = load_rate_ai_config()
	tomorrow_hourly_details = tomorrow_hourly_details or []
	if not (hourly_details or tomorrow_hourly_details) or not rate_ai_is_configured(config):
		return False

	with RATE_ANALYSIS_LOCK:
		thoughts = load_rate_thoughts()
		jobs = [
			job for job in _needed_analysis_jobs(thoughts, now, tomorrow_details=tomorrow_hourly_details)
			if job[1] not in RATE_ANALYSIS_IN_FLIGHT
		]
		if not jobs:
			return False
		for _, job_key in jobs:
			RATE_ANALYSIS_IN_FLIGHT.add(job_key)
		thoughts.update({
			"analysis_status": "running",
			"analysis_error": "",
			"model": config["model"],
		})
		save_rate_thoughts(thoughts)
		RATE_ANALYSIS_QUEUE.append((list(hourly_details), now, jobs, config, list(tomorrow_hourly_details)))
		_start_rate_analysis_worker_locked()
	return True


def _job_runtime(analysis_kind, now):
	if analysis_kind == "tomorrow_daily":
		tomorrow_date = now.date() + timedelta(days=1)
		return datetime.combine(tomorrow_date, datetime.min.time())
	return now


def _job_hourly_details(analysis_kind, hourly_details, tomorrow_hourly_details):
	if analysis_kind == "tomorrow_daily":
		return tomorrow_hourly_details
	return hourly_details


def _job_prompt_kind(analysis_kind):
	return "daily" if analysis_kind == "tomorrow_daily" else analysis_kind


def _save_analysis_result(thoughts, analysis_kind, statement, stats, now, tomorrow_date_key):
	generated_at = datetime.now().isoformat(timespec="seconds")
	if analysis_kind == "daily":
		thoughts["daily_statement"] = statement
		thoughts["daily_generated_at"] = generated_at
		thoughts["daily_stats"] = stats
	elif analysis_kind == "hourly":
		thoughts["hourly_statement"] = statement
		thoughts["hourly_generated_at"] = generated_at
		thoughts["hourly_stats"] = stats
	else:
		thoughts["tomorrow_date"] = tomorrow_date_key
		thoughts["tomorrow_daily_statement"] = statement
		thoughts["tomorrow_daily_generated_at"] = generated_at
		thoughts["tomorrow_daily_stats"] = stats


def _run_rate_analysis_jobs(hourly_details, now, jobs, config, tomorrow_hourly_details=None):
	tomorrow_hourly_details = tomorrow_hourly_details or []
	date_key, hour_key = _analysis_keys(now)
	tomorrow_date_key = _tomorrow_analysis_date(now)
	job_keys = [job_key for _, job_key in jobs]

	try:
		template = load_rate_prompt_template()
		with RATE_ANALYSIS_LOCK:
			thoughts = load_rate_thoughts()
			_clear_stale_analysis_fields(thoughts, date_key, hour_key, tomorrow_date_key=tomorrow_date_key)
			thoughts.update({
				"date": date_key,
				"hour_key": hour_key,
				"analysis_status": "running",
				"analysis_error": "",
				"model": config["model"],
			})
			save_rate_thoughts(thoughts)

		for analysis_kind, _ in jobs:
			job_details = _job_hourly_details(analysis_kind, hourly_details, tomorrow_hourly_details)
			if not job_details:
				continue
			context = build_rate_analysis_context(job_details, now=_job_runtime(analysis_kind, now))
			statement, stats = query_rate_llm(
				build_rate_prompt(template, _job_prompt_kind(analysis_kind), context, config),
				config,
			)
			with RATE_ANALYSIS_LOCK:
				thoughts = load_rate_thoughts()
				thoughts["date"] = date_key
				thoughts["hour_key"] = hour_key
				thoughts["model"] = config["model"]
				thoughts["analysis_status"] = "running"
				thoughts["analysis_error"] = ""
				_save_analysis_result(thoughts, analysis_kind, statement, stats, now, tomorrow_date_key)
				save_rate_thoughts(thoughts)
	except Exception as exc:
		with RATE_ANALYSIS_LOCK:
			thoughts = load_rate_thoughts()
			thoughts.update({
				"date": date_key,
				"hour_key": hour_key,
				"model": config["model"],
				"analysis_status": "error",
				"analysis_error": str(exc),
			})
			save_rate_thoughts(thoughts)
	finally:
		with RATE_ANALYSIS_LOCK:
			for job_key in job_keys:
				RATE_ANALYSIS_IN_FLIGHT.discard(job_key)
			thoughts = load_rate_thoughts()
			if not RATE_ANALYSIS_IN_FLIGHT and thoughts.get("analysis_status") != "error":
				thoughts["analysis_status"] = "ready"
				thoughts["analysis_error"] = ""
				save_rate_thoughts(thoughts)


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


def build_table(hourly_details, now=None, bar=DEFAULT_bar, highlight_current_hour=True):
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

		highlight_price = highlight_current_hour and normalized_hour == current_hour
		if highlight_current_hour and (highlight_price or (normalized_hour + 12) % 24 == current_hour):
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


def _bottom_bar_plain_text(next_refresh_at, now=None, width=None, rate_view="today"):
	now = now or datetime.now()
	remaining = (next_refresh_at - now).total_seconds()
	width = width or _terminal_size()[0]
	toggle_target = "Later" if rate_view == "today" else "Today"
	left = f" [i] AI [t] {toggle_target} "
	right = f" {_format_refresh_seconds(remaining)} "
	if width <= len(right):
		return right[-width:]
	available_left = max(0, width - len(right))
	if len(left) >= available_left:
		return f"{left[:available_left]}{right}"[:width]
	padding = width - len(left) - len(right)
	return f"{left}{' ' * padding}{right}"


def _bottom_bar_line(next_refresh_at, now=None, width=None, rate_view="today"):
	return f"{BG_BLUE}{WHITE}{_bottom_bar_plain_text(next_refresh_at, now=now, width=width, rate_view=rate_view)}{RESET}"


def _analysis_bottom_bar_plain_text(next_refresh_at, now=None, width=None):
	now = now or datetime.now()
	remaining = (next_refresh_at - now).total_seconds()
	width = width or _terminal_size()[0]
	left = " [i] Rates  ↑↓ Scroll "
	right = f" {_format_refresh_seconds(remaining)} "
	if width <= len(right):
		return right[-width:]
	available_left = max(0, width - len(right))
	if _visible_width(left) >= available_left:
		return f"{left[:available_left]}{right}"[:width]
	padding = width - _visible_width(left) - len(right)
	return f"{left}{' ' * padding}{right}"


def _analysis_bottom_bar_line(next_refresh_at, now=None, width=None):
	return f"{BG_BLUE}{WHITE}{_analysis_bottom_bar_plain_text(next_refresh_at, now=now, width=width)}{RESET}"


def _write_bottom_bar(next_refresh_at, output=None, now=None, rate_view="today"):
	output = output or sys.stdout
	width, height = _terminal_size()
	output.write(f"\033[{height};1H\r{CLEAR_LINE}{_bottom_bar_line(next_refresh_at, now=now, width=width, rate_view=rate_view)}")
	output.flush()


def _write_analysis_bottom_bar(next_refresh_at, output=None, now=None):
	output = output or sys.stdout
	width, height = _terminal_size()
	output.write(f"\033[{height};1H\r{CLEAR_LINE}{_analysis_bottom_bar_line(next_refresh_at, now=now, width=width)}")
	output.flush()


def render_screen(
	bar=DEFAULT_bar,
	output=None,
	clear_screen=False,
	next_refresh_at=None,
	now=None,
	start_analysis=True,
	rate_view="today",
	today_data=UNSET,
	tomorrow_data=UNSET,
):
	output = output or sys.stdout
	now = now or datetime.now()

	if clear_screen:
		print(CLEAR_SCREEN, end="", file=output)

	today_data = fetch_or_load_rates(now=now) if today_data is UNSET else today_data
	tomorrow_data = fetch_or_load_tomorrow_rates(now=now) if tomorrow_data is UNSET else tomorrow_data
	ptc_loaded = apply_price_to_compare_threshold()
	today_hourly_details = (today_data or {}).get("hourlyPriceDetails") or []
	tomorrow_hourly_details = (tomorrow_data or {}).get("hourlyPriceDetails") or []
	today_error_kind = _load_rate_cache_entry(now=now).get("error_kind", "")
	tomorrow_error_kind = _load_rate_cache_entry(
		target_date=now.date() + timedelta(days=1),
		cache_filename=TOMORROW_CACHE_FILENAME,
		require_next_day=True,
		now=now,
	).get("error_kind", "")
	if start_analysis:
		ensure_rate_analysis_background(today_hourly_details, tomorrow_hourly_details=tomorrow_hourly_details, now=now)
	if rate_view == "tomorrow":
		if tomorrow_hourly_details:
			table = build_table(tomorrow_hourly_details, now=now, bar=bar, highlight_current_hour=False)
			print(_format_plain_table(table, headers=["Hour", "AM", "PM"]), file=output)
		else:
			print(_rate_unavailable_notice("tomorrow", tomorrow_error_kind, require_next_day=True), file=output)
	else:
		if today_hourly_details:
			table = build_table(today_hourly_details, now=now, bar=bar)
			print(_format_plain_table(table, headers=["Hour", "AM", "PM"]), file=output)
		else:
			print(_rate_unavailable_notice("today", today_error_kind), file=output)
	if not ptc_loaded:
		print(f"{RED}PTC FETCH FAILURE{RESET}", file=output)
	if next_refresh_at is not None:
		_write_bottom_bar(next_refresh_at, output=output, now=now, rate_view=rate_view)
	output.flush()


def _update_countdown_line(next_refresh_at, output=None, rate_view="today"):
	_write_bottom_bar(next_refresh_at, output=output, rate_view=rate_view)


def _update_analysis_countdown_line(next_refresh_at, output=None):
	_write_analysis_bottom_bar(next_refresh_at, output=output)


def _wrap_analysis_text(text, width):
	lines = []
	for paragraph in text.splitlines():
		if not paragraph.strip():
			lines.append("")
		else:
			lines.extend(textwrap.wrap(paragraph, width=max(20, width)) or [""])
	return lines


def _apply_inline_markdown(line, base_style=""):
	if "**" not in line:
		return f"{base_style}{line}{RESET}" if base_style else line

	parts = line.split("**")
	rendered = []
	for index, part in enumerate(parts):
		if not part:
			continue
		style = f"{base_style}{BOLD}" if index % 2 else base_style
		rendered.append(f"{style}{part}{RESET}" if style else part)
	return "".join(rendered)


def _render_analysis_markdown_line(line):
	stripped = line.lstrip()
	indent = line[:len(line) - len(stripped)]
	base_style = ""

	if stripped.startswith("###"):
		base_style = f"{BG_GREEN}{BLACK}{BOLD}"
		stripped = stripped[4:] if stripped.startswith("### ") else stripped[3:]
	elif stripped.startswith("##"):
		base_style = f"{BG_BLUE}{WHITE}{BOLD}"
		stripped = stripped[3:] if stripped.startswith("## ") else stripped[2:]
	elif stripped.startswith("#"):
		base_style = f"{BG_CYAN}{BLACK}{BOLD}"
		stripped = stripped[2:] if stripped.startswith("# ") else stripped[1:]
	elif stripped.startswith("*") and not stripped.startswith("**"):
		stripped = "•" + stripped[1:]

	return _apply_inline_markdown(f"{indent}{stripped}", base_style=base_style)


def _analysis_lines(now=None, width=None):
	width = width or _terminal_size()[0]
	return _wrap_analysis_text(_analysis_display_text(now=now), width - 1)


def _analysis_display_text(now=None):
	now = now or datetime.now()
	config = load_rate_ai_config()
	thoughts = load_rate_thoughts()
	date_key, hour_key = _analysis_keys(now)
	tomorrow_date_key = _tomorrow_analysis_date(now)
	today_cached = load_cached_rates(now=now)
	today_error_kind = _load_rate_cache_entry(now=now).get("error_kind", "")
	tomorrow_daily = thoughts.get("tomorrow_daily_statement") if thoughts.get("tomorrow_date") == tomorrow_date_key else ""
	tomorrow_cached = load_cached_tomorrow_rates(now=now)
	tomorrow_error_kind = _load_rate_cache_entry(
		target_date=now.date() + timedelta(days=1),
		cache_filename=TOMORROW_CACHE_FILENAME,
		require_next_day=True,
		now=now,
	).get("error_kind", "")

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
		sections.append(f"## Hourly action ({hour_key})\n{hourly}")
	elif not today_cached:
		sections.append(f"## Hourly action ({hour_key})\n{_rate_unavailable_text('today', today_error_kind)}")
	else:
		sections.append(f"## Hourly action ({hour_key})\nAnalysis is warming up.")

	if daily:
		sections.append(f"## Daily analysis ({date_key})\n{daily}")
	elif not today_cached:
		sections.append(f"## Daily analysis ({date_key})\n{_rate_unavailable_text('today', today_error_kind)}")
	else:
		sections.append(f"## Daily analysis ({date_key})\nAnalysis is warming up.")

	if tomorrow_daily:
		sections.append(f"## Tomorrow outlook ({tomorrow_date_key})\n{tomorrow_daily}")
	elif tomorrow_cached:
		sections.append(f"## Tomorrow outlook ({tomorrow_date_key})\nAnalysis is warming up.")
	else:
		sections.append(
			f"## Tomorrow outlook ({tomorrow_date_key})\n"
			f"{_rate_unavailable_text('tomorrow', tomorrow_error_kind, require_next_day=True)}"
		)

	if status == "running":
		sections.append("### Status\nGenerating in the background.")
	elif status == "error" and error:
		sections.append(f"### Status\nLLM error: {error}")
	elif thoughts.get("model"):
		stats_candidates = []
		if thoughts.get("date") == date_key and isinstance(thoughts.get("daily_stats"), dict):
			stats_candidates.append((thoughts.get("daily_generated_at", ""), thoughts["daily_stats"]))
		if thoughts.get("hour_key") == hour_key and isinstance(thoughts.get("hourly_stats"), dict):
			stats_candidates.append((thoughts.get("hourly_generated_at", ""), thoughts["hourly_stats"]))
		if thoughts.get("tomorrow_date") == tomorrow_date_key and isinstance(thoughts.get("tomorrow_daily_stats"), dict):
			stats_candidates.append((thoughts.get("tomorrow_daily_generated_at", ""), thoughts["tomorrow_daily_stats"]))
		latest_stats = max(stats_candidates, default=("", {}))[1]
		tok_per_sec = latest_stats.get("tok_per_sec") if isinstance(latest_stats, dict) else 0
		if tok_per_sec:
			sections.append(f"### Status\nReady from {thoughts['model']} at {tok_per_sec:.1f}t/s.")
		else:
			sections.append(f"### Status\nReady from {thoughts['model']}.")

	return "\n\n".join(sections)


def render_analysis_screen(output=None, clear_screen=False, next_refresh_at=None, now=None, scroll=0):
	output = output or sys.stdout
	now = now or datetime.now()
	width, height = _terminal_size()
	body_height = max(1, height - 2)
	lines = _analysis_lines(now=now, width=width)
	max_scroll = max(0, len(lines) - body_height)
	scroll = min(max(0, scroll), max_scroll)

	if clear_screen:
		print(CLEAR_SCREEN, end="", file=output)

	for line in lines[scroll:scroll + body_height]:
		print(_render_analysis_markdown_line(line[:width - 1]), file=output)

	if next_refresh_at is not None:
		_write_analysis_bottom_bar(next_refresh_at, output=output, now=now)
	output.flush()
	return max_scroll


def _decode_terminal_key(sequence):
	if sequence in {"\x1b[A", "\x1bOA"}:
		return KEY_UP
	if sequence in {"\x1b[B", "\x1bOB"}:
		return KEY_DOWN
	if sequence == "\x1b[5~":
		return KEY_PAGE_UP
	if sequence == "\x1b[6~":
		return KEY_PAGE_DOWN

	mouse_match = re.match(r"\x1b\[<(\d+);\d+;\d+([mM])", sequence)
	if mouse_match:
		button = int(mouse_match.group(1))
		if button == 64:
			return KEY_MOUSE_UP
		if button == 65:
			return KEY_MOUSE_DOWN

	if sequence.startswith("\x1b[M") and len(sequence) >= 6:
		button = ord(sequence[3]) - 32
		if button == 64:
			return KEY_MOUSE_UP
		if button == 65:
			return KEY_MOUSE_DOWN

	return sequence


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
	sys.stdout.write(MOUSE_ENABLE)
	sys.stdout.flush()
	try:
		def read_key(timeout=0):
			ready, _, _ = select.select([input_stream], [], [], timeout)
			if not ready:
				return ""
			first = input_stream.read(1)
			if first != "\x1b":
				return first

			sequence = [first]
			while True:
				ready, _, _ = select.select([input_stream], [], [], 0.01)
				if not ready:
					break
				sequence.append(input_stream.read(1))
				joined = "".join(sequence)
				if re.match(r"\x1b\[(?:[AB]|[56]~)$", joined):
					break
				if re.match(r"\x1b\[<\d+;\d+;\d+[mM]$", joined):
					break
				if joined.startswith("\x1b[M") and len(joined) >= 6:
					break
			return _decode_terminal_key("".join(sequence))

		yield read_key
	finally:
		sys.stdout.write(MOUSE_DISABLE)
		sys.stdout.flush()
		termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _refresh_screen(screen_mode, rate_view, args, output, next_refresh_at, analysis_scroll=0):
	now = datetime.now()
	today_data = fetch_or_load_rates(now=now)
	tomorrow_data = fetch_or_load_tomorrow_rates(now=now)
	apply_price_to_compare_threshold()
	today_hourly_details = (today_data or {}).get("hourlyPriceDetails") or []
	tomorrow_hourly_details = (tomorrow_data or {}).get("hourlyPriceDetails") or []
	ensure_rate_analysis_background(today_hourly_details, tomorrow_hourly_details=tomorrow_hourly_details, now=now)
	if screen_mode == "analysis":
		return render_analysis_screen(
			output=output,
			clear_screen=True,
			next_refresh_at=next_refresh_at,
			now=now,
			scroll=analysis_scroll,
		)
	render_screen(
		bar=args.bar,
		output=output,
		clear_screen=True,
		next_refresh_at=next_refresh_at,
		now=now,
		start_analysis=False,
		rate_view=rate_view,
		today_data=today_data,
		tomorrow_data=tomorrow_data,
	)
	return 0


def run_refresh_loop(args, output=None):
	output = output or sys.stdout
	next_delay = seconds_until_next_minute()
	screen_mode = "rates"
	rate_view = "today"
	analysis_scroll = 0
	analysis_max_scroll = 0

	with terminal_key_reader() as read_key:
		while True:
			next_refresh_at = datetime.now() + timedelta(seconds=next_delay)
			refresh_ready = threading.Event()
			timer = threading.Timer(next_delay, refresh_ready.set)
			timer.daemon = True
			timer.start()

			try:
				analysis_max_scroll = _refresh_screen(
					screen_mode,
					rate_view,
					args,
					output,
					next_refresh_at,
					analysis_scroll=analysis_scroll,
				)
				while not refresh_ready.is_set():
					if screen_mode == "analysis":
						_update_analysis_countdown_line(next_refresh_at, output=output)
					else:
						_update_countdown_line(next_refresh_at, output=output, rate_view=rate_view)
					key = read_key(timeout=1)
					if key in {"i", "I"}:
						screen_mode = "analysis" if screen_mode == "rates" else "rates"
						if screen_mode == "analysis":
							analysis_scroll = 0
						analysis_max_scroll = _refresh_screen(
							screen_mode,
							rate_view,
							args,
							output,
							next_refresh_at,
							analysis_scroll=analysis_scroll,
						)
					elif screen_mode == "rates" and key in {"t", "T"}:
						rate_view = "tomorrow" if rate_view == "today" else "today"
						analysis_max_scroll = _refresh_screen(
							screen_mode,
							rate_view,
							args,
							output,
							next_refresh_at,
							analysis_scroll=analysis_scroll,
						)
					elif screen_mode == "analysis" and key in {KEY_UP, KEY_MOUSE_UP}:
						step = 3 if key == KEY_MOUSE_UP else 1
						analysis_scroll = max(0, analysis_scroll - step)
						analysis_max_scroll = render_analysis_screen(
							output=output,
							clear_screen=True,
							next_refresh_at=next_refresh_at,
							scroll=analysis_scroll,
						)
					elif screen_mode == "analysis" and key in {KEY_DOWN, KEY_MOUSE_DOWN}:
						step = 3 if key == KEY_MOUSE_DOWN else 1
						analysis_scroll = min(analysis_max_scroll, analysis_scroll + step)
						analysis_max_scroll = render_analysis_screen(
							output=output,
							clear_screen=True,
							next_refresh_at=next_refresh_at,
							scroll=analysis_scroll,
						)
					elif screen_mode == "analysis" and key == KEY_PAGE_UP:
						_, height = _terminal_size()
						analysis_scroll = max(0, analysis_scroll - max(1, height - 3))
						analysis_max_scroll = render_analysis_screen(
							output=output,
							clear_screen=True,
							next_refresh_at=next_refresh_at,
							scroll=analysis_scroll,
						)
					elif screen_mode == "analysis" and key == KEY_PAGE_DOWN:
						_, height = _terminal_size()
						analysis_scroll = min(analysis_max_scroll, analysis_scroll + max(1, height - 3))
						analysis_max_scroll = render_analysis_screen(
							output=output,
							clear_screen=True,
							next_refresh_at=next_refresh_at,
							scroll=analysis_scroll,
						)
					elif key in {"q", "Q"}:
						return
				if screen_mode == "analysis":
					_update_analysis_countdown_line(next_refresh_at, output=output)
				else:
					_update_countdown_line(next_refresh_at, output=output, rate_view=rate_view)
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
