"""Tests pour timeseries.py (audit Horizon 2 #12 : "Construire séries temporelles par
acteur, marché, technologie, maturité et signal").

Vérifie : (1) une capture calcule bien les 5 dimensions à partir de données réelles insérées
dans les 3 bases, (2) une capture répétée dans le même mois RAFFINE (upsert) sans dupliquer,
(3) read_timeseries/list_timeseries_keys renvoient un historique trié, décompressé et vide
(jamais une erreur) pour une clé inconnue, (4) la maturité canonicalise un industrial_stage
composite ou non reconnu plutôt que de créer une catégorie fantôme.
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
import timeseries as ts


class TimeseriesTestCase(unittest.TestCase):
    """Base commune : bases fraîches avec le schéma initialisé, patchées à la fois dans db et
    dans timeseries (celui-ci importe ACTORS_DB/MARKET_DB/TECH_DB par valeur au chargement du
    module, donc un patch de dbmod seul ne suffit pas -- même piège que pour les autres
    modules qui importent ces constantes, voir scrapers.py)."""

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
            patch.object(ts, "ACTORS_DB", self.actors_db),
            patch.object(ts, "MARKET_DB", self.market_db),
            patch.object(ts, "TECH_DB", self.tech_db),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)
        dbmod.init_databases()

    def _insert_evidence(self, actor_name, bucket, market, industrial_stage, fact_key):
        stamp = dbmod.utc_now()
        with dbmod.connect(self.market_db) as db:
            db.execute(
                """INSERT INTO evidence(actor_name,bucket,market,component,operation,industrial_stage,
                   source_url,source_title,source_date,quote,source_group,fingerprint,review_status,
                   fact_status,evidence_kind,fact_key,created_at,updated_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    actor_name, bucket, market, "Composant", "Perçage", industrial_stage,
                    f"https://example.test/{fact_key}", "titre", "2026-08-01", "citation exacte de test",
                    "test", fact_key, "accepted", "validated", "market_application", fact_key, stamp, stamp, stamp,
                ),
            )

    def _insert_offer(self, actor_name, fact_key):
        stamp = dbmod.utc_now()
        with dbmod.connect(self.market_db) as db:
            db.execute(
                """INSERT INTO offers(actor_name,offer_type,capability,industrial_stage,source_url,quote,
                   fact_key,fingerprint,review_status,created_at,updated_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (actor_name, "service", "micro-usinage", "Prototype", f"https://example.test/{fact_key}",
                 "citation", fact_key, fact_key, "accepted", stamp, stamp, stamp),
            )


class CaptureTests(TimeseriesTestCase):
    def test_capture_computes_all_five_dimensions_from_real_rows(self):
        self._insert_evidence("ACunity", "existing", "Medtech", "Production", "fk1")
        self._insert_offer("ACunity", "fko1")

        summary = ts.capture_metric_snapshot()
        self.assertIn("actor", summary)
        self.assertIn("market", summary)
        self.assertIn("technology", summary)
        self.assertIn("maturity", summary)
        self.assertIn("signal", summary)
        # 'technology' always has at least the '__global__' documents key even with 0 documents.
        self.assertGreaterEqual(summary["technology"], 1)

        actor_points = ts.read_timeseries("actor", "ACunity")
        self.assertEqual(1, len(actor_points))
        self.assertEqual(1, actor_points[0]["evidence_existing"])
        self.assertEqual(1, actor_points[0]["offers_count"])

        market_points = ts.read_timeseries("market", "Medtech")
        self.assertEqual(1, market_points[0]["existing"])
        self.assertEqual(1, market_points[0]["actors_count"])

        signal_points = ts.read_timeseries("signal", "__global__")
        self.assertEqual(1, signal_points[0]["new_evidence"])
        self.assertEqual(1, signal_points[0]["new_offers"])

    def test_rejected_and_pending_evidence_is_excluded(self):
        # fact_status stays at its default ('review') here -- never promoted to 'validated' --
        # so this row must not inflate any counter, exactly like /api/market's own filter.
        stamp = dbmod.utc_now()
        with dbmod.connect(self.market_db) as db:
            db.execute(
                """INSERT INTO evidence(actor_name,bucket,market,source_url,quote,source_group,fingerprint,
                   review_status,evidence_kind,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("ACunity", "pending", "Medtech", "https://example.test/unreviewed", "citation",
                 "test", "fk-unreviewed", "accepted", "market_application", stamp, stamp),
            )
        ts.capture_metric_snapshot()
        actor_points = ts.read_timeseries("actor", "ACunity")
        self.assertEqual(0, actor_points[0]["evidence_existing"])
        self.assertEqual(0, actor_points[0]["evidence_pending"])  # not 'validated' -> excluded entirely

    def test_repeated_capture_in_same_month_upserts_instead_of_duplicating(self):
        self._insert_evidence("ACunity", "existing", "Medtech", "Production", "fk1")
        ts.capture_metric_snapshot()
        ts.capture_metric_snapshot()
        ts.capture_metric_snapshot()
        with dbmod.connect(self.market_db) as db:
            n = db.execute(
                "SELECT COUNT(*) FROM metric_snapshots WHERE dimension='actor' AND dimension_key='ACunity'"
            ).fetchone()[0]
        self.assertEqual(1, n)

    def test_capture_for_a_different_period_adds_a_second_point_not_a_replacement(self):
        self._insert_evidence("ACunity", "existing", "Medtech", "Production", "fk1")
        ts.capture_metric_snapshot(period="2026-06")
        ts.capture_metric_snapshot(period="2026-07")
        points = ts.read_timeseries("signal", "__global__")
        self.assertEqual(["2026-06", "2026-07"], [p["period"] for p in points])

    def test_maturity_canonicalizes_composite_and_unknown_stage_labels(self):
        self._insert_evidence("ACunity", "existing", "Medtech", "Prototype | Matériau: Verre", "fk1")
        self._insert_evidence("HAILTEC", "radar", "Medtech", "un-libelle-invente", "fk2")
        ts.capture_metric_snapshot()
        points = ts.read_timeseries("maturity", "__global__")
        stages = points[0]["stage_distribution"]
        self.assertEqual(1, stages.get("Prototype"))
        self.assertEqual(1, stages.get("Maturité industrielle non déterminée"))
        self.assertNotIn("un-libelle-invente", stages)

    def test_technology_axis_and_global_documents_are_tracked_separately(self):
        stamp = dbmod.utc_now()
        with dbmod.connect(self.tech_db) as db:
            db.execute(
                """INSERT INTO technology_signals(axis,maturity_stage,bucket,actor_names,source_url,
                   source_title,quote,fact_key,fingerprint,review_status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("SLE", "industrialisation", "existing", '["ACunity"]', "https://example.test/sig",
                 "titre", "citation", "fk-sig", "fp-sig", "accepted", stamp, stamp),
            )
            db.execute(
                """INSERT INTO documents(actor_name,document_type,title,source_url,fingerprint,created_at)
                   VALUES(?,?,?,?,?,?)""",
                ("ACunity", "patent", "titre brevet", "https://example.test/doc", "fp-doc", stamp),
            )
        ts.capture_metric_snapshot()
        axis_points = ts.read_timeseries("technology", "SLE")
        self.assertEqual(1, axis_points[0]["signals_existing"])
        global_points = ts.read_timeseries("technology", "__global__")
        self.assertEqual(1, global_points[0]["documents_total"])
        self.assertEqual({"patent": 1}, global_points[0]["documents_by_type"])


class ReadTests(TimeseriesTestCase):
    def test_read_timeseries_for_unknown_key_returns_empty_list_not_an_error(self):
        self.assertEqual([], ts.read_timeseries("actor", "Acteur Inexistant"))

    def test_list_timeseries_keys_reflects_captured_dimension_keys(self):
        self._insert_evidence("ACunity", "existing", "Medtech", "Production", "fk1")
        ts.capture_metric_snapshot()
        self.assertIn("Medtech", ts.list_timeseries_keys("market"))
        self.assertIn("__global__", ts.list_timeseries_keys("technology"))


if __name__ == "__main__":
    unittest.main()
