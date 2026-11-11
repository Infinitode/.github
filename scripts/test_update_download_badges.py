#!/usr/bin/env python3
"""Offline tests for scripts/update-download-badges.py.

The network layer is replaced with a router that answers with payloads shaped
like the real GitHub, PyPI, pepy and shields.io responses. Everything else --
repo discovery and ownership checks, counting, badge parsing,
the regression guards and the README rewrite -- is production code.

Run with:  python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import importlib.util

_spec = importlib.util.spec_from_file_location("udb", os.path.join(_HERE, "update-download-badges.py"))
udb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(udb)

TOKEN_VARS = ("PEPY_API_KEY", "GITHUB_TOKEN")


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


def repo(owner, name, stars=0, forks=0, private=False):
    """A repository object as the GitHub listing API returns it (trimmed)."""
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner},
        "private": private,
        "visibility": "private" if private else "public",
        "stargazers_count": stars,
        "forks_count": forks,
    }


def pypi_meta(home=None, description=""):
    return {
        "info": {
            "home_page": home,
            "download_url": None,
            "project_url": None,
            "project_urls": None,
            "description": description,
        }
    }


def npm_meta(repository=None, homepage=None, created=None):
    """npm registry metadata shaped like the real ``GET /{package}`` response."""
    latest = {"repository": {"type": "git", "url": repository} if repository else None,
              "homepage": homepage, "bugs": None}
    meta = {"dist-tags": {"latest": "1.0.2"}, "versions": {"1.0.2": latest}}
    if created:
        meta["time"] = {"created": created, "modified": created}
    return meta


def contents_file(name, text):
    """What GET /repos/{o}/{r}/contents/{file} returns for a file."""
    return {"content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "name": name}


def http_error(code):
    return urllib.error.HTTPError("http://test", code, "test", None, None)


class FakeWeb:
    """Routes each URL to the most specific fragment that matches it.

    A route value is a payload, an exception to raise, or a callable taking the
    URL and returning either of those.
    """

    def __init__(self):
        self.json = {}
        self.text = {}
        self.json_calls = []

    def fetch_json(self, url, headers=None, timeout=30):
        self.json_calls.append((url, headers or {}))
        return self._route(self.json, url, headers)

    def fetch_text(self, url, headers=None, timeout=30):
        return self._route(self.text, url, headers)

    @staticmethod
    def _route(table, url, headers):
        matches = [needle for needle in table if needle in url]
        if not matches:
            raise urllib.error.URLError(f"unstubbed url: {url}")
        value = table[max(matches, key=len)]
        if callable(value):
            value = value(url, headers)
        if isinstance(value, Exception):
            raise value
        return value


def page_of(url):
    """The ``page=`` query value of a listing URL (1 when absent)."""
    for part in url.split("?", 1)[-1].split("&"):
        if part.startswith("page="):
            return int(part.split("=", 1)[1])
    return 1


def listing(repos):
    """A repo listing that serves everything on page 1 and nothing after."""
    return lambda url, headers: repos if page_of(url) == 1 else []


def standard_world():
    """Two owners, one private repo, one foreign repo, and packages of every kind."""
    web = FakeWeb()
    web.json.update(
        {
            "/users/johanbeytell1/repos": listing(
                [
                    repo("johanbeytell1", "LocalShare", stars=1),  # PyPI name taken by someone else
                    repo("johanbeytell1", "Alloy", stars=1),
                    repo("johanbeytell1", "Secret", stars=99, private=True),
                ]
            ),
            "/orgs/Infinitode/repos": listing(
                [
                    repo("Infinitode", "Hued", stars=2, forks=1),
                    repo("Infinitode", "qrforge"),
                    repo("Infinitode", "CloakJS"),
                    repo("Infinitode", "TailClass"),
                    repo("someone-else", "hued-fork", stars=50),  # not ours, even if listed
                ]
            ),
            # PyPI: hued links back to our repo; qrforge only declares its name in setup.py;
            # localshare is someone else's project; alloy and cloakjs are not on PyPI.
            "pypi.org/pypi/hued/json": pypi_meta(home="https://github.com/Infinitode/Hued"),
            "pypi.org/pypi/qrforge/json": pypi_meta(description="# qrforge\nNo links here."),
            "pypi.org/pypi/localshare/json": pypi_meta(home="https://github.com/sunu/localshare"),
            "pypi.org/pypi/alloy/json": http_error(404),
            "pypi.org/pypi/cloakjs/json": http_error(404),
            "pypi.org/pypi/tailclass/json": http_error(404),
            # npm: tailclass links back to our repo through a git+ URL;
            # hued is somebody else's package; cloakjs and friends are not on npm.
            "registry.npmjs.org/tailclass": npm_meta(
                repository="git+https://github.com/Infinitode/TailClass.git",
                created="2026-11-08T10:09:47.000Z",
            ),
            "registry.npmjs.org/hued": npm_meta(repository="https://github.com/someone-else/hued"),
            "registry.npmjs.org/cloakjs": http_error(404),
            "registry.npmjs.org/qrforge": http_error(404),
            "registry.npmjs.org/localshare": http_error(404),
            "registry.npmjs.org/alloy": http_error(404),
            "/repos/Infinitode/qrforge/contents/setup.py": contents_file(
                "setup.py", "setup(name='qrforge', version='1.0.1')"
            ),
            "/repos/Infinitode/qrforge/contents": [
                {"type": "file", "name": "setup.py", "url": "https://api.github.com/repos/Infinitode/qrforge/contents/setup.py"},
                {"type": "dir", "name": "tests", "url": "https://api.github.com/x"},
            ],
            "/repos/johanbeytell1/LocalShare/contents": [{"type": "file", "name": "readme.md", "url": "x"}],
            "/repos/Infinitode/Hued/contents": http_error(404),
        }
    )
    web.json["/repos/johanbeytell1/Alloy/contents"] = []
    web.json["/repos/Infinitode/CloakJS/contents"] = []
    return web


def standard_text_world(web):
    web.text.update(
        {
            "img.shields.io/pepy/dt/": lambda url, headers: shields_svg("downloads", "1.0k"),
            "img.shields.io/npm/dt/": lambda url, headers: shields_svg("downloads", "350"),
            "img.shields.io/jsdelivr/gh/hy/infinitode/cloakjs": shields_svg("hits", "250"),
            "img.shields.io/jsdelivr/gh/hy/": shields_svg("hits", "0"),
        }
    )
    return web


class StubMixin:
    """Points the module's network layer at a FakeWeb for the duration of a test."""

    def install(self, web):
        self._saved = (udb.fetch_json, udb.fetch_text)
        udb.fetch_json, udb.fetch_text = web.fetch_json, web.fetch_text
        self.addCleanup(self._restore)

    def _restore(self):
        udb.fetch_json, udb.fetch_text = self._saved


