"""Budget d'exploration du crawl (29/09/2026) : en plus du budget normal (file par score, qui
revisite les pages clés), chaque collecte télécharge des pages d'offre jamais visitées, en
tourniquet par type -- sans quoi la même file triée repart à l'identique à chaque collecte."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db as dbmod
import scrapers
from scrapers import EXPLORATION_PAGE_TYPES, _exploration_queue
from site_profiles import explore_budget, get_site_profile


def _source(i: int, page_type: str, score: int = 10, checked: str | None = None) -> dict:
    return {"id": i, "url": f"https://example.test/{page_type}/{i}", "page_type": page_type,
            "source_score": score, "last_checked_at": checked}


class ExplorationQueueTests(unittest.TestCase):
    def test_only_never_fetched_offer_pages_are_queued(self):
        queue = _exploration_queue([
            _source(1, "service"),
            _source(2, "service", checked="2026-08-30T00:00:00+00:00"),
            _source(3, "news"),
            _source(4, "other"),
            _source(5, "product"),
        ])
        self.assertEqual([1, 5], [item["source_id"] for item in queue])

    def test_round_robin_across_types_best_score_first_within_a_type(self):
        queue = _exploration_queue([
            _source(1, "technology", 90), _source(2, "technology", 80), _source(3, "technology", 70),
            _source(4, "service", 5), _source(5, "service", 50), _source(6, "product", 1),
        ])
        self.assertEqual([5, 6, 1, 4, 2, 3], [item["source_id"] for item in queue])

    def test_default_budget_applies_to_every_profile(self):
        self.assertEqual(20, explore_budget(get_site_profile({"name": "Unknown", "official_url": "https://x.test/"})))
        self.assertEqual(20, explore_budget(get_site_profile({"name": "ALPHANOV", "official_url": "https://www.alphanov.com/"})))
        self.assertIn("product", EXPLORATION_PAGE_TYPES)


class ExplorationCrawlTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, url: str):
            self.url = url
            self.text = """<html><head><title>Page</title></head><body><main><h1>Page</h1>
                <p>Femtosecond laser processing services for industrial customers worldwide.</p>
                </main></body></html>"""
            self.content = self.text.encode("utf-8")
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

    class FakeClient:
        fetched: list[str] = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, **kwargs):
            ExplorationCrawlTests.FakeClient.fetched.append(url)
            return ExplorationCrawlTests.FakeResponse(url)

    def test_each_run_drains_unfetched_offer_pages_beyond_the_normal_budget(self):
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
                dbmod.create_actor("Target Actor", "France", "Test", "https://example.test/", priority=False)
                with dbmod.connect(actors_db) as db:
                    actor_id = db.execute("SELECT id FROM actors").fetchone()[0]
                    pages = [("technology", 15), ("service", 15), ("product", 10)]
                    for page_type, count in pages:
                        for i in range(count):
                            db.execute(
                                "INSERT INTO actor_sources(actor_id,url,source_kind,page_type,source_score,discovery_depth) VALUES(?,?,?,?,?,0)",
                                (actor_id, f"https://example.test/{page_type}/{i}", "sitemap", page_type, 10),
                            )

            def offer_pages_fetched() -> int:
                with dbmod.connect(actors_db) as db:
                    return db.execute(
                        "SELECT COUNT(*) FROM actor_sources WHERE page_type IN ('technology','service','product') AND last_checked_at IS NOT NULL"
                    ).fetchone()[0]

            with (
                patch.object(scrapers, "ACTORS_DB", actors_db),
                patch.object(scrapers.httpx, "Client", self.FakeClient),
                patch.object(scrapers.OllamaClient, "available", return_value=False),
                patch.object(scrapers.time, "sleep"),
            ):
                self.FakeClient.fetched = []
                scrapers.scrape_actors(actor_names=["Target Actor"])
                first_run = offer_pages_fetched()
                first_run_products = sum("/product/" in url for url in self.FakeClient.fetched)
                scrapers.scrape_actors(actor_names=["Target Actor"])
                second_run = offer_pages_fetched()

            # Budget normal (6, dont la page d'accueil) + 20 d'exploration : bien au-delà des 6
            # pages d'avant, et le tourniquet atteint les produits malgré leur score égal.
            self.assertGreaterEqual(first_run, 20)
            self.assertGreaterEqual(first_run_products, 5)
            # La 2e collecte reprend là où la 1re s'est arrêtée au lieu de revisiter les mêmes pages.
            self.assertEqual(40, second_run)

    def test_explicit_page_cap_disables_exploration(self):
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
                dbmod.create_actor("Target Actor", "France", "Test", "https://example.test/", priority=False)
                with dbmod.connect(actors_db) as db:
                    actor_id = db.execute("SELECT id FROM actors").fetchone()[0]
                    for i in range(10):
                        db.execute(
                            "INSERT INTO actor_sources(actor_id,url,source_kind,page_type,source_score,discovery_depth) VALUES(?,?,?,?,?,0)",
                            (actor_id, f"https://example.test/service/{i}", "sitemap", "service", 10),
                        )
            with (
                patch.object(scrapers, "ACTORS_DB", actors_db),
                patch.object(scrapers.httpx, "Client", self.FakeClient),
                patch.object(scrapers.OllamaClient, "available", return_value=False),
                patch.object(scrapers.time, "sleep"),
            ):
                result = scrapers.scrape_actors(max_pages_per_actor=2, actor_names=["Target Actor"])
            self.assertEqual(2, result["scanned"])


if __name__ == "__main__":
    unittest.main()
