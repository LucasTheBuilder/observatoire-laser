"""Tests pour la matrice de référence marché x composant x opération (reference_matrix.py,
Lot 4 §13)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import reference_matrix
from reference_matrix import add_reference_cell, coverage_summary, delete_reference_cell, list_reference_matrix


class ReferenceMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.market_db = tmp_path / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"),
            patch.object(dbmod, "MARKET_DB", self.market_db),
            patch.object(dbmod, "TECH_DB", tmp_path / "technology.db"),
        ):
            dbmod.init_databases()
        self.market_patch = patch.object(reference_matrix, "MARKET_DB", self.market_db)
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def _seed_evidence(self, *, market, component, operation, bucket="existing", fact_status="validated"):
        stamp = dbmod.utc_now()
        with dbmod.connect(self.market_db) as db:
            db.execute(
                """INSERT INTO evidence(actor_name,bucket,market,component,operation,source_url,quote,
                       source_group,fingerprint,fact_status,created_at,updated_at)
                   VALUES('Test Actor',?,?,?,?,'https://example.com','quote','grp',?,?,?,?)""",
                (bucket, market, component, operation, f"fp-{market}-{component}-{operation}", fact_status, stamp, stamp),
            )

    def test_add_rejects_blank_fields(self):
        with self.assertRaises(ValueError):
            add_reference_cell(market="Médical", component="", operation="Microperçage")

    def test_add_rejects_duplicate_cell(self):
        add_reference_cell(market="Médical", component="Stents", operation="Microperçage")
        with self.assertRaises(ValueError):
            add_reference_cell(market="Médical", component="Stents", operation="Microperçage")

    def test_covered_cell_is_flagged_when_a_matching_validated_evidence_exists(self):
        add_reference_cell(market="Médical", component="Stents", operation="Microperçage")
        self._seed_evidence(market="Médical", component="Stents", operation="Microperçage")
        cells = list_reference_matrix()
        self.assertEqual(len(cells), 1)
        self.assertTrue(cells[0]["covered"])
        self.assertEqual(cells[0]["covered_bucket"], "existing")

    def test_white_space_cell_has_no_matching_evidence(self):
        add_reference_cell(market="Médical", component="Stents", operation="Microperçage")
        cells = list_reference_matrix()
        self.assertFalse(cells[0]["covered"])

    def test_a_non_validated_evidence_row_does_not_count_as_coverage(self):
        add_reference_cell(market="Médical", component="Stents", operation="Microperçage")
        self._seed_evidence(market="Médical", component="Stents", operation="Microperçage", fact_status="review")
        cells = list_reference_matrix()
        self.assertFalse(cells[0]["covered"])

    def test_coverage_summary_counts_white_space_correctly(self):
        add_reference_cell(market="Médical", component="Stents", operation="Microperçage")
        add_reference_cell(market="Optique", component="Lentilles", operation="Gravure")
        self._seed_evidence(market="Médical", component="Stents", operation="Microperçage")
        summary = coverage_summary()
        self.assertEqual(summary["total_cells"], 2)
        self.assertEqual(summary["covered_cells"], 1)
        self.assertEqual(summary["white_space_cells"], 1)
        self.assertEqual(summary["coverage_rate"], 0.5)
        self.assertEqual(len(summary["white_space"]), 1)
        self.assertEqual(summary["white_space"][0]["market"], "Optique")

    def test_coverage_summary_with_no_cells_declared(self):
        summary = coverage_summary()
        self.assertEqual(summary["total_cells"], 0)
        self.assertIsNone(summary["coverage_rate"])

    def test_delete_removes_cell(self):
        cell_id = add_reference_cell(market="Médical", component="Stents", operation="Microperçage")
        delete_reference_cell(cell_id)
        self.assertEqual(list_reference_matrix(), [])

    def test_delete_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            delete_reference_cell(999)


if __name__ == "__main__":
    unittest.main()
