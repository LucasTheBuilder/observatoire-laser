"""Tests pour le CRUD de taille de marché (market_sizing.py, Lot 4 §14)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import market_sizing
from market_sizing import add_market_sizing, delete_market_sizing, list_market_sizing


class MarketSizingTests(unittest.TestCase):
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
        self.market_patch = patch.object(market_sizing, "MARKET_DB", self.market_db)
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_add_requires_source_url(self):
        with self.assertRaises(ValueError):
            add_market_sizing(market="Médical", scope="Machines uniquement", value=100.0, currency="EUR", year=2025, source_url="not-a-url")

    def test_add_rejects_blank_scope(self):
        with self.assertRaises(ValueError):
            add_market_sizing(market="Médical", scope="  ", value=100.0, currency="EUR", year=2025, source_url="https://example.com/report")

    def test_add_and_list_roundtrip(self):
        entry_id = add_market_sizing(
            market="Médical", scope="Machines uniquement, hors services", value=250.0, currency="EUR",
            year=2025, cagr=0.08, method="Extrapolation ventes constructeurs", source_url="https://example.com/report",
            added_by="analyst",
        )
        entries = list_market_sizing()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], entry_id)
        self.assertEqual(entries[0]["market"], "Médical")
        self.assertEqual(entries[0]["value"], 250.0)
        self.assertEqual(entries[0]["cagr"], 0.08)

    def test_list_filters_by_market(self):
        add_market_sizing(market="Médical", scope="A", value=1.0, currency="EUR", year=2024, source_url="https://example.com/1")
        add_market_sizing(market="Optique", scope="B", value=2.0, currency="EUR", year=2024, source_url="https://example.com/2")
        self.assertEqual(len(list_market_sizing(market="Médical")), 1)
        self.assertEqual(len(list_market_sizing()), 2)

    def test_delete_removes_entry(self):
        entry_id = add_market_sizing(market="Médical", scope="A", value=1.0, currency="EUR", year=2024, source_url="https://example.com/1")
        delete_market_sizing(entry_id)
        self.assertEqual(list_market_sizing(), [])

    def test_delete_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            delete_market_sizing(999)


if __name__ == "__main__":
    unittest.main()
