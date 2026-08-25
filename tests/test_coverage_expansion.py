from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as appmod
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

    def test_init_databases_does_not_crash_with_a_manually_added_actor(self):
        # Regression test: init_databases() re-runs on every app startup and used to derive
        # each actor's priority by looking it up in the static ACTORS list, which raised
        # StopIteration (crash-looping the whole app on every restart, in a real deployment)
        # the moment a non-seed actor -- created via create_actor()/POST /api/actors -- existed.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example", priority=True)
            # init_databases() touches all three databases, so all three must stay isolated --
            # not just ACTORS_DB -- for this regression call, same as _fresh_actors_db above.
            with patch.object(dbmod, "ACTORS_DB", actors_db), \
                 patch.object(dbmod, "MARKET_DB", directory / "market.db"), \
                 patch.object(dbmod, "TECH_DB", directory / "technology.db"):
                dbmod.init_databases()  # simulates the next app startup/container restart
            with dbmod.connect(actors_db) as db:
                profile = db.execute("SELECT strategy FROM site_profiles WHERE actor_id=?", (actor_id,)).fetchone()
            self.assertEqual("adaptive", profile["strategy"])

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

    def test_update_actor_classification_sets_only_the_given_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                dbmod.update_actor_classification(actor_id, competitive_class="C1", is_reference=False)
                with dbmod.connect(actors_db) as db:
                    row = db.execute(
                        "SELECT competitive_class,is_reference,parent_actor,entity_note FROM actors WHERE id=?", (actor_id,)
                    ).fetchone()
                self.assertEqual(("C1", 0, None, None), tuple(row))

                # A second call touching only parent_actor must not clobber competitive_class.
                dbmod.update_actor_classification(actor_id, parent_actor="Bigger Group")
                with dbmod.connect(actors_db) as db:
                    row = db.execute(
                        "SELECT competitive_class,parent_actor FROM actors WHERE id=?", (actor_id,)
                    ).fetchone()
                self.assertEqual(("C1", "Bigger Group"), tuple(row))

                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(999999, competitive_class="C1")

    def test_update_actor_classification_rejects_invalid_review_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(actor_id, review_status="not-a-status")
                dbmod.update_actor_classification(actor_id, review_status="candidate")
                with dbmod.connect(actors_db) as db:
                    status = db.execute("SELECT review_status FROM actors WHERE id=?", (actor_id,)).fetchone()[0]
                self.assertEqual("candidate", status)

    def test_update_actor_classification_sets_actor_type_and_business_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                dbmod.update_actor_classification(
                    actor_id, actor_type="societe_technologique_specialisee", business_models=["equipment", "service"]
                )
                with dbmod.connect(actors_db) as db:
                    row = db.execute("SELECT actor_type,business_models FROM actors WHERE id=?", (actor_id,)).fetchone()
                self.assertEqual("societe_technologique_specialisee", row["actor_type"])
                self.assertEqual(["equipment", "service"], json.loads(row["business_models"]))

                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(actor_id, actor_type="not-a-real-type")
                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(actor_id, business_models=["equipment", "not-a-real-model"])

    def test_update_actor_classification_edits_descriptive_fields_and_rejects_name_clash(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                first_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                second_id = dbmod.create_actor("Other Laser Co", "Allemagne", "Job-shop", "https://other.example")

                dbmod.update_actor_classification(
                    first_id, name="Renamed Laser Co", country="Belgique", role="Job-shop laser",
                    official_url="https://renamed.example", priority=True,
                )
                with dbmod.connect(actors_db) as db:
                    row = db.execute(
                        "SELECT name,country,role,official_url,priority FROM actors WHERE id=?", (first_id,)
                    ).fetchone()
                self.assertEqual(("Renamed Laser Co", "Belgique", "Job-shop laser", "https://renamed.example", 1), tuple(row))

                # Renaming to another actor's existing name must be rejected, not silently
                # accepted (actors.name is UNIQUE) or crash with a raw sqlite3.IntegrityError.
                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(second_id, name="Renamed Laser Co")
                # Renaming an actor to its own current name is not a clash.
                dbmod.update_actor_classification(first_id, name="Renamed Laser Co")

                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(first_id, official_url="not-a-url")
                with self.assertRaises(ValueError):
                    dbmod.update_actor_classification(first_id, name="   ")

    def test_delete_actor_removes_it_and_cascades_its_site_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                dbmod.delete_actor(actor_id)
                with dbmod.connect(actors_db) as db:
                    actor_row = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
                    profile_row = db.execute("SELECT 1 FROM site_profiles WHERE actor_id=?", (actor_id,)).fetchone()
                self.assertIsNone(actor_row)
                self.assertIsNone(profile_row)

                with self.assertRaises(ValueError):
                    dbmod.delete_actor(999999)

    def test_add_actor_relation_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                relation_id = dbmod.add_actor_relation(actor_id, "partner", "Big University Lab", note="Co-published a paper")
                with dbmod.connect(actors_db) as db:
                    row = db.execute(
                        "SELECT actor_id,related_name,relation_type,note FROM actor_relations WHERE id=?", (relation_id,)
                    ).fetchone()
                self.assertEqual((actor_id, "Big University Lab", "partner", "Co-published a paper"), tuple(row))

                with self.assertRaises(ValueError):
                    dbmod.add_actor_relation(actor_id, "rival", "Someone")  # invalid relation_type
                with self.assertRaises(ValueError):
                    dbmod.add_actor_relation(999999, "partner", "Someone")  # unknown actor
                with self.assertRaises(ValueError):
                    dbmod.add_actor_relation(actor_id, "partner", "  ")  # blank name

    def test_update_actor_classification_sets_strategic_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                dbmod.update_actor_classification(actor_id, strategic_summary="Prestataire de micro-usinage USP.")
                with dbmod.connect(actors_db) as db:
                    summary = db.execute("SELECT strategic_summary FROM actors WHERE id=?", (actor_id,)).fetchone()[0]
                self.assertEqual("Prestataire de micro-usinage USP.", summary)

                # A call touching a different field must not clobber the summary.
                dbmod.update_actor_classification(actor_id, parent_actor="Bigger Group")
                with dbmod.connect(actors_db) as db:
                    summary = db.execute("SELECT strategic_summary FROM actors WHERE id=?", (actor_id,)).fetchone()[0]
                self.assertEqual("Prestataire de micro-usinage USP.", summary)

    def test_add_actor_fact_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                fact_id = dbmod.add_actor_fact(actor_id, "certification", "ISO 9001", source_url="https://newlaser.example/iso")
                with dbmod.connect(actors_db) as db:
                    row = db.execute(
                        "SELECT actor_id,dimension,value,source_url,review_status FROM actor_facts WHERE id=?", (fact_id,)
                    ).fetchone()
                self.assertEqual((actor_id, "certification", "ISO 9001", "https://newlaser.example/iso", "verified"), tuple(row))

                with self.assertRaises(ValueError):
                    dbmod.add_actor_fact(actor_id, "not-a-dimension", "value")
                with self.assertRaises(ValueError):
                    dbmod.add_actor_fact(actor_id, "certification", "  ")  # blank value
                with self.assertRaises(ValueError):
                    dbmod.add_actor_fact(999999, "certification", "ISO 9001")  # unknown actor

    def test_add_actor_event_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                event_id = dbmod.add_actor_event(
                    actor_id, "acquisition", "Acquired by Bigger Group.", event_date="2026-05-19", source_url="https://news.example/deal"
                )
                with dbmod.connect(actors_db) as db:
                    row = db.execute(
                        "SELECT actor_id,event_type,description,event_date,source_url FROM actor_events WHERE id=?", (event_id,)
                    ).fetchone()
                self.assertEqual(
                    (actor_id, "acquisition", "Acquired by Bigger Group.", "2026-05-19", "https://news.example/deal"), tuple(row)
                )

                with self.assertRaises(ValueError):
                    dbmod.add_actor_event(actor_id, "  ", "Description")  # blank event_type
                with self.assertRaises(ValueError):
                    dbmod.add_actor_event(actor_id, "acquisition", "  ")  # blank description
                with self.assertRaises(ValueError):
                    dbmod.add_actor_event(999999, "acquisition", "Description")  # unknown actor

    def test_find_actor_duplicate_candidates_flags_domain_parent_and_name_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._fresh_actors_db(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                a = dbmod.create_actor("Acme Laser", "France", "Fabricant", "https://acme-laser.example/en")
                b = dbmod.create_actor("Acme Laser GmbH", "Allemagne", "Fabricant", "https://acme-laser.example/de")
                dbmod.create_actor("Unrelated Co", "France", "Fabricant", "https://unrelated.example")
                acquired = dbmod.create_actor("Acquired Sub", "Irlande", "Fabricant", "https://acquired.example")
                dbmod.update_actor_classification(acquired, parent_actor="Acme Laser")

                candidates = dbmod.find_actor_duplicate_candidates()
                pairs = {frozenset((c["actor_a_id"], c["actor_b_id"])) for c in candidates}
                self.assertIn(frozenset((a, b)), pairs)  # same domain + same name once "GmbH" is stripped
                self.assertIn(frozenset((a, acquired)), pairs)  # parent/subsidiary link
                flagged_ids = {actor_id for pair in pairs for actor_id in pair}
                unrelated_id = next(
                    row["id"] for row in dbmod.rows(actors_db, "SELECT id FROM actors WHERE name='Unrelated Co'")
                )
                self.assertNotIn(unrelated_id, flagged_ids)


class ActorFichesEnrichmentTests(unittest.TestCase):
    """Fiches-cibles audit integration: coverage_level is computed (never hand-set) from
    market.db's own distinct-source count, and actor_facts/actor_events ride along with each
    actor in /api/actors so the drawer never has to make a second round trip.
    """

    def test_coverage_level_thresholds(self):
        self.assertEqual("weak", appmod._coverage_level(0))
        self.assertEqual("partial", appmod._coverage_level(1))
        self.assertEqual("partial", appmod._coverage_level(9))
        self.assertEqual("good", appmod._coverage_level(10))
        self.assertEqual("good", appmod._coverage_level(24))

    def test_list_actors_attaches_coverage_level_and_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actors_db, market_db, tech_db = root / "actors.db", root / "market.db", root / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                actor_id = dbmod.create_actor("New Laser Co", "France", "Intégrateur", "https://newlaser.example")
                dbmod.update_actor_classification(actor_id, strategic_summary="Prestataire USP.")
                dbmod.add_actor_fact(actor_id, "certification", "ISO 9001", source_url="https://newlaser.example/iso")
                dbmod.add_actor_event(actor_id, "acquisition", "Racheté par Big Group.", event_date="2026-01-01")

            stamp = dbmod.utc_now()
            with dbmod.connect(market_db) as db:
                for i in range(2):
                    db.execute(
                        """INSERT INTO offers(actor_name,offer_type,capability,source_url,source_title,quote,
                               fact_key,fingerprint,review_status,created_at,updated_at,last_seen_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("New Laser Co", "service", "Découpe", f"https://example.test/offer{i}", "t", "q",
                         f"key{i}", f"fp{i}", "accepted", stamp, stamp, stamp),
                    )
                    db.execute(
                        """INSERT INTO offer_sources(offer_id,source_url,quote,fingerprint,created_at)
                           VALUES((SELECT id FROM offers WHERE fingerprint=?),?,?,?,?)""",
                        (f"fp{i}", f"https://example.test/offer{i}", "q", f"src-fp{i}", stamp),
                    )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                actors = appmod.list_actors()

            actor = next(a for a in actors if a["name"] == "New Laser Co")
            self.assertEqual("Prestataire USP.", actor["strategic_summary"])
            self.assertEqual("partial", actor["coverage_level"])  # 2 distinct sources -> partial
            self.assertEqual([{"actor_id": actor_id, "dimension": "certification", "value": "ISO 9001",
                                "source_url": "https://newlaser.example/iso"}], actor["facts"])
            self.assertEqual(1, len(actor["events"]))
            self.assertEqual("acquisition", actor["events"][0]["event_type"])


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
