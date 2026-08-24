from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from scrapers import _load_custom_lexicon_entries


class ActorManagementTests(unittest.TestCase):
    def _fresh_actors_db(self, tmp: str) -> Path:
        directory = Path(tmp)
        actors_db = directory / "actors.db"
        with patch.object(dbmod, "ACTORS_DB", actors_db), \
             patch.object(dbmod, "MARKET_DB", directory / "market.db"), \
             patch.object(dbmod, "TECH_DB", directory / "technology.db"):
            dbmod.init_databases()
        return actors_db

    def test_create_actor_bootstraps_a_site_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example", priority=True)

            with dbmod.connect(actors_db) as db:
                actor = db.execute("SELECT * FROM actors WHERE id=?", (actor_id,)).fetchone()
                profile = db.execute("SELECT strategy,status,generated_by FROM site_profiles WHERE actor_id=?", (actor_id,)).fetchone()
            self.assertEqual("New Laser Co", actor["name"])
            self.assertEqual(1, actor["active"])
            self.assertEqual(1, actor["priority"])
            self.assertEqual(("adaptive", "pending", "manual"), tuple(profile))

    def test_create_actor_rejects_duplicate_name_and_bad_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                with self.assertRaises(ValueError):
                    dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://other.example")
                with self.assertRaises(ValueError):
                    dbmod.create_actor("Another Co", "France", "Intégrateur", "not-a-url")

    def test_set_actor_active_toggles_and_rejects_unknown_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                dbmod.set_actor_active(actor_id, False)
                with dbmod.connect(actors_db) as db:
                    active = db.execute("SELECT active FROM actors WHERE id=?", (actor_id,)).fetchone()[0]
                self.assertEqual(0, active)
                with self.assertRaises(ValueError):
                    dbmod.set_actor_active(999999, True)


class VocabularyPromotionTests(unittest.TestCase):
    def _fresh_market_db(self, tmp: str) -> Path:
        directory = Path(tmp)
        market_db = directory / "market.db"
        with patch.object(dbmod, "MARKET_DB", market_db), \
             patch.object(dbmod, "ACTORS_DB", directory / "actors.db"), \
             patch.object(dbmod, "TECH_DB", directory / "technology.db"):
            dbmod.init_databases()
        return market_db

    def _seed_candidate(self, market_db: Path, proposed_labels: dict) -> int:
        with dbmod.connect(market_db) as db:
            return db.execute(
                """INSERT INTO vocabulary_candidates(
                       actor_name,source_url,source_title,quote,block_heading,proposed_labels,resolved_labels,
                       fingerprint,review_status,created_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    "Example", "https://example.test/aero", "Applications",
                    "Femtosecond laser texturing of advanced pressure transducer housings.",
                    "Aerospace pressure sensors", json.dumps(proposed_labels, ensure_ascii=False), "{}",
                    "vocab-fp-1", dbmod.utc_now(), dbmod.utc_now(),
                ),
            ).lastrowid

    def test_accept_creates_a_custom_lexicon_entry_and_marks_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            with patch.object(dbmod, "MARKET_DB", market_db):
                candidate_id = self._seed_candidate(market_db, {"component": "Boîtiers de capteurs de pression avancés"})
                result = dbmod.accept_vocabulary_candidate(candidate_id, "component")

            self.assertEqual({"dimension": "component", "label": "Boîtiers de capteurs de pression avancés"}, result)
            with dbmod.connect(market_db) as db:
                status = db.execute("SELECT review_status FROM vocabulary_candidates WHERE id=?", (candidate_id,)).fetchone()[0]
                entry = db.execute(
                    "SELECT dimension,label,match_terms FROM custom_lexicon_entries WHERE dimension='component'"
                ).fetchone()
            self.assertEqual("accepted", status)
            self.assertEqual("Boîtiers de capteurs de pression avancés", entry["label"])
            self.assertEqual(["Boîtiers de capteurs de pression avancés"], json.loads(entry["match_terms"]))

    def test_accept_rejects_unknown_candidate_or_unproposed_dimension(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            with patch.object(dbmod, "MARKET_DB", market_db):
                candidate_id = self._seed_candidate(market_db, {"component": "Boîtiers avancés"})
                with self.assertRaises(ValueError):
                    dbmod.accept_vocabulary_candidate(999999, "component")
                with self.assertRaises(ValueError):
                    dbmod.accept_vocabulary_candidate(candidate_id, "market")

    def test_reject_marks_rejected_and_rejects_unknown_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            with patch.object(dbmod, "MARKET_DB", market_db):
                candidate_id = self._seed_candidate(market_db, {"component": "Boîtiers avancés"})
                dbmod.reject_vocabulary_candidate(candidate_id)
                with dbmod.connect(market_db) as db:
                    status = db.execute("SELECT review_status FROM vocabulary_candidates WHERE id=?", (candidate_id,)).fetchone()[0]
                self.assertEqual("rejected", status)
                with self.assertRaises(ValueError):
                    dbmod.reject_vocabulary_candidate(999999)


class CustomLexiconMergeTests(unittest.TestCase):
    def test_load_custom_lexicon_entries_merges_into_the_given_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            market_db = directory / "market.db"
            with patch.object(dbmod, "MARKET_DB", market_db), \
                 patch.object(dbmod, "ACTORS_DB", directory / "actors.db"), \
                 patch.object(dbmod, "TECH_DB", directory / "technology.db"):
                dbmod.init_databases()
            with dbmod.connect(market_db) as db:
                db.execute(
                    """INSERT INTO custom_lexicon_entries(dimension,label,match_terms,created_at)
                       VALUES('component',?,?,?)""",
                    ("Boîtiers de capteurs de pression avancés",
                     json.dumps(["Boîtiers de capteurs de pression avancés"], ensure_ascii=False), dbmod.utc_now()),
                )

            target = {"market": {}, "component": {}, "operation": {}}
            with patch("scrapers.MARKET_DB", market_db):
                _load_custom_lexicon_entries(target)

            self.assertIn("Boîtiers de capteurs de pression avancés", target["component"])
            self.assertEqual({}, target["market"])

    def test_missing_table_is_a_silent_no_op(self):
        # An empty/never-migrated market.db (e.g. a raw sqlite3.connect in another test)
        # must not raise -- this is called unconditionally at the top of scrape_market().
        with tempfile.TemporaryDirectory() as tmp:
            missing_db = Path(tmp) / "does-not-exist.db"
            target = {"market": {}, "component": {}, "operation": {}}
            with patch("scrapers.MARKET_DB", missing_db):
                _load_custom_lexicon_entries(target)
            self.assertEqual({"market": {}, "component": {}, "operation": {}}, target)


if __name__ == "__main__":
    unittest.main()
