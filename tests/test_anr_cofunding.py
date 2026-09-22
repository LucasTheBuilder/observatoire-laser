"""Tests pour la troisième source de signaux de demande : le cofinancement industriel ANR.

Une entreprise qui met de l'argent dans un projet ANR dont l'objet nomme une OPÉRATION laser
achète cette technologie -- un signal d'achat, au même titre qu'un appel d'offres, et le seul
que l'observatoire tire d'une donnée déjà en cache, sans requête réseau.

Le point délicat que ces tests protègent est la seconde condition. Sans elle, un fabricant de
sources partenaire d'un projet de spectroscopie compterait comme acheteur de micro-usinage,
et la page Marché dirait faux sur les gens qu'elle nomme.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import demand_signals

PROJETS = (
    # Nomme une opération (« texturation ») : un cofinancement industriel y est un achat.
    ("ANR-22-CE01-0001", "2022", "TEXTUR", "Texturation femtoseconde de moules d'injection",
     "Le projet développe la texturation laser femtoseconde de moules pour le médical."),
    # Femtoseconde, mais aucune opération : de la spectroscopie, pas un procédé.
    ("ANR-22-CE02-0002", "2022", "SPECTRO", "Spectroscopie femtoseconde de complexes moléculaires",
     "Nous sondons la dynamique électronique par impulsions laser femtosecondes."),
    # Rien à voir : ne doit même pas coûter un passage de lexique (pré-filtre).
    ("ANR-22-CE03-0003", "2022", "POULET", "Élevage avicole et nutrition",
     "Effets de la ration sur la croissance des volailles."),
)

PARTENAIRES = (
    ("ANR-22-CE01-0001", "MOULES DU JURA", "PME (petite et moyenne entreprise)", "Morez"),
    ("ANR-22-CE01-0001", "Laboratoire Hubert Curien", "Organisme de recherche", "Saint-Étienne"),
    ("ANR-22-CE02-0002", "FABRICANT DE SOURCES SA", "PME (petite et moyenne entreprise)", "Talence"),
    ("ANR-22-CE03-0003", "COOPERATIVE AVICOLE", "PME (petite et moyenne entreprise)", "Rennes"),
)


class AnrCofundingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cache = Path(self._tmp.name)

        with (cache / "anr-test-projets.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([
                "Projet.Code_Decision", "AAP.Edition", "Projet.Acronyme",
                "Projet.Titre.Francais", "Projet.Titre.Anglais",
                "Projet.Resume.Francais", "Projet.Resume.Anglais",
            ])
            for code, edition, acronyme, titre, resume in PROJETS:
                writer.writerow([code, edition, acronyme, titre, "", resume, ""])

        with (cache / "anr-test-partenaires.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([
                "Projet.Code_Decision", "Projet.Partenaire.Nom_organisme",
                "Projet.Partenaire.Categorie_organisme", "Projet.Partenaire.Adresse.Ville",
            ])
            for ligne in PARTENAIRES:
                writer.writerow(list(ligne))

        self._patcher = patch.object(demand_signals, "ANR_CACHE_DIR", cache)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _signaux(self):
        return {s["buyer_name"]: s for s in demand_signals._anr_cofunding_signals()}

    def test_an_industrial_partner_of_a_process_project_is_a_buy_signal(self):
        signaux = self._signaux()
        self.assertIn("MOULES DU JURA", signaux)
        signal = signaux["MOULES DU JURA"]
        self.assertEqual("ANR", signal["source"])
        self.assertEqual("2022", signal["published_at"])
        self.assertIn("anr.fr", signal["url"])

    def test_the_title_carries_what_the_lexicon_read(self):
        """C'est ce qui rend la ligne lisible sans rouvrir le projet, et c'est aussi la preuve
        du classement : les mots viennent du texte ANR, pas d'une interprétation."""
        self.assertIn("Texturation", self._signaux()["MOULES DU JURA"]["title"])

    def test_a_laboratory_is_never_a_buy_signal(self):
        """Le laboratoire fait la recherche, il ne l'achète pas -- et l'ANR finance surtout des
        laboratoires : sans ce tri, la page Marché serait une liste d'universités."""
        self.assertNotIn("Laboratoire Hubert Curien", self._signaux())

    def test_a_project_without_a_named_operation_produces_nothing(self):
        """La condition qui compte : « femtoseconde » dans un résumé ne dit pas qu'on achète du
        procédé. Sans elle, un fabricant de sources partenaire d'une spectroscopie compterait
        comme acheteur de micro-usinage."""
        self.assertNotIn("FABRICANT DE SOURCES SA", self._signaux())

    def test_an_unrelated_project_produces_nothing(self):
        self.assertNotIn("COOPERATIVE AVICOLE", self._signaux())

    def test_an_absent_cache_is_silence_not_an_error(self):
        """L'ANR peut n'avoir jamais été collectée : il n'y a alors pas de signal, ce qui est la
        vérité, pas une panne."""
        with tempfile.TemporaryDirectory() as vide:
            with patch.object(demand_signals, "ANR_CACHE_DIR", Path(vide)):
                self.assertEqual([], demand_signals._anr_cofunding_signals())

    def test_the_pass_can_be_switched_off(self):
        """`include_cofunding=False` existe pour les tests des deux autres sources : la passe lit
        un cache de 136 Mo sur le disque réel, et la suite entière en dépendrait."""
        with patch.object(demand_signals, "_anr_cofunding_signals") as jamais:
            demand_signals.collect_demand_signals(include_cofunding=False)
        jamais.assert_not_called()


if __name__ == "__main__":
    unittest.main()
