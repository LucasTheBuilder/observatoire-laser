"""Tests pour le bloc « Sources et bases interrogées » de la page Technologie laser.

Demandé par Lucas le 15/09/2026. Ce que ces tests protègent n'est pas l'affichage mais la
VÉRACITÉ de ce qu'il affiche : une page qui prétend dire d'où vient son corpus doit dire vrai,
sinon elle est pire que rien.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sources import SOURCES, list_sources, technology_sources

ROLES_CONNUS = {"publication scientifique", "projet européen", "projet national", "brevet"}


class TechnologySourcesTests(unittest.TestCase):
    def test_only_sources_that_feed_the_corpus_are_listed(self):
        """GLEIF, les registres d'entreprises et la presse ne touchent jamais technology.db ;
        les faire figurer sur cette page dirait faux."""
        listees = {s["id"] for s in technology_sources()}
        self.assertEqual(
            {
                "crossref", "openalex", "cordis", "hal", "arxiv", "anr", "ukri_gtr",
                "epo_ops", "lens", "google_patents",
            },
            listees,
        )

    def test_every_listed_source_declares_a_known_role(self):
        for source in technology_sources():
            with self.subTest(source=source["id"]):
                self.assertIn(source["role"], ROLES_CONNUS)

    def test_a_listed_source_always_writes_into_the_technology_database(self):
        """Le rôle est déclaré à la main ; cette cohérence-là ne doit pas dépendre d'un
        relecteur attentif."""
        for source in SOURCES:
            if source.technology_role:
                with self.subTest(source=source.id):
                    self.assertIn("technology.db", source.target)

    def test_a_source_without_a_role_never_appears(self):
        sans_role = {s.id for s in SOURCES if not s.technology_role}
        listees = {s["id"] for s in technology_sources()}
        self.assertFalse(sans_role & listees)

    def test_the_key_state_is_recomputed_at_each_call(self):
        """Une clé ajoutée dans .env doit se voir au rechargement suivant, sans redémarrage --
        c'est précisément ce qu'on vient lire quand une source affiche zéro."""
        with patch.dict("os.environ", {"EPO_OPS_KEY": "", "EPO_OPS_SECRET": ""}):
            epo = next(s for s in technology_sources() if s["id"] == "epo_ops")
            self.assertEqual("missing_key", epo["status"])
        with patch.dict("os.environ", {"EPO_OPS_KEY": "k", "EPO_OPS_SECRET": "s"}):
            epo = next(s for s in technology_sources() if s["id"] == "epo_ops")
            self.assertEqual("configured", epo["status"])

    def test_the_short_view_agrees_with_the_full_registry(self):
        """technology_sources() est une vue de SOURCES, pas une seconde liste à maintenir."""
        complet = {s["id"]: s for s in list_sources()}
        for source in technology_sources():
            with self.subTest(source=source["id"]):
                for champ in ("name", "domain", "module", "purpose", "status", "requires_key"):
                    self.assertEqual(complet[source["id"]][champ], source[champ])

    def test_the_anr_entry_points_at_the_source_actually_queried(self):
        """data.enseignementsup-recherche.gouv.fr était une ARCHIVE figée en 2016 qui répondait
        200 sans le dire (voir national_projects.py). L'afficher serait afficher un mensonge."""
        anr = next(s for s in technology_sources() if s["id"] == "anr")
        self.assertIn("data.gouv.fr", anr["domain"])
        self.assertNotIn("enseignementsup", anr["domain"])


if __name__ == "__main__":
    unittest.main()
