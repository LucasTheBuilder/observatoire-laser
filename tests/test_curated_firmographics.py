"""Tests de curated_firmographics : années d'origine déclarées, jamais prioritaires sur un registre."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import curated_firmographics
import db as dbmod
from curated_firmographics import CURATED_FOUNDING_YEARS, DECLARED_LABEL, apply_curated_founding_years

# Sources dont l'année est portée par l'URL de période ou la mise en page, pas par la phrase citée.
YEAR_CARRIED_BY_LAYOUT = {"Fraunhofer ILT", "Workshop of Photonics"}


class CuratedEntriesTests(unittest.TestCase):
    def test_every_entry_is_sourced_and_its_quote_is_verbatim_evidence_of_the_year(self):
        for name, entry in CURATED_FOUNDING_YEARS.items():
            with self.subTest(actor=name):
                self.assertTrue(entry["source_url"].startswith("https://"))
                self.assertGreater(len(entry["quote"]), 20)
                # L'année n'est écrite que si la citation (ou la rubrique d'archive citée) l'établit :
                # la chronique Fraunhofer porte l'année dans son URL de période, pas dans la phrase.
                year = str(entry["founded_year"])
                self.assertTrue(year in entry["quote"] or name in YEAR_CARRIED_BY_LAYOUT, f"{name}: année absente de la citation")
                self.assertTrue(1900 <= entry["founded_year"] <= 2026)


class ApplyCuratedTests(unittest.TestCase):
    def _run(self, fn):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(curated_firmographics, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                fn(actors_db)

    def test_writes_year_with_declared_label_and_provenance(self):
        def body(actors_db):
            dbmod.create_actor("Femtika", "Lituanie", "Test", "https://femtika.example/", priority=False)
            report = apply_curated_founding_years({"Femtika": CURATED_FOUNDING_YEARS["Femtika"]})
            self.assertEqual(["Femtika"], report["written"])
            with dbmod.connect(actors_db) as db:
                row = db.execute("SELECT founded_year,registry_name,source_url,registry_id FROM actor_profile").fetchone()
            self.assertEqual(2013, row["founded_year"])
            self.assertEqual(DECLARED_LABEL, row["registry_name"])
            self.assertEqual("https://femtika.com/about-us/", row["source_url"])
            self.assertIsNone(row["registry_id"])

        self._run(body)

    def test_never_overwrites_a_year_that_is_already_present(self):
        def body(actors_db):
            dbmod.create_actor("Oxford Lasers", "Royaume-Uni", "Test", "https://oxford.example/", priority=False)
            apply_curated_founding_years({"Oxford Lasers": CURATED_FOUNDING_YEARS["Oxford Lasers"]})
            with dbmod.connect(actors_db) as db:
                db.execute("UPDATE actor_profile SET founded_year=1999, registry_name='Registre test'")
            report = apply_curated_founding_years({"Oxford Lasers": CURATED_FOUNDING_YEARS["Oxford Lasers"]})
            self.assertEqual({"Oxford Lasers": "année déjà renseignée"}, report["skipped"])
            with dbmod.connect(actors_db) as db:
                row = db.execute("SELECT founded_year,registry_name FROM actor_profile").fetchone()
            self.assertEqual((1999, "Registre test"), (row["founded_year"], row["registry_name"]))

        self._run(body)

    def test_unknown_actor_is_skipped_not_created(self):
        def body(actors_db):
            report = apply_curated_founding_years({"Inconnu": {"founded_year": 2000, "source_url": "https://x.example/", "quote": "x"}})
            self.assertEqual({"Inconnu": "acteur absent"}, report["skipped"])

        self._run(body)


if __name__ == "__main__":
    unittest.main()
