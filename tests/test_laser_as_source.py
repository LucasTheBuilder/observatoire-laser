"""Tests pour lexicon.is_laser_the_source() — la garde « construire le laser n'est pas s'en servir ».

Décision de périmètre de Lucas, 10/09/2026 : « les informations uniquement liées aux sources
sont HORS SUJET ». La garde a la même forme que sa jumelle is_laser_the_instrument : un
vocabulaire de source, ET aucun procédé nommé. C'est ce second membre que ces tests protègent —
une machine de fabrication décrit sa source autant qu'un article de source, et la seule chose
qui les sépare est de savoir si le texte dit ce qu'on FAIT avec.

Les titres ci-dessous sont tous réels : les deux premiers ont été retirés du corpus à la main
le 10/09/2026, les autres viennent d'une reconnaissance OpenAlex des 65 acteurs suivis le même
jour et seraient entrés à la collecte suivante.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lexicon import _laser_match, is_laser_the_source, is_on_topic

# La source et rien d'autre : ni matériau, ni opération, ni pièce fabriquée.
SOURCE_SEULE = (
    "Additive-manufactured monolithic femtosecond laser platform",
    "High-power and ultrashort IR-driver platform for industrial coherent XUV metrology",
    # Une source décrite par son ARCHITECTURE INTERNE. La première liste de vocabulaire ne
    # connaissait que « laser » accolé à un type de source, donc elle laissait tout ceci passer.
    "All-fiber 10-µJ class femtosecond thulium energy scalable CPA front-end platform based on "
    "long-term stable all-PM dissipative soliton oscillator",
    # ... ou par sa PERFORMANCE. « Productivity » ne dit pas ce qu'on usine.
    "Power-scaled femtosecond lasers for industrial productivity",
    "kW femtosecond laser with beam steering functionality for optimized productivity",
    # L'optique de la source : un empilement diélectrique est un composant, pas un procédé.
    "Physics-Informed Inverse Design of Ultrafast Coatings: From Direct Optimization to "
    "Generalizable Fine-Tuning",
    # « ultrafast » y qualifie le SCANNER, pas les impulsions, et le procédé est de la fusion
    # sur lit de poudre — deux raisons de sortir, une seule suffit.
    "Ultrafast solid-state laser beam steering system for productivity increase in PBF-LB/M of 316L",
)

# Le texte nomme une source ET ce qu'on en fait : c'est une machine de fabrication, elle reste.
SOURCE_AVEC_PROCEDE = (
    "Development of a modular femtosecond laser system for optical fiber and surface micromachining",
    "Femtosecond laser writing of Fiber Bragg Grating within a PM active fiber for 1535 nm "
    "all-fiber laser demonstration",
    "High power femtosecond laser: from micro to macro femtosecond laser processing",
)


class LaserAsSourceTests(unittest.TestCase):
    def test_source_only_work_is_flagged(self):
        for text in SOURCE_SEULE:
            with self.subTest(text=text[:60]):
                # Le piège est là : ces textes passent tous le filtre laser existant.
                self.assertTrue(_laser_match(text))
                self.assertTrue(is_laser_the_source(text))

    def test_a_source_that_names_its_process_is_kept(self):
        for text in SOURCE_AVEC_PROCEDE:
            with self.subTest(text=text[:60]):
                self.assertFalse(is_laser_the_source(text))

    def test_plain_process_work_is_untouched(self):
        for text in (
            "Femtosecond laser micromachining of sapphire wafers",
            "Ultrafast laser drilling of through vias in soda-lime glass using GHz-burst mode operation",
            "A feasibility study on femtosecond laser texturing of sprayed nanocellulose coatings",
        ):
            with self.subTest(text=text[:60]):
                self.assertFalse(is_laser_the_source(text))

    def test_coating_alone_is_not_a_source_cue(self):
        """Le vocabulaire d'optique est volontairement précis : « coating » seul ferait sortir
        les cinq publications Sirris de texturation de revêtements nanocellulose et bois."""
        self.assertFalse(is_laser_the_source(
            "Femtosecond Laser Texturing of Wood Coatings with Bio-Based Epoxy and Wax Additives"
        ))

    def test_text_without_any_cue_is_never_flagged(self):
        self.assertFalse(is_laser_the_source("Ultrafast laser structuring of battery electrodes"))
        self.assertFalse(is_laser_the_source(""))

    def test_is_on_topic_applies_the_guard_to_every_source(self):
        for text in SOURCE_SEULE:
            with self.subTest(text=text[:60]):
                self.assertFalse(is_on_topic(text))
        for text in SOURCE_AVEC_PROCEDE:
            with self.subTest(text=text[:60]):
                self.assertTrue(is_on_topic(text))


class FrenchSourceVocabularyTests(unittest.TestCase):
    """La garde était entièrement anglophone jusqu'au 14/09/2026 ; l'arrivée des projets
    nationaux (national_projects.py) lui a apporté des textes français, où l'ANR finance
    beaucoup de développement de source. Cas trouvé en production le jour même : le projet
    ANR-07-PRIB-0013 (SOFICARS, Amplitude), « Sources optiques fibrées pour la microscopie
    CARS », entrait comme projet de l'observatoire."""

    SOFICARS = (
        "Sources optiques fibrées pour la microscopie CARS. Nous présentons un projet de "
        "recherche visant à développer et explorer les performances de sources lasers fibrées "
        "compactes pour la microscopie CARS, sans avoir recours à aucun marquage fluorescent."
    )

    def test_a_french_source_project_is_out_of_scope(self):
        self.assertTrue(is_laser_the_source(self.SOFICARS))
        self.assertFalse(is_on_topic(self.SOFICARS))

    def test_biological_labelling_is_not_the_laser_operation(self):
        """« marquage » est un faux ami : en biologie c'est un traçage fluorescent. Le
        confondre avec le marquage laser nommait un procédé dans SOFICARS, et un procédé nommé
        désarme la seconde moitié de is_laser_the_source."""
        self.assertTrue(is_on_topic("Marquage laser femtoseconde de pièces horlogères en acier"))
        self.assertFalse(is_on_topic(
            "Imagerie de cellules vivantes sans marquage fluorescent par microscopie non linéaire"
        ))

    def test_a_french_process_project_still_passes(self):
        self.assertTrue(is_on_topic(
            "Découpe et texturation par impulsions ultracourtes de supports de culture cellulaire"
        ))


