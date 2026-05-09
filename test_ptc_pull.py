import json
import tempfile
import unittest
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


class PriceToCompareTests(unittest.TestCase):
	def test_extract_price_to_compare_handles_nbsp_entity(self):
		html = "<p>8.769 cents per&nbsp;0 - 800kWh</p>"
		self.assertEqual(ptc_pull.extract_price_to_compare(html), "8.769")

	def test_extract_price_to_compare_handles_decoded_nbsp(self):
		html = "<p>8.769 cents per\xa00 - 800kWh</p>"
		self.assertEqual(ptc_pull.extract_price_to_compare(html), "8.769")

	def test_refresh_price_to_compare_writes_price_for_date(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			response = FakeResponse("<p>8.769 cents per&nbsp;0 - 800kWh</p>")

			with mock.patch.object(ptc_pull.requests, "get", return_value=response):
				price = ptc_pull.refresh_price_to_compare(
					"2026-07-12",
					filename=str(filename),
				)

			self.assertEqual(price, "8.769")
			self.assertEqual(json.loads(filename.read_text()), {"2026-07-12": "8.769"})

	def test_refresh_price_to_compare_writes_null_on_failure(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"

			with mock.patch.object(ptc_pull.requests, "get", side_effect=Exception("boom")):
				price = ptc_pull.refresh_price_to_compare(
					"2026-07-12",
					filename=str(filename),
				)

			self.assertEqual(price, ptc_pull.PTC_FAILURE_VALUE)
			self.assertEqual(json.loads(filename.read_text()), {"2026-07-12": "NULL"})

	def test_get_cached_price_to_compare_refreshes_when_file_is_missing(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"

			with mock.patch.object(ptc_pull, "refresh_price_to_compare", return_value="8.769") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
				)

			self.assertEqual(price, "8.769")
			refresh.assert_called_once_with("2026-07-12", filename=str(filename))

	def test_get_cached_price_to_compare_refreshes_when_file_is_stale(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			filename.write_text(json.dumps({"2026-07-11": "8.769"}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare", return_value="8.98") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
				)

			self.assertEqual(price, "8.98")
			refresh.assert_called_once_with("2026-07-12", filename=str(filename))

	def test_get_cached_price_to_compare_reuses_todays_null(self):
		with tempfile.TemporaryDirectory() as tmp:
			filename = Path(tmp) / "ptc.json"
			filename.write_text(json.dumps({"2026-07-12": "NULL"}))

			with mock.patch.object(ptc_pull, "refresh_price_to_compare") as refresh:
				price = ptc_pull.get_cached_price_to_compare(
					"2026-07-12",
					filename=str(filename),
				)

			self.assertEqual(price, ptc_pull.PTC_FAILURE_VALUE)
			refresh.assert_not_called()


if __name__ == "__main__":
	unittest.main()
