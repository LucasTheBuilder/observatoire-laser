"""Tests pour le classement d'un document sur les SIX vocabulaires (09/09/2026).

Le chemin documentaire ne lisait que PROCESS_TECHNOLOGIES : 61 des 92 documents ressortaient
sans aucune famille, dont 41 que les cinq autres vocabulaires fermés classaient déjà -- ils ne
servaient qu'à l'extraction de faits marché.

L'invariant à protéger est la DISJONCTION des libellés : un même libellé présent dans deux
vocabulaires s'afficherait deux fois sur la page et fausserait tout comptage par famille. C'est
ce qui a fait fusionner "Fonctionnalisation de surface" dans OPERATIONS plutôt que la laisser
aussi dans PROCESS_TECHNOLOGIES.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from lexicon import DOCUMENT_LEXICONS
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


class LexiconDisjunctionTests(unittest.TestCase):
    def test_the_six_vocabularies_share_no_label(self):
        owners = defaultdict(list)
        for dimension, lexicon in DOCUMENT_LEXICONS.items():
            for label in lexicon:
                owners[label].append(dimension)
        collisions = {label: dims for label, dims in owners.items() if len(dims) > 1}
        self.assertEqual({}, collisions, "un libellé partagé casserait le comptage par famille")

    def test_every_dimension_has_a_distinct_name(self):
        self.assertEqual(len(DOCUMENT_LEXICONS), len(set(DOCUMENT_LEXICONS)))


class DocumentFamilyClassificationTests(unittest.TestCase):
    def _families(self, tech_db: Path) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for row in dbmod.rows(tech_db, "SELECT axis,dimension FROM technology_signals"):
            out[row["dimension"]].add(row["axis"])
        return dict(out)

    def test_one_document_can_span_several_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Femtosecond laser ablation of fused silica in GHz burst regime", "",
                    "https://doi.org/10.1/multi",
                )
            families = self._families(tech_db)
            self.assertIn("Ablation", families.get("operation", set()))
            self.assertIn("Verre", families.get("material", set()))
            self.assertIn("Burst GHz/MHz", families.get("process_technology", set()))

    def test_dimension_is_written_on_every_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Femtosecond laser ablation of fused silica", "", "https://doi.org/10.1/a")
            missing = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals WHERE dimension IS NULL OR dimension=''")
            self.assertEqual(0, missing)

    def test_every_signal_still_carries_its_citation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Femtosecond laser ablation of fused silica in GHz burst regime", "",
                    "https://doi.org/10.1/multi")
            orphans = dbmod.scalar(tech_db, """SELECT COUNT(*) FROM technology_signals t
                LEFT JOIN technology_signal_sources s ON s.signal_id=t.id WHERE s.id IS NULL""")
            self.assertEqual(0, orphans)

    def test_replaying_the_same_document_creates_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                first = upsert_document_technology_signal(
                    db, "Femtosecond laser ablation of fused silica", "", "https://doi.org/10.1/a")
                second = upsert_document_technology_signal(
                    db, "Femtosecond laser ablation of fused silica", "", "https://doi.org/10.1/a")
            self.assertGreater(first, 0)
            self.assertEqual(0, second)

    def test_generic_axis_guard_still_applies_to_process_technologies_only(self):
        # "surface texturing" est un axe générique : sans terme ultra-rapide il ne doit pas
        # créer de signal de procédé. Le matériau, lui, n'a jamais été un critère de
        # pertinence -- il classe un document déjà admis, il ne le fait pas entrer.
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Chemical digital twin monitoring of glass panels", "", "https://doi.org/10.1/b")
            families = self._families(tech_db)
            self.assertNotIn("Monitoring IA procédé", families.get("process_technology", set()))
            self.assertIn("Verre", families.get("material", set()))


class DimensionReconciliationTests(unittest.TestCase):
    def test_dimension_is_derived_from_the_owning_vocabulary(self):
        """La dimension est déduite du lexique propriétaire, pas figée à l'écriture : c'est ce
        qui rattrape les lignes antérieures à la colonne ET celles dont le libellé a changé de
        vocabulaire, sans migration ponctuelle."""
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                # Écrit avec la mauvaise dimension, comme une ligne d'avant la colonne.
                dbmod.upsert_technology_signal(
                    db, fact_key=dbmod.technology_signal_key("Ablation", "https://doi.org/10.1/c"),
                    axis="Ablation", maturity_stage="Prototype", bucket="radar", actor_names=[],
                    source_url="https://doi.org/10.1/c", quote="q", field_confidence=0.7,
                    dimension="process_technology",
                )
                dbmod._reconcile_technology_signal_dimensions(db)
            row = dbmod.rows(tech_db, "SELECT axis,dimension FROM technology_signals")[0]
            self.assertEqual("operation", row["dimension"])

    def test_a_renamed_axis_gets_the_dimension_of_its_new_label(self):
        """init_databases doit normaliser les libellés AVANT de déduire les dimensions : une
        ligne renommée par un alias garderait sinon la dimension de son ancien libellé.
        Constaté en production sur "Soudage / assemblage de transparents" -> "Soudage", qui
        restait en `process_technology` alors que "Soudage" appartient à OPERATIONS."""
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            url = "https://doi.org/10.1/renamed"
            with dbmod.connect(tech_db) as db:
                dbmod.upsert_technology_signal(
                    db, fact_key=dbmod.technology_signal_key("Soudage / assemblage de transparents", url),
                    axis="Soudage / assemblage de transparents", maturity_stage="Prototype",
                    bucket="radar", actor_names=[], source_url=url, quote="q",
                    field_confidence=0.7, dimension="process_technology",
                )
            with (
                patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
            row = dbmod.rows(tech_db, "SELECT axis,dimension FROM technology_signals")[0]
            self.assertEqual("Soudage", row["axis"])
            self.assertEqual("operation", row["dimension"])

    def test_an_unknown_label_falls_back_to_process_technology(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                dbmod.upsert_technology_signal(
                    db, fact_key=dbmod.technology_signal_key("Axe hérité inconnu", "https://x.test/1"),
                    axis="Axe hérité inconnu", maturity_stage="Prototype", bucket="radar",
                    actor_names=[], source_url="https://x.test/1", quote="q", field_confidence=0.7,
                    dimension="material",
                )
                dbmod._reconcile_technology_signal_dimensions(db)
            row = dbmod.rows(tech_db, "SELECT dimension FROM technology_signals")[0]
            self.assertEqual("process_technology", row["dimension"])


if __name__ == "__main__":
    unittest.main()
