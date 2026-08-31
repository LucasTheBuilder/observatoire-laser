"""Tests pour la purge du reliquat de découverte (Lot 2 §2.2, audit veille §9.1, 30/08/2026) :
"89,4% des URLs découvertes ne sont jamais visitées [...] et rien ne les purge." db.
purge_stale_backlog() désactive les URLs page_type='other', jamais visitées, découvertes il y a
plus de N jours -- jamais sur une ligne sans discovered_at connu (découverte avant l'existence
de cette colonne : jamais purgée sur une hypothèse).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod


def _setup(tmp: str) -> tuple[Path, int]:
    actors_db = Path(tmp) / "actors.db"
    with (
        patch.object(dbmod, "ACTORS_DB", actors_db),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
    ):
        dbmod.init_databases()
        actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
    return actors_db, actor_id


def _insert_source(db, actor_id: int, url: str, *, page_type: str = "other", last_checked_at: str | None = None, discovered_at: str | None = None) -> None:
    db.execute(
        "INSERT INTO actor_sources(actor_id,url,active,page_type,last_checked_at,discovered_at) VALUES(?,?,1,?,?,?)",
        (actor_id, url, page_type, last_checked_at, discovered_at),
    )


class PurgeStaleBacklogTests(unittest.TestCase):
    def test_old_unvisited_other_url_is_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = _setup(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
            with dbmod.connect(actors_db) as db:
                _insert_source(db, actor_id, "https://example.test/old-other", discovered_at=old)
            purged = dbmod.purge_stale_backlog(days=90, db_path=actors_db)
            self.assertEqual(1, purged)
            active = dbmod.scalar(actors_db, "SELECT active FROM actor_sources WHERE url=?", ("https://example.test/old-other",))
            self.assertEqual(0, active)

    def test_recently_discovered_url_is_not_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = _setup(tmp)
            recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            with dbmod.connect(actors_db) as db:
                _insert_source(db, actor_id, "https://example.test/recent-other", discovered_at=recent)
            purged = dbmod.purge_stale_backlog(days=90, db_path=actors_db)
            self.assertEqual(0, purged)

    def test_url_without_known_discovery_date_is_never_purged(self):
        # Rows discovered before the discovered_at column existed have no known date -- never
        # purged on a fabricated/assumed date.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                _insert_source(db, actor_id, "https://example.test/unknown-age", discovered_at=None)
            purged = dbmod.purge_stale_backlog(days=90, db_path=actors_db)
            self.assertEqual(0, purged)
            active = dbmod.scalar(actors_db, "SELECT active FROM actor_sources WHERE url=?", ("https://example.test/unknown-age",))
            self.assertEqual(1, active)

    def test_visited_url_is_never_purged_even_if_old_and_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = _setup(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
            with dbmod.connect(actors_db) as db:
                _insert_source(db, actor_id, "https://example.test/visited-other", discovered_at=old, last_checked_at=old)
            purged = dbmod.purge_stale_backlog(days=90, db_path=actors_db)
            self.assertEqual(0, purged)

    def test_non_other_page_type_is_never_purged_even_if_old_and_unvisited(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = _setup(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
            with dbmod.connect(actors_db) as db:
                _insert_source(db, actor_id, "https://example.test/old-application", page_type="application", discovered_at=old)
            purged = dbmod.purge_stale_backlog(days=90, db_path=actors_db)
            self.assertEqual(0, purged)

    def test_defaults_to_module_level_actors_db_when_no_path_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = _setup(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
            with dbmod.connect(actors_db) as db:
                _insert_source(db, actor_id, "https://example.test/old-other", discovered_at=old)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                purged = dbmod.purge_stale_backlog(days=90)
            self.assertEqual(1, purged)


class DiscoveredAtWiringTests(unittest.TestCase):
    def test_scrape_actors_sets_discovered_at_on_seed_urls(self):
        import scrapers

        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with (
                patch.object(scrapers, "ACTORS_DB", actors_db),
                patch.object(scrapers.httpx, "Client") as mock_client_cls,
                patch.object(scrapers.OllamaClient, "available", return_value=False),
            ):
                mock_client_cls.return_value.__enter__.return_value.get.side_effect = RuntimeError("network disabled in this test")
                scrapers.scrape_actors(max_pages_per_actor=1, actor_names=["Example"])
            row = dbmod.rows(actors_db, "SELECT discovered_at FROM actor_sources WHERE url=?", ("https://example.test/",))[0]
            self.assertIsNotNone(row["discovered_at"])


if __name__ == "__main__":
    unittest.main()
