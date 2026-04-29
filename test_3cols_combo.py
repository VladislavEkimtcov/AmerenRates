import importlib.util
import re
import unittest
from datetime import datetime
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("3cols_combo.py")
spec = importlib.util.spec_from_file_location("threecols_combo", MODULE_PATH)
combo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combo)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
BG_RE = re.compile(r"\x1b\[(41|42|43|47)m")


def strip_ansi(value):
    return ANSI_RE.sub("", value)


class ComboFormattingTests(unittest.TestCase):
    def test_parse_args_uses_default_bar(self):
        args = combo.parse_args([])
        self.assertEqual(args.bar, 5)

    def test_parse_args_allows_bar_override(self):
        args = combo.parse_args(["-bar", "9"])
        self.assertEqual(args.bar, 9)

    def test_colorize_price_uses_scaled_bar_when_text_does_not_fit(self):
        rendered = combo.colorize_price(
            12.3,
            thresholds=(5.0, 9.0),
            max_price_cents=20.0,
        )
        plain = strip_ansi(rendered)
        self.assertEqual(plain, "¢12.3")
        self.assertRegex(rendered, BG_RE)
        self.assertIn(f"{combo.RED}.{combo.RESET}", rendered)
        self.assertIn(f"{combo.RED}3{combo.RESET}", rendered)

    def test_colorize_price_overlays_price_text_when_bar_is_wide_enough(self):
        rendered = combo.colorize_price(
            12.3,
            thresholds=(5.0, 9.0),
            max_price_cents=20.0,
            bar=8,
        )
        plain = strip_ansi(rendered)
        self.assertIn("¢12.3", plain)
        self.assertRegex(rendered, BG_RE)

    def test_colorize_price_can_right_align_text_inside_bar(self):
        rendered = combo.colorize_price(
            9.5,
            thresholds=(5.0, 9.0),
            max_price_cents=9.5,
            is_max=True,
            bar=7,
            align_text_right=True,
        )
        plain = strip_ansi(rendered)
        self.assertEqual(plain, "   ¢9.5")
        self.assertRegex(rendered, BG_RE)

    def test_negative_price_uses_negative_indicator(self):
        rendered = combo.colorize_price(
            -1.5,
            thresholds=(5.0, 9.0),
            max_price_cents=20.0,
        )
        plain = strip_ansi(rendered)
        self.assertIn("¢-1.5", plain)
        self.assertIn("▒", plain)

    def test_max_price_uses_distinct_background_overlay(self):
        rendered = combo.colorize_price(
            20.0,
            thresholds=(5.0, 9.0),
            max_price_cents=20.0,
            is_max=True,
        )
        plain = strip_ansi(rendered)
        self.assertIn("¢20.0", plain)
        self.assertIn("\x1b[47m", rendered)

    def test_max_price_overflow_digits_remain_colored(self):
        rendered = combo.colorize_price(
            120.0,
            thresholds=(5.0, 9.0),
            max_price_cents=120.0,
            is_max=True,
        )
        plain = strip_ansi(rendered)
        self.assertEqual(plain, "¢120.0")
        self.assertIn(combo.BG_WHITE, rendered)
        self.assertIn(f"{combo.WHITE}0{combo.RESET}", rendered)

    def test_build_table_highlights_current_hour_and_keeps_pm_column(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        table = combo.build_table(details, now=datetime(2026, 4, 24, 3, 15), bar=8)

        self.assertEqual(len(table), 12)
        self.assertEqual(len(table[0]), 3)

        highlighted_row = table[3]
        self.assertIn(">", highlighted_row[0])
        self.assertIn("03:15", strip_ansi(highlighted_row[0]))
        self.assertIn("¢4.0", strip_ansi(highlighted_row[1]))
        self.assertTrue(strip_ansi(highlighted_row[1]).endswith("<"))
        self.assertIn("¢16.0", strip_ansi(highlighted_row[2]))

    def test_build_table_right_aligns_pm_bar_padding(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        table = combo.build_table(details, now=datetime(2026, 4, 24, 3, 15), bar=7)

        pm_plain = strip_ansi(table[11][2])
        self.assertEqual(pm_plain, "  ¢24.0")
        self.assertFalse(pm_plain.endswith(" "))

    def test_build_table_rejects_empty_hour_list(self):
        with self.assertRaises(ValueError):
            combo.build_table([])

    def test_build_table_rejects_non_positive_bar(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        with self.assertRaises(ValueError):
            combo.build_table(details, bar=0)


if __name__ == "__main__":
    unittest.main()

