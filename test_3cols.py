import unittest

from hour_utils import detect_hour_offset, normalize_hour, hour_to_time


class HourParsingTests(unittest.TestCase):
    def test_detects_zero_based_hours(self):
        details = [
            {"hour": "00", "price": 0.10},
            {"hour": "01", "price": 0.12},
        ]
        self.assertEqual(detect_hour_offset(details), 0)
        self.assertEqual(normalize_hour("00", offset=0), 0)
        self.assertEqual(hour_to_time("00", offset=0), "12:00")

    def test_detects_one_based_hours(self):
        details = [
            {"hour": "01", "price": 0.10},
            {"hour": "02", "price": 0.12},
        ]
        offset = detect_hour_offset(details)
        self.assertEqual(offset, 1)
        self.assertEqual(normalize_hour("01", offset), 0)
        self.assertEqual(hour_to_time("01", offset), "12:00")


if __name__ == "__main__":
    unittest.main()
