"""Tests pour db.exclude_document() / document_exclusions.

Le manque s'est vu le 10/09/2026 : onze publications retirées à la main le matin, et la
collecte de l'après-midi en a ramené une. Le cas en question — « Selective laser-induced
etching enabled study on [...] liquid ammonia flash-boiling » — ne peut être tranché par aucune
règle lexicale : son titre nomme bel et bien un procédé ultra-rapide, c'est le SUJET de
l'article qui est ailleurs. Sans trace de la décision, chaque collecte défait l'audit précédent.
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
from db import connect, exclude_document, upsert_document, upsert_technology_signal

DOI = "10.1016/j.example.2026.1234"
TITRE = "Selective laser-induced etching enabled study on liquid ammonia flash-boiling"


class DocumentExclusionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patchers = [
            patch.object(dbmod, "ACTORS_DB", root / "actors.db"),
            patch.object(dbmod, "MARKET_DB", root / "market.db"),
            patch.object(dbmod, "TECH_DB", root / "technology.db"),
        ]
        for patcher in self._patchers:
            patcher.start()
        dbmod.init_databases()
        self.tech_db = dbmod.TECH_DB

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._tmp.cleanup()

    def _insert(self, db):
        return upsert_document(
            db, document_type="publication", title=TITRE,
            source_url=f"https://doi.org/{DOI}", fingerprint_source=DOI, doi=DOI,
        )

    def _signal(self, db, project_name=None):
        return upsert_technology_signal(
            db, fact_key=f"SLE|{project_name or DOI}", axis="SLE",
            maturity_stage="Maturité industrielle non déterminée", bucket="radar",
            actor_names=[], source_url=f"https://doi.org/{DOI}", quote="q",
            field_confidence=1.0, project_name=project_name,
        )

    def _exclude(self, db):
        return exclude_document(
            db, fingerprint_source=DOI, title=TITRE,
            reason="sujet = mécanique des fluides", excluded_by="audit du 10/09/2026",
        )

    def test_excluding_removes_the_document(self):
        with connect(self.tech_db) as db:
            self.assertEqual((1, 0), self._insert(db))
            self.assertEqual(1, self._exclude(db))
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def test_a_collector_cannot_bring_it_back(self):
        """Le test qui compte : c'est exactement ce qui s'est produit le 10/09/2026."""
        with connect(self.tech_db) as db:
            self._insert(db)
            self._exclude(db)
            self.assertEqual((0, 0), self._insert(db))
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def test_the_url_form_of_the_fingerprint_is_also_blocked(self):
        """Un même document peut être resoumis sans DOI, sourcé sur son URL : l'empreinte se
        dérive de la chaîne passée, donc l'exclusion doit porter sur celle réellement utilisée
        à l'insertion. Ici on exclut sur l'URL, et c'est l'URL qui doit rester bloquée."""
        url = "https://example.org/actes/2026/flash-boiling"
        with connect(self.tech_db) as db:
            upsert_document(db, document_type="publication", title=TITRE, source_url=url,
                            fingerprint_source=url)
            exclude_document(db, fingerprint_source=url, title=TITRE, reason="hors sujet",
                             excluded_by="test")
            upsert_document(db, document_type="publication", title=TITRE, source_url=url,
                            fingerprint_source=url)
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def test_excluding_takes_the_technology_signals_with_it(self):
        with connect(self.tech_db) as db:
            self._insert(db)
            self._signal(db)
            self._exclude(db)
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM technology_signals").fetchone()[0])

    def test_a_project_signal_on_the_same_url_is_left_alone(self):
        """Une publication et un projet européen peuvent partager une URL ; seuls les signaux
        documentaires (project_name NULL) appartiennent au document qu'on écarte."""
        with connect(self.tech_db) as db:
            self._insert(db)
            self._signal(db, project_name="UNPROJET")
            self._exclude(db)
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM technology_signals").fetchone()[0])

    def test_is_idempotent_and_keeps_the_latest_reason(self):
        with connect(self.tech_db) as db:
            self._insert(db)
            self.assertEqual(1, self._exclude(db))
            # Deuxième passage : plus rien à retirer, mais l'exclusion tient toujours.
            self.assertEqual(0, self._exclude(db))
            exclude_document(db, fingerprint_source=DOI, title=TITRE,
                             reason="motif révisé", excluded_by="relecture")
            row = db.execute("SELECT reason,excluded_by FROM document_exclusions").fetchone()
            self.assertEqual("motif révisé", row["reason"])
            self.assertEqual("relecture", row["excluded_by"])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM document_exclusions").fetchone()[0])

    def test_excluding_something_never_collected_still_arms_the_guard(self):
        """On doit pouvoir écarter d'avance, sans que le document soit déjà en base."""
        with connect(self.tech_db) as db:
            self.assertEqual(0, self._exclude(db))
            self.assertEqual((0, 0), self._insert(db))


if __name__ == "__main__":
    unittest.main()
