import importlib.util
import re
import unittest
from datetime import datetime
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("3cols_graph.py")
spec = importlib.util.spec_from_file_location("threecols_graph", MODULE_PATH)
graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(graph)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(value):
    return ANSI_RE.sub("", value)


class GraphFormattingTests(unittest.TestCase):
    def test_parse_args_uses_default_max_bar_width(self):
        args = graph.parse_args([])
        self.assertEqual(args.max_bar_width, 5)

    def test_parse_args_allows_max_bar_width_override(self):
        args = graph.parse_args(["--max-bar-width", "9"])
        self.assertEqual(args.max_bar_width, 9)

    def test_colorize_price_respects_max_bar_width(self):
        small = strip_ansi(graph.colorize_price(10.0, (2.0, 8.0), 10.0, max_bar_width=3))
        large = strip_ansi(graph.colorize_price(10.0, (2.0, 8.0), 10.0, max_bar_width=7))
        self.assertEqual(small, "███")
        self.assertEqual(large, "███████")

    def test_build_table_rejects_empty_hour_list(self):
        with self.assertRaises(ValueError):
            graph.build_table([])

    def test_build_table_rejects_non_positive_max_bar_width(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        with self.assertRaises(ValueError):
            graph.build_table(details, max_bar_width=0)

    def test_build_table_highlights_current_hour(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        table = graph.build_table(details, now=datetime(2026, 4, 24, 3, 15))

        self.assertEqual(len(table), 12)
        self.assertEqual(len(table[0]), 3)
        self.assertIn(">", table[3][0])
        self.assertIn("03:15", strip_ansi(table[3][0]))


if __name__ == "__main__":
    unittest.main()

