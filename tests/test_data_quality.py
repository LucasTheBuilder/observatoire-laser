"""Tests pour les 4 indicateurs de qualité de la veille (data_quality.py, Lot 4 §17)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import data_quality
import db as dbmod
from data_quality import (
    add_golden_fact,
    compute_coverage_health,
    compute_detection_latency,
    compute_precision,
    compute_recall,
    data_quality_report,
    delete_golden_fact,
    list_golden_facts,
)


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.actors_db = tmp_path / "actors.db"
        self.market_db = tmp_path / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", self.actors_db),
            patch.object(dbmod, "MARKET_DB", self.market_db),
            patch.object(dbmod, "TECH_DB", tmp_path / "technology.db"),
        ):
            dbmod.init_databases()
        self.actors_patch = patch.object(dbmod, "ACTORS_DB", self.actors_db)
        self.market_patch = patch.object(dbmod, "MARKET_DB", self.market_db)
        self.actors_patch.start()
        self.market_patch.start()
        self.addCleanup(self.actors_patch.stop)
        self.addCleanup(self.market_patch.stop)
        self.dq_actors_patch = patch.object(data_quality, "ACTORS_DB", self.actors_db)
        self.dq_market_patch = patch.object(data_quality, "MARKET_DB", self.market_db)
        self.dq_actors_patch.start()
        self.dq_market_patch.start()
        self.addCleanup(self.dq_actors_patch.stop)
        self.addCleanup(self.dq_market_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    _evidence_counter = 0

    def _seed_evidence(self, *, actor_name="Test Actor", market="Médical", component="Stents",
                        operation="Microperçage", fact_status="validated", review_status="accepted",
                        reviewed_at=None, extraction_mode=None, source_date=None, created_at=None):
        stamp = created_at or dbmod.utc_now()
        DataQualityTests._evidence_counter += 1
        with dbmod.connect(self.market_db) as db:
            evidence_id = db.execute(
                """INSERT INTO evidence(actor_name,bucket,market,component,operation,source_url,quote,
                       source_group,fingerprint,fact_status,review_status,reviewed_at,source_date,created_at,updated_at)
                   VALUES(?,'existing',?,?,?,'https://example.com','quote','grp',?,?,?,?,?,?,?)""",
                (actor_name, market, component, operation,
                 f"fp-{DataQualityTests._evidence_counter}", fact_status, review_status,
                 reviewed_at, source_date, stamp, stamp),
            ).lastrowid
            if extraction_mode is not None:
                db.execute(
                    """INSERT INTO evidence_sources(evidence_id,source_url,quote,extraction_mode,fingerprint,created_at)
                       VALUES(?,'https://example.com','quote',?,?,?)""",
                    (evidence_id, extraction_mode, f"fp-src-{evidence_id}", stamp),
                )
        return evidence_id

    # --- recall -----------------------------------------------------------------------------

    def test_recall_is_none_when_no_golden_facts_exist(self):
        self.assertEqual(compute_recall(), {"recall": None, "golden_facts": 0, "matched": 0})

    def test_recall_matches_a_golden_fact_against_validated_evidence(self):
        add_golden_fact(actor_name="Test Actor", market="Médical", component="Stents", operation="Microperçage",
                         source_url="https://example.com/verified", expected_quote="verified by a human")
        self._seed_evidence()
        result = compute_recall()
        self.assertEqual(result, {"recall": 1.0, "golden_facts": 1, "matched": 1})

    def test_recall_is_zero_when_the_pipeline_never_found_the_golden_fact(self):
        add_golden_fact(actor_name="Test Actor", market="Médical", component="Stents", operation="Microperçage",
                         source_url="https://example.com/verified", expected_quote="verified by a human")
        result = compute_recall()
        self.assertEqual(result, {"recall": 0.0, "golden_facts": 1, "matched": 0})

    # --- precision ---------------------------------------------------------------------------

    def test_precision_ignores_facts_never_actually_reviewed(self):
        self._seed_evidence(reviewed_at=None)  # never went through the review queue
        result = compute_precision()
        self.assertEqual(result["reviewed_total"], 0)
        self.assertEqual(result["by_extraction_mode"], [])

    def test_precision_computes_rejection_rate_by_extraction_mode(self):
        now = dbmod.utc_now()
        self._seed_evidence(review_status="accepted", reviewed_at=now, extraction_mode="block-rules")
        self._seed_evidence(review_status="rejected", reviewed_at=now, extraction_mode="block-rules")
        self._seed_evidence(review_status="accepted", reviewed_at=now, extraction_mode="anthropic:claude-haiku-4-5")
        result = compute_precision()
        self.assertEqual(result["reviewed_total"], 3)
        by_mode = {row["extraction_mode"]: row for row in result["by_extraction_mode"]}
        self.assertEqual(by_mode["block-rules"]["total"], 2)
        self.assertEqual(by_mode["block-rules"]["rejected"], 1)
        self.assertEqual(by_mode["block-rules"]["rejection_rate"], 0.5)
        self.assertEqual(by_mode["anthropic:claude-haiku-4-5"]["rejection_rate"], 0.0)

    # --- detection latency ---------------------------------------------------------------------

    def test_latency_is_none_without_any_published_date(self):
        self._seed_evidence(source_date=None)
        self.assertEqual(compute_detection_latency(), {"median_days": None, "sample_size": 0})

    def test_latency_computes_median_days_between_publication_and_detection(self):
        self._seed_evidence(source_date="2026-01-01", created_at="2026-01-11T00:00:00+00:00")
        self._seed_evidence(source_date="2026-01-01", created_at="2026-01-21T00:00:00+00:00")
        result = compute_detection_latency()
        self.assertEqual(result["sample_size"], 2)
        self.assertEqual(result["median_days"], 15)

    def test_latency_skips_malformed_dates_without_crashing(self):
        self._seed_evidence(source_date="not-a-date")
        result = compute_detection_latency()
        self.assertEqual(result["sample_size"], 0)

    # --- coverage health ------------------------------------------------------------------------

    def test_coverage_health_flags_stale_actors_and_missing_strategic_pages(self):
        with dbmod.connect(self.actors_db) as db:
            db.execute("DELETE FROM actors")
        actor_id = dbmod.create_actor("Test Actor", "France", "Test", "https://example.com/", priority=False)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        with dbmod.connect(self.actors_db) as db:
            db.execute(
                "INSERT INTO actor_sources(actor_id,url,last_http_status,last_checked_at) VALUES(?,?,200,?)",
                (actor_id, "https://example.com/", old_timestamp),
            )
            db.execute(
                "UPDATE site_profiles SET coverage_json=? WHERE actor_id=?",
                (json.dumps({"service": {"status": "missing"}, "market": {"status": "ready"}}), actor_id),
            )
        result = compute_coverage_health(stale_days=60)
        self.assertEqual(result["active_actors"], 1)
        self.assertEqual(result["stale_actors"], 1)
        self.assertEqual(result["missing_strategic_pages_total"], 1)

    def test_coverage_health_actor_with_recent_crawl_is_not_stale(self):
        with dbmod.connect(self.actors_db) as db:
            db.execute("DELETE FROM actors")
        actor_id = dbmod.create_actor("Test Actor", "France", "Test", "https://example.com/", priority=False)
        recent_timestamp = datetime.now(timezone.utc).isoformat()
        with dbmod.connect(self.actors_db) as db:
            db.execute(
                "INSERT INTO actor_sources(actor_id,url,last_http_status,last_checked_at) VALUES(?,?,200,?)",
                (actor_id, "https://example.com/", recent_timestamp),
            )
        result = compute_coverage_health(stale_days=60)
        self.assertEqual(result["stale_actors"], 0)

    # --- golden facts CRUD ---------------------------------------------------------------------

    def test_add_golden_fact_requires_absolute_url(self):
        with self.assertRaises(ValueError):
            add_golden_fact(actor_name="A", market="M", component="C", operation="O", source_url="bad", expected_quote="q")

    def test_add_list_delete_golden_fact_roundtrip(self):
        fact_id = add_golden_fact(actor_name="A", market="M", component="C", operation="O",
                                   source_url="https://example.com/x", expected_quote="a verified quote")
        self.assertEqual(len(list_golden_facts()), 1)
        delete_golden_fact(fact_id)
        self.assertEqual(list_golden_facts(), [])

    def test_delete_unknown_golden_fact_raises(self):
        with self.assertRaises(ValueError):
            delete_golden_fact(999)

    def test_adding_the_same_golden_fact_twice_is_idempotent(self):
        # Depuis que la file de revue propose « garder comme référence » en un clic, le doublon
        # n'est plus une faute de frappe improbable mais un simple double-clic. Deux lignes
        # identiques sur le quadruplet apparieraient la MÊME preuve dans compute_recall() et
        # gonfleraient son dénominateur : le rappel baisserait sans qu'aucune régression réelle
        # n'ait eu lieu.
        first = add_golden_fact(actor_name="A", market="M", component="C", operation="O",
                                source_url="https://example.com/x", expected_quote="a verified quote")
        second = add_golden_fact(actor_name="A", market="M", component="C", operation="O",
                                 source_url="https://example.com/autre-page", expected_quote="une autre citation")
        self.assertEqual(first, second)
        self.assertEqual(len(list_golden_facts()), 1)
        # La première saisie fait foi : la ré-ajouter ne réécrit ni sa source ni sa citation.
        self.assertEqual("https://example.com/x", list_golden_facts()[0]["source_url"])

    def test_a_different_dimension_still_creates_a_second_golden_fact(self):
        add_golden_fact(actor_name="A", market="M", component="C", operation="O",
                        source_url="https://example.com/x", expected_quote="q")
        add_golden_fact(actor_name="A", market="M", component="C", operation="AUTRE",
                        source_url="https://example.com/x", expected_quote="q")
        self.assertEqual(len(list_golden_facts()), 2)

    # --- combined report -------------------------------------------------------------------------

    def test_data_quality_report_combines_all_four_indicators(self):
        report = data_quality_report()
        self.assertIn("recall", report)
        self.assertIn("precision", report)
        self.assertIn("detection_latency", report)
        self.assertIn("coverage_health", report)


if __name__ == "__main__":
    unittest.main()
