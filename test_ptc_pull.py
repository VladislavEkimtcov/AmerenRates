import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import ptc_pull


class FakeResponse:
	def __init__(self, text, status_error=None):
		self.text = text
		self.status_error = status_error

	def raise_for_status(self):
		if self.status_error:
			raise self.status_error


_HOUR_A = datetime(2026, 7, 12, 8, 0, 0)   # "2026-07-12T08"
_HOUR_B = datetime(2026, 7, 12, 9, 0, 0)   # "2026-07-12T09"


class PriceToCompareTests(unittest.TestCase):
	# ------------------------------------------------------------------
	# extract_price_to_compare
	# ------------------------------------------------------------------

	def test_extract_price_to_compare_handles_nbsp_entity(self):
		html = "<p>8.769 cents per&nbsp;0 - 800kWh</p>"
		self.assertEqual(ptc_pull.extract_price_to_compare(html), "8.769")

	def test_extract_price_to_compare_handles_decoded_nbsp(self):
		html = "<p>8.769 cents per\xa00 - 800kWh</p>"
		self.assertEqual(ptc_pull.extract_price_to_compare(html), "8.769")

	def test_extract_price_to_compare_new_format_nbsp_entity(self):
		"""Matches the new live HTML: <p>11.326 cents per&nbsp;0 - 800kWh</p>"""
		html = "<p>11.326 cents per&nbsp;0 - 800kWh</p>"
		self.assertEqual(ptc_pull.extract_price_to_compare(html), "11.326")

	def test_extract_price_to_compare_new_format_decoded_nbsp(self):
		"""Same after html.unescape: &nbsp; → \\xa0"""
		html = "<p>11.326 cents per\xa00 - 800kWh</p>"
		self.assertEqual(ptc_pull.extract_price_to_compare(html), "11.326")

	# ------------------------------------------------------------------
	# refresh_price_to_compare
	# ------------------------------------------------------------------

	def test_refresh_price_to_compare_writes_price_for_date(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			response = FakeResponse("<p>8.769 cents per&nbsp;0 - 800kWh</p>")

			with mock.patch.object(ptc_pull.requests, "get", return_value=response):
				price = ptc_pull.refresh_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,
				)

			self.assertEqual(price, "8.769")
			# Successful fetch must NOT write __last_failed_hour__
			cached = json.loads(filename.read_text())
			self.assertEqual(cached["2026-07-12"], "8.769")
			self.assertNotIn(ptc_pull._FAILED_HOUR_KEY, cached)

	def test_refresh_price_to_compare_writes_null_on_failure(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"

			with mock.patch.object(ptc_pull.requests, "get", side_effect=Exception("boom")):
				price = ptc_pull.refresh_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,
				)

			self.assertEqual(price, ptc_pull.PTC_FAILURE_VALUE)
			cached = json.loads(filename.read_text())
			self.assertEqual(cached["2026-07-12"], "NULL")
			# Failed fetch must record the hour so retries are throttled
			self.assertEqual(cached[ptc_pull._FAILED_HOUR_KEY], "2026-07-12T08")

	# ------------------------------------------------------------------
	# get_cached_price_to_compare
	# ------------------------------------------------------------------

	def test_get_cached_price_to_compare_refreshes_when_file_is_missing(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"

			with mock.patch.object(ptc_pull, "refresh_price_to_compare", return_value="8.769") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,
				)

			self.assertEqual(price, "8.769")
			refresh.assert_called_once_with("2026-07-12", filename=str(filename), now=_HOUR_A)

	def test_get_cached_price_to_compare_refreshes_when_file_is_stale(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			filename.write_text(json.dumps({"2026-07-11": "8.769"}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare", return_value="8.98") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,
				)

			self.assertEqual(price, "8.98")
			refresh.assert_called_once_with("2026-07-12", filename=str(filename), now=_HOUR_A)

	def test_get_cached_price_to_compare_reuses_null_within_same_hour(self):
		"""A cached NULL recorded in the same hour must NOT trigger a re-fetch."""
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			filename.write_text(json.dumps({
				"2026-07-12": "NULL",
				ptc_pull._FAILED_HOUR_KEY: "2026-07-12T08",
			}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,   # same hour as cached failure
				)

			self.assertEqual(price, ptc_pull.PTC_FAILURE_VALUE)
			refresh.assert_not_called()

	def test_get_cached_price_to_compare_retries_null_on_next_hour(self):
		"""A cached NULL from a previous hour must be retried in the new hour."""
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			filename.write_text(json.dumps({
				"2026-07-12": "NULL",
				ptc_pull._FAILED_HOUR_KEY: "2026-07-12T08",
			}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare", return_value="11.326") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_B,   # one hour later → must retry
				)

			self.assertEqual(price, "11.326")
			refresh.assert_called_once_with("2026-07-12", filename=str(filename), now=_HOUR_B)

	def test_get_cached_price_to_compare_retries_null_on_startup_different_hour(self):
		"""On startup, if the cached failure hour differs from now, retry immediately."""
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			# Simulates a failure cached during an earlier run (no __last_failed_hour__ key)
			filename.write_text(json.dumps({"2026-07-12": "NULL"}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare", return_value="11.326") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,
				)

			self.assertEqual(price, "11.326")
			refresh.assert_called_once()

	def test_get_cached_price_to_compare_returns_valid_price_without_refresh(self):
		"""A successfully cached price must be returned as-is."""
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			filename.write_text(json.dumps({"2026-07-12": "11.326"}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
					now=_HOUR_A,
				)

			self.assertEqual(price, "11.326")
			refresh.assert_not_called()


if __name__ == "__main__":
	unittest.main()
