"""Tests pour la santé de collecte (audit veille §9.2, Lot 1 item 1.5, 30/08/2026) :
réconciliation des runs orphelins au démarrage, et l'endpoint /api/collection-health.
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


class ReconcileOrphanedRunsTests(unittest.TestCase):
    def test_running_row_becomes_interrupted_on_next_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                # Simulates a process that died mid-run: a row inserted as 'running' and never
                # updated to 'completed'/'failed' because _run_job never got to finish.
                with dbmod.connect(actors_db) as db:
                    db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (dbmod.utc_now(), "running"))
                # A later startup (this second init_databases() call) must reconcile it.
                dbmod.init_databases()
                status = dbmod.scalar(actors_db, "SELECT status FROM collection_runs")
            self.assertEqual("interrupted", status)

    def test_completed_run_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute(
                        "INSERT INTO collection_runs(started_at,finished_at,status) VALUES(?,?,?)",
                        (dbmod.utc_now(), dbmod.utc_now(), "completed"),
                    )
                dbmod.init_databases()
                status = dbmod.scalar(actors_db, "SELECT status FROM collection_runs")
            self.assertEqual("completed", status)

    def test_reconciles_across_all_three_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                for path in (actors_db, market_db, tech_db):
                    with dbmod.connect(path) as db:
                        db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (dbmod.utc_now(), "running"))
                dbmod.init_databases()
                statuses = [dbmod.scalar(path, "SELECT status FROM collection_runs") for path in (actors_db, market_db, tech_db)]
            self.assertEqual(["interrupted", "interrupted", "interrupted"], statuses)


class CollectionHealthEndpointTests(unittest.TestCase):
    def _fresh_dbs(self, tmp: str) -> tuple[Path, Path, Path]:
        actors_db = Path(tmp) / "actors.db"
        market_db = Path(tmp) / "market.db"
        tech_db = Path(tmp) / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", market_db),
            patch.object(dbmod, "TECH_DB", tech_db),
        ):
            dbmod.init_databases()
            with dbmod.connect(actors_db) as db:
                db.execute("DELETE FROM actors")
        return actors_db, market_db, tech_db

    def test_actor_with_many_failed_sources_is_flagged_with_its_top_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = self._fresh_dbs(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Blocked Actor", "Allemagne", "Test", "https://blocked.example/", priority=True)
            with dbmod.connect(actors_db) as db:
                for i in range(3):
                    db.execute(
                        "INSERT INTO actor_sources(actor_id,url,active,last_http_status) VALUES(?,?,1,?)",
                        (actor_id, f"https://blocked.example/{i}", 403),
                    )
                db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active,last_http_status) VALUES(?,?,1,?)",
                    (actor_id, "https://blocked.example/ok", 200),
                )
                db.execute(
                    "UPDATE site_profiles SET health_score=0.3,failure_count=5,last_error='403 Forbidden' WHERE actor_id=?",
                    (actor_id,),
                )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                result = appmod.collection_health()

            actor = next(a for a in result["actors"] if a["name"] == "Blocked Actor")
            self.assertEqual(4, actor["sources_total"])
            self.assertEqual(3, actor["sources_failed"])
            self.assertEqual(403, actor["top_error_status"])
            self.assertEqual(3, actor["top_error_count"])
            self.assertEqual(0.3, actor["health_score"])

    def test_healthy_actor_has_no_failed_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = self._fresh_dbs(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Healthy Actor", "France", "Test", "https://healthy.example/", priority=False)
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active,last_http_status) VALUES(?,?,1,?)",
                    (actor_id, "https://healthy.example/", 200),
                )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                result = appmod.collection_health()

            actor = next(a for a in result["actors"] if a["name"] == "Healthy Actor")
            self.assertEqual(0, actor["sources_failed"])
            self.assertIsNone(actor["top_error_status"])
            self.assertEqual(0.0, actor["error_rate"])

    def test_recent_runs_are_reported_with_error_rate_across_all_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = self._fresh_dbs(tmp)
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO collection_runs(started_at,finished_at,status,scanned,errors,message) VALUES(?,?,?,?,?,?)",
                    (dbmod.utc_now(), dbmod.utc_now(), "completed", 100, 45, "test run"),
                )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                result = appmod.collection_health()

            run = next(r for r in result["recent_runs"] if r["message"] == "test run")
            self.assertEqual("actors", run["source"])
            self.assertEqual(100, run["scanned"])
            self.assertEqual(45, run["errors"])
            self.assertEqual(0.45, run["error_rate"])


if __name__ == "__main__":
    unittest.main()
