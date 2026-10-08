#!/usr/bin/env python3
"""Offline tests for scripts/update-download-badges.py.

The download APIs are not reachable from every environment, so the network
layer is stubbed here with payloads shaped like the real pepy and jsDelivr
responses. Everything else -- count extraction, summing, formatting and the
README rewrite -- is the real production code.

Run with:  python3 -m unittest scripts.test_update_download_badges -v
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "udb", os.path.join(os.path.dirname(os.path.abspath(__file__)), "update-download-badges.py")
)
udb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(udb)


def http_error(url):
    return urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))


class StubFetcher:
    """Maps URLs to canned payloads, and records the headers it was given."""

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
        raise urllib.error.URLError(f"unstubbed url: {url}")


PEPY = {"total_downloads": 1234, "recent_downloads": {"last_month": 90}}
JSDELIVR = {"type": "github", "name": "infinitode/blurjs", "rank": 42, "totalHits": 5678}


class TestCountExtraction(unittest.TestCase):
    def test_finds_pepy_total_downloads(self):
        self.assertEqual(udb.find_count(PEPY), 1234)

    def test_finds_jsdelivr_total_hits(self):
        self.assertEqual(udb.find_count(JSDELIVR), 5678)

    def test_prefers_all_time_key_over_nested_period_hits(self):
        payload = {"hits": {"yesterday": 5, "lastMonth": 50}, "totalHits": 999}
        self.assertEqual(udb.find_count(payload), 999)

    def test_ignores_non_numeric_matches(self):
        self.assertIsNone(udb.find_count({"hits": {"total": 1}, "other": "x"}))

    def test_returns_none_when_absent(self):
        self.assertIsNone(udb.find_count({"name": "nothing useful"}))

    def test_does_not_treat_booleans_as_counts(self):
        self.assertIsNone(udb.find_count({"downloads": True}))


class TestTotals(unittest.TestCase):
    def test_pypi_sums_every_package(self):
        fetcher = StubFetcher({pkg: {"total_downloads": 100} for pkg in udb.PYPI_PACKAGES})
        totals, failures = udb.pypi_totals(udb.PYPI_PACKAGES, fetcher=fetcher)
        self.assertEqual(len(totals), len(udb.PYPI_PACKAGES))
        self.assertEqual(sum(totals.values()), 100 * len(udb.PYPI_PACKAGES))
        self.assertEqual(failures, {})

    def test_pypi_records_failure_without_abandoning_the_run(self):
        responses = {pkg: {"total_downloads": 100} for pkg in udb.PYPI_PACKAGES}
        responses["qrforge"] = urllib.error.URLError("boom")
        totals, failures = udb.pypi_totals(udb.PYPI_PACKAGES, fetcher=StubFetcher(responses))
        self.assertNotIn("qrforge", totals)
        self.assertIn("qrforge", failures)
        self.assertEqual(sum(totals.values()), 100 * (len(udb.PYPI_PACKAGES) - 1))

    def test_pypi_falls_back_to_legacy_endpoint(self):
        # with a key configured the keyed v2 endpoint is tried first, so a 401
        # there must fall through to the keyless legacy endpoint
        fetcher = StubFetcher({"api/v2/projects": http_error("x"), "api/projects": PEPY})
        totals, failures = udb.pypi_totals(["qrforge"], api_key="secret-key", fetcher=fetcher)
        self.assertEqual(totals, {"qrforge": 1234})
        self.assertEqual(failures, {})
        self.assertEqual(len(fetcher.calls), 2)

    def test_pypi_sends_api_key_only_to_keyed_endpoint(self):
        fetcher = StubFetcher({"api/v2/projects": PEPY})
        udb.pypi_totals(["qrforge"], api_key="secret-key", fetcher=fetcher)
        keyed = [h for url, h in fetcher.calls if "api/v2/projects" in url]
        self.assertEqual(keyed, [{"X-Api-Key": "secret-key"}])

    def test_pypi_skips_keyed_endpoint_when_no_key_configured(self):
        fetcher = StubFetcher({"api/projects": PEPY})
        totals, _ = udb.pypi_totals(["qrforge"], api_key=None, fetcher=fetcher)
        self.assertEqual(totals, {"qrforge": 1234})
        self.assertNotIn("api/v2/projects", [url for url, _ in fetcher.calls])

    def test_jsdelivr_sums_repos(self):
        fetcher = StubFetcher({"packages/gh": JSDELIVR})
        totals, failures = udb.jsdelivr_totals(["infinitode/blurjs"], fetcher=fetcher)
        self.assertEqual(totals, {"infinitode/blurjs": 5678})
        self.assertEqual(failures, {})

    def test_collect_combines_both_sources(self):
        responses = {pkg: {"total_downloads": 10} for pkg in udb.PYPI_PACKAGES}
        responses["packages/gh"] = JSDELIVR
        result = udb.collect(fetcher=StubFetcher(responses))
        expected = 10 * len(udb.PYPI_PACKAGES) + 5678
        self.assertEqual(result["total"], expected)
        self.assertEqual(result["failures"], {})


class TestFormatting(unittest.TestCase):
    def test_boundaries(self):
        cases = [(0, "0"), (999, "999"), (1_000, "1.0k"), (45_678, "45.7k"),
                 (999_999, "1000.0k"), (1_234_567, "1.23M"), (12_300_000, "12.30M")]
        for value, expected in cases:
            self.assertEqual(udb.format_count(value), expected, f"format_count({value})")

    def test_value_is_url_safe_for_shields(self):
        for value in (0, 999, 45_678, 1_234_567):
            text = udb.format_count(value)
            self.assertRegex(text, r"^[0-9.]+[kM]?$")


class TestReadmeRewrite(unittest.TestCase):
    def setUp(self):
        self.readme = f"# Profile\n\n{udb.ANCHOR}\n\nSome text.\n"

    def test_inserts_after_visitor_counter_on_first_run(self):
        updated, changed = udb.update_readme(self.readme, 1_234_567)
        self.assertTrue(changed)
        self.assertIn("![Total Downloads](https://img.shields.io/badge/Total%20Downloads-1.23M-5d17eb)", updated)
        self.assertIn(udb.START_MARK, updated)
        self.assertIn(udb.END_MARK, updated)
        self.assertLess(updated.index(udb.ANCHOR), updated.index(udb.START_MARK))

    def test_replaces_in_place_on_later_runs(self):
        once, _ = udb.update_readme(self.readme, 100)
        twice, changed = udb.update_readme(once, 2_000_000)
        self.assertTrue(changed)
        self.assertEqual(twice.count("TOTAL-DOWNLOADS:START"), 1)
        self.assertEqual(twice.count("TOTAL-DOWNLOADS:END"), 1)
        self.assertIn("2.00M", twice)
        self.assertNotIn("Total%20Downloads-100-", twice)

    def test_idempotent_for_same_value(self):
        once, _ = udb.update_readme(self.readme, 1234)
        twice, changed = udb.update_readme(once, 1234)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_preserves_surrounding_content(self):
        updated, _ = udb.update_readme(self.readme, 5_000_000)
        self.assertIn("# Profile", updated)
        self.assertIn("Some text.", updated)

    def test_no_anchor_and_no_marker_is_a_noop(self):
        updated, changed = udb.update_readme("nothing here\n", 1)
        self.assertFalse(changed)
        self.assertEqual(updated, "nothing here\n")


class TestMainEndToEnd(unittest.TestCase):
    """Drives the real main() against a throwaway copy of the README."""

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _run(self, argv, responses):
        original = udb.fetch_json
        udb.fetch_json = StubFetcher(responses)
        self.addCleanup(setattr, udb, "fetch_json", original)
        buffer = io.StringIO()
        original_stdout, sys.stdout = sys.stdout, buffer
        try:
            code = udb.main(argv)
        finally:
            sys.stdout = original_stdout
        return code, buffer.getvalue()

    def test_end_to_end_writes_the_tally(self):
        readme = f"# Profile\n\n{udb.ANCHOR}\n"
        path = self._write(readme)
        responses = {pkg: {"total_downloads": 1_000} for pkg in udb.PYPI_PACKAGES}
        responses["packages/gh"] = {"totalHits": 4_000}

        code, output = self._run(["--readme", path], responses)

        expected = 1_000 * len(udb.PYPI_PACKAGES) + 4_000
        self.assertEqual(code, 0)
        self.assertIn(f"{expected:>12,}", output)
        with open(path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn(f"Total%20Downloads-{udb.format_count(expected)}-", written)

    def test_dry_run_leaves_the_file_alone(self):
        path = self._write(f"# Profile\n\n{udb.ANCHOR}\n")
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        responses = {pkg: {"total_downloads": 7} for pkg in udb.PYPI_PACKAGES}
        responses["packages/gh"] = {"totalHits": 8}

        code, output = self._run(["--readme", path, "--dry-run"], responses)

        self.assertEqual(code, 0)
        self.assertIn("--dry-run", output)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_partial_failure_still_updates_and_exits_nonzero(self):
        path = self._write(f"# Profile\n\n{udb.ANCHOR}\n")
        responses = {pkg: {"total_downloads": 10} for pkg in udb.PYPI_PACKAGES}
        responses["qrforge"] = urllib.error.URLError("boom")
        responses["packages/gh"] = {"totalHits": 0}

        code, _ = self._run(["--readme", path], responses)

        self.assertEqual(code, 1)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("TOTAL-DOWNLOADS:START", handle.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
