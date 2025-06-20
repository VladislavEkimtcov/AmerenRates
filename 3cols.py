RATES_URL = "https://www.ameren.com/api/ameren/promotion/RtpHourlyPricesbyDate"

import requests
from datetime import datetime, timedelta
import math
from tabulate import tabulate

# ANSI escape sequences for terminal colors
BOLD = '\033[1m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
RESET = '\033[0m'

def hour_to_time(hour_str):
    hour = int(hour_str) - 1  # shift because "01" is 00:00
    time_obj = (datetime.min + timedelta(hours=hour)).strftime("%-I:%M")
    return time_obj

def colorize_price(price, thresholds, should_highlight=False):
    arrow_prefix = f">{BOLD}" if should_highlight else ""
    arrow_postfix = f"<" if should_highlight else ""
    if price <= thresholds[0]:
        return f"{arrow_prefix}{GREEN}¢{price:.1f}{RESET}{arrow_postfix}"
    elif price <= thresholds[1]:
        return f"{arrow_prefix}{YELLOW}¢{price:.1f}{RESET}{arrow_postfix}"
    else:
        return f"{arrow_prefix}{RED}¢{price:.1f}{RESET}{arrow_postfix}"

if __name__ == "__main__":
    today = datetime.now().date()
    bodyDict = {
        "SelectedDate": today.strftime("%Y-%m-%d")
    }

    response = requests.post(RATES_URL, json=bodyDict)
    if response.status_code == 200:
        data = response.json()

        all_prices = [item["price"] for item in data["hourlyPriceDetails"]]
        sorted_prices = sorted(all_prices)
        n = len(sorted_prices)
        lower_third = sorted_prices[n // 3]
        upper_third = sorted_prices[(2 * n) // 3]

        first_half = []
        second_half = []

        for i, item in enumerate(data["hourlyPriceDetails"]):
            time_label = hour_to_time(item["hour"])
            hour_int = int(item["hour"]) - 1
            current_hour = datetime.now().hour
            highlight_price = False
            if hour_int == current_hour or hour_int + 12 == current_hour:
                time_label = f">{BOLD}{datetime.now().hour : 02}:{datetime.now().minute:02}{RESET}<"
                highlight_price = (hour_int == current_hour)
            price = round(item["price"] * 100, 1)
            price_colored = colorize_price(price, (lower_third * 100, upper_third * 100), highlight_price)

            if i < 12:
                first_half.append([time_label, price_colored])
            else:
                second_half.append([price_colored])

        table = []
        for left, right in zip(first_half, second_half):
            table.append(left + right)

        print(tabulate(table, headers=["Hour", "AM", "PM"], tablefmt="plain"))

    else:
        print(f"Error: {response.status_code} - {response.text}")
