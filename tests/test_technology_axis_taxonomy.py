"""Tests pour la taxonomie des axes technologiques (Lot 2 §2.7, audit veille §10.10,
30/08/2026) : "deux libellés d'axe coexistent pour le même concept [...] à 500 lignes, tout
comptage par axe sera faux et personne ne s'en apercevra" -- et "deux signaux portent
maturity_stage='Industrialisation' ET bucket='radar', deux champs qui se contredisent". Vérifié
contre les données réelles : cette paire N'EST PAS incohérente une fois TECHNOLOGY_STAGE_TO_
BUCKET appliqué (seul 'Production' vaut 'existing') -- le garde-fou corrige les vraies
incohérences sans jamais toucher une paire déjà correcte.
"""

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
from scrapers import PROCESS_TECHNOLOGIES, _match_all_labels, is_on_topic


def _setup(tmp: str) -> Path:
    tech_db = Path(tmp) / "technology.db"
    with (
        patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", tech_db),
    ):
        dbmod.init_databases()
    return tech_db


def _insert_signal(db, *, axis: str, project_name: str, actor_names: list[str], maturity_stage: str = "Prototype", bucket: str = "radar") -> int:
    fact_key = dbmod.technology_signal_key(axis, project_name)
    return db.execute(
        """INSERT INTO technology_signals(
               axis,maturity_stage,bucket,project_name,actor_names,source_url,quote,fact_key,fingerprint,
               review_status,field_confidence,created_at,updated_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            axis, maturity_stage, bucket, project_name, json.dumps(actor_names), "https://example.test/project",
            "quote", fact_key, f"fp-{fact_key}", "accepted", 0.9, dbmod.utc_now(), dbmod.utc_now(), dbmod.utc_now(),
        ),
    ).lastrowid


class NormalizeTechnologyAxesTests(unittest.TestCase):
    def test_legacy_digital_twin_label_is_renamed_to_canonical_axis(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Monitoring + IA / digital twin", project_name="ProjectX", actor_names=["Actor A"])
                dbmod._normalize_technology_axes(db)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("Monitoring IA procédé", row["axis"])

    def test_legacy_beam_shaping_label_is_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Beam shaping / surfaces 3D", project_name="ProjectY", actor_names=["Actor B"])
                dbmod._normalize_technology_axes(db)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("Beam shaping", row["axis"])

    def test_merges_into_existing_canonical_row_instead_of_creating_a_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Monitoring IA procédé", project_name="ProjectX", actor_names=["Actor A"])
                _insert_signal(db, axis="Monitoring + IA / digital twin", project_name="ProjectX", actor_names=["Actor B"])
                dbmod._normalize_technology_axes(db)
            rows = dbmod.rows(tech_db, "SELECT axis,actor_names FROM technology_signals")
            self.assertEqual(1, len(rows))
            self.assertEqual("Monitoring IA procédé", rows[0]["axis"])
            self.assertEqual(["Actor A", "Actor B"], sorted(json.loads(rows[0]["actor_names"])))

    def test_is_idempotent_on_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Monitoring + IA / digital twin", project_name="ProjectX", actor_names=["Actor A"])
                dbmod._normalize_technology_axes(db)
                dbmod._normalize_technology_axes(db)
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(1, count)

    def test_axis_not_in_the_alias_map_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="SLE", project_name="ProjectZ", actor_names=["Actor C"])
                dbmod._normalize_technology_axes(db)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("SLE", row["axis"])


class ReconcileTechnologySignalMaturityTests(unittest.TestCase):
    def test_production_stage_with_wrong_bucket_is_corrected_to_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                signal_id = _insert_signal(db, axis="SLE", project_name="P1", actor_names=["A"], maturity_stage="Production", bucket="radar")
                dbmod._reconcile_technology_signal_maturity(db)
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals WHERE id=?", (signal_id,))[0]
            self.assertEqual("existing", row["bucket"])

    def test_industrialisation_stage_with_radar_bucket_is_already_correct_and_untouched(self):
        # §10.10's own cited example -- confirmed NOT a real contradiction: only 'Production'
        # maps to 'existing' in TECHNOLOGY_STAGE_TO_BUCKET, so this pairing is expected.
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                signal_id = _insert_signal(db, axis="SLE", project_name="P2", actor_names=["A"], maturity_stage="Industrialisation", bucket="radar")
                dbmod._reconcile_technology_signal_maturity(db)
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals WHERE id=?", (signal_id,))[0]
            self.assertEqual("radar", row["bucket"])

    def test_unrecognized_stage_defaults_to_radar_never_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                signal_id = _insert_signal(db, axis="SLE", project_name="P3", actor_names=["A"], maturity_stage="Something Unrecognized", bucket="existing")
                dbmod._reconcile_technology_signal_maturity(db)
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals WHERE id=?", (signal_id,))[0]
            self.assertEqual("radar", row["bucket"])

    def test_all_five_canonical_stages_map_as_expected(self):
        expected = {
            "Production": "existing",
            "Industrialisation": "radar",
            "Pré-industrialisation": "radar",
            "Prototype": "radar",
            "R&D": "radar",
        }
        self.assertEqual(expected, {k: v for k, v in dbmod.TECHNOLOGY_STAGE_TO_BUCKET.items() if k in expected})


class NewProcessTechnologyAxesTests(unittest.TestCase):
    def test_three_new_axes_are_registered(self):
        for axis in ("Haute puissance / hauts taux", "Multi-beam / parallélisation", "Fabrication roll-to-roll (batteries)"):
            self.assertIn(axis, PROCESS_TECHNOLOGIES)

    def test_roll_to_roll_alone_is_not_on_topic_without_laser_context(self):
        # §10.10: these new axes carry generic manufacturing terms (roll-to-roll, parallel
        # processing) that must not, by themselves, make unrelated content look laser-relevant.
        text = "This system supports roll-to-roll battery electrode manufacturing."
        labels = {label for label, _ in _match_all_labels(text, PROCESS_TECHNOLOGIES)}
        self.assertIn("Fabrication roll-to-roll (batteries)", labels)
        self.assertFalse(is_on_topic(text))

    def test_roll_to_roll_with_laser_context_is_on_topic(self):
        text = "Femtosecond laser structuring enables roll-to-roll battery electrode manufacturing."
        self.assertTrue(is_on_topic(text))


if __name__ == "__main__":
    unittest.main()