# --- parsers and helpers ------------------------------------------------------


class TestParsers(unittest.TestCase):
    def test_shields_badge_reads_the_value(self):
        self.assertEqual(udb.parse_shields_badge(shields_svg("hits", "5.6k")), 5600)
        self.assertEqual(udb.parse_shields_badge(shields_svg("downloads", "1,234")), 1234)

    def test_shields_error_badge_has_no_value(self):
        self.assertIsNone(udb.parse_shields_badge(shields_svg("downloads", "unavailable")))
        self.assertIsNone(udb.parse_shields_badge(""))

    def test_format_count(self):
        self.assertEqual(udb.format_count(999), "999")
        self.assertEqual(udb.format_count(45_678), "45.7k")
        self.assertEqual(udb.format_count(1_234_567), "1.23M")
        self.assertEqual(udb.format_count(999_999), "1.00M")  # never "1000.0k"
        self.assertEqual(udb.format_count(2_000_000_000), "2.00B")

    def test_find_count_prefers_exact_all_time_keys(self):
        self.assertEqual(udb.find_count({"downloads": {"last_month": 5}, "total_downloads": 9}), 9)
        self.assertIsNone(udb.find_count({"nothing": "here"}))

    def test_parse_badge_value_reads_downloads_only(self):
        text = udb.render_block([("Total Downloads", "5.17M"), ("Total Stars", "50")])
        self.assertEqual(udb.parse_badge_value(text), 5_170_000)
        self.assertIsNone(udb.parse_badge_value("no badge here"))

    def test_normalize_name_follows_pep503(self):
        self.assertEqual(udb.normalize_name("Py_AutoPlot"), "py-autoplot")
        self.assertEqual(udb.normalize_name("LocalSiteMap"), "localsitemap")

    def test_is_counted_keeps_only_public_repos_of_the_owner(self):
        self.assertTrue(udb.is_counted(repo("Infinitode", "Hued"), "infinitode"))
        self.assertFalse(udb.is_counted(repo("Infinitode", "Hued", private=True), "Infinitode"))
        self.assertFalse(udb.is_counted(repo("someone-else", "hued-fork"), "Infinitode"))

    def test_links_back_needs_the_exact_repo(self):
        self.assertTrue(udb.links_back(pypi_meta(home="https://github.com/Infinitode/Hued"), "Infinitode", "Hued"))
        self.assertFalse(udb.links_back(pypi_meta(home="https://github.com/sunu/localshare"), "johanbeytell1", "LocalShare"))
        # a longer repo name must not count as a match for the shorter one
        self.assertFalse(udb.links_back(pypi_meta(home="https://github.com/infinitode/localsitemap-tools"), "infinitode", "localsitemap"))

    def test_packaging_names(self):
        self.assertEqual(udb.packaging_names("setup.py", "setup(name='qrforge', version='1')"), {"qrforge"})
        self.assertEqual(udb.packaging_names("pyproject.toml", '[project]\nname = "hued"\n'), {"hued"})
        self.assertEqual(udb.packaging_names("setup.cfg", "[metadata]\nname = valx\n"), {"valx"})

    def test_npm_name_lowercases_the_repo_name(self):
        self.assertEqual(udb.npm_name("TailClass"), "tailclass")
        self.assertEqual(udb.npm_name("LocalSiteMap"), "localsitemap")
        self.assertEqual(udb.npm_name("Scikit-learn-Model-Updater"), "scikit-learn-model-updater")
        self.assertEqual(udb.npm_name("Not A Package"), "not-a-package")

    def test_npm_links_back_accepts_git_urls(self):
        meta = npm_meta(repository="git+https://github.com/Infinitode/TailClass.git")
        self.assertTrue(udb.npm_links_back(meta, "Infinitode", "TailClass"))
        meta = npm_meta(repository="git+ssh://git@github.com/Infinitode/TailClass.git")
        self.assertTrue(udb.npm_links_back(meta, "infinitode", "tailclass"))
        self.assertTrue(udb.npm_links_back(npm_meta(homepage="https://github.com/Infinitode/TailClass"), "Infinitode", "TailClass"))
        # someone else's package, and a longer repo name, must not match
        self.assertFalse(udb.npm_links_back(npm_meta(repository="https://github.com/someone-else/hued"), "Infinitode", "Hued"))
        self.assertFalse(udb.npm_links_back(npm_meta(homepage="https://github.com/infinitode/tailclass-tools"), "infinitode", "tailclass"))

    def test_npm_packaging_names_reads_package_json(self):
        self.assertEqual(udb.npm_packaging_names("package.json", '{"name": "tailclass"}'), {"tailclass"})
        self.assertEqual(udb.npm_packaging_names("package.json", '{"version": "1.0.0"}'), set())
        self.assertEqual(udb.npm_packaging_names("package.json", "not json"), set())

    def test_npm_first_day_reads_time_created(self):
        meta = npm_meta(created="2026-11-08T10:09:47.000Z")
        self.assertEqual(udb.npm_first_day(meta), date(2026, 11, 8))
        self.assertIsNone(udb.npm_first_day(npm_meta()), "no time block")
        self.assertIsNone(udb.npm_first_day({"time": {"created": "not a date"}}))
        self.assertIsNone(udb.npm_first_day({"time": {"created": ""}}))


