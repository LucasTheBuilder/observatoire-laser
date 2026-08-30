"""Tests pour scoring.py (audit Horizon 2 #14 : "Créer scores séparés : confiance, menace,
attractivité marché" ; corrigé par l'audit veille du 30/08/2026, §10.2/§10.3/§10.4).

Vérifie : (1) confidence_score récompense verbatim + date publiée + validation humaine,
EXCLUT les faits is_verbatim=0 du calcul, et distingue "pas de données" (None) de "confiance
faible" (un score bas mais réel) ; (2) threat_score suit la classe concurrentielle, exclut
is_verbatim=0, lit COALESCE(source_date,created_at) pour la vélocité, neutralise le bonus de
vélocité tant qu'une seule période existe dans metric_snapshots, et retombe toujours à 0 pour
un acteur de référence interne ; (3) compute_competitive_intensity_scores (renommé depuis
compute_market_attractiveness_scores) récompense la traction (existing) et la proximité de la
Production, exclut is_verbatim=0, et n'invente pas de marché absent des faits validés.
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
import scoring


class ScoringTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.actors_db = root / "actors.db"
        self.market_db = root / "market.db"
        self.tech_db = root / "technology.db"
        self._patches = [
            patch.object(dbmod, "ACTORS_DB", self.actors_db),
            patch.object(dbmod, "MARKET_DB", self.market_db),
            patch.object(dbmod, "TECH_DB", self.tech_db),
            patch.object(scoring, "ACTORS_DB", self.actors_db),
            patch.object(scoring, "MARKET_DB", self.market_db),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)
        dbmod.init_databases()

    def _set_actor(self, name, *, competitive_class=None, is_reference=0, review_status="verified", active=1):
        with dbmod.connect(self.actors_db) as db:
            existing = db.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone()
            if not existing:
                dbmod.create_actor(name, "France", "Test", f"https://{name.lower()}.example/", priority=False)
            db.execute(
                "UPDATE actors SET competitive_class=?,is_reference=?,review_status=?,active=? WHERE name=?",
                (competitive_class, is_reference, review_status, active, name),
            )

    def _insert_evidence(self, actor_name, *, fact_status="validated", is_verbatim=1, date_confidence="published",
                          created_at=None, source_date=None, market="Medtech", industrial_stage="Production", fact_key=None):
        stamp = created_at or dbmod.utc_now()
        fact_key = fact_key or f"fk-{actor_name}-{stamp}-{is_verbatim}-{date_confidence}"
        with dbmod.connect(self.market_db) as db:
            db.execute(
                """INSERT INTO evidence(actor_name,bucket,market,component,operation,industrial_stage,source_url,
                   source_title,source_date,quote,source_group,fingerprint,review_status,fact_status,evidence_kind,
                   fact_key,date_confidence,is_verbatim,created_at,updated_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (actor_name, "existing", market, "Composant", "Percage", industrial_stage,
                 f"https://example.test/{fact_key}", "titre", source_date, "citation exacte", "test", fact_key,
                 "accepted", fact_status, "market_application", fact_key, date_confidence, is_verbatim, stamp, stamp, stamp),
            )
            row = db.execute("SELECT id FROM evidence WHERE fact_key=?", (fact_key,)).fetchone()
            db.execute(
                """INSERT INTO evidence_sources(evidence_id,source_url,quote,is_verbatim,fingerprint,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (row["id"], f"https://example.test/{fact_key}", "citation exacte", is_verbatim, f"fp-{fact_key}", stamp),
            )

    def _seed_two_collection_windows(self):
        """compute_threat_scores' velocity bonus is neutralized unless metric_snapshots covers
        more than one period -- tests that want to exercise the velocity bonus must call this
        first (see the module docstring and scoring._single_collection_window)."""
        with dbmod.connect(self.market_db) as db:
            db.execute(
                "INSERT INTO metric_snapshots(dimension,dimension_key,period,captured_at,metrics_json) VALUES(?,?,?,?,?)",
                ("actor", "__global__", "2026-07", dbmod.utc_now(), "{}"),
            )
            db.execute(
                "INSERT INTO metric_snapshots(dimension,dimension_key,period,captured_at,metrics_json) VALUES(?,?,?,?,?)",
                ("actor", "__global__", "2026-08", dbmod.utc_now(), "{}"),
            )


class ConfidenceScoreTests(ScoringTestCase):
    def test_actor_with_no_data_has_no_confidence_score(self):
        self._set_actor("Ghost Actor")
        scores = scoring.compute_confidence_scores()
        self.assertNotIn("Ghost Actor", scores)  # never inserted into the dict -- nothing to score

    def test_verbatim_published_verified_actor_scores_higher_than_a_weaker_one(self):
        self._set_actor("Strong Actor", review_status="verified")
        self._insert_evidence("Strong Actor", is_verbatim=1, date_confidence="published")

        self._set_actor("Weak Actor", review_status="candidate")
        self._insert_evidence("Weak Actor", is_verbatim=1, date_confidence="unknown")

        scores = scoring.compute_confidence_scores()
        self.assertGreater(scores["Strong Actor"], scores["Weak Actor"])
        self.assertGreaterEqual(scores["Strong Actor"], 90)

    def test_non_verbatim_only_actor_has_no_confidence_score_at_all(self):
        # Audit veille §10.2 (30/08/2026): 40 of 71 "validated" market facts in production
        # turned out to be hand-entered reading notes (is_verbatim=0, SEED_EVIDENCE -- now an
        # empty list in db.py -- can no longer reproduce them), inflating confidence_score
        # without any flag distinguishing them from a genuinely extracted, verified fact. An
        # actor whose only evidence is non-verbatim must score exactly like an actor with none
        # at all: None, not a low-but-real number.
        self._set_actor("Seed Only Actor", review_status="verified")
        self._insert_evidence("Seed Only Actor", is_verbatim=0, date_confidence="published")
        scores = scoring.compute_confidence_scores()
        self.assertIsNone(scores["Seed Only Actor"])

    def test_mixed_verbatim_and_seed_facts_scores_only_on_the_verbatim_ones(self):
        self._set_actor("Mixed Verbatim Actor")
        self._insert_evidence("Mixed Verbatim Actor", is_verbatim=1, fact_key="fk-real")
        self._insert_evidence("Mixed Verbatim Actor", is_verbatim=0, fact_key="fk-seed")
        scores = scoring.compute_confidence_scores()
        # 1 verbatim-validated out of 2 total rows -> validated_ratio=0.5, same shape as the
        # existing pending-facts test below, not None (there IS a genuine validated fact).
        self.assertIsNotNone(scores["Mixed Verbatim Actor"])
        self.assertLess(scores["Mixed Verbatim Actor"], 90)

    def test_pending_facts_are_excluded_from_the_validated_ratio(self):
        self._set_actor("Mixed Actor")
        self._insert_evidence("Mixed Actor", fact_status="validated", fact_key="fk-validated")
        self._insert_evidence("Mixed Actor", fact_status="review", fact_key="fk-review")
        scores = scoring.compute_confidence_scores()
        # 1 validated out of 2 total rows collected -> validated_ratio=0.5, so the score sits
        # well below the near-100 a fully-validated actor would reach.
        self.assertLess(scores["Mixed Actor"], 90)


class ThreatScoreTests(ScoringTestCase):
    def test_c1_scores_higher_than_c2_which_scores_higher_than_t1(self):
        self._set_actor("Direct Competitor", competitive_class="C1")
        self._set_actor("Partial Competitor", competitive_class="C2")
        self._set_actor("Tech Centre", competitive_class="T1")
        scores = scoring.compute_threat_scores()
        self.assertGreater(scores["Direct Competitor"], scores["Partial Competitor"])
        self.assertGreater(scores["Partial Competitor"], scores["Tech Centre"])

    def test_reference_actor_is_always_zero_regardless_of_class(self):
        self._set_actor("Internal Reference", competitive_class="C1", is_reference=1)
        self._insert_evidence("Internal Reference")
        scores = scoring.compute_threat_scores()
        self.assertEqual(0.0, scores["Internal Reference"])

    def test_recent_activity_raises_the_score_over_stale_activity(self):
        self._seed_two_collection_windows()
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
        self._set_actor("Stale Actor", competitive_class="C1")
        self._insert_evidence("Stale Actor", created_at=old_stamp, fact_key="fk-stale")

        self._set_actor("Active Actor", competitive_class="C1")
        self._insert_evidence("Active Actor", fact_key="fk-active-1")  # created "now" by default
        self._insert_evidence("Active Actor", fact_key="fk-active-2", market="Optique")

        scores = scoring.compute_threat_scores()
        self.assertGreater(scores["Active Actor"], scores["Stale Actor"])

    def test_velocity_bonus_is_neutralized_with_a_single_collection_window(self):
        # Audit veille §10.3.b: the corpus spanned a single 7-day collection window in
        # production, so "recent" could not be distinguished from "first ever crawl" --
        # without _seed_two_collection_windows(), the velocity component must contribute
        # nothing regardless of how recent the facts actually are.
        self._set_actor("Fresh Actor", competitive_class="C1")
        self._insert_evidence("Fresh Actor", fact_key="fk-fresh-1")
        self._insert_evidence("Fresh Actor", fact_key="fk-fresh-2", market="Optique")

        self._set_actor("Bare Actor", competitive_class="C1")

        scores = scoring.compute_threat_scores()
        # Same base (C1) and demonstrated_bonus difference only -- no velocity contribution.
        expected_gap = min(25, 2 * 1.5)  # demonstrated_bonus for 2 facts, capped
        self.assertAlmostEqual(expected_gap, scores["Fresh Actor"] - scores["Bare Actor"], places=1)

    def test_non_verbatim_facts_are_excluded_from_threat_score(self):
        # Audit veille §10.2/§10.3.a: is_verbatim=0 facts (hand-entered, not reproducible by
        # the pipeline) must never count as "demonstrated" for threat scoring either.
        self._set_actor("Seed Only Actor", competitive_class="C1")
        self._insert_evidence("Seed Only Actor", is_verbatim=0, fact_key="fk-seed-1")
        self._insert_evidence("Seed Only Actor", is_verbatim=0, fact_key="fk-seed-2", market="Optique")

        self._set_actor("No Facts Actor", competitive_class="C1")

        scores = scoring.compute_threat_scores()
        self.assertEqual(scores["No Facts Actor"], scores["Seed Only Actor"])

    def test_inactive_actor_scores_lower_than_an_otherwise_identical_active_one(self):
        self._set_actor("Paused Actor", competitive_class="C2", active=0)
        self._set_actor("Live Actor", competitive_class="C2", active=1)
        scores = scoring.compute_threat_scores()
        self.assertLess(scores["Paused Actor"], scores["Live Actor"])


class CompetitiveIntensityTests(ScoringTestCase):
    """Renamed from MarketAttractivenessTests (audit veille §10.4, 30/08/2026) along with the
    function itself: this score measures competitive supply already present, not business
    attractiveness -- see scoring.compute_competitive_intensity_scores' docstring."""

    def test_market_with_more_existing_facts_and_actors_scores_higher(self):
        self._insert_evidence("Actor A", market="Medtech", fact_key="fk-a")
        self._insert_evidence("Actor B", market="Medtech", fact_key="fk-b")
        self._insert_evidence("Actor C", market="Niche", fact_key="fk-c")
        results = {r["market"]: r for r in scoring.compute_competitive_intensity_scores()}
        self.assertGreater(results["Medtech"]["intensity_score"], results["Niche"]["intensity_score"])
        self.assertEqual(2, results["Medtech"]["actors_count"])

    def test_production_stage_share_is_reflected_in_the_score(self):
        self._insert_evidence("Actor A", market="Mature", industrial_stage="Production", fact_key="fk-mature")
        self._insert_evidence("Actor B", market="Early", industrial_stage="R&D", fact_key="fk-early")
        results = {r["market"]: r for r in scoring.compute_competitive_intensity_scores()}
        self.assertEqual(1.0, results["Mature"]["production_share"])
        self.assertEqual(0.0, results["Early"]["production_share"])
        self.assertGreater(results["Mature"]["intensity_score"], results["Early"]["intensity_score"])

    def test_market_with_no_validated_evidence_is_absent_not_fabricated(self):
        self._insert_evidence("Actor A", market="Real", fact_key="fk-real")
        results = {r["market"] for r in scoring.compute_competitive_intensity_scores()}
        self.assertIn("Real", results)
        self.assertNotIn("Fictitious", results)

    def test_non_verbatim_facts_are_excluded(self):
        # Audit veille §10.2/§10.4.3: this score was calculated on a base where 56% of the
        # "Médical" market's facts were hand-entered seed data, closing a circular loop with
        # whatever market intuitions had already shaped what got seeded.
        self._insert_evidence("Actor A", market="Seeded Only", is_verbatim=0, fact_key="fk-seed")
        results = {r["market"] for r in scoring.compute_competitive_intensity_scores()}
        self.assertNotIn("Seeded Only", results)


if __name__ == "__main__":
    unittest.main()
