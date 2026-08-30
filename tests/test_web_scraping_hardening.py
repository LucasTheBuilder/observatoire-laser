"""Tests pour les actions retenues de la revue "web scraping methods" : détection d'anomalie
de source (priorité #1), diagnostic render_required (priorité #4), crawl_delay robots.txt +
délai adaptatif (idée isolée reprise du rejet de Scrapy), et GET conditionnel (priorité #3).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as appmod
import db as dbmod
import scrapers
from hybrid import DEFAULT_SITE_PROFILE, parse_document
from scrapers import ANOMALY_MIN_HISTORY, _detect_content_anomaly, _robots_crawl_delay, _throttle


class DetectContentAnomalyTests(unittest.TestCase):
    def _seed_actor_and_source(self, actors_db: Path) -> int:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
        actor_id = dbmod.create_actor("Anomaly Test Actor", "France", "Test", "https://anomaly.example/", priority=False)
        with dbmod.connect(actors_db) as db:
            source_id = db.execute(
                "INSERT INTO actor_sources(actor_id,url,active) VALUES(?,?,1)",
                (actor_id, "https://anomaly.example/page"),
            ).lastrowid
        return source_id

    def _history(self, actors_db: Path, source_id: int, counts: list[int]) -> None:
        with dbmod.connect(actors_db) as db:
            for count in counts:
                db.execute(
                    "INSERT INTO source_metrics(source_id,block_count,text_chars,captured_at) VALUES(?,?,?,?)",
                    (source_id, count, count * 40, dbmod.utc_now()),
                )

    def test_not_enough_history_yields_no_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"), patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"):
                dbmod.init_databases()
                source_id = self._seed_actor_and_source(actors_db)
                self._history(actors_db, source_id, [20, 22])  # fewer than ANOMALY_MIN_HISTORY
                with dbmod.connect(actors_db) as db:
                    self.assertLess(len(self._history_rows(db, source_id)), ANOMALY_MIN_HISTORY)
                    at, detail = _detect_content_anomaly(db, source_id, 1)
            self.assertIsNone(at)
            self.assertIsNone(detail)

    def _history_rows(self, db, source_id):
        return db.execute("SELECT block_count FROM source_metrics WHERE source_id=?", (source_id,)).fetchall()

    def test_stable_history_then_collapse_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"), patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"):
                dbmod.init_databases()
                source_id = self._seed_actor_and_source(actors_db)
                self._history(actors_db, source_id, [18, 20, 19, 21, 20])
                with dbmod.connect(actors_db) as db:
                    at, detail = _detect_content_anomaly(db, source_id, 1)
            self.assertIsNotNone(at)
            self.assertIn("médiane historique", detail)

    def test_within_normal_range_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"), patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"):
                dbmod.init_databases()
                source_id = self._seed_actor_and_source(actors_db)
                self._history(actors_db, source_id, [18, 20, 19, 21, 20])
                with dbmod.connect(actors_db) as db:
                    at, detail = _detect_content_anomaly(db, source_id, 17)
            self.assertIsNone(at)
            self.assertIsNone(detail)

    def test_already_thin_page_never_flagged_as_anomalous(self):
        # A page that was already down at 1-2 blocks historically is just a thin page, not a
        # collapse -- ANOMALY_MIN_BASELINE_BLOCKS guards against flagging noise on it.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"), patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"):
                dbmod.init_databases()
                source_id = self._seed_actor_and_source(actors_db)
                self._history(actors_db, source_id, [2, 1, 2, 1, 2])
                with dbmod.connect(actors_db) as db:
                    at, detail = _detect_content_anomaly(db, source_id, 0)
            self.assertIsNone(at)
            self.assertIsNone(detail)


class RenderRequiredSignalTests(unittest.TestCase):
    def test_empty_spa_shell_is_flagged(self):
        html = """<html><head><title>App</title></head><body>
            <div id="root"></div>
            <script src="/static/js/bundle.abc123.js"></script>
            <script src="/static/js/vendor.def456.js"></script>
            <script src="/static/js/runtime.ghi789.js"></script>
        </body></html>"""
        doc = parse_document(html, "https://spa.example/", profile=DEFAULT_SITE_PROFILE)
        self.assertTrue(doc.render_required)

    def test_genuinely_thin_but_static_page_is_not_flagged(self):
        html = """<html><head><title>Contact</title></head><body><main>
            <h1>Contact</h1><p>Call us at +33 1 23 45 67 89 for any inquiry about our services.</p>
        </main></body></html>"""
        doc = parse_document(html, "https://static.example/contact", profile=DEFAULT_SITE_PROFILE)
        self.assertFalse(doc.render_required)

    def test_normal_content_rich_page_is_not_flagged(self):
        html = """<html><head><title>Example</title></head><body><main><h1>Example</h1>
            <p>This industrial laser company develops precision manufacturing technologies for customers.</p>
            <section><h2>Applications</h2><p>Femtosecond laser micromachining supports medical components.</p></section>
        </main></body></html>"""
        doc = parse_document(html, "https://rich.example/", profile=DEFAULT_SITE_PROFILE)
        self.assertFalse(doc.render_required)


class RobotsCrawlDelayAndThrottleTests(unittest.TestCase):
    def test_no_cached_robots_parser_yields_no_delay(self):
        scrapers._robots_cache.clear()
        self.assertIsNone(_robots_crawl_delay("https://unseen.example"))

    def test_declared_crawl_delay_is_read_from_cached_parser(self):
        class FakeParser:
            def crawl_delay(self, agent):
                return 2.5

        scrapers._robots_cache.clear()
        scrapers._robots_cache["https://slow.example"] = (0.0, FakeParser())
        self.assertEqual(2.5, _robots_crawl_delay("https://slow.example"))

    def test_throttle_waits_at_least_the_robots_declared_delay(self):
        class FakeParser:
            def crawl_delay(self, agent):
                return 0.05

        scrapers._robots_cache.clear()
        scrapers._last_request_at.clear()
        scrapers._last_latency_seconds.clear()
        scrapers._robots_cache["https://paced.example"] = (0.0, FakeParser())
        with patch.object(scrapers, "CRAWL_DELAY_SECONDS", 0.0):
            _throttle("https://paced.example/a")
            import time as time_module
            started = time_module.monotonic()
            _throttle("https://paced.example/b")
            elapsed = time_module.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.04)


class FakeResponse304:
    def __init__(self, status_code=304):
        self.url = "https://cond.example/"
        self.status_code = status_code
        self.text = ""
        self.content = b""
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        # Mirrors real httpx: raise_for_status() raises for ANY non-2xx, 304 included (a real
        # production run caught _fetch() not accounting for this -- a stub that always no-ops
        # here would hide the exact same regression again).
        if not (200 <= self.status_code < 300):
            import httpx as _httpx
            raise _httpx.HTTPStatusError("fake non-2xx", request=None, response=self)  # type: ignore[arg-type]


class FakeClientConditional:
    def __init__(self, *args, **kwargs):
        self.received_headers: list[dict | None] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        self.received_headers.append(headers)
        return FakeResponse304()


class FetchConditionalGetTests(unittest.TestCase):
    def test_conditional_headers_are_forwarded_and_304_is_returned_as_is(self):
        scrapers._robots_cache.clear()
        client = FakeClientConditional()
        response = scrapers._fetch(client, "https://cond.example/", headers={"If-None-Match": '"abc"'})
        self.assertEqual(304, response.status_code)
        self.assertEqual({"If-None-Match": '"abc"'}, client.received_headers[-1])


class ConditionalGetCrawlIntegrationTests(unittest.TestCase):
    """End-to-end: scrape_actors() actually sends back the etag it stored on the previous
    crawl, and a 304 response leaves content_hash untouched (no reparse happened)."""

    class FakeResponse:
        def __init__(self, url: str, text: str, etag: str, status_code: int = 200):
            self.url = url
            self.text = text
            self.content = text.encode("utf-8")
            self.status_code = status_code
            self.headers = {"etag": etag} if status_code == 200 else {}

        def raise_for_status(self):
            # Same real-httpx behavior as FakeResponse304 above -- see its comment.
            if not (200 <= self.status_code < 300):
                import httpx as _httpx
                raise _httpx.HTTPStatusError("fake non-2xx", request=None, response=self)  # type: ignore[arg-type]

    class FakeClient:
        page_text = (
            "<html><head><title>Example</title></head><body><main><h1>Example</h1>"
            "<p>This industrial laser company develops precision manufacturing technologies for customers.</p>"
            "</main></body></html>"
        )
        etag = '"v1"'
        received_headers: list[dict | None] = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None):
            ConditionalGetCrawlIntegrationTests.FakeClient.received_headers.append(headers)
            if headers and headers.get("If-None-Match") == self.etag:
                return ConditionalGetCrawlIntegrationTests.FakeResponse(url, "", self.etag, status_code=304)
            return ConditionalGetCrawlIntegrationTests.FakeResponse(url, self.page_text, self.etag)

    def _setup(self, tmp: str) -> Path:
        actors_db = Path(tmp) / "actors.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
            dbmod.create_actor("Conditional Actor", "France", "Test", "https://example.test/", priority=False)
        return actors_db

    def _crawl_once(self, actors_db: Path):
        with (
            patch.object(scrapers, "ACTORS_DB", actors_db),
            patch.object(scrapers.httpx, "Client", self.FakeClient),
            patch.object(scrapers.OllamaClient, "available", return_value=False),
        ):
            return scrapers.scrape_actors(max_pages_per_actor=1, actor_names=["Conditional Actor"])

    def test_second_crawl_sends_stored_etag_and_304_leaves_content_hash_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            self.FakeClient.received_headers = []

            self._crawl_once(actors_db)
            with dbmod.connect(actors_db) as db:
                after_first = db.execute(
                    "SELECT content_hash,etag,last_http_status FROM actor_sources WHERE url=?",
                    ("https://example.test/",),
                ).fetchone()
            self.assertEqual('"v1"', after_first["etag"])
            self.assertIsNotNone(after_first["content_hash"])

            self._crawl_once(actors_db)
            with dbmod.connect(actors_db) as db:
                after_second = db.execute(
                    "SELECT content_hash,last_http_status FROM actor_sources WHERE url=?",
                    ("https://example.test/",),
                ).fetchone()

            self.assertEqual(304, after_second["last_http_status"])
            self.assertEqual(after_first["content_hash"], after_second["content_hash"])
            # The second GET must have carried the etag stored after the first crawl.
            self.assertTrue(any(h and h.get("If-None-Match") == '"v1"' for h in self.FakeClient.received_headers))


class SchedulerTests(unittest.TestCase):
    def test_disabled_by_default(self):
        # A fresh import (no SCHEDULER_ENABLED in the environment) must never start crawling
        # automatically on an existing deployment that just picked up this change.
        self.assertFalse(appmod.SCHEDULER_ENABLED)

    def test_scheduler_status_endpoint_reports_disabled(self):
        with patch.object(appmod, "SCHEDULER_ENABLED", False):
            result = appmod.scheduler_status()
        self.assertEqual({"enabled": False, "cron": None, "next_run_at": None}, result)

    def test_scheduled_run_is_skipped_when_a_job_is_already_running(self):
        with patch.object(appmod, "jobs", {"monthly": {"status": "running", "result": None, "error": None}, "actors": {"status": "idle", "result": None, "error": None}}):
            with patch.object(appmod.executor, "submit") as submit:
                appmod._scheduled_monthly_run()
            submit.assert_not_called()

    def test_scheduled_run_submits_when_idle(self):
        fresh_jobs = {"monthly": {"status": "idle", "result": None, "error": None}}
        with patch.object(appmod, "jobs", fresh_jobs):
            with patch.object(appmod.executor, "submit") as submit:
                appmod._scheduled_monthly_run()
            submit.assert_called_once_with(appmod._run_job, "monthly")
        self.assertEqual("running", fresh_jobs["monthly"]["status"])


if __name__ == "__main__":
    unittest.main()
