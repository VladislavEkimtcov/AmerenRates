import html
import json
import re
from datetime import date

import requests


PTC_URL = "https://plugin.illinois.gov/understanding-the-price-to-compare/price-to-compare-ameren-illinois.html"
PTC_FILENAME = "ptc.json"
PTC_FAILURE_VALUE = "NULL"
PTC_PATTERN = re.compile(
	r"<p[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s+cents\s+per\s*0\s*-",
	re.IGNORECASE,
)


def extract_price_to_compare(page_html):
	decoded_html = html.unescape(page_html)
	match = PTC_PATTERN.search(decoded_html)
	if not match:
		raise ValueError("Price to Compare value was not found.")
	return match.group(1)


def _write_ptc_cache(cache_date, price, filename=PTC_FILENAME):
	with open(filename, "w") as f:
		json.dump({cache_date: price}, f)


def refresh_price_to_compare(cache_date=None, filename=PTC_FILENAME, url=PTC_URL):
	cache_date = cache_date or date.today().isoformat()
	try:
		response = requests.get(url, timeout=10)
		response.raise_for_status()
		price = extract_price_to_compare(response.text)
	except Exception:
		price = PTC_FAILURE_VALUE

	_write_ptc_cache(cache_date, price, filename=filename)
	return price


def get_cached_price_to_compare(cache_date=None, filename=PTC_FILENAME):
	cache_date = cache_date or date.today().isoformat()
	try:
		with open(filename, "r") as f:
			cached = json.load(f)
	except (FileNotFoundError, json.JSONDecodeError):
		return refresh_price_to_compare(cache_date, filename=filename)

	if not isinstance(cached, dict) or cache_date not in cached:
		return refresh_price_to_compare(cache_date, filename=filename)

	return cached.get(cache_date, PTC_FAILURE_VALUE)
