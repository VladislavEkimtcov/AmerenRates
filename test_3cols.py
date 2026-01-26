import unittest

from hour_utils import detect_hour_offset, normalize_hour, hour_to_time, shift_hours_if_last_zero


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

    def test_handles_one_to_twentyfour_hours(self):
        details = [
            {"hour": f"{i:02d}", "price": 0.10 + i} for i in range(1, 25)
        ]
        offset = detect_hour_offset(details)
        self.assertEqual(offset, 1)
        self.assertEqual(normalize_hour("01", offset), 0)
        self.assertEqual(hour_to_time("01", offset), "12:00")
        # 24 should represent 11 PM when using 1-based hours
        self.assertEqual(normalize_hour("24", offset), 23)
        self.assertEqual(hour_to_time("24", offset), "11:00")

    def test_shift_hours_if_last_zero(self):
        details = [
            {"hour": "00", "price": 0.10},
            {"hour": "01", "price": 0.20},
            {"hour": "02", "price": 0.0},  # last is zero -> rotate
        ]
        shifted = shift_hours_if_last_zero(details)
        self.assertEqual([d["hour"] for d in shifted], ["00", "01", "02"])
        self.assertEqual(shifted[0]["price"], 0.0)
        self.assertEqual(shifted[1]["price"], 0.10)
        self.assertEqual(shifted[2]["price"], 0.20)

    def test_shift_hours_if_last_zero_preserves_length(self):
        details = [{"hour": f"{i:02d}", "price": i} for i in range(1, 25)]
        details[-1]["price"] = 0  # last one is zero
        shifted = shift_hours_if_last_zero(details)
        self.assertEqual(len(shifted), 24)
        self.assertEqual(shifted[0]["price"], 0)
        self.assertEqual(shifted[-1]["price"], 23)


if __name__ == "__main__":
    unittest.main()
