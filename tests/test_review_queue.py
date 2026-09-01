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
import scrapers


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


_OFFER_CANDIDATE = {
    "actor": "Example", "offer_type": "service", "capability": "soudage laser", "operation": "assemblage",
    "process": "fiber", "material": None, "performance": None, "stage": "production", "page_type": "service",
    "url": "https://example.test/services/soudage", "title": "Soudage laser", "source_date": "2026-01-01",
    "quote": "Nous réalisons le soudage laser de composants pour l'assemblage de batteries.",
    "is_verbatim": True, "evidence_type": "claim", "date_confidence": "published",
    "fact_key": "example|service|soudage laser|assemblage|fiber", "fingerprint": "off-fp1",
    "source_fingerprint": "off-sfp1", "confidence": 0.83, "block_heading": "Soudage",
    "block_path": "/main/section[1]", "mode": "block-rules",
}

_EVIDENCE_CANDIDATE = {
    "actor": "Example", "bucket": "existing", "market": "Médical", "component": "Stents",
    "operation": "Découpe", "stage": "production", "url": "https://example.test/marches/medical",
    "title": "Médical", "source_date": "2026-01-01",
    "quote": "Nous découpons des stents pour l'industrie médicale.", "is_verbatim": True,
    "evidence_type": "claim", "date_confidence": "published",
    "fact_key": "example|existing|medical|stents|decoupe",
    "application_key": "example|medical|stents|decoupe", "fingerprint": "ev-fp1",
    "source_fingerprint": "ev-sfp1", "block_heading": "Marchés", "block_path": "/main",
    "mode": "block-rules", "confidence": 0.60,
}


class HumanDecisionSurvivesRecrawlTests(unittest.TestCase):
    """Audit du 01/09/2026, reproduit avant correctif : le crawler recalculait review_status (et
    pour l'evidence fact_status) à CHAQUE réobservation, sans regarder si un humain avait déjà
    tranché. Un fait rejeté redevenait donc 'accepted' à la collecte suivante, son reject_reason
    toujours collé dessus -- et repassait dans la matrice marché et le scoring.

    La garde teste `reviewed_at IS NULL`. Elle couvre les DEUX colonnes de statut : la matrice,
    le scoring et les séries temporelles filtrent sur fact_status='validated' sans jamais
    regarder review_status (app.py:375-377, timeseries.py:121), donc ne protéger que
    review_status laissait quand même ressortir un fait rejeté.
    """

    def test_offer_rejected_through_the_unified_queue_stays_rejected_after_a_recrawl(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patch_dbs(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_offer_candidate(db, _OFFER_CANDIDATE)
                    # Deuxième source distincte : l'offre franchit le seuil qui la ferait
                    # basculer en 'accepted' toute seule -- c'est précisément ce qui écrasait
                    # le rejet.
                    scrapers._upsert_offer_candidate(db, dict(
                        _OFFER_CANDIDATE, url="https://example.test/actu/soudage", source_fingerprint="off-sfp2",
                    ))
                rq.decide_review_item("offers", 1, "reject", reviewed_by="lucas", reject_reason="wrong_actor")
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_offer_candidate(db, _OFFER_CANDIDATE)
                    row = db.execute("SELECT review_status,reject_reason,last_seen_at FROM offers WHERE id=1").fetchone()
        self.assertEqual("rejected", row["review_status"])
        self.assertEqual("wrong_actor", row["reject_reason"])
        # last_seen_at continue d'être rafraîchi : savoir qu'une offre rejetée est toujours en
        # ligne reste une information, ce n'est pas une remise en cause du rejet.
        self.assertIsNotNone(row["last_seen_at"])

    def test_evidence_rejected_through_the_legacy_endpoint_survives_being_re_seen_as_validated(self):
        # Chemin HÉRITÉ (/api/market/review/{id}/reject -> db.reject_evidence_review), celui de
        # l'écran marché. Il ne posait pas reviewed_at avant ce correctif, donc la garde
        # n'aurait pas reconnu ses décisions : c'était le trou principal.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patch_dbs(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_market_candidate(db, dict(_EVIDENCE_CANDIDATE, fact_status="review"))
                dbmod.reject_evidence_review(1)
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_market_candidate(db, dict(_EVIDENCE_CANDIDATE, fact_status="validated"))
                    row = db.execute("SELECT review_status,fact_status,reviewed_at FROM evidence WHERE id=1").fetchone()
        self.assertEqual("rejected", row["review_status"])
        # Le point décisif : sans la garde sur fact_status, le fait rejeté ressortait dans la
        # matrice marché, qui ne filtre que sur cette colonne.
        self.assertNotEqual("validated", row["fact_status"])
        self.assertIsNotNone(row["reviewed_at"])

    def test_evidence_rejected_stays_rejected_when_re_seen_with_higher_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patch_dbs(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_market_candidate(db, dict(_EVIDENCE_CANDIDATE, fact_status="review"))
                dbmod.reject_evidence_review(1)
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_market_candidate(db, dict(_EVIDENCE_CANDIDATE, fact_status="review", confidence=0.95))
                    row = db.execute("SELECT review_status,field_confidence FROM evidence WHERE id=1").fetchone()
        # Avant le correctif il repassait en 'review' et revenait dans la file, à re-rejeter
        # indéfiniment.
        self.assertEqual("rejected", row["review_status"])
        # Les données, elles, restent rafraîchies : rejeter fige la DÉCISION, pas la preuve.
        self.assertAlmostEqual(0.95, row["field_confidence"])

    def test_a_row_no_human_ever_reviewed_is_still_promoted_normally(self):
        # Non-régression : la garde ne doit rien figer d'autre que les décisions humaines.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patch_dbs(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    scrapers._upsert_market_candidate(db, dict(_EVIDENCE_CANDIDATE, fact_status="review"))
                    scrapers._upsert_market_candidate(db, dict(_EVIDENCE_CANDIDATE, fact_status="validated"))
                    row = db.execute("SELECT review_status,fact_status,reviewed_at FROM evidence WHERE id=1").fetchone()
        self.assertEqual("accepted", row["review_status"])
        self.assertEqual("validated", row["fact_status"])
        self.assertIsNone(row["reviewed_at"])


if __name__ == "__main__":
    unittest.main()