class TestReadmeRewrite(unittest.TestCase):
    def setUp(self):
        self.readme = f"# Profile\n\n{udb.ANCHOR}\n\nSome text.\n"
        self.values = [
            ("Total Downloads", "1.23M"),
            ("Total Stars", "50"),
            ("Total Forks", "10"),
        ]

    def test_inserts_all_badges_after_visitor_counter_on_first_run(self):
        updated, changed = udb.update_readme(self.readme, self.values)
        self.assertTrue(changed)
        self.assertIn("Total%20Downloads-1.23M-", updated)
        self.assertIn("Total%20Stars-50-", updated)
        self.assertIn("Total%20Forks-10-", updated)
        self.assertLess(updated.index(udb.ANCHOR), updated.index(udb.START_MARK))

    def test_replaces_in_place_on_later_runs(self):
        once, _ = udb.update_readme(self.readme, self.values)
        twice, changed = udb.update_readme(once, [("Total Downloads", "2.00M"), ("Total Stars", "51")])
        self.assertTrue(changed)
        self.assertEqual(twice.count("TOTAL-DOWNLOADS:START"), 1)
        self.assertIn("Total%20Downloads-2.00M-", twice)
        self.assertIn("Total%20Stars-51-", twice)
        self.assertNotIn("Total%20Forks", twice, "the block is rewritten as a whole")

    def test_idempotent_for_same_values(self):
        once, _ = udb.update_readme(self.readme, self.values)
        twice, changed = udb.update_readme(once, self.values)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_no_anchor_and_no_marker_is_a_noop(self):
        updated, changed = udb.update_readme("nothing here\n", self.values)
        self.assertFalse(changed)
        self.assertEqual(updated, "nothing here\n")

    def test_current_downloads_text_reads_the_published_value(self):
        text, _ = udb.update_readme(self.readme, self.values)
        self.assertEqual(udb.current_downloads_text(text), "1.23M")
        self.assertIsNone(udb.current_downloads_text("no badge"))


