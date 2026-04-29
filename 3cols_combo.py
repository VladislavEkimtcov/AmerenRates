RATES_URL = "https://www.ameren.com/api/ameren/promotion/RtpHourlyPricesbyDate"
CACHE_FILENAME = "cached_rates.json"
HIGH_PRICE_THRESHOLD = 10
DEFAULT_bar = 5

import argparse
import json
import os
from datetime import datetime

import requests
from tabulate import tabulate

from hour_utils import detect_hour_offset, normalize_hour, hour_to_time, shift_hours_if_last_zero

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
RESET = '\033[0m'
CENT = '¢'


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
	trim_trailing_fill=False,
):
	render_width = len(text) if trim_trailing_fill and width > len(text) else max(width, len(text))
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
	trim_trailing_fill=False,
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
			trim_trailing_fill=trim_trailing_fill,
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
		return data

	raise RuntimeError(f"Failed to fetch rates: {response.status_code} - {response.text}")


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
	current_time_marker = f"{BOLD}{now.hour:02}:{now.minute:02}{RESET}<"

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
			trim_trailing_fill=(index >= 12),
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


def parse_args(argv=None):
	parser = argparse.ArgumentParser(description="Show Ameren hourly rates with prices overlaid on bars.")
	parser.add_argument(
		"-bar",
		type=_positive_int,
		default=DEFAULT_bar,
		help=f"Maximum width used to scale the colored bars (default: {DEFAULT_bar})",
	)
	return parser.parse_args(argv)


def main(argv=None):
	args = parse_args(argv)
	data = fetch_or_load_rates()
	hourly_details = data.get("hourlyPriceDetails") or []
	table = build_table(hourly_details, bar=args.bar)
	print(tabulate(table, headers=["Hour", "AM", "PM"], tablefmt="plain"))


if __name__ == "__main__":
	try:
		main()
	except Exception as e:
		print(f"Error: {e}")

