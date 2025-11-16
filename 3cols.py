RATES_URL = "https://www.ameren.com/api/ameren/promotion/RtpHourlyPricesbyDate"
CACHE_FILENAME = "cached_rates.json"
HIGH_PRICE_THRESHOLD = 10

import requests
import os
import json
from datetime import datetime
from tabulate import tabulate
from hour_utils import detect_hour_offset, normalize_hour, hour_to_time

# ANSI escape sequences for terminal colors
BOLD = '\033[1m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
RESET = '\033[0m'
CENT = '¢'


def colorize_price(price, thresholds, should_highlight=False, is_max=False):
    if is_max:
        color = RESET
    elif price > HIGH_PRICE_THRESHOLD or price > thresholds[1]:
        color = RED
    elif price <= thresholds[0]:
        color = GREEN
    else:
        color = YELLOW

    prefix = f">{BOLD}" if should_highlight else ""
    postfix = "<" if should_highlight else ""
    return f"{prefix}{color}{CENT}{price:.1f}{RESET}{postfix}"


def fetch_or_load_rates():
    # Use ISO date for cache key, but send "Month DD, YYYY" to the API per new contract
    today_iso = datetime.now().date().isoformat()  # e.g. 2025-11-11
    today_display = datetime.now().strftime("%B %d, %Y")  # e.g. November 11, 2025

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
                "data": data
            }, f)
        return data
    else:
        raise RuntimeError(f"Failed to fetch rates: {response.status_code} - {response.text}")


if __name__ == "__main__":
    try:
        data = fetch_or_load_rates()
        hourly_details = data.get("hourlyPriceDetails") or []
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
        now = datetime.now()
        current_hour = now.hour
        current_time_marker = f">{BOLD}{now.hour:02}:{now.minute:02}{RESET}<"

        for i, item in enumerate(hourly_details):
            time_label = hour_to_time(item["hour"], hour_offset)
            normalized_hour = normalize_hour(item["hour"], hour_offset)

            highlight_price = normalized_hour == current_hour
            if highlight_price or (normalized_hour + 12) % 24 == current_hour:
                time_label = current_time_marker

            price = round(item["price"] * 100, 1)
            price_colored = colorize_price(
                price,
                (lower_third * 100, upper_third * 100),
                highlight_price,
                is_max=(item["price"] == max_price)
            )

            if i < 12:
                first_half.append([time_label, price_colored])
            else:
                second_half.append([price_colored])

        while len(second_half) < len(first_half):
            second_half.append([""])

        table = []
        for left, right in zip(first_half, second_half):
            table.append(left + right)

        print(tabulate(table, headers=["Hour", "AM", "PM"], tablefmt="plain"))

    except Exception as e:
        print(f"Error: {e}")
