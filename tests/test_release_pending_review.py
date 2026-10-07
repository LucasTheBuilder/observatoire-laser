"""Tests pour l'audit des files de validation (release_pending_review.py, 07/10/2026)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import actor_discovery
import db as dbmod
import release_pending_review as release
import review_queue
from scrapers import _vocabulary_candidate, is_known_label_list, proposal_parts


class KnownLabelListTests(unittest.TestCase):
    def test_a_python_list_of_known_labels_is_not_a_vocabulary_gap(self):
        self.assertEqual(["Gravure", "Microdécoupe"], proposal_parts("['Gravure', 'Microdécoupe']"))
        self.assertTrue(is_known_label_list("operation", "['Gravure', 'Microdécoupe']"))
        self.assertTrue(is_known_label_list("market", "Médical, Optique"))

    def test_one_unknown_label_keeps_the_proposal(self):
        self.assertFalse(is_known_label_list("market", "Médical, Microélectronique"))
        self.assertFalse(is_known_label_list("operation", "Scellage"))

    def test_vocabulary_candidate_ignores_known_label_lists(self):
        class Block:
            heading = None
        proposed = {"market": "Médical, Optique", "component": "", "operation": "Scellage"}
        candidate = _vocabulary_candidate("ACME", "https://a.test", "t", "q", Block(), proposed, {"market": None, "component": None, "operation": None})
        self.assertEqual({"operation": "Scellage"}, candidate["proposed_labels"])
        only_known = {"market": "Médical, Optique", "component": "", "operation": ""}
        self.assertIsNone(_vocabulary_candidate("ACME", "https://a.test", "t", "q", Block(), only_known, {"market": None, "component": None, "operation": None}))


class ReleasePendingReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.paths = {"ACTORS_DB": tmp / "actors.db", "MARKET_DB": tmp / "market.db", "TECH_DB": tmp / "technology.db"}
        with patch.multiple(dbmod, **self.paths):
            dbmod.init_databases()
        # review_journal lit db.MARKET_DB au moment de l'appel : c'est le module db lui-même
        # qu'il faut rediriger, en plus des copies importées par les autres modules.
        for module in (dbmod, release, review_queue, actor_discovery):
            for name, path in self.paths.items():
                if hasattr(module, name):
                    patcher = patch.object(module, name, path)
                    patcher.start()
                    self.addCleanup(patcher.stop)
        patcher = patch.object(release, "backup_all_databases", lambda: [])
        patcher.start()
        self.addCleanup(patcher.stop)
        self._seed()

    def _seed(self):
        stamp = dbmod.utc_now()
        with dbmod.connect(self.paths["MARKET_DB"]) as db:
            for table in ("vocabulary_candidates",):
                db.execute(f"DELETE FROM {table}")
            for vid, labels in ((1, {"operation": "['Gravure', 'Microdécoupe']"}), (2, {"operation": "Scellage"})):
                db.execute(
                    """INSERT INTO vocabulary_candidates(id,actor_name,source_url,quote,proposed_labels,resolved_labels,
                                                         review_status,fingerprint,created_at,last_seen_at)
                       VALUES(?,?,?,?,?,'{}','pending',?,?,?)""",
                    (vid, "ACME", "https://a.test", "q", json.dumps(labels, ensure_ascii=False), f"v{vid}", stamp, stamp),
                )
        with dbmod.connect(self.paths["ACTORS_DB"]) as db:
            known = db.execute("SELECT name FROM actors LIMIT 1").fetchone()["name"]
            db.execute("DELETE FROM actor_candidates")
            for cid, name in ((1, known.upper()), (2, "Totally New Laser GmbH")):
                db.execute(
                    """INSERT INTO actor_candidates(id,name,normalized_name,score,review_status,first_seen_at,last_seen_at)
                       VALUES(?,?,?,1,'pending',?,?)""",
                    (cid, name, name.casefold(), stamp, stamp),
                )
        self.known_actor = known

    def test_dry_run_plans_without_writing(self):
        plan = release.release_pending_review(dry_run=True)
        self.assertEqual([1], [item["id"] for item in plan["vocabulary"]])
        self.assertEqual([(1, self.known_actor)], [(item["id"], item["actor"]) for item in plan["candidates"]])
        pending = dbmod.rows(self.paths["ACTORS_DB"], "SELECT id FROM actor_candidates WHERE review_status='pending'")
        self.assertEqual(2, len(pending))

    def test_apply_rejects_duplicates_and_known_vocabulary_only(self):
        release.release_pending_review()
        candidates = {r["id"]: (r["review_status"], r["reject_reason"]) for r in dbmod.rows(self.paths["ACTORS_DB"], "SELECT * FROM actor_candidates")}
        self.assertEqual({1: ("rejected", "duplicate"), 2: ("pending", None)}, candidates)
        vocabulary = {r["id"]: r["review_status"] for r in dbmod.rows(self.paths["MARKET_DB"], "SELECT * FROM vocabulary_candidates")}
        self.assertEqual({1: "rejected", 2: "pending"}, vocabulary)

    def test_confirming_sentence_requires_the_same_triple_and_a_strict_market(self):
        good = "We perform femtosecond laser cutting of stents for medical devices."
        self.assertEqual(good, release._confirming_sentence(good, ("Médical", "Stents", "Microdécoupe")))
        self.assertIsNone(release._confirming_sentence(good, ("Médical", "Cathéters", "Microdécoupe")))


if __name__ == "__main__":
    unittest.main()
