"""Tests pour le score de complétude par acteur (chantier 6, app._completeness) : "nb de
dimensions renseignées / nb de dimensions attendues, pondéré par la fraîcheur" -- l'audit
citait Pulsar Photonics (acteur priority, 164 URLs découvertes) comme n'ayant AUCUN fait
marché sans que rien ne le signale. _freshness_factor/_completeness sont testées comme des
fonctions pures ; un test d'intégration confirme le câblage réel dans /api/actors.
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

import app as appmod
import db as dbmod
from app import COMPLETENESS_DIMENSIONS, _completeness, _freshness_factor


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class FreshnessFactorTests(unittest.TestCase):
    def test_never_scraped(self):
        self.assertEqual(0.3, _freshness_factor(None))

    def test_malformed_timestamp(self):
        self.assertEqual(0.3, _freshness_factor("not-a-date"))

    def test_recent_is_full_confidence(self):
        self.assertEqual(1.0, _freshness_factor(_iso(5)))

    def test_boundary_buckets(self):
        self.assertEqual(0.85, _freshness_factor(_iso(60)))
        self.assertEqual(0.6, _freshness_factor(_iso(150)))
        self.assertEqual(0.35, _freshness_factor(_iso(400)))


class CompletenessTests(unittest.TestCase):
    def test_all_dimensions_present_and_fresh_scores_one(self):
        present = {key: True for key, _label in COMPLETENESS_DIMENSIONS}
        result = _completeness(present, _iso(1))
        self.assertEqual(1.0, result["completeness_score"])
        self.assertEqual(len(COMPLETENESS_DIMENSIONS), result["completeness_present"])
        self.assertEqual([], result["completeness_missing"])

    def test_nothing_present_scores_zero_and_lists_every_label(self):
        present = {key: False for key, _label in COMPLETENESS_DIMENSIONS}
        result = _completeness(present, _iso(1))
        self.assertEqual(0.0, result["completeness_score"])
        self.assertEqual(0, result["completeness_present"])
        self.assertEqual([label for _key, label in COMPLETENESS_DIMENSIONS], result["completeness_missing"])

    def test_ratio_is_weighted_by_freshness(self):
        # 7 dimensions total (COMPLETENESS_DIMENSIONS), all present, but stale (150 days -> 0.6).
        present = {key: True for key, _label in COMPLETENESS_DIMENSIONS}
        result = _completeness(present, _iso(150))
        self.assertEqual(0.6, result["completeness_score"])

    def test_missing_dimension_is_actionable_by_label_not_key(self):
        present = {key: True for key, _label in COMPLETENESS_DIMENSIONS}
        present["market_facts"] = False
        result = _completeness(present, _iso(1))
        self.assertEqual(["Faits marché confirmés"], result["completeness_missing"])


class ListActorsCompletenessIntegrationTests(unittest.TestCase):
    def test_pulsar_like_actor_with_zero_market_facts_is_flagged(self):
        """Mirrors the audit's own example: a priority actor, freshly (re)crawled, with
        no evidence/offers/profile/spec/facts/documents at all -- exactly zero dimensions."""
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
                    db.execute("DELETE FROM actors")
                actor_id = dbmod.create_actor("Zero Facts Actor", "France", "Test", "https://zero.example/", priority=True)
                with dbmod.connect(actors_db) as db:
                    db.execute("UPDATE actors SET last_scraped_at=? WHERE id=?", (dbmod.utc_now(), actor_id))

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                actors = appmod.list_actors()

            actor = next(a for a in actors if a["name"] == "Zero Facts Actor")
            self.assertEqual(0, actor["completeness_present"])
            self.assertEqual(len(COMPLETENESS_DIMENSIONS), len(actor["completeness_missing"]))
            self.assertEqual(0.0, actor["completeness_score"])

    def test_actor_with_validated_evidence_gets_one_dimension_credited(self):
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
                    db.execute("DELETE FROM actors")
                dbmod.create_actor("Has Evidence Actor", "France", "Test", "https://evidence.example/", priority=False)
                stamp = dbmod.utc_now()
                with dbmod.connect(market_db) as db:
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,operation,industrial_stage,source_url,source_title,source_date,
                               quote,source_group,fingerprint,fact_key,evidence_kind,language,fact_status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'market_application',?,'validated',?,?)""",
                        (
                            "Has Evidence Actor", "existing", "Médical", "Implants", "Ablation", "Production",
                            "https://evidence.example/page", "Page", "2026-01-01", "quote text",
                            "grp", "fp-1", "key-1", "en", stamp, stamp,
                        ),
                    )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                actors = appmod.list_actors()

            actor = next(a for a in actors if a["name"] == "Has Evidence Actor")
            self.assertNotIn("Faits marché confirmés", actor["completeness_missing"])
            self.assertGreaterEqual(actor["completeness_present"], 1)


if __name__ == "__main__":
    unittest.main()
