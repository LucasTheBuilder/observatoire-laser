from __future__ import annotations

import heapq
import itertools
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db as dbmod
import scrapers
from hybrid import classify_source, parse_document
from scrapers import _push_crawl_item, _select_market_sources
from site_profiles import crawl_budget, get_site_profile

FIXTURES = Path(__file__).parent / "fixtures"


class SiteFixtureTests(unittest.TestCase):
    CASES = (
        ("ALPHANOV", "https://www.alphanov.com", "alphanov.html", "project"),
        ("MANUTECH USD", "https://www.manutech-usd.fr/en/", "manutech.html", "application"),
        ("HEF", "https://hef.group/en/", "hef.html", "project"),
        ("LASEA", "https://www.lasea.eu", "lasea.html", "service"),
        ("Pulsar Photonics", "https://www.pulsar-photonics.de/en-gb/", "pulsar.html", "service"),
    )

    def test_five_reference_sites_extract_hierarchy_and_links(self):
        for actor_name, url, fixture, expected_type in self.CASES:
            with self.subTest(actor=actor_name):
                profile = get_site_profile({"name": actor_name, "official_url": url})
                html = (FIXTURES / fixture).read_text(encoding="utf-8")
                doc = parse_document(html, url, profile=profile)
                self.assertGreaterEqual(len(doc.blocks), 1)
                self.assertTrue(any(block.h1 for block in doc.blocks))
                self.assertTrue(any("laser" in block.text.lower() for block in doc.blocks))
                self.assertTrue(doc.links)
                self.assertEqual(doc.page_type, expected_type)

    def test_manutech_short_items_do_not_hide_parent_section(self):
        profile = get_site_profile({"name": "MANUTECH USD", "official_url": "https://www.manutech-usd.fr/en/"})
        html = (FIXTURES / "manutech.html").read_text(encoding="utf-8")
        doc = parse_document(html, "https://www.manutech-usd.fr/en/applications/", profile=profile)
        joined = " ".join(block.text for block in doc.blocks)
        self.assertIn("surface texturing", joined.lower())
        self.assertTrue(any(block.h2 == "Functional surfaces" for block in doc.blocks))


class UrlScoringTests(unittest.TestCase):
    def test_high_value_pages_outrank_generic_pages(self):
        profile = get_site_profile({"name": "Pulsar Photonics", "official_url": "https://www.pulsar-photonics.de"})
        service_type, service_score = classify_source(
            "https://www.pulsar-photonics.de/en/laser-contract-manufacturing/",
            "Laser contract manufacturing",
            profile=profile,
        )
        about_type, about_score = classify_source(
            "https://www.pulsar-photonics.de/en/company/",
            "Company",
            profile=profile,
        )
        self.assertEqual(service_type, "service")
        self.assertGreater(service_score, about_score)
        self.assertEqual(about_type, "about")

    def test_ignored_urls_score_zero(self):
        profile = get_site_profile({"name": "ALPHANOV", "official_url": "https://www.alphanov.com"})
        page_type, score = classify_source("https://www.alphanov.com/privacy/", "Privacy", profile=profile)
        self.assertEqual((page_type, score), ("ignore", 0))


class PriorityQueueTests(unittest.TestCase):
    def test_newly_discovered_high_value_page_moves_to_front(self):
        heap = []
        scores = {}
        counter = itertools.count()
        _push_crawl_item(heap, scores, counter, {"url": "https://example.test/about", "score": 20, "depth": 0})
        _push_crawl_item(heap, scores, counter, {"url": "https://example.test/news", "score": 90, "depth": 1})
        _push_crawl_item(heap, scores, counter, {"url": "https://example.test/applications", "score": 112, "depth": 1})
        _, _, _, item = heapq.heappop(heap)
        self.assertEqual(item["url"], "https://example.test/applications")


class DynamicCrawlIntegrationTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, url: str, text: str):
            self.url = url
            self.text = text
            self.content = text.encode("utf-8")
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

    class FakeClient:
        pages = {
            "https://example.test/": """<html><head><title>Example</title></head><body><main><h1>Example</h1>
                <p>This industrial laser company develops precision manufacturing technologies for customers.</p>
                <a href='/products/'>Products</a><a href='/applications/'>Laser applications</a></main></body></html>""",
            "https://example.test/applications/": """<html><head><title>Femtosecond laser applications</title></head><body><main><h1>Applications</h1>
                <section><h2>Medical processing</h2><p>Femtosecond laser micromachining supports medical components and production applications.</p></section>
                <a href='/news/'>News</a></main></body></html>""",
            "https://example.test/news/": """<html><head><title>Laser news</title></head><body><main><h1>News</h1>
                <article><p>New ultrafast laser manufacturing capability is now available for industrial processing customers.</p></article></main></body></html>""",
            "https://example.test/products/": """<html><head><title>Products</title></head><body><main><h1>Products</h1>
                <p>General industrial products and systems supplied by the organization for multiple customer segments.</p></main></body></html>""",
        }

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None):
            return DynamicCrawlIntegrationTests.FakeResponse(url, self.pages[url])

    def test_discovered_urls_are_visited_during_same_run_by_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "actors.db"
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE actors (
                    id INTEGER PRIMARY KEY, name TEXT, country TEXT, role TEXT, priority INTEGER,
                    official_url TEXT, active INTEGER, last_scraped_at TEXT, last_status TEXT, updated_at TEXT
                );
                CREATE TABLE actor_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, url TEXT UNIQUE, source_kind TEXT,
                    active INTEGER DEFAULT 1, content_hash TEXT, last_http_status INTEGER, last_checked_at TEXT,
                    last_changed_at TEXT, page_type TEXT, source_score INTEGER DEFAULT 0, discovery_depth INTEGER DEFAULT 0,
                    discovery_context TEXT, discovery_reason TEXT, parent_url TEXT, extraction_mode TEXT,
                    structure_hash TEXT, last_title TEXT, last_error TEXT, ambiguous INTEGER DEFAULT 0, blocks_json TEXT,
                    published_date TEXT, market_extracted_hash TEXT, anomaly_detected_at TEXT,
                    anomaly_detail TEXT, render_required INTEGER DEFAULT 0, etag TEXT, last_modified_header TEXT, discovered_at TEXT
                );
                CREATE TABLE source_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, block_count INTEGER,
                    text_chars INTEGER, captured_at TEXT
                );
                CREATE TABLE page_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, content_hash TEXT,
                    blocks_json TEXT, title TEXT, captured_at TEXT, archived_at TEXT
                );
                CREATE TABLE collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, status TEXT,
                    scanned INTEGER DEFAULT 0, changed INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, message TEXT
                );
                CREATE TABLE site_profiles (
                    actor_id INTEGER PRIMARY KEY, strategy TEXT, status TEXT, profile_json TEXT, profile_hash TEXT,
                    confidence REAL DEFAULT 0, generated_by TEXT, version INTEGER DEFAULT 1, last_profiled_at TEXT,
                    needs_reprofile INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0, health_score REAL DEFAULT 1,
                    last_error TEXT, coverage_json TEXT DEFAULT '{}', coverage_ready INTEGER DEFAULT 0,
                    coverage_discovered INTEGER DEFAULT 0
                );
            """)
            conn.execute(
                "INSERT INTO actors VALUES(1,'Test Actor','France','Test',0,'https://example.test/',1,NULL,'never','now')"
            )
            conn.execute(
                "INSERT INTO site_profiles(actor_id,strategy,status,generated_by) VALUES(1,'generic','pending','bootstrap')"
            )
            conn.commit()
            conn.close()

            with patch.object(scrapers, "ACTORS_DB", db_path), \
                 patch.object(scrapers.httpx, "Client", self.FakeClient), \
                 patch.object(scrapers.OllamaClient, "available", return_value=False):
                result = scrapers.scrape_actors(max_pages_per_actor=3)

            conn = sqlite3.connect(db_path)
            rows = {row[0]: row[1] for row in conn.execute("SELECT url,last_checked_at FROM actor_sources")}
            conn.close()
            self.assertEqual(result["scanned"], 3)
            self.assertIsNotNone(rows["https://example.test/applications/"])
            self.assertIsNotNone(rows["https://example.test/news/"])
            self.assertIsNone(rows["https://example.test/products/"])

    class DeepServiceChainClient(FakeClient):
        # /service/ and /service/deep/ both classify as page_type="service" from their URL
        # path alone (hybrid.classify_source folds the path into its word-match text).
        pages = {
            "https://example.test/": """<html><head><title>Example</title></head><body><main><h1>Example</h1>
                <p>This industrial laser company develops precision manufacturing technologies for customers.</p>
                <a href='/service/'>Our services</a></main></body></html>""",
            "https://example.test/service/": """<html><head><title>Service</title></head><body><main><h1>Job shop</h1>
                <p>Contract manufacturing and job shop capacity for industrial laser processing customers.</p>
                <a href='/service/deep/'>Capabilities</a></main></body></html>""",
            "https://example.test/service/deep/": """<html><head><title>Service capabilities</title></head><body><main><h1>Capabilities</h1>
                <p>Detailed laser processing capability breakdown for industrial manufacturing customers.</p>
                <a href='/service/deep/deeper/'>Further detail</a></main></body></html>""",
        }

    def test_a_service_page_at_max_depth_still_discovers_one_more_level(self):
        # DEFAULT_SITE_PROFILE's max_depth is 2 (site_profiles.DEFAULT_SITE_PROFILE), so the
        # depth-2 page ("/service/deep/") would normally be the crawl's last stop. Because it
        # classifies as a "service" page, HIGH_VALUE_PAGE_TYPES lets the crawler take one more
        # hop and register "/service/deep/deeper/" -- a static max_depth would have missed it.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "actors.db"
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE actors (
                    id INTEGER PRIMARY KEY, name TEXT, country TEXT, role TEXT, priority INTEGER,
                    official_url TEXT, active INTEGER, last_scraped_at TEXT, last_status TEXT, updated_at TEXT
                );
                CREATE TABLE actor_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, url TEXT UNIQUE, source_kind TEXT,
                    active INTEGER DEFAULT 1, content_hash TEXT, last_http_status INTEGER, last_checked_at TEXT,
                    last_changed_at TEXT, page_type TEXT, source_score INTEGER DEFAULT 0, discovery_depth INTEGER DEFAULT 0,
                    discovery_context TEXT, discovery_reason TEXT, parent_url TEXT, extraction_mode TEXT,
                    structure_hash TEXT, last_title TEXT, last_error TEXT, ambiguous INTEGER DEFAULT 0, blocks_json TEXT,
                    published_date TEXT, market_extracted_hash TEXT, anomaly_detected_at TEXT,
                    anomaly_detail TEXT, render_required INTEGER DEFAULT 0, etag TEXT, last_modified_header TEXT, discovered_at TEXT
                );
                CREATE TABLE source_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, block_count INTEGER,
                    text_chars INTEGER, captured_at TEXT
                );
                CREATE TABLE page_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, content_hash TEXT,
                    blocks_json TEXT, title TEXT, captured_at TEXT, archived_at TEXT
                );
                CREATE TABLE collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, status TEXT,
                    scanned INTEGER DEFAULT 0, changed INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, message TEXT
                );
                CREATE TABLE site_profiles (
                    actor_id INTEGER PRIMARY KEY, strategy TEXT, status TEXT, profile_json TEXT, profile_hash TEXT,
                    confidence REAL DEFAULT 0, generated_by TEXT, version INTEGER DEFAULT 1, last_profiled_at TEXT,
                    needs_reprofile INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0, health_score REAL DEFAULT 1,
                    last_error TEXT, coverage_json TEXT DEFAULT '{}', coverage_ready INTEGER DEFAULT 0,
                    coverage_discovered INTEGER DEFAULT 0
                );
            """)
            conn.execute(
                "INSERT INTO actors VALUES(1,'Test Actor','France','Test',0,'https://example.test/',1,NULL,'never','now')"
            )
            conn.execute(
                "INSERT INTO site_profiles(actor_id,strategy,status,generated_by) VALUES(1,'generic','pending','bootstrap')"
            )
            conn.commit()
            conn.close()

            with patch.object(scrapers, "ACTORS_DB", db_path), \
                 patch.object(scrapers.httpx, "Client", self.DeepServiceChainClient), \
                 patch.object(scrapers.OllamaClient, "available", return_value=False):
                scrapers.scrape_actors(max_pages_per_actor=3)

            conn = sqlite3.connect(db_path)
            urls = {row[0] for row in conn.execute("SELECT url FROM actor_sources")}
            conn.close()
            self.assertIn("https://example.test/service/deep/deeper/", urls)


