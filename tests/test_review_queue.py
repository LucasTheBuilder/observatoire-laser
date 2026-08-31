"""Tests pour la file de revue unifiée (Lot 1 §1.1, audit veille §5.G, 30/08/2026) : un seul
contrat pour les 7 files (evidence/offers/tech_signals/events/facts/actors/vocabulary),
priorisation, et traçabilité de la décision (reviewed_by/reviewed_at/reject_reason typé).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as appmod
import db as dbmod
import review_queue as rq


def _setup(tmp: str) -> tuple[Path, Path, Path]:
    actors_db = Path(tmp) / "actors.db"
    market_db = Path(tmp) / "market.db"
    tech_db = Path(tmp) / "technology.db"
    with (
        patch.object(dbmod, "ACTORS_DB", actors_db),
        patch.object(dbmod, "MARKET_DB", market_db),
        patch.object(dbmod, "TECH_DB", tech_db),
    ):
        dbmod.init_databases()
    return actors_db, market_db, tech_db


def _patch_dbs(actors_db: Path, market_db: Path, tech_db: Path) -> ExitStack:
    """Single context manager entering every DB-path patch review_queue.decide_review_item
    needs -- its own rq.ACTORS_DB/MARKET_DB/TECH_DB, but ALSO db.MARKET_DB: for the evidence
    and vocabulary queues it delegates to db.accept_evidence_review/reject_evidence_review/
    accept_vocabulary_candidate/reject_vocabulary_candidate, which read db.py's own
    module-level MARKET_DB, not review_queue's copy of the name."""
    stack = ExitStack()
    stack.enter_context(patch.object(rq, "ACTORS_DB", actors_db))
    stack.enter_context(patch.object(rq, "MARKET_DB", market_db))
    stack.enter_context(patch.object(rq, "TECH_DB", tech_db))
    stack.enter_context(patch.object(dbmod, "ACTORS_DB", actors_db))
    stack.enter_context(patch.object(dbmod, "MARKET_DB", market_db))
    stack.enter_context(patch.object(dbmod, "TECH_DB", tech_db))
    return stack


