#!/usr/bin/env python3
"""Offline tests for scripts/update-download-badges.py.

The download APIs are unreachable from CI sandboxes, so the network layer is
stubbed here with payloads shaped like the real pepy JSON and shields.io SVG
responses. Everything else -- count extraction, badge parsing, summing,
formatting, the regression guards and the README rewrite -- is production code.

Run with:  python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import importlib.util

_spec = importlib.util.spec_from_file_location("udb", os.path.join(_HERE, "update-download-badges.py"))
udb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(udb)


def shields_svg(label, value):
    """A shields.io flat-style badge, including the duplicated shadow text."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="98" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <g fill="#fff" text-anchor="middle" font-size="110">
    <text aria-hidden="true" x="315" y="150" fill="#010101" transform="scale(.1)">{label}</text>
    <text x="315" y="140" transform="scale(.1)" fill="#fff">{label}</text>
    <text aria-hidden="true" x="785" y="150" fill="#010101" transform="scale(.1)">{value}</text>
    <text x="785" y="140" transform="scale(.1)" fill="#fff">{value}</text>
  </g>
</svg>"""


class StubJson:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, headers=None, timeout=30):
        self.calls.append((url, headers or {}))
        for needle, payload in self.responses.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise urllib.error.URLError(f"unstubbed json url: {url}")


class StubText:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, headers=None, timeout=30):
        self.calls.append((url, headers or {}))
        for needle, payload in self.responses.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise urllib.error.URLError(f"unstubbed text url: {url}")


PEPY_JSON = {"name": "codesafe", "total_downloads": 1234, "recent_downloads": {"last_month": 90}}
BADGE_SVG = shields_svg("downloads", "1.2k")


class TestShieldsBadgeParsing(unittest.TestCase):
    def test_reads_the_value_not_the_label(self):
        self.assertEqual(udb.parse_shields_badge(BADGE_SVG), 1200)

    def test_thousands_separator(self):
        self.assertEqual(udb.parse_shields_badge(shields_svg("hits", "1,234")), 1234)

    def test_mega_suffix(self):
        self.assertEqual(udb.parse_shields_badge(shields_svg("downloads", "2.5M")), 2_500_000)

    def test_plain_integer(self):
        self.assertEqual(udb.parse_shields_badge(shields_svg("hits", "999")), 999)

    def test_error_badge_yields_none(self):
        self.assertIsNone(udb.parse_shields_badge(shields_svg("downloads", "unavailable")))

    def test_empty_input_yields_none(self):
        self.assertIsNone(udb.parse_shields_badge(""))
        self.assertIsNone(udb.parse_shields_badge(None))

    def test_title_element_is_not_mistaken_for_the_value(self):
        svg = shields_svg("downloads", "42")
        self.assertIn("<title>downloads: 42</title>", svg)
        self.assertEqual(udb.parse_shields_badge(svg), 42)


class TestCountExtraction(unittest.TestCase):
    def test_finds_pepy_total_downloads(self):
        self.assertEqual(udb.find_count(PEPY_JSON), 1234)

    def test_finds_jsdelivr_total_hits(self):
        self.assertEqual(udb.find_count({"type": "github", "totalHits": 5678}), 5678)

    def test_prefers_all_time_key_over_period_hits(self):
        self.assertEqual(udb.find_count({"hits": {"lastMonth": 50}, "totalHits": 999}), 999)

    def test_returns_none_when_absent_or_non_numeric(self):
        self.assertIsNone(udb.find_count({"name": "nothing"}))
        self.assertIsNone(udb.find_count({"downloads": True}))


class TestFormatting(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (0, "0"), (999, "999"), (1_000, "1.0k"), (45_678, "45.7k"),
            (999_949, "999.9k"), (999_950, "1.00M"), (1_234_567, "1.23M"),
            (1_000_000_000, "1.00B"), (12_300_000_000, "12.30B"),
        ]
        for value, expected in cases:
            self.assertEqual(udb.format_count(value), expected, f"format_count({value})")

    def test_never_renders_1000k(self):
        self.assertNotIn("1000.0k", [udb.format_count(n) for n in range(990_000, 1_010_000, 997)])

    def test_value_is_url_safe(self):
        for value in (0, 999, 45_678, 999_950, 1_234_567):
            self.assertRegex(udb.format_count(value), r"^[0-9.]+[kMB]?$")

    def test_round_trips_through_parse_badge_value(self):
        for value in (0, 999, 45_678, 999_950, 1_234_567, 12_300_000_000):
            badge = udb.BADGE.format(value=udb.format_count(value))
            parsed = udb.parse_badge_value(badge)
            # rounded display loses precision, but must stay within 0.5%
            self.assertAlmostEqual(parsed, value, delta=max(1, value * 0.005))


class TestPypiTotals(unittest.TestCase):
    def test_uses_the_pepy_api_when_a_key_is_set(self):
        fetch_json = StubJson({"api.pepy.tech": PEPY_JSON})
        totals, failures, notes = udb.pypi_totals(
            ["codesafe"], api_key="secret", fetch_json=fetch_json, fetch_text=StubText({})
        )
        self.assertEqual(totals, {"codesafe": 1234})
        self.assertEqual(failures, {})
        self.assertEqual(notes, [])
        self.assertEqual(fetch_json.calls[0][1], {"X-Api-Key": "secret"})

    def test_falls_back_to_shields_without_a_key(self):
        fetch_text = StubText({"img.shields.io": BADGE_SVG})
        totals, failures, notes = udb.pypi_totals(
            ["codesafe"], api_key=None, fetch_json=StubJson({}), fetch_text=fetch_text
        )
        self.assertEqual(totals, {"codesafe": 1200})
        self.assertEqual(failures, {})
        self.assertTrue(any("PEPY_API_KEY" in n for n in notes))

    def test_falls_back_to_shields_when_the_pepy_api_rejects_the_key(self):
        fetch_json = StubJson({"api.pepy.tech": urllib.error.HTTPError("u", 403, "Forbidden", {}, None)})
        totals, _, _ = udb.pypi_totals(
            ["codesafe"], api_key="bad", fetch_json=fetch_json, fetch_text=StubText({"img.shields.io": BADGE_SVG})
        )
        self.assertEqual(totals, {"codesafe": 1200})

    def test_records_failure_when_both_sources_fail(self):
        totals, failures, _ = udb.pypi_totals(
            ["codesafe"],
            api_key="k",
            fetch_json=StubJson({"api.pepy.tech": urllib.error.HTTPError("u", 403, "Forbidden", {}, None)}),
            fetch_text=StubText({"img.shields.io": shields_svg("downloads", "unavailable")}),
        )
        self.assertEqual(totals, {})
        self.assertIn("codesafe", failures)


class TestJsdelivrTotals(unittest.TestCase):
    def test_reads_hits_from_the_badge(self):
        fetch_text = StubText({"img.shields.io": shields_svg("hits", "5.6k")})
        totals, failures = udb.jsdelivr_totals(["infinitode/blurjs"], fetch_text=fetch_text)
        self.assertEqual(totals, {"infinitode/blurjs": 5600})
        self.assertEqual(failures, {})
        self.assertIn("/jsdelivr/gh/hy/infinitode/blurjs", fetch_text.calls[0][0])

    def test_records_failure_for_an_error_badge(self):
        totals, failures = udb.jsdelivr_totals(
            ["infinitode/blurjs"],
            fetch_text=StubText({"img.shields.io": shields_svg("hits", "unavailable")}),
        )
        self.assertEqual(totals, {})
        self.assertIn("infinitode/blurjs", failures)


class TestReadmeRewrite(unittest.TestCase):
    def setUp(self):
        self.readme = f"# Profile\n\n{udb.ANCHOR}\n\nSome text.\n"

    def test_inserts_after_visitor_counter_on_first_run(self):
        updated, changed = udb.update_readme(self.readme, 1_234_567)
        self.assertTrue(changed)
        self.assertIn("Total%20Downloads-1.23M-", updated)
        self.assertLess(updated.index(udb.ANCHOR), updated.index(udb.START_MARK))

    def test_replaces_in_place_on_later_runs(self):
        once, _ = udb.update_readme(self.readme, 1_000)
        twice, changed = udb.update_readme(once, 2_000_000)
        self.assertTrue(changed)
        self.assertEqual(twice.count("TOTAL-DOWNLOADS:START"), 1)
        self.assertIn("2.00M", twice)

    def test_idempotent_for_same_value(self):
        once, _ = udb.update_readme(self.readme, 1234)
        twice, changed = udb.update_readme(once, 1234)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_no_anchor_and_no_marker_is_a_noop(self):
        updated, changed = udb.update_readme("nothing here\n", 1)
        self.assertFalse(changed)


class TestMain(unittest.TestCase):
    """Drives the real main() against a throwaway copy of the README."""

    def setUp(self):
        self.readme = f"# Profile\n\n{udb.ANCHOR}\n"
        self.path = self._write(self.readme)

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def _run(self, argv, json_responses=None, text_responses=None, api_key=None):
        # text_responses is keyed by URL fragment: "pepy" or "jsdelivr".
        if api_key is None:
            os.environ.pop("PEPY_API_KEY", None)
        else:
            os.environ["PEPY_API_KEY"] = api_key
        self.addCleanup(os.environ.pop, "PEPY_API_KEY", None)

        udb.fetch_json = StubJson(json_responses or {})
        udb.fetch_text = StubText(text_responses or {})
        buffer = io.StringIO()
        original_stdout, sys.stdout = sys.stdout, buffer
        try:
            code = udb.main(argv)
        finally:
            sys.stdout = original_stdout
        return code, buffer.getvalue()

    def test_all_sources_failing_leaves_the_badge_untouched(self):
        """The regression that shipped: a total wipeout must never publish 0."""
        dead = shields_svg("downloads", "unavailable")
        code, _ = self._run(
            ["--readme", self.path],
            text_responses={"pepy": dead, "jsdelivr": shields_svg("hits", "unavailable")},
        )
        self.assertEqual(code, 1)
        self.assertEqual(self._read(), self.readme, "README must stay byte-identical")

    def test_published_zero_is_treated_as_unpublished(self):
        """A bogus 0 left by a failed run must not block the next good one."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(udb.update_readme(self.readme, 0)[0])
        self.assertIn("Total%20Downloads-0-", self._read())

        code, _ = self._run(
            ["--readme", self.path],
            json_responses={pkg: {"total_downloads": 1_000} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "0")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        self.assertNotIn("Total%20Downloads-0-", self._read())
        self.assertIn(udb.format_count(1_000 * len(udb.PYPI_PACKAGES)), self._read())

    def test_lower_total_is_refused(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(udb.update_readme(self.readme, 10_000_000)[0])
        code, _ = self._run(
            ["--readme", self.path],
            json_responses={pkg: {"total_downloads": 100} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "0")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        self.assertIn("10.00M", self._read())

    def test_rounding_level_decrease_still_writes(self):
        """"5.17M" stands for [5,165,000, 5,175,000); 5,168,400 is not a drop."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(udb.update_readme(self.readme, 5_170_000)[0])
        self.assertIn("5.17M", self._read())

        per_package = 5_168_400 // len(udb.PYPI_PACKAGES)
        total = per_package * len(udb.PYPI_PACKAGES)
        self.assertLess(total, 5_170_000)  # genuinely below the published figure
        self.assertGreater(total, 5_170_000 * 0.95)  # but inside the tolerance

        code, output = self._run(
            ["--readme", self.path],
            json_responses={pkg: {"total_downloads": per_package} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "0")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        self.assertNotIn("Refusing", output, "rounding noise must not trip the guard")
        self.assertIn(udb.format_count(total), self._read())

    def test_drop_beyond_tolerance_is_refused(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(udb.update_readme(self.readme, 10_000_000)[0])
        # ~40% drop -- one or more sources missing
        code, _ = self._run(
            ["--readme", self.path],
            json_responses={pkg: {"total_downloads": 400_000} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "0")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        self.assertIn("10.00M", self._read())

    def test_allow_decrease_overrides_the_guard(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(udb.update_readme(self.readme, 10_000_000)[0])
        code, _ = self._run(
            ["--readme", self.path, "--allow-decrease"],
            json_responses={pkg: {"total_downloads": 100} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "0")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        expected = 100 * len(udb.PYPI_PACKAGES)
        self.assertIn(udb.format_count(expected), self._read())

    def test_partial_failure_still_publishes_the_rest(self):
        responses = {pkg: {"total_downloads": 1_000} for pkg in udb.PYPI_PACKAGES}
        responses["qrforge"] = urllib.error.URLError("boom")
        code, _ = self._run(
            ["--readme", self.path],
            json_responses=responses,
            text_responses={
                "pepy": shields_svg("downloads", "unavailable"),  # fallback also dead
                "jsdelivr": shields_svg("hits", "4k"),
            },
            api_key="k",
        )
        self.assertEqual(code, 0)
        expected = 1_000 * (len(udb.PYPI_PACKAGES) - 1) + 4_000
        self.assertIn(udb.format_count(expected), self._read())

    def test_happy_path_writes_the_tally(self):
        code, output = self._run(
            ["--readme", self.path],
            json_responses={pkg: {"total_downloads": 1_000_000} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "400k")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        expected = 1_000_000 * len(udb.PYPI_PACKAGES) + 400_000
        self.assertIn(f"{expected:>14,}", output)
        self.assertIn(udb.format_count(expected), self._read())

    def test_dry_run_changes_nothing(self):
        code, output = self._run(
            ["--readme", self.path, "--dry-run"],
            json_responses={pkg: {"total_downloads": 7} for pkg in udb.PYPI_PACKAGES},
            text_responses={"jsdelivr": shields_svg("hits", "8")},
            api_key="k",
        )
        self.assertEqual(code, 0)
        self.assertIn("--dry-run", output)
        self.assertEqual(self._read(), self.readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
