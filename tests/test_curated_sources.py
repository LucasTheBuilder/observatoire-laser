"""Tests pour curated_sources.py : les sources entrées à la main (BILASURF, 09/09/2026).

Le point que ces tests protègent n'est pas l'écriture -- c'est la SURVIE. Ces lignes existent
précisément parce que le filtre automatique les rejette : l'objectif CORDIS de BILASURF
n'emploie aucun terme ultra-rapide, et trois des neuf contributions non plus. Sans les deux
gardes (``documents.curated_by``, ``technology_signals.reviewed_at``), la première passe de
nettoyage venue les effacerait toutes.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import curated_sources
import db as dbmod
import prune_off_topic_sources as prune
from lexicon import is_on_topic


class CuratedSourcesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.tech_db = tmp / "technology.db"
        self._patches = [
            patch.object(dbmod, "ACTORS_DB", tmp / "actors.db"),
            patch.object(dbmod, "MARKET_DB", tmp / "market.db"),
            patch.object(dbmod, "TECH_DB", self.tech_db),
            patch.object(curated_sources, "TECH_DB", self.tech_db),
            patch.object(prune, "TECH_DB", self.tech_db),
        ]
        for item in self._patches:
            item.start()
        dbmod.init_databases()

    def tearDown(self):
        for item in self._patches:
            item.stop()
        self._tmp.cleanup()

    def test_the_project_would_be_rejected_by_the_automatic_filter(self):
        """La justification d'exister de ce module. Si un jour le lexique évolue au point que
        BILASURF passe tout seul, ce test échouera -- et il faudra alors se demander si la
        curation est encore nécessaire, plutôt que la laisser doubler une collecte."""
        objectif = (
            "BILASURF aims at developing and integrating a process for high-rate laser "
            "functionalization of complex 3D surfaces using tailored designed bio inspired riblets "
            "to reduce friction and improve the environmental footprint of industrial parts, "
            "assuring a high throughput with the help of inline monitoring capabilities."
        )
        self.assertFalse(is_on_topic(objectif))

    def test_restore_writes_the_project_and_its_documents(self):
        report = curated_sources.restore_curated_sources()
        self.assertEqual(1, report["projects_added"])
        self.assertEqual(2, report["project_sources_added"])
        self.assertEqual(len(curated_sources.CURATED_DOCUMENTS), report["documents_added"])

        signal = dbmod.rows(self.tech_db, "SELECT project_name,axis,reviewed_by,reviewed_at FROM technology_signals")[0]
        self.assertEqual("BILASURF", signal["project_name"])
        self.assertEqual("Fonctionnalisation de surface", signal["axis"])
        self.assertIsNotNone(signal["reviewed_at"])

    def test_every_curated_document_is_marked(self):
        curated_sources.restore_curated_sources()
        unmarked = dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM documents WHERE curated_by IS NULL")
        self.assertEqual(0, unmarked)

    def test_restore_is_idempotent(self):
        curated_sources.restore_curated_sources()
        second = curated_sources.restore_curated_sources()
        self.assertEqual({"projects_added": 0, "project_sources_added": 0, "documents_added": 0, "axes_added": 0}, second)

    def test_conference_contributions_sharing_a_page_do_not_deduplicate_each_other(self):
        # Trois contributions n'ont pas de DOI et sont sourcées sur la même page : sans
        # fingerprint_source explicite, elles s'écraseraient l'une l'autre.
        curated_sources.restore_curated_sources()
        same_page = dbmod.scalar(
            self.tech_db,
            "SELECT COUNT(*) FROM documents WHERE source_url=?",
            (curated_sources.BILASURF_SITE_URL,),
        )
        self.assertEqual(3, same_page)

    def test_the_ultrashort_proof_is_stored_as_a_second_citation(self):
        # L'objectif CORDIS ne prouve pas le caractère ultra-rapide ; la page du projet si.
        # Les deux sont citées, la seconde est celle qui porte la preuve.
        curated_sources.restore_curated_sources()
        quotes = [row["quote"] for row in dbmod.rows(self.tech_db, "SELECT quote FROM technology_signal_sources")]
        self.assertTrue(any("Ultrashort pulsed laser process development" in quote for quote in quotes))

    def test_pruning_never_removes_curated_rows(self):
        """Le test qui compte : rejouer le nettoyage ne doit rien effacer."""
        curated_sources.restore_curated_sources()
        documents_before = dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM documents")
        signals_before = dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM technology_signals")

        documents_report = prune.prune_documents()
        signals_report = prune.prune_technology_signals(cache_path=Path("/inexistant/horizon.zip"))

        self.assertEqual(0, documents_report["documents_removed"])
        self.assertEqual(0, signals_report["signals_removed"])
        self.assertEqual(documents_before, dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM documents"))
        self.assertEqual(signals_before, dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM technology_signals"))

    def test_an_uncurated_off_topic_document_is_still_removed(self):
        # La garde ne doit pas neutraliser le nettoyage pour tout le monde.
        curated_sources.restore_curated_sources()
        with dbmod.connect(self.tech_db) as db:
            dbmod.upsert_document(
                db, document_type="publication", title="Poultry farming productivity in Galicia",
                source_url="https://example.test/poultry", fingerprint_source="poultry",
            )
        report = prune.prune_documents()
        self.assertEqual(1, report["documents_removed"])

    def test_axes_are_derived_from_titles_not_declared_by_hand(self):
        curated_sources.restore_curated_sources()
        axes = {row["axis"] for row in dbmod.rows(
            self.tech_db, "SELECT axis FROM technology_signals WHERE project_name IS NULL")}
        # "burst mode" dans le titre du premier article, détecté par le lexique seul.
        self.assertIn("Burst GHz/MHz", axes)


if __name__ == "__main__":
    unittest.main()