class MarketSourceSelectionTests(unittest.TestCase):
    def _seed_actor_with_application_sources(self, actors_db, name, *, priority, count):
        with patch.object(dbmod, "ACTORS_DB", actors_db):
            with dbmod.connect(actors_db) as db:
                existing = db.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone()
            if existing:
                # init_databases() already seeded this actor (e.g. FEMTOprint, a real
                # SITE_OVERRIDES key) -- reuse its row instead of failing on a name clash.
                actor_id = existing["id"]
                with dbmod.connect(actors_db) as db:
                    db.execute("UPDATE actors SET priority=? WHERE id=?", (int(priority), actor_id))
            else:
                actor_id = dbmod.create_actor(
                    name, "Suisse", "Test", f"https://{name.lower().replace(' ', '')}.example/", priority=priority
                )
            with dbmod.connect(actors_db) as db:
                for i in range(count):
                    db.execute(
                        """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,active,source_score)
                           VALUES(?,?,?,?,1,?)""",
                        (
                            actor_id,
                            f"https://{name.lower().replace(' ', '')}.example/applications/market-{i}.asp",
                            "discovered", "application", 50 - i,
                        ),
                    )
        return actor_id

    def test_site_override_deepens_pass_two_even_without_the_priority_flag(self):
        # FEMTOprint is a real SITE_OVERRIDES key (site_profiles.py) with priority=0 in the
        # live base -- Pass 2 must still deepen it via its own market_source_quotas override,
        # or its whole per-market applications taxonomy stays capped at Pass 1's baseline of 1
        # forever. A plain actor with no override and no priority flag gets no such deepening.
        # max_pages is kept tight (below the 20 rows available) so Pass 3's "fill remaining
        # capacity" can't silently paper over a broken Pass 2.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            # init_databases() touches all three databases (it also runs the evidence/offer
            # migrations against whatever MARKET_DB/TECH_DB currently point to) -- patching only
            # ACTORS_DB here let it run those migrations against the real production market.db
            # on every test run, corrupting it (duplicate evidence_sources/offer_sources rows).
            with patch.object(dbmod, "ACTORS_DB", actors_db), \
                 patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"), \
                 patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"):
                dbmod.init_databases()
                # init_databases() also seeds the ~20 real actors from db.ACTORS (with their
                # own SEED_SOURCES rows); drop them so Pass 1's per-actor budget isn't spent
                # on unrelated actors before FEMTOprint's Pass-2 deepening gets a turn.
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors WHERE name NOT IN ('FEMTOprint')")
            self._seed_actor_with_application_sources(actors_db, "FEMTOprint", priority=False, count=10)
            self._seed_actor_with_application_sources(actors_db, "Generic Co", priority=False, count=10)

            with patch.object(scrapers, "ACTORS_DB", actors_db):
                selected = _select_market_sources(max_pages=10)

            counts: dict[str, int] = {}
            for row in selected:
                if row["page_type"] == "application":
                    counts[row["name"]] = counts.get(row["name"], 0) + 1
            self.assertGreaterEqual(counts.get("FEMTOprint", 0), 8)
            self.assertLessEqual(counts.get("Generic Co", 0), 2)


