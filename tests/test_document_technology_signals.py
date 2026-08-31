"""Tests pour la classification axe + maturité des documents Crossref/OpenAlex (Lot 3 §3.5,
audit veille §4.C.2, 30/08/2026) : "branchement de lexiques existants" -- un document déjà
on-topic (filtré en amont par scrape_technology/openalex.py) n'avait jusqu'ici aucun axe
technologique. Réutilise exactement PROCESS_TECHNOLOGIES/MATURITY_RULES, comme cordis.py pour
les projets.
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
from scrapers import upsert_document_technology_signal


def _setup(tmp: str) -> Path:
    tech_db = Path(tmp) / "technology.db"
    with (
        patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", tech_db),
    ):
        dbmod.init_databases()
    return tech_db


class UpsertDocumentTechnologySignalTests(unittest.TestCase):
    def test_known_process_axis_is_detected_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) of fused silica microchannels", "",
                    "https://doi.org/10.1/example", "ALPHANOV",
                )
            self.assertEqual(1, added)
            row = dbmod.rows(tech_db, "SELECT axis,bucket,actor_names FROM technology_signals")[0]
            self.assertEqual("SLE", row["axis"])
            self.assertIn("ALPHANOV", row["actor_names"])

    def test_no_matching_axis_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Femtosecond laser micromachining of glass for medical devices", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(0, added)
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(0, count)

    def test_generic_axis_without_laser_context_is_excluded(self):
        # Same false-positive guard as is_on_topic(): "digital twin" alone (Monitoring IA
        # procédé's match term) must not create a signal without a real laser term present --
        # this is the exact false positive the audit found in production (a Tekniker safety
        # document with zero laser mentions).
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "AI-Enriched Safety Criteria Catalogue and Digital Twin Framework", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(0, added)

    def test_generic_axis_with_laser_context_is_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Femtosecond laser process monitoring via digital twin models", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(1, added)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("Monitoring IA procédé", row["axis"])

    def test_maturity_stage_is_detected_from_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) for mass production of glass wafers", "",
                    "https://doi.org/10.1/example",
                )
            row = dbmod.rows(tech_db, "SELECT maturity_stage,bucket FROM technology_signals")[0]
            self.assertEqual("Production", row["maturity_stage"])
            self.assertEqual("existing", row["bucket"])

    def test_unknown_maturity_defaults_to_radar(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) of photonic glass components", "",
                    "https://doi.org/10.1/example",
                )
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals")[0]
            self.assertEqual("radar", row["bucket"])

    def test_repeated_call_on_same_document_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                first = upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")
                second = upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")
            self.assertEqual(1, first)
            self.assertEqual(0, second)
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(1, count)

    def test_second_actor_on_same_document_merges_into_actor_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example", "ALPHANOV")
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example", "Fraunhofer ILT")
            row = dbmod.rows(tech_db, "SELECT actor_names FROM technology_signals")[0]
            import json
            self.assertEqual(["ALPHANOV", "Fraunhofer ILT"], sorted(json.loads(row["actor_names"])))

    def test_different_documents_with_the_same_axis_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/a")
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/b")
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(2, count)

    def test_multiple_axes_on_one_document_each_create_a_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) combined with LIPSS structuring of glass", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(2, added)
            axes = {row["axis"] for row in dbmod.rows(tech_db, "SELECT axis FROM technology_signals")}
            self.assertEqual({"SLE", "LIPSS"}, axes)


if __name__ == "__main__":
    unittest.main()