class GainMediumIsASourceTests(unittest.TestCase):
    """La garde décrivait l'ARCHITECTURE d'une source (oscillateur, amplificateur, CPA) mais
    pas son MILIEU À GAIN. Cas trouvé le 14/09/2026 dans les projets UKRI de Coherent."""

    GRATIS = (
        "GraTi:S - Graphene for Titanium Sapphire Lasers. This high-risk feasibility project "
        "aims to pave the way for the UK's first graphene-enabled ultrafast lasers."
    )

    def test_a_gain_medium_project_is_out_of_scope(self):
        self.assertTrue(is_laser_the_source(self.GRATIS))
        self.assertFalse(is_on_topic(self.GRATIS))

    def test_naming_the_source_of_a_process_never_excludes_it(self):
        """Un vrai travail de procédé décrit sa source autant qu'un projet de source : c'est
        le procédé nommé qui tranche, pas le vocabulaire de source."""
        for text in (
            "Ultrashort pulse laser micromachining of glass using a mode-locked fibre laser",
            "Femtosecond laser welding of dissimilar materials with a Ti:Sapphire amplifier",
        ):
            with self.subTest(text=text[:50]):
                self.assertTrue(is_on_topic(text))


class DocumentIntakeAppliesTheGuardTests(unittest.TestCase):
    def test_openalex_rejects_source_only_titles(self):
        from openalex import _work_is_on_topic
        for text in SOURCE_SEULE:
            with self.subTest(text=text[:60]):
                self.assertFalse(_work_is_on_topic(text))


if __name__ == "__main__":
    unittest.main()