class TargetedActorCrawlTests(unittest.TestCase):
    """scrape_actors(actor_names=...) : correction du périmètre -- crawler un acteur précis
    (ex: ajouté après le dernier run complet et jamais visité) sans payer une passe complète."""

    class FakeResponse:
        def __init__(self, url: str):
            self.url = url
            self.text = """<html><head><title>Home</title></head><body><main><h1>Home</h1>
                <p>Femtosecond laser processing services for industrial customers worldwide.</p>
                </main></body></html>"""
            self.content = self.text.encode("utf-8")
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, **kwargs):
            return TargetedActorCrawlTests.FakeResponse(url)

    def test_actor_names_filter_only_crawls_the_named_actors(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                dbmod.create_actor("Target Actor", "Espagne", "Test", "https://example.test/", priority=False)
                dbmod.create_actor("Other Actor", "Espagne", "Test", "https://other.example/", priority=False)

            with (
                patch.object(scrapers, "ACTORS_DB", actors_db),
                patch.object(scrapers.httpx, "Client", self.FakeClient),
                patch.object(scrapers.OllamaClient, "available", return_value=False),
            ):
                result = scrapers.scrape_actors(actor_names=["Target Actor"])

            with dbmod.connect(actors_db) as db:
                rows = {row["name"]: row["last_status"] for row in db.execute("SELECT name,last_status FROM actors")}

            self.assertEqual(1, result["profiled"])
            self.assertEqual("ok", rows["Target Actor"])
            # Untouched: never selected by the actor_names filter in the first place.
            self.assertEqual("never", rows["Other Actor"])


class ProfileTests(unittest.TestCase):
    def test_generic_plus_override(self):
        priority = get_site_profile({"name": "ALPHANOV", "official_url": "https://www.alphanov.com"})
        generic = get_site_profile({"name": "Unknown", "official_url": "https://example.test"})
        self.assertIn("/collaborative-projects/", priority["priority_paths"])
        self.assertIn("/privacy", priority["ignore_paths"])
        self.assertEqual(crawl_budget(priority, True), 12)
        self.assertEqual(crawl_budget(generic, False), 6)


if __name__ == "__main__":
    unittest.main()
