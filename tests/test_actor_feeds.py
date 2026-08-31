"""Tests pour les flux RSS/Atom des acteurs eux-mêmes (Lot 2 §2.5, audit veille §4.A,
30/08/2026) : découverte via <link rel="alternate"> sur la page d'accueil, mise en cache du
résultat (trouvé ou non) pour ne jamais retenter automatiquement, puis lecture du flux comme
une source primaire datée -- contrairement à collect_press_mentions (presse tierce), aucun
matching par mot-clé sur le nom de l'acteur n'est nécessaire : le flux appartient déjà à l'acteur.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import press
from press import _discover_actor_feed, collect_actor_feeds

HOMEPAGE_WITH_FEED = """<!doctype html><html><head><title>Example</title>
<link rel="alternate" type="application/rss+xml" title="News" href="/feed.xml">
</head><body><main><h1>Example</h1></main></body></html>"""

HOMEPAGE_WITH_ATOM_FEED = """<!doctype html><html><head><title>Example</title>
<link rel="alternate" type="application/atom+xml" title="News" href="https://feeds.example.test/atom">
</head><body><main><h1>Example</h1></main></body></html>"""

HOMEPAGE_WITHOUT_FEED = """<!doctype html><html><head><title>Example</title>
</head><body><main><h1>Example</h1></main></body></html>"""

SAMPLE_ACTOR_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Example News</title>
<item>
  <title>Example raises EUR 10M in Series B</title>
  <link>https://example.test/news/series-b</link>
  <description>Example announces a new funding round.</description>
  <pubDate>24 Aug 2026 04:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, text_or_bytes):
        if isinstance(text_or_bytes, bytes):
            self.content = text_or_bytes
            self.text = text_or_bytes.decode("utf-8")
        else:
            self.text = text_or_bytes
            self.content = text_or_bytes.encode("utf-8")
        self.status_code = 200

    def raise_for_status(self):
        return None


class FakeClient:
    # Maps a URL to its served content -- both discovery (HTML) and feed (RSS) requests go
    # through the same client.get(), so both must be dispatchable by URL.
    responses: dict[str, str | bytes] = {}
    fail_urls: set[str] = set()

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        if url in self.fail_urls:
            raise RuntimeError("network error")
        if url not in self.responses:
            raise RuntimeError(f"unexpected URL requested: {url}")
        return FakeResponse(self.responses[url])


def _setup(tmp: str) -> Path:
    actors_db = Path(tmp) / "actors.db"
    with (
        patch.object(dbmod, "ACTORS_DB", actors_db),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
    ):
        dbmod.init_databases()
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
    return actors_db


class DiscoverActorFeedTests(unittest.TestCase):
    def test_rss_link_is_discovered_and_resolved_to_absolute_url(self):
        FakeClient.responses = {"https://example.test/": HOMEPAGE_WITH_FEED}
        FakeClient.fail_urls = set()
        with patch.object(press.httpx, "Client", FakeClient):
            with press.httpx.Client() as client:
                feed_url = _discover_actor_feed(client, "https://example.test/")
        self.assertEqual("https://example.test/feed.xml", feed_url)

    def test_atom_link_is_also_discovered(self):
        FakeClient.responses = {"https://example.test/": HOMEPAGE_WITH_ATOM_FEED}
        FakeClient.fail_urls = set()
        with patch.object(press.httpx, "Client", FakeClient):
            with press.httpx.Client() as client:
                feed_url = _discover_actor_feed(client, "https://example.test/")
        self.assertEqual("https://feeds.example.test/atom", feed_url)

    def test_no_feed_link_returns_none(self):
        FakeClient.responses = {"https://example.test/": HOMEPAGE_WITHOUT_FEED}
        FakeClient.fail_urls = set()
        with patch.object(press.httpx, "Client", FakeClient):
            with press.httpx.Client() as client:
                feed_url = _discover_actor_feed(client, "https://example.test/")
        self.assertIsNone(feed_url)

    def test_unreachable_homepage_returns_none(self):
        FakeClient.responses = {}
        FakeClient.fail_urls = {"https://example.test/"}
        with patch.object(press.httpx, "Client", FakeClient):
            with press.httpx.Client() as client:
                feed_url = _discover_actor_feed(client, "https://example.test/")
        self.assertIsNone(feed_url)


class CollectActorFeedsTests(unittest.TestCase):
    def test_discovers_caches_and_reads_a_new_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(press, "ACTORS_DB", actors_db):
                dbmod.create_actor("Example", "France", "Test", "https://example.test/")
                FakeClient.responses = {
                    "https://example.test/": HOMEPAGE_WITH_FEED,
                    "https://example.test/feed.xml": SAMPLE_ACTOR_FEED,
                }
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_actor_feeds()
            self.assertEqual(1, result["feeds_discovered"])
            self.assertEqual(1, result["feeds_ok"])
            self.assertEqual(1, result["events_added"])
            row = dbmod.rows(actors_db, "SELECT rss_feed_url,rss_feed_checked_at FROM actors WHERE name='Example'")[0]
            self.assertEqual("https://example.test/feed.xml", row["rss_feed_url"])
            self.assertIsNotNone(row["rss_feed_checked_at"])
            event = dbmod.rows(actors_db, "SELECT event_type,review_status,description FROM actor_events")[0]
            self.assertEqual("investment", event["event_type"])
            self.assertEqual("pending", event["review_status"])

    def test_discovery_negative_result_is_cached_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(press, "ACTORS_DB", actors_db):
                dbmod.create_actor("Example", "France", "Test", "https://example.test/")
                FakeClient.responses = {"https://example.test/": HOMEPAGE_WITHOUT_FEED}
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    first = collect_actor_feeds()
                row = dbmod.rows(actors_db, "SELECT rss_feed_url,rss_feed_checked_at FROM actors WHERE name='Example'")[0]
                self.assertIsNone(row["rss_feed_url"])
                self.assertIsNotNone(row["rss_feed_checked_at"])

                # Second run: homepage is no longer in FakeClient.responses at all -- if
                # collect_actor_feeds tried to re-fetch it, this would raise "unexpected URL".
                FakeClient.responses = {}
                with patch.object(press.httpx, "Client", FakeClient):
                    second = collect_actor_feeds()
            self.assertEqual(0, first["feeds_ok"])
            self.assertEqual(0, second["feeds_discovered"])
            self.assertEqual(0, second["feeds_ok"])

    def test_already_cached_feed_url_is_not_rediscovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "UPDATE actors SET rss_feed_url=?,rss_feed_checked_at=? WHERE id=?",
                    ("https://example.test/feed.xml", dbmod.utc_now(), actor_id),
                )
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(press, "ACTORS_DB", actors_db):
                # Homepage deliberately absent -- rediscovery would raise "unexpected URL".
                FakeClient.responses = {"https://example.test/feed.xml": SAMPLE_ACTOR_FEED}
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_actor_feeds()
            self.assertEqual(0, result["feeds_discovered"])
            self.assertEqual(1, result["feeds_ok"])
            self.assertEqual(1, result["events_added"])

    def test_second_run_of_the_same_feed_does_not_duplicate_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(press, "ACTORS_DB", actors_db):
                dbmod.create_actor("Example", "France", "Test", "https://example.test/")
                FakeClient.responses = {
                    "https://example.test/": HOMEPAGE_WITH_FEED,
                    "https://example.test/feed.xml": SAMPLE_ACTOR_FEED,
                }
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    first = collect_actor_feeds()
                    second = collect_actor_feeds()
            self.assertEqual(1, first["events_added"])
            self.assertEqual(0, second["events_added"])

    def test_feed_fetch_failure_is_counted_as_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "UPDATE actors SET rss_feed_url=?,rss_feed_checked_at=? WHERE id=?",
                    ("https://example.test/feed.xml", dbmod.utc_now(), actor_id),
                )
            with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(press, "ACTORS_DB", actors_db):
                FakeClient.responses = {}
                FakeClient.fail_urls = {"https://example.test/feed.xml"}
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_actor_feeds()
            self.assertEqual(1, result["errors"])
            self.assertEqual(0, result["feeds_ok"])


if __name__ == "__main__":
    unittest.main()