# --- discovery ----------------------------------------------------------------


class TestDiscovery(StubMixin, unittest.TestCase):
    def setUp(self):
        self.web = standard_world()
        self.install(self.web)

    def test_lists_only_public_repos_of_the_two_owners(self):
        names = sorted(r["full_name"] for r in udb.discover_repos())
        self.assertEqual(
            names,
            [
                "Infinitode/CloakJS",
                "Infinitode/Hued",
                "Infinitode/TailClass",
                "Infinitode/qrforge",
                "johanbeytell1/Alloy",
                "johanbeytell1/LocalShare",
            ],
        )

    def test_listing_follows_pages(self):
        many = [repo("Infinitode", f"r{i}") for i in range(100)]
        self.web.json["/orgs/Infinitode/repos"] = lambda url, headers: many if page_of(url) == 1 else [repo("Infinitode", "last")]
        repos = udb.list_public_repos("Infinitode", "org", fetch_json=self.web.fetch_json)
        self.assertEqual(len(repos), 101)

    def test_sends_the_github_token_only_when_given(self):
        udb.discover_repos(token="tok")
        self.assertTrue(all(h.get("Authorization") == "Bearer tok" for _, h in self.web.json_calls))

    def test_pypi_package_must_belong_to_the_repo(self):
        pairs = {
            "Infinitode/Hued": "hued",  # linked from PyPI metadata
            "Infinitode/qrforge": "qrforge",  # declared by the repo's setup.py
            "johanbeytell1/LocalShare": None,  # PyPI "localshare" is someone else's
            "johanbeytell1/Alloy": None,  # not on PyPI at all
        }
        for full_name, expected in pairs.items():
            owner, name = full_name.split("/")
            found = udb.pypi_package_for(repo(owner, name), fetch_json=self.web.fetch_json)
            self.assertEqual(found, expected, full_name)

    def test_discover_pypi_maps_repos_to_packages(self):
        repos = udb.discover_repos()
        packages, notes = udb.discover_pypi(repos, fetch_json=self.web.fetch_json)
        self.assertEqual(packages, {"Infinitode/Hued": "hued", "Infinitode/qrforge": "qrforge"})
        self.assertEqual(notes, [])

    def test_pypi_server_error_is_a_note_not_a_crash(self):
        self.web.json["pypi.org/pypi/hued/json"] = http_error(500)
        packages, notes = udb.discover_pypi(udb.discover_repos(), fetch_json=self.web.fetch_json)
        self.assertNotIn("Infinitode/Hued", packages)
        self.assertTrue(any("Infinitode/Hued" in n for n in notes))

    def test_npm_package_must_belong_to_the_repo(self):
        pairs = {
            "Infinitode/TailClass": "tailclass",  # registry metadata links back to the repo
            "Infinitode/Hued": None,  # npm "hued" is someone else's package
            "Infinitode/CloakJS": None,  # not on npm at all
        }
        for full_name, expected in pairs.items():
            owner, name = full_name.split("/")
            found = udb.npm_package_for(repo(owner, name), fetch_json=self.web.fetch_json)
            self.assertEqual(found, expected, full_name)

    def test_npm_package_can_be_declared_by_package_json(self):
        web = FakeWeb()
        web.json["registry.npmjs.org/widget"] = npm_meta(homepage="https://example.com")
        web.json["/repos/Infinitode/Widget/contents"] = [
            {
                "type": "file",
                "name": "package.json",
                "url": "https://api.github.com/repos/Infinitode/Widget/contents/package.json",
            }
        ]
        web.json["/repos/Infinitode/Widget/contents/package.json"] = contents_file(
            "package.json", '{"name": "widget"}'
        )
        found = udb.npm_package_for(repo("Infinitode", "Widget"), fetch_json=web.fetch_json)
        self.assertEqual(found, "widget")

        web.json["/repos/Infinitode/Widget/contents/package.json"] = contents_file(
            "package.json", '{"name": "something-else"}'
        )
        self.assertIsNone(udb.npm_package_for(repo("Infinitode", "Widget"), fetch_json=web.fetch_json))

    def test_npm_metadata_pointing_at_another_repo_beats_a_name_match(self):
        """An unrelated package that merely shares our repo's name is not ours.

        npm's ``localshare`` is someone else's 2019 package while our
        ``johanbeytell1/LocalShare`` repo's package.json declares that name.
        """
        web = FakeWeb()
        web.json["registry.npmjs.org/localshare"] = npm_meta(
            repository="git+https://github.com/localshare/localshare.git"
        )
        web.json["/repos/johanbeytell1/LocalShare/contents"] = [
            {
                "type": "file",
                "name": "package.json",
                "url": "https://api.github.com/repos/johanbeytell1/LocalShare/contents/package.json",
            }
        ]
        web.json["/repos/johanbeytell1/LocalShare/contents/package.json"] = contents_file(
            "package.json", '{"name": "localshare"}'
        )
        found = udb.npm_package_for(
            repo("johanbeytell1", "LocalShare"), fetch_json=web.fetch_json
        )
        self.assertIsNone(found)

    def test_discover_npm_maps_repos_to_packages(self):
        packages, first_days, notes = udb.discover_npm(
            udb.discover_repos(), fetch_json=self.web.fetch_json
        )
        self.assertEqual(packages, {"Infinitode/TailClass": "tailclass"})
        self.assertEqual(first_days, {"tailclass": date(2026, 11, 8)}, "lifetime start is kept")
        self.assertEqual(notes, [])

    def test_npm_server_error_is_a_note_not_a_crash(self):
        self.web.json["registry.npmjs.org/tailclass"] = http_error(500)
        packages, _, notes = udb.discover_npm(udb.discover_repos(), fetch_json=self.web.fetch_json)
        self.assertNotIn("Infinitode/TailClass", packages)
        self.assertTrue(any("Infinitode/TailClass" in n for n in notes))


