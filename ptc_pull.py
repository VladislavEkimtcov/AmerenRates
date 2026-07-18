import html
import json
import re
from datetime import date, datetime
from pathlib import Path

import requests


PTC_URL = "https://plugin.illinois.gov/understanding-the-price-to-compare/price-to-compare-ameren-illinois.html"
PTC_FILENAME = "ptc.json"
PTC_FAILURE_VALUE = "NULL"
PTC_PATTERN = re.compile(
	r"<p[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s+cents\s+per\s*0\s*-",
	re.IGNORECASE,
)

# Key stored in the cache dict to track when the last failed fetch happened,
# so that failures are retried once per hour rather than cached for the whole day.
_FAILED_HOUR_KEY = "__last_failed_hour__"


def _current_hour_key(now=None):
	return (now or datetime.now()).strftime("%Y-%m-%dT%H")


def extract_price_to_compare(page_html):
	decoded_html = html.unescape(page_html)
	match = PTC_PATTERN.search(decoded_html)
	if not match:
		raise ValueError("Price to Compare value was not found.")
	return match.group(1)


def _write_ptc_cache(cache_date, price, filename=PTC_FILENAME, now=None):
	payload = {cache_date: price}
	if price == PTC_FAILURE_VALUE:
		payload[_FAILED_HOUR_KEY] = _current_hour_key(now)
	path = Path(filename)
	with path.open("w", encoding="utf-8") as f:
		json.dump(payload, f)


def refresh_price_to_compare(cache_date=None, filename=PTC_FILENAME, url=PTC_URL, now=None):
	cache_date = cache_date or date.today().isoformat()
	try:
		response = requests.get(url, timeout=10)
		response.raise_for_status()
		price = extract_price_to_compare(response.text)
	except Exception:
		price = PTC_FAILURE_VALUE

	_write_ptc_cache(cache_date, price, filename=filename, now=now)
	return price


def get_cached_price_to_compare(cache_date=None, filename=PTC_FILENAME, now=None):
	"""Return the cached PTC for *cache_date*, fetching fresh data when needed.

	A ``PTC_FAILURE_VALUE`` result is only reused within the same clock-hour it
	was cached.  On the next hour (or on startup if the last attempt was a
	different hour) the fetch is retried so that a transient network outage does
	not suppress the PTC for the entire day.
	"""
	cache_date = cache_date or date.today().isoformat()
	try:
		path = Path(filename)
		with path.open("r", encoding="utf-8") as f:
			cached = json.load(f)
	except (OSError, json.JSONDecodeError):
		return refresh_price_to_compare(cache_date, filename=filename, now=now)

	if not isinstance(cached, dict) or cache_date not in cached:
		return refresh_price_to_compare(cache_date, filename=filename, now=now)

	value = cached[cache_date]

	# If the previously-cached result was a failure, retry once per hour so
	# that transient outages at startup or during the day are self-healing.
	if value == PTC_FAILURE_VALUE:
		last_failed_hour = cached.get(_FAILED_HOUR_KEY, "")
		if last_failed_hour != _current_hour_key(now):
			return refresh_price_to_compare(cache_date, filename=filename, now=now)

	return value