def _insert_evidence(db, *, fingerprint: str, actor_name: str = "Example", bucket: str = "existing", field_confidence: float | None = 0.6, review_status: str = "review", fact_status: str = "review") -> int:
    return db.execute(
        """INSERT INTO evidence(
               actor_name,bucket,market,component,operation,source_url,quote,source_group,fingerprint,
               review_status,fact_status,field_confidence,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, bucket, "Médical", "Stents", "Découpe", "https://example.test/a",
            "a verbatim excerpt", "grp", fingerprint, review_status, fact_status, field_confidence,
            "2026-08-20", "2026-08-20",
        ),
    ).lastrowid


class EvidenceQueueTests(unittest.TestCase):
    def test_pending_lists_review_facts_not_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", review_status="review", fact_status="review")
                _insert_evidence(db, fingerprint="fp2", review_status="accepted", fact_status="validated")
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("evidence", "pending")
            self.assertEqual(1, len(items))
            self.assertEqual("evidence", items[0]["queue"])

    def test_high_value_actor_outranks_low_value_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                c1_id = dbmod.create_actor("C1 Actor", "France", "Test", "https://c1.test/")
                t1_id = dbmod.create_actor("T1 Actor", "France", "Test", "https://t1.test/")
                dbmod.update_actor_classification(c1_id, competitive_class="C1")
                dbmod.update_actor_classification(t1_id, competitive_class="T1")
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp-t1", actor_name="T1 Actor", field_confidence=0.6)
                _insert_evidence(db, fingerprint="fp-c1", actor_name="C1 Actor", field_confidence=0.6)
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("evidence", "pending")
            self.assertEqual("C1 Actor", items[0]["actor_name"])

    def test_lower_confidence_outranks_higher_confidence_at_equal_actor_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                high_conf_id = _insert_evidence(db, fingerprint="fp-high-conf", field_confidence=0.9)
                low_conf_id = _insert_evidence(db, fingerprint="fp-low-conf", field_confidence=0.3)
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("evidence", "pending")
            self.assertEqual(low_conf_id, items[0]["id"])
            self.assertEqual(high_conf_id, items[1]["id"])


class DecideEvidenceTests(unittest.TestCase):
    def test_accept_validates_and_records_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
            with _patch_dbs(actors_db, market_db, tech_db):
                rq.decide_review_item("evidence", evidence_id, "accept", reviewed_by="lucas")
            row = dbmod.rows(market_db, "SELECT fact_status,reviewed_by,reviewed_at FROM evidence WHERE id=?", (evidence_id,))[0]
            self.assertEqual("validated", row["fact_status"])
            self.assertEqual("lucas", row["reviewed_by"])
            self.assertIsNotNone(row["reviewed_at"])

    def test_reject_requires_a_typed_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
            with _patch_dbs(actors_db, market_db, tech_db):
                with self.assertRaises(ValueError):
                    rq.decide_review_item("evidence", evidence_id, "reject", reviewed_by="lucas", reject_reason="not_a_real_reason")

    def test_reject_with_valid_reason_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
            with _patch_dbs(actors_db, market_db, tech_db):
                rq.decide_review_item("evidence", evidence_id, "reject", reviewed_by="lucas", reject_reason="duplicate")
            row = dbmod.rows(market_db, "SELECT review_status,reject_reason,reviewed_by FROM evidence WHERE id=?", (evidence_id,))[0]
            self.assertEqual("rejected", row["review_status"])
            self.assertEqual("duplicate", row["reject_reason"])
            self.assertEqual("lucas", row["reviewed_by"])


class OffersQueueTests(unittest.TestCase):
    def _insert_offer(self, db, *, fingerprint: str, actor_name: str = "Example", operation: str | None = "Découpe", review_status: str = "review", field_confidence: float = 0.6) -> int:
        return db.execute(
            """INSERT INTO offers(
                   actor_name,offer_type,capability,operation,source_url,source_title,quote,fact_key,fingerprint,
                   review_status,field_confidence,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                actor_name, "service", "Découpe laser", operation, "https://example.test/services", "Services",
                "we drill precisely", f"fact-{fingerprint}", fingerprint, review_status, field_confidence,
                "2026-08-20", "2026-08-20",
            ),
        ).lastrowid

    def test_pending_offers_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                self._insert_offer(db, fingerprint="fp1")
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("offers", "pending")
            self.assertEqual(1, len(items))
            self.assertEqual("offers", items[0]["queue"])

    def test_accept_moves_offer_to_accepted_with_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                offer_id = self._insert_offer(db, fingerprint="fp1")
            with _patch_dbs(actors_db, market_db, tech_db):
                rq.decide_review_item("offers", offer_id, "accept", reviewed_by="lucas")
            row = dbmod.rows(market_db, "SELECT review_status,reviewed_by FROM offers WHERE id=?", (offer_id,))[0]
            self.assertEqual("accepted", row["review_status"])
            self.assertEqual("lucas", row["reviewed_by"])