# --- npm downloads ------------------------------------------------------------


class TestNpmDownloads(StubMixin, unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb()
        self.install(self.web)

    @staticmethod
    def _window(url):
        span = url.split("/point/", 1)[1].split("/", 1)[0]
        start, end = span.split(":")
        return date.fromisoformat(start), date.fromisoformat(end)

    def test_all_time_total_is_summed_over_18_month_windows(self):
        calls = []

        def respond(url, headers):
            calls.append(url)
            return {"downloads": 10}

        self.web.json["api.npmjs.org/downloads/point/"] = respond
        total = udb.npm_range_total("tailclass", today=date(2019, 1, 1))
        self.assertGreater(len(calls), 1, "the API caps a query at 18 months")
        self.assertEqual(total, 10 * len(calls))
        windows = [self._window(url) for url in calls]
        self.assertEqual(windows[0][0], udb.NPM_FIRST_DAY)
        self.assertEqual(windows[-1][1], date(2019, 1, 1))
        for start, end in windows:
            self.assertLessEqual((end - start).days + 1, udb.NPM_WINDOW_DAYS)
        for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
            self.assertEqual(next_start, previous_end + timedelta(days=1), "no gaps or overlaps")

    def test_a_short_history_needs_a_single_window(self):
        calls = []

        def respond(url, headers):
            calls.append(url)
            return {"downloads": 7}

        self.web.json["api.npmjs.org/downloads/point/"] = respond
        total = udb.npm_range_total("tailclass", today=date(2015, 1, 11))
        self.assertEqual(len(calls), 1)
        self.assertEqual(total, 7)

    def test_walk_starts_at_the_first_publish_day(self):
        calls = []

        def respond(url, headers):
            calls.append(url)
            return {"downloads": 4}

        self.web.json["api.npmjs.org/downloads/point/"] = respond
        total = udb.npm_range_total(
            "tailclass", today=date(2026, 11, 11), first_day=date(2026, 11, 8)
        )
        self.assertEqual(len(calls), 1, "a four-day-old package needs a single query")
        self.assertEqual(self._window(calls[0]), (date(2026, 11, 8), date(2026, 11, 11)))
        self.assertEqual(total, 4)

    def test_first_day_before_npm_data_is_clamped_to_the_floor(self):
        calls = []

        def respond(url, headers):
            calls.append(url)
            return {"downloads": 1}

        self.web.json["api.npmjs.org/downloads/point/"] = respond
        udb.npm_range_total("python-era", today=date(2015, 2, 1), first_day=date(2010, 1, 1))
        self.assertEqual(self._window(calls[0])[0], udb.NPM_FIRST_DAY)

    def test_a_package_published_today_queries_only_today(self):
        calls = []

        def respond(url, headers):
            calls.append(url)
            return {"downloads": 99}

        self.web.json["api.npmjs.org/downloads/point/"] = respond
        totals, failures = udb.npm_totals(
            ["tailclass"], today=date(2026, 11, 11), first_days={"tailclass": date(2026, 11, 11)}
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._window(calls[0]), (date(2026, 11, 11), date(2026, 11, 11)))
        self.assertEqual(totals, {"tailclass": 99})
        self.assertEqual(failures, {})

    def test_a_first_day_after_today_queries_nothing(self):
        calls = []

        def respond(url, headers):
            calls.append(url)
            return {"downloads": 99}

        self.web.json["api.npmjs.org/downloads/point/"] = respond
        totals, failures = udb.npm_totals(
            ["tailclass"], today=date(2026, 11, 11), first_days={"tailclass": date(2026, 12, 1)}
        )
        self.assertEqual(calls, [])
        self.assertEqual(totals, {"tailclass": 0})
        self.assertEqual(failures, {})

    def test_a_window_without_a_count_raises_instead_of_undercounting(self):
        self.web.json["api.npmjs.org/downloads/point/"] = {"unexpected": 1}
        with self.assertRaises(ValueError):
            udb.npm_range_total("tailclass", today=date(2015, 1, 11))

    def test_totals_fall_back_to_shields_when_the_api_fails(self):
        self.web.json["api.npmjs.org/downloads/point/"] = urllib.error.URLError("offline")
        self.web.text["img.shields.io/npm/dt/"] = shields_svg("downloads", "1.2k")
        totals, failures = udb.npm_totals(["tailclass"], today=date(2015, 1, 11))
        self.assertEqual(totals, {"tailclass": 1200})
        self.assertEqual(failures, {})

    def test_totals_report_a_failure_when_every_source_fails(self):
        self.web.json["api.npmjs.org/downloads/point/"] = urllib.error.URLError("offline")
        self.web.text["img.shields.io/npm/dt/"] = urllib.error.URLError("offline")
        totals, failures = udb.npm_totals(["tailclass"], today=date(2015, 1, 11))
        self.assertEqual(totals, {})
        self.assertIn("tailclass", failures)


# --- collection ---------------------------------------------------------------


class TestCollect(StubMixin, unittest.TestCase):
    def setUp(self):
        self.web = standard_text_world(standard_world())
        self.web.json["api.pepy.tech"] = lambda url, headers: {"total_downloads": 1_000}
        self.web.json["api.npmjs.org/downloads/point/"] = {"downloads": 100}
        self.install(self.web)

    def _collect(self):
        # A fixed "today" keeps the npm window count (and so the total) stable.
        # tailclass was published 2026-11-08, so its lifetime is a single window.
        return udb.collect(
            api_key="k",
            fetch_json=self.web.fetch_json,
            fetch_text=self.web.fetch_text,
            today=date(2026, 11, 11),
        )

    def test_stars_and_forks_are_summed_over_counted_repos(self):
        result = self._collect()
        self.assertEqual(result["stars"], 1 + 1 + 2)  # LocalShare, Alloy, Hued; Secret and the foreign fork excluded
        self.assertEqual(result["forks"], 1)

    def test_downloads_come_from_pypi_npm_and_jsdelivr(self):
        result = self._collect()
        self.assertEqual(result["pypi"], {"hued": 1_000, "qrforge": 1_000})
        self.assertEqual(result["npm"], {"tailclass": 100})
        self.assertEqual(result["jsdelivr"]["infinitode/cloakjs"], 250)
        self.assertEqual(result["downloads"], 1_000 + 1_000 + 100 + 250)
        self.assertNotIn("localshare", result["pypi"])



# --- entry point --------------------------------------------------------------


class TestMain(StubMixin, unittest.TestCase):
    """Drives the real main() against a throwaway README."""

    def setUp(self):
        self.readme_text = f"# Profile\n\n{udb.ANCHOR}\n"
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.readme = os.path.join(self.dir.name, "README.md")
        self._write(self.readme, self.readme_text)
        saved = {var: os.environ.pop(var) for var in TOKEN_VARS if var in os.environ}
        self.addCleanup(os.environ.update, saved)
        self.addCleanup(lambda: [os.environ.pop(var, None) for var in TOKEN_VARS])

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _run(self, web, argv=(), env=None):
        self.install(web)
        for var, value in (env or {}).items():
            os.environ[var] = value
        buffer = io.StringIO()
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buffer
        try:
            code = udb.main([*argv, "--readme", self.readme])
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr
        return code, buffer.getvalue()

    def _healthy_web(self):
        web = standard_text_world(standard_world())
        web.json["api.pepy.tech"] = lambda url, headers: {"total_downloads": 1_000}
        # Force the npm fallback so the total does not depend on today's date.
        web.json["api.npmjs.org/downloads/point/"] = http_error(500)
        return web

    def test_listing_failure_changes_nothing(self):
        web = self._healthy_web()
        web.json["/orgs/Infinitode/repos"] = http_error(503)
        code, _ = self._run(web)
        self.assertEqual(code, 1)
        self.assertEqual(self._read(self.readme), self.readme_text)

    def test_every_download_source_failing_changes_nothing(self):
        web = standard_world()
        web.json["api.pepy.tech"] = http_error(500)
        web.text["img.shields.io"] = urllib.error.URLError("offline")
        code, _ = self._run(web, env={"PEPY_API_KEY": "k"})
        self.assertEqual(code, 1)
        self.assertEqual(self._read(self.readme), self.readme_text)

    def test_tokenless_run_writes_downloads_stars_and_forks(self):
        code, output = self._run(self._healthy_web())
        self.assertEqual(code, 0, output)
        readme = self._read(self.readme)
        self.assertIn("Total%20Downloads-", readme)
        self.assertIn("Total%20Stars-4-", readme)  # LocalShare 1 + Alloy 1 + Hued 2
        self.assertIn("Total%20Forks-1-", readme)
        self.assertNotIn("Clones", readme)

    def test_happy_path_with_pepy_key_is_exact(self):
        code, output = self._run(self._healthy_web(), env={"PEPY_API_KEY": "k"})
        self.assertEqual(code, 0, output)
        # PyPI: hued + qrforge at 1,000 each (pepy); npm: tailclass 350 from the
        # shields fallback; jsDelivr: CloakJS 250, the rest 0.
        self.assertIn(f"Total%20Downloads-{udb.format_count(2_600)}-", self._read(self.readme))

    def test_dry_run_changes_nothing(self):
        code, output = self._run(self._healthy_web(), argv=["--dry-run"], env={"PEPY_API_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertIn("--dry-run", output)
        self.assertEqual(self._read(self.readme), self.readme_text)

    def test_drop_beyond_tolerance_keeps_the_published_downloads(self):
        self._write(self.readme, udb.update_readme(self.readme_text, [("Total Downloads", "10.00M")])[0])
        code, output = self._run(self._healthy_web(), env={"PEPY_API_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertIn("Refusing", output)
        readme = self._read(self.readme)
        self.assertIn("Total%20Downloads-10.00M-", readme)
        self.assertIn("Total%20Stars-4-", readme, "other figures still refresh")

    def test_allow_decrease_publishes_a_lower_total(self):
        self._write(self.readme, udb.update_readme(self.readme_text, [("Total Downloads", "10.00M")])[0])
        code, _ = self._run(self._healthy_web(), argv=["--allow-decrease"], env={"PEPY_API_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertNotIn("Total%20Downloads-10.00M-", self._read(self.readme))

    def test_repos_are_discovered_without_any_hand_list(self):
        code, output = self._run(self._healthy_web(), argv=["--dry-run"], env={"PEPY_API_KEY": "k"})
        self.assertIn("Infinitode/CloakJS", output)
        self.assertIn("johanbeytell1/Alloy", output)
        self.assertNotIn("hued-fork", output)
        self.assertNotIn("Secret", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
