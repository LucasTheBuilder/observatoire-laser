"""Tests pour l'historisation de page (chantier 6) : db.page_versions, alimentée par
scrapers.scrape_actors() juste avant qu'un content_hash changé n'écrase la ligne
actor_sources correspondante -- voir le docstring de page_versions dans db.py.

Le premier crawl d'une page ne doit jamais créer de version (rien à archiver, previous_hash
est NULL) ; seul un RE-crawl dont le contenu a changé le doit. Ces tests font donc tourner
scrape_actors() deux fois de suite contre un FakeClient dont le contenu servi change entre les
deux appels, en réutilisant exactement le double httpx (FakeClient/FakeResponse) déjà validé
par tests/test_deterministic_crawler.py.
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
import scrapers


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
    pages: dict[str, str] = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url):
        return FakeResponse(url, self.pages[url])


def _page(paragraph: str) -> str:
    return (
        "<html><head><title>Example</title></head><body><main><h1>Example</h1>"
        f"<p>This industrial laser company develops precision manufacturing technologies for {paragraph}.</p>"
        "</main></body></html>"
    )


class PageVersionsArchivalTests(unittest.TestCase):
    def _crawl_once(self, actors_db: Path, paragraph: str) -> dict:
        FakeClient.pages = {"https://example.test/": _page(paragraph)}
        with (
            patch.object(scrapers, "ACTORS_DB", actors_db),
            patch.object(scrapers.httpx, "Client", FakeClient),
            patch.object(scrapers.OllamaClient, "available", return_value=False),
        ):
            return scrapers.scrape_actors(max_pages_per_actor=1, actor_names=["Test Actor"])

    def _setup(self, tmp: str) -> Path:
        actors_db = Path(tmp) / "actors.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
            dbmod.create_actor("Test Actor", "France", "Test", "https://example.test/", priority=False)
        return actors_db

    def test_first_crawl_creates_no_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            self._crawl_once(actors_db, "customer segment 1")
            with dbmod.connect(actors_db) as db:
                count = db.execute("SELECT COUNT(*) FROM page_versions").fetchone()[0]
            self.assertEqual(0, count)

    def test_second_crawl_with_changed_content_archives_the_old_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            self._crawl_once(actors_db, "customer segment 1")
            with dbmod.connect(actors_db) as db:
                before = db.execute("SELECT id,content_hash,blocks_json,last_title FROM actor_sources WHERE url=?", ("https://example.test/",)).fetchone()

            self._crawl_once(actors_db, "a very different customer segment 2")

            with dbmod.connect(actors_db) as db:
                after = db.execute("SELECT content_hash FROM actor_sources WHERE url=?", ("https://example.test/",)).fetchone()
                versions = db.execute("SELECT source_id,content_hash,blocks_json,title FROM page_versions").fetchall()

            self.assertNotEqual(before["content_hash"], after["content_hash"])
            self.assertEqual(1, len(versions))
            self.assertEqual(before["id"], versions[0]["source_id"])
            self.assertEqual(before["content_hash"], versions[0]["content_hash"])
            self.assertEqual(before["blocks_json"], versions[0]["blocks_json"])
            self.assertEqual(before["last_title"], versions[0]["title"])

    def test_unchanged_content_on_recrawl_archives_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            self._crawl_once(actors_db, "the exact same customer segment")
            self._crawl_once(actors_db, "the exact same customer segment")
            with dbmod.connect(actors_db) as db:
                count = db.execute("SELECT COUNT(*) FROM page_versions").fetchone()[0]
            self.assertEqual(0, count)

    def test_retention_caps_history_length_per_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            # One version per crawl beyond the first; PAGE_VERSIONS_RETENTION + 3 changes should
            # still leave exactly PAGE_VERSIONS_RETENTION rows, keeping only the most recent.
            for i in range(scrapers.PAGE_VERSIONS_RETENTION + 3):
                self._crawl_once(actors_db, f"customer segment number {i}")
            with dbmod.connect(actors_db) as db:
                count = db.execute("SELECT COUNT(*) FROM page_versions").fetchone()[0]
            self.assertEqual(scrapers.PAGE_VERSIONS_RETENTION, count)


if __name__ == "__main__":
    unittest.main()