class TechSignalsQueueTests(unittest.TestCase):
    def _insert_signal(self, db, *, fingerprint: str, actor_names: list[str], bucket: str = "existing", review_status: str = "review") -> int:
        import json
        return db.execute(
            """INSERT INTO technology_signals(
                   axis,maturity_stage,bucket,actor_names,source_url,quote,fact_key,fingerprint,
                   review_status,field_confidence,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SLE", "Industrialisation", bucket, json.dumps(actor_names), "https://example.test/tech",
                "quote", f"fact-{fingerprint}", fingerprint, review_status, 0.5, "2026-08-20", "2026-08-20",
            ),
        ).lastrowid

    def test_signal_priority_uses_the_highest_value_co_signing_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                c1_id = dbmod.create_actor("C1 Actor", "France", "Test", "https://c1.test/")
                dbmod.update_actor_classification(c1_id, competitive_class="C1")
                dbmod.create_actor("T1 Actor", "France", "Test", "https://t1.test/")
            with dbmod.connect(tech_db) as db:
                self._insert_signal(db, fingerprint="fp-solo-t1", actor_names=["T1 Actor"])
                self._insert_signal(db, fingerprint="fp-consortium", actor_names=["T1 Actor", "C1 Actor"])
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("tech_signals", "pending")
            self.assertIn("C1 Actor", items[0]["actor_name"])


class EventsAndFactsQueueTests(unittest.TestCase):
    def test_pending_event_is_listed_and_accept_promotes_to_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                event_id = db.execute(
                    "INSERT INTO actor_events(actor_id,event_type,description,review_status,created_at) VALUES(?,?,?,?,?)",
                    (actor_id, "investment", "raised funding", "pending", "2026-08-20"),
                ).lastrowid
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("events", "pending")
                self.assertEqual(1, len(items))
                rq.decide_review_item("events", event_id, "accept", reviewed_by="lucas")
            row = dbmod.rows(actors_db, "SELECT review_status,reviewed_by FROM actor_events WHERE id=?", (event_id,))[0]
            self.assertEqual("verified", row["review_status"])
            self.assertEqual("lucas", row["reviewed_by"])

    def test_pending_fact_is_listed_and_reject_records_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                fact_id = db.execute(
                    "INSERT INTO actor_facts(actor_id,dimension,value,review_status,created_at) VALUES(?,?,?,?,?)",
                    (actor_id, "certification", "ISO 9001", "pending", "2026-08-20"),
                ).lastrowid
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("facts", "pending")
                self.assertEqual(1, len(items))
                rq.decide_review_item("facts", fact_id, "reject", reviewed_by="lucas", reject_reason="unconvincing_citation")
            row = dbmod.rows(actors_db, "SELECT review_status,reject_reason FROM actor_facts WHERE id=?", (fact_id,))[0]
            self.assertEqual("rejected", row["review_status"])
            self.assertEqual("unconvincing_citation", row["reject_reason"])


class ActorsCandidateQueueTests(unittest.TestCase):
    def test_candidate_actor_is_listed_and_accept_verifies_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("New Candidate", "France", "Test", "https://newcandidate.test/")
                dbmod.update_actor_classification(actor_id, review_status="candidate")
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("actors", "pending")
                self.assertEqual(1, len(items))
                rq.decide_review_item("actors", actor_id, "accept", reviewed_by="lucas")
            row = dbmod.rows(actors_db, "SELECT review_status,reviewed_by FROM actors WHERE id=?", (actor_id,))[0]
            self.assertEqual("verified", row["review_status"])
            self.assertEqual("lucas", row["reviewed_by"])

    def test_no_candidates_gives_an_empty_list_not_fabricated_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("actors", "pending")
            self.assertEqual([], items)


class VocabularyQueueTests(unittest.TestCase):
    def test_pending_candidate_is_listed_and_accept_requires_a_dimension(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                candidate_id = db.execute(
                    """INSERT INTO vocabulary_candidates(
                           actor_name,source_url,quote,proposed_labels,fingerprint,created_at,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    ("Example", "https://example.test/a", "quote", '{"component": "New Widget"}', "vocab-fp-1", "2026-08-20", "2026-08-20"),
                ).lastrowid
            with _patch_dbs(actors_db, market_db, tech_db):
                items = rq.list_review_queue("vocabulary", "pending")
                self.assertEqual(1, len(items))
                with self.assertRaises(ValueError):
                    rq.decide_review_item("vocabulary", candidate_id, "accept", reviewed_by="lucas")
                rq.decide_review_item("vocabulary", candidate_id, "accept", reviewed_by="lucas", dimension="component")
            row = dbmod.rows(market_db, "SELECT review_status,reviewed_by FROM vocabulary_candidates WHERE id=?", (candidate_id,))[0]
            self.assertEqual("accepted", row["review_status"])
            self.assertEqual("lucas", row["reviewed_by"])
            lexicon = dbmod.rows(market_db, "SELECT label FROM custom_lexicon_entries WHERE dimension='component'")
            self.assertEqual(["New Widget"], [r["label"] for r in lexicon])


class UnknownQueueTests(unittest.TestCase):
    def test_unknown_queue_name_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patch_dbs(actors_db, market_db, tech_db):
                with self.assertRaises(ValueError):
                    rq.list_review_queue("not_a_real_queue", "pending")
                with self.assertRaises(ValueError):
                    rq.decide_review_item("not_a_real_queue", 1, "accept")


class ReviewEndpointTests(unittest.TestCase):
    def test_get_review_returns_items_for_a_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1")
            with (
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(rq, "ACTORS_DB", actors_db),
                patch.object(rq, "MARKET_DB", market_db),
            ):
                result = appmod.review_queue_list(queue="evidence", status="pending")
            self.assertEqual(1, len(result["items"]))

    def test_decide_endpoint_rejects_invalid_reject_reason_as_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
            with (
                patch.object(rq, "ACTORS_DB", actors_db),
                patch.object(rq, "MARKET_DB", market_db),
            ):
                payload = appmod.ReviewDecisionRequest(decision="reject", reviewed_by="lucas", reject_reason="nonsense")
                with self.assertRaises(appmod.HTTPException) as ctx:
                    appmod.review_queue_decide("evidence", evidence_id, payload)
            self.assertEqual(400, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
