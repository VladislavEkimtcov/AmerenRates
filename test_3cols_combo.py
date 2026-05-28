import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("3cols_combo.py")
spec = importlib.util.spec_from_file_location("threecols_combo", MODULE_PATH)
combo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combo)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
BG_RE = re.compile(r"\x1b\[(41|42|43|47)m")


def strip_ansi(value):
    return ANSI_RE.sub("", value)


class FakeRatesResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"hourlyPriceDetails": []}


class TomorrowRatesResponse:
    status_code = 200
    text = ""

    def __init__(self, selected_date):
        self.selected_date = selected_date

    def json(self):
        return {
            "selectedDate": self.selected_date.strftime("%B %d, %Y"),
            "isNextDay": True,
            "hourlyPriceDetails": [
                {"hour": f"{hour:02d}", "date": f"{self.selected_date.isoformat()}T00:00:00", "price": 0.01 * hour}
                for hour in range(1, 25)
            ],
            "isErrorFetchingData": False,
        }


class JsonRatesResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class ComboFormattingTests(unittest.TestCase):
    def tearDown(self):
        combo.HIGH_PRICE_THRESHOLD = 10

    def test_parse_args_uses_default_bar(self):
        args = combo.parse_args([])
        self.assertEqual(args.bar, 5)
        self.assertFalse(args.once)

    def test_parse_args_allows_bar_override(self):
        args = combo.parse_args(["-bar", "9"])
        self.assertEqual(args.bar, 9)

    def test_parse_args_allows_one_shot_mode(self):
        args = combo.parse_args(["--once"])
        self.assertTrue(args.once)

    def test_seconds_until_next_minute_counts_to_next_minute_boundary(self):
        seconds = combo.seconds_until_next_minute(datetime(2026, 4, 24, 3, 15, 42, 250000))

        self.assertEqual(seconds, 17.75)

    def test_bottom_bar_formats_seconds_until_refresh(self):
        line = combo._bottom_bar_plain_text(
            datetime(2026, 4, 24, 3, 16, 0),
            now=datetime(2026, 4, 24, 3, 15, 18),
            width=32,
        )

        self.assertEqual(line, " [i] AI [t] Later        :42 ")

    def test_bottom_bar_switches_label_on_tomorrow_view(self):
        line = combo._bottom_bar_plain_text(
            datetime(2026, 4, 24, 3, 16, 0),
            now=datetime(2026, 4, 24, 3, 15, 18),
            width=29,
            rate_view="tomorrow",
        )

        self.assertEqual(line, " [i] AI [t] Today        :42 ")

    def test_needed_analysis_jobs_detects_daily_and_hourly_misses(self):
        jobs = combo._needed_analysis_jobs({}, now=datetime(2026, 4, 24, 3, 15))

        self.assertEqual(jobs, [("daily", "daily:2026-04-24"), ("hourly", "hourly:2026-04-24T03")])

    def test_needed_analysis_jobs_detects_new_hour(self):
        thoughts = {
            "date": "2026-04-24",
            "daily_statement": "Today is cheap overnight.",
            "hour_key": "2026-04-24T02",
            "hourly_statement": "Do laundry now.",
        }

        jobs = combo._needed_analysis_jobs(thoughts, now=datetime(2026, 4, 24, 3, 0))

        self.assertEqual(jobs, [("hourly", "hourly:2026-04-24T03")])

    def test_needed_analysis_jobs_adds_tomorrow_daily_when_rates_are_available(self):
        tomorrow_details = [{"hour": f"{hour:02d}", "price": 0.01 * hour} for hour in range(1, 25)]
        thoughts = {
            "date": "2026-04-24",
            "daily_statement": "Today summary.",
            "hour_key": "2026-04-24T03",
            "hourly_statement": "This hour summary.",
        }

        jobs = combo._needed_analysis_jobs(
            thoughts,
            now=datetime(2026, 4, 24, 3, 0),
            tomorrow_details=tomorrow_details,
        )

        self.assertEqual(jobs, [("tomorrow_daily", "tomorrow_daily:2026-04-25")])

    def test_build_rate_prompt_includes_context_and_extra_prompt(self):
        config = {"extra_prompt": "Mention EV charging."}
        context = {"date": "2026-04-24", "hourly_prices": [{"hour": "03:00", "price_cents": 4.2}]}

        prompt = combo.build_rate_prompt(
            "Kind={{ANALYSIS_KIND}}\n{{EXTRA_PROMPT}}\n{{RATE_DATA}}",
            "hourly",
            context,
            config,
        )

        self.assertIn("Kind=hourly", prompt)
        self.assertIn("Mention EV charging.", prompt)
        self.assertIn('"price_cents": 4.2', prompt)

    def test_build_rate_analysis_context_marks_current_and_upcoming_hours(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]

        context = combo.build_rate_analysis_context(details, now=datetime(2026, 4, 24, 3, 15))

        self.assertEqual(context["date"], "2026-04-24")
        self.assertEqual(context["hour_key"], "2026-04-24T03")
        self.assertEqual(context["current_hour"]["hour"], "03:00")
        self.assertEqual(context["current_hour"]["price_cents"], 4.0)
        self.assertEqual(context["upcoming_hours"][0]["hour"], "03:00")

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

    def test_colorize_price_preserves_full_bar_width_when_text_fits_inside(self):
        rendered = combo.colorize_price(
            9.5,
            thresholds=(5.0, 9.0),
            max_price_cents=9.5,
            is_max=True,
            bar=7,
        )
        plain = strip_ansi(rendered)
        self.assertEqual(plain, "¢9.5   ")
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

    def test_build_table_keeps_full_pm_bar_width(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        table = combo.build_table(details, now=datetime(2026, 4, 24, 3, 15), bar=7)

        pm_plain = strip_ansi(table[11][2])
        self.assertEqual(pm_plain, "¢24.0  ")

    def test_format_plain_table_uses_single_space_gutters(self):
        rows = [[">03:15<", "¢4.0 ", "¢16.0  "]]
        rendered = combo._format_plain_table(rows, headers=["Hour", "AM", "PM"])

        lines = rendered.splitlines()
        plain_lines = [strip_ansi(line) for line in lines]
        self.assertEqual(plain_lines[1], ">03:15< ¢4.0  ¢16.0")

    def test_build_table_rejects_empty_hour_list(self):
        with self.assertRaises(ValueError):
            combo.build_table([])

    def test_build_table_rejects_non_positive_bar(self):
        details = [{"hour": f"{i:02d}", "price": 0.01 * (i + 1)} for i in range(24)]
        with self.assertRaises(ValueError):
            combo.build_table(details, bar=0)

    def test_apply_price_to_compare_threshold_uses_cached_value(self):
        with mock.patch.object(combo, "get_cached_price_to_compare", return_value="8.769"):
            loaded = combo.apply_price_to_compare_threshold()

        self.assertTrue(loaded)
        self.assertEqual(combo.HIGH_PRICE_THRESHOLD, 8.769)

    def test_apply_price_to_compare_threshold_defaults_on_null(self):
        combo.HIGH_PRICE_THRESHOLD = 8.769

        with mock.patch.object(combo, "get_cached_price_to_compare", return_value="NULL"):
            loaded = combo.apply_price_to_compare_threshold()

        self.assertFalse(loaded)
        self.assertEqual(combo.HIGH_PRICE_THRESHOLD, 10)

    def test_fetch_or_load_rates_checks_ptc_when_fetching_fresh_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_filename = Path(tmp) / "cached_rates.json"
            checked_dates = []

            with (
                mock.patch.object(combo, "CACHE_FILENAME", str(cache_filename)),
                mock.patch.object(combo.requests, "post", return_value=FakeRatesResponse()),
                mock.patch.object(combo, "get_cached_price_to_compare", side_effect=checked_dates.append),
            ):
                data = combo.fetch_or_load_rates()

        self.assertEqual(data, {"hourlyPriceDetails": []})
        self.assertEqual(len(checked_dates), 1)

    def test_fetch_or_load_rates_skips_ptc_when_using_cached_rates(self):
        today_iso = datetime.now().date().isoformat()

        with tempfile.TemporaryDirectory() as tmp:
            cache_filename = Path(tmp) / "cached_rates.json"
            cache_filename.write_text(json.dumps({
                "date": today_iso,
                "data": {"hourlyPriceDetails": [{"hour": "00", "price": 0.01}]},
            }))

            with (
                mock.patch.object(combo, "CACHE_FILENAME", str(cache_filename)),
                mock.patch.object(combo, "get_cached_price_to_compare") as get_ptc,
            ):
                data = combo.fetch_or_load_rates()

        self.assertEqual(data, {"hourlyPriceDetails": [{"hour": "00", "price": 0.01}]})
        get_ptc.assert_not_called()

    def test_fetch_or_load_tomorrow_rates_caches_available_next_day_data(self):
        now = datetime(2026, 5, 27, 9, 15)
        tomorrow = now.date() + timedelta(days=1)

        with tempfile.TemporaryDirectory() as tmp:
            cache_filename = Path(tmp) / "tomorrow_cached.json"

            with (
                mock.patch.object(combo, "TOMORROW_CACHE_FILENAME", str(cache_filename)),
                mock.patch.object(combo.requests, "post", return_value=TomorrowRatesResponse(tomorrow)),
            ):
                data = combo.fetch_or_load_tomorrow_rates(now=now)
                cached = json.loads(cache_filename.read_text())

        self.assertTrue(data["isNextDay"])
        self.assertEqual(len(data["hourlyPriceDetails"]), 24)
        self.assertTrue(cached["available"])
        self.assertEqual(cached["date"], tomorrow.isoformat())

    def test_fetch_or_load_tomorrow_rates_retries_iso_after_display_format_false_negative(self):
        now = datetime(2026, 5, 27, 9, 15)
        tomorrow = now.date() + timedelta(days=1)
        requests_seen = []

        display_false_negative = JsonRatesResponse({
            "selectedDate": tomorrow.strftime("%B %d, %Y"),
            "isNextDay": False,
            "hourlyPriceDetails": [],
            "isErrorFetchingData": False,
        })
        iso_success = TomorrowRatesResponse(tomorrow)

        def post_side_effect(url, json):
            requests_seen.append(json["SelectedDate"])
            if json["SelectedDate"] == tomorrow.strftime("%B %d, %Y"):
                return display_false_negative
            if json["SelectedDate"] == tomorrow.isoformat():
                return iso_success
            raise AssertionError(f"Unexpected SelectedDate payload: {json['SelectedDate']}")

        with tempfile.TemporaryDirectory() as tmp:
            cache_filename = Path(tmp) / "tomorrow_cached.json"

            with (
                mock.patch.object(combo, "TOMORROW_CACHE_FILENAME", str(cache_filename)),
                mock.patch.object(combo.requests, "post", side_effect=post_side_effect),
            ):
                data = combo.fetch_or_load_tomorrow_rates(now=now)
                cached = json.loads(cache_filename.read_text())

        self.assertEqual(requests_seen, [tomorrow.strftime("%B %d, %Y"), tomorrow.isoformat()])
        self.assertTrue(data["isNextDay"])
        self.assertEqual(cached["successful_format"], "iso")
        self.assertEqual(cached["selectedDateSent"], tomorrow.isoformat())

    def test_fetch_or_load_tomorrow_rates_retries_only_once_per_hour_when_unavailable(self):
        now = datetime(2026, 5, 27, 9, 15)

        with tempfile.TemporaryDirectory() as tmp:
            cache_filename = Path(tmp) / "tomorrow_cached.json"
            call_count = 0

            def unavailable_response(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                return FakeRatesResponse()

            with (
                mock.patch.object(combo, "TOMORROW_CACHE_FILENAME", str(cache_filename)),
                mock.patch.object(combo.requests, "post", side_effect=unavailable_response),
            ):
                first = combo.fetch_or_load_tomorrow_rates(now=now)
                second = combo.fetch_or_load_tomorrow_rates(now=now + timedelta(minutes=10))

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(call_count, 2)

    def test_fetch_or_load_tomorrow_rates_retries_same_hour_for_old_cache_without_iso_attempt(self):
        now = datetime(2026, 5, 27, 9, 15)
        tomorrow = now.date() + timedelta(days=1)

        with tempfile.TemporaryDirectory() as tmp:
            cache_filename = Path(tmp) / "tomorrow_cached.json"
            cache_filename.write_text(json.dumps({
                "date": tomorrow.isoformat(),
                "last_checked_hour": now.strftime("%Y-%m-%dT%H"),
                "available": False,
                "data": None,
            }))

            with (
                mock.patch.object(combo, "TOMORROW_CACHE_FILENAME", str(cache_filename)),
                mock.patch.object(combo.requests, "post", side_effect=[FakeRatesResponse(), TomorrowRatesResponse(tomorrow)]),
            ):
                data = combo.fetch_or_load_tomorrow_rates(now=now)

        self.assertTrue(data["isNextDay"])

    def test_main_prints_ptc_failure_after_table(self):
        output = io.StringIO()

        with (
            mock.patch.object(combo, "fetch_or_load_rates", return_value={"hourlyPriceDetails": []}),
            mock.patch.object(combo, "apply_price_to_compare_threshold", return_value=False),
            mock.patch.object(combo, "build_table", return_value=[["12:00", "¢1.0", ""]]),
            redirect_stdout(output),
        ):
            combo.main(["--once"])

        lines = strip_ansi(output.getvalue()).splitlines()
        self.assertEqual(lines[0], "Hour  AM   PM")
        self.assertEqual(lines[-1], "PTC FETCH FAILURE")

    def test_render_screen_prints_countdown_at_bottom(self):
        output = io.StringIO()

        with (
            mock.patch.object(combo, "fetch_or_load_rates", return_value={"hourlyPriceDetails": []}),
            mock.patch.object(combo, "apply_price_to_compare_threshold", return_value=True),
            mock.patch.object(combo, "build_table", return_value=[["12:00", "¢1.0", ""]]),
            mock.patch.object(combo, "ensure_rate_analysis_background"),
        ):
            combo.render_screen(
                output=output,
                now=datetime(2026, 4, 24, 3, 15, 0),
                next_refresh_at=datetime(2026, 4, 24, 3, 16, 0),
                rate_view="today",
            )

        plain = strip_ansi(output.getvalue())
        self.assertIn("[i] AI [t] Later", plain)
        self.assertTrue(plain.endswith(":60 "))

    def test_render_screen_shows_tomorrow_unavailable_message(self):
        output = io.StringIO()

        with (
            mock.patch.object(combo, "fetch_or_load_rates", return_value={"hourlyPriceDetails": []}),
            mock.patch.object(combo, "fetch_or_load_tomorrow_rates", return_value=None),
            mock.patch.object(combo, "apply_price_to_compare_threshold", return_value=True),
            mock.patch.object(combo, "ensure_rate_analysis_background"),
        ):
            combo.render_screen(
                output=output,
                now=datetime(2026, 4, 24, 3, 15, 0),
                rate_view="tomorrow",
            )

        self.assertIn("Tomorrow rates are not available yet.", strip_ansi(output.getvalue()))

    def test_render_screen_uses_iso_fallback_tomorrow_data(self):
        output = io.StringIO()
        now = datetime(2026, 5, 27, 9, 15)
        tomorrow = now.date() + timedelta(days=1)

        with (
            mock.patch.object(combo, "fetch_or_load_rates", return_value={"hourlyPriceDetails": []}),
            mock.patch.object(combo, "fetch_or_load_tomorrow_rates", return_value=TomorrowRatesResponse(tomorrow).json()),
            mock.patch.object(combo, "apply_price_to_compare_threshold", return_value=True),
            mock.patch.object(combo, "build_table", return_value=[["12:00", "¢1.0", "¢2.0"]]),
            mock.patch.object(combo, "ensure_rate_analysis_background"),
        ):
            combo.render_screen(
                output=output,
                now=now,
                rate_view="tomorrow",
            )

        plain = strip_ansi(output.getvalue())
        self.assertIn("Hour  AM   PM", plain)
        self.assertNotIn("Tomorrow rates are not available yet.", plain)

    def test_analysis_display_explains_missing_config(self):
        with mock.patch.object(combo, "load_rate_ai_config", return_value={"enabled": True, "model": ""}):
            text = combo._analysis_display_text(now=datetime(2026, 4, 24, 3, 15))

        self.assertIn("Analysis is not configured yet.", text)

    def test_analysis_display_shows_latest_token_rate(self):
        thoughts = {
            "date": "2026-04-24",
            "hour_key": "2026-04-24T03",
            "analysis_status": "ready",
            "analysis_error": "",
            "model": "deepseek-r1:70b",
            "daily_statement": "Daily summary.",
            "daily_generated_at": "2026-04-24T03:14:04",
            "daily_stats": {"tokens": 120, "tok_per_sec": 9.8, "elapsed": 12.2},
            "hourly_statement": "Hourly summary.",
            "hourly_generated_at": "2026-04-24T03:14:16",
            "hourly_stats": {"tokens": 42, "tok_per_sec": 12.3, "elapsed": 3.4},
        }

        with (
            mock.patch.object(combo, "load_rate_ai_config", return_value={"enabled": True, "model": "deepseek-r1:70b"}),
            mock.patch.object(combo, "load_rate_thoughts", return_value=thoughts),
        ):
            text = combo._analysis_display_text(now=datetime(2026, 4, 24, 3, 15))

        self.assertIn("Ready from deepseek-r1:70b at 12.3t/s.", text)

    def test_analysis_display_includes_tomorrow_outlook(self):
        thoughts = {
            "date": "2026-04-24",
            "hour_key": "2026-04-24T03",
            "analysis_status": "ready",
            "analysis_error": "",
            "model": "deepseek-r1:70b",
            "daily_statement": "Daily summary.",
            "hourly_statement": "Hourly summary.",
            "tomorrow_date": "2026-04-25",
            "tomorrow_daily_statement": "Tomorrow spikes in the evening.",
            "tomorrow_daily_stats": {"tokens": 22, "tok_per_sec": 4.8, "elapsed": 4.6},
        }

        with (
            mock.patch.object(combo, "load_rate_ai_config", return_value={"enabled": True, "model": "deepseek-r1:70b"}),
            mock.patch.object(combo, "load_rate_thoughts", return_value=thoughts),
            mock.patch.object(combo, "load_cached_tomorrow_rates", return_value={"hourlyPriceDetails": [1]}),
        ):
            text = combo._analysis_display_text(now=datetime(2026, 4, 24, 3, 15))

        self.assertIn("Tomorrow outlook (2026-04-25)", text)
        self.assertIn("Tomorrow spikes in the evening.", text)

    def test_run_rate_analysis_jobs_persists_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 4, 24, 3, 15)
            config = {
                "endpoint": "http://127.0.0.1:6767/v1",
                "api_key": "",
                "model": "deepseek-r1:70b",
                "temperature": 0.2,
                "max_tokens": 700,
                "extra_prompt": "",
            }
            responses = [
                ("Daily summary.", {"tokens": 120, "tok_per_sec": 9.8, "elapsed": 12.2}),
                ("Hourly summary.", {"tokens": 42, "tok_per_sec": 12.3, "elapsed": 3.4}),
                ("Tomorrow outlook.", {"tokens": 30, "tok_per_sec": 10.0, "elapsed": 3.0}),
            ]
            tomorrow_details = [{"hour": f"{hour:02d}", "price": 0.02 * hour} for hour in range(1, 25)]

            with (
                mock.patch.object(combo, "BASE_DIR", Path(tmp)),
                mock.patch.object(combo, "load_rate_prompt_template", return_value="Kind={{ANALYSIS_KIND}}\n{{RATE_DATA}}"),
                mock.patch.object(combo, "build_rate_analysis_context", return_value={"hourly_prices": []}),
                mock.patch.object(combo, "query_rate_llm", side_effect=responses),
            ):
                combo._run_rate_analysis_jobs(
                    [{"hour": "03", "price": 0.042}],
                    now,
                    [
                        ("daily", "daily:2026-04-24"),
                        ("hourly", "hourly:2026-04-24T03"),
                        ("tomorrow_daily", "tomorrow_daily:2026-04-25"),
                    ],
                    config,
                    tomorrow_hourly_details=tomorrow_details,
                )
                thoughts = combo.load_rate_thoughts()

        self.assertEqual(thoughts["analysis_status"], "ready")
        self.assertEqual(thoughts["daily_statement"], "Daily summary.")
        self.assertEqual(thoughts["daily_stats"], {"tokens": 120, "tok_per_sec": 9.8, "elapsed": 12.2})
        self.assertEqual(thoughts["hourly_statement"], "Hourly summary.")
        self.assertEqual(thoughts["hourly_stats"], {"tokens": 42, "tok_per_sec": 12.3, "elapsed": 3.4})
        self.assertEqual(thoughts["tomorrow_date"], "2026-04-25")
        self.assertEqual(thoughts["tomorrow_daily_statement"], "Tomorrow outlook.")
        self.assertEqual(thoughts["tomorrow_daily_stats"], {"tokens": 30, "tok_per_sec": 10.0, "elapsed": 3.0})

    def test_analysis_markdown_line_colors_headings_and_bold(self):
        rendered = combo._render_analysis_markdown_line("## **Hourly** action")

        self.assertIn(combo.BG_BLUE, rendered)
        self.assertIn(combo.BOLD, rendered)
        self.assertEqual(strip_ansi(rendered), "Hourly action")

    def test_decode_terminal_key_handles_arrows_and_mouse_wheel(self):
        self.assertEqual(combo._decode_terminal_key("\x1b[A"), combo.KEY_UP)
        self.assertEqual(combo._decode_terminal_key("\x1b[B"), combo.KEY_DOWN)
        self.assertEqual(combo._decode_terminal_key("\x1b[<64;10;5M"), combo.KEY_MOUSE_UP)
        self.assertEqual(combo._decode_terminal_key("\x1b[<65;10;5M"), combo.KEY_MOUSE_DOWN)

    def test_render_analysis_screen_scrolls_statement(self):
        output = io.StringIO()

        with (
            mock.patch.object(combo, "_terminal_size", return_value=(40, 6)),
            mock.patch.object(combo, "_analysis_display_text", return_value="# Top\nline1\nline2\nline3\nline4\nline5"),
        ):
            max_scroll = combo.render_analysis_screen(output=output, scroll=2)

        plain = strip_ansi(output.getvalue())
        self.assertEqual(max_scroll, 2)
        self.assertNotIn("Top", plain)
        self.assertIn("line2", plain)
        self.assertIn("line5", plain)

    def test_main_runs_refresh_loop_by_default(self):
        with mock.patch.object(combo, "run_refresh_loop") as run_refresh_loop:
            combo.main([])

        run_refresh_loop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
