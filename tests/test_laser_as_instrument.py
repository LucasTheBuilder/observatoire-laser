"""Tests pour lexicon.is_laser_the_instrument() (audit du 09/09/2026).

`_laser_match` dit vrai dès qu'un terme ultra-rapide apparaît -- y compris quand l'impulsion
femtoseconde est l'INSTRUMENT DE MESURE et non le procédé. Un article de spectroscopie
d'absorption transitoire dit "femtosecond" autant qu'un article d'usinage, et c'est ainsi
qu'une publication de photocatalyse s'est retrouvée affichée comme document de l'observatoire.

Le point délicat, celui que ces tests protègent : la spectroscopie sert AUSSI, légitimement, à
surveiller un procédé laser. Deux publications IREPA du corpus sont dans ce cas.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lexicon import _laser_match, is_laser_the_instrument, is_on_topic

# Le cas trouvé en production (doc 207), et ses voisins de la même famille.
INSTRUMENT_SEUL = (
    "Revealing the enhanced photocatalytic hydrogen production mechanism of 3DOM TiO2/g-C3N4 "
    "S-scheme heterojunction by femtosecond transient absorption spectroscopy",
    # "pump-probe spectroscopy" seul ne suffirait pas à faire entrer ce titre (aucun terme de
    # LASER_RULES), donc il ne serait jamais un faux positif : c'est bien la version
    # "femtosecond ..." qui pose problème, et c'est elle qu'on teste.
    "Carrier dynamics in halide perovskites probed by femtosecond pump-probe spectroscopy",
    "Time-resolved photoluminescence of quantum dots under ultrafast excitation",
)

# Le laser EST le procédé, la spectroscopie ne fait que le surveiller : titres réels du corpus.
PROCEDE_SURVEILLE = (
    "Monitoring of ultrashort pulse laser surface texturing using spectroscopy and deep learning model",
    "Enabling in-situ monitoring of ultrashort pulse laser surface texturing",
)


class LaserAsInstrumentTests(unittest.TestCase):
    def test_characterisation_only_work_is_flagged(self):
        for text in INSTRUMENT_SEUL:
            with self.subTest(text=text[:60]):
                # Le piège est là : ces textes passent le filtre laser existant.
                self.assertTrue(_laser_match(text))
                self.assertTrue(is_laser_the_instrument(text))

    def test_spectroscopy_monitoring_a_named_process_is_kept(self):
        for text in PROCEDE_SURVEILLE:
            with self.subTest(text=text[:60]):
                self.assertFalse(is_laser_the_instrument(text))

    def test_plain_machining_work_is_untouched(self):
        for text in (
            "Femtosecond laser micromachining of sapphire wafers",
            "Large-area glass welding using femtosecond lasers",
            "Customised through vias in glass interposer production using ultrafast lasers",
        ):
            with self.subTest(text=text[:60]):
                self.assertFalse(is_laser_the_instrument(text))

    def test_text_without_any_cue_is_never_flagged(self):
        self.assertFalse(is_laser_the_instrument("Ultrafast laser structuring of battery electrodes"))
        self.assertFalse(is_laser_the_instrument(""))

    def test_is_on_topic_applies_the_guard_to_every_source(self):
        """Décision de périmètre prise le 09/09/2026 : la veille suit les DÉVELOPPEMENTS de la
        technologie laser ultra-rapide, pas les travaux qui s'en servent comme instrument pour
        observer autre chose. La garde est donc câblée dans is_on_topic, donc appliquée aussi
        aux projets CORDIS -- où elle retire 26 des 257 projets qui y étaient admis.

        La version précédente de ce test fixait l'inverse, précisément pour que ce changement
        soit délibéré plutôt que subi.
        """
        for text in INSTRUMENT_SEUL:
            with self.subTest(text=text[:60]):
                self.assertFalse(is_on_topic(text))

    def test_a_source_described_in_prose_is_a_known_limit(self):
        """Ce test disait l'inverse jusqu'au 10/09/2026 : « développer une source ultra-rapide
        EST un développement de la technologie ». Lucas a tranché le contraire ce jour-là — les
        informations uniquement liées aux sources sont hors sujet — et c'est
        lexicon.is_laser_the_source qui s'en charge (voir tests/test_laser_as_source.py).

        Ce qu'on fixe ici est la LIMITE de cette garde, pour qu'elle soit connue plutôt que
        découverte : elle lit un vocabulaire de source (« laser platform », « CPA front-end »,
        « power-scaled »...), et un objectif CORDIS qui décrit la même chose en prose n'en
        emploie aucun. Les trois projets concernés — PACRI, THRILL, VISUAL — ont donc été
        retirés à la main. Le jour où un collecteur les ramène, c'est ici qu'il faut regarder.
        """
        for text in (
            "Technology for High-Repetition-rate Intense Laser Laboratories: high-energy ultrafast laser technology",
            "This high-average-power platform will deliver ultrashort optical pulses at very high repetition rates",
        ):
            with self.subTest(text=text[:60]):
                self.assertTrue(is_on_topic(text))


class FrenchInstrumentVocabularyTests(unittest.TestCase):
    """La garde était entièrement anglophone jusqu'au 15/09/2026, alors que sa jumelle
    is_laser_the_source parlait français depuis la veille -- une garde traduite, l'autre non.

    L'asymétrie se voyait à l'œil nu : la MÊME phrase, traduite, changeait de verdict. Sans
    conséquence tant que le corpus était anglophone ; l'arrivée des projets ANR l'a rendue
    coûteuse, la moitié des projets femtoseconde financés en France employant le laser comme
    instrument de mesure.
    """

    # Titres réels de projets ANR (jeux ANR_01/ANR_02, édition 2022-2025).
    INSTRUMENT_FR = (
        "Spectroscopie ultrarapide non-linéaire de plexcitons à base de bipyramides d'or",
        "Microscopie multimodale à 3 photons pour l'analyse de l'oxygénation du sang",
        "Un nouveau regard sur la dynamique des métaux carbonyles grâce à la spectroscopie "
        "femtoseconde dans l'extrême ultraviolet",
        "Imagerie par absorption transitoire femtoseconde de nanoparticules",
    )

    def test_a_french_measurement_project_is_out_of_scope(self):
        for text in self.INSTRUMENT_FR:
            with self.subTest(text=text[:56]):
                self.assertTrue(is_laser_the_instrument(text))
                self.assertFalse(is_on_topic(text))

    def test_the_same_sentence_gets_the_same_verdict_in_both_languages(self):
        """Ce que le correctif rétablit, et la seule chose qui compte vraiment ici."""
        paires = (
            ("Femtosecond transient absorption spectroscopy of nanoparticles",
             "Spectroscopie d'absorption transitoire femtoseconde de nanoparticules"),
            ("Ultrafast spectroscopy of plexcitons in gold bipyramids",
             "Spectroscopie ultrarapide de plexcitons dans des bipyramides d'or"),
        )
        for anglais, francais in paires:
            with self.subTest(text=francais[:48]):
                self.assertEqual(is_laser_the_instrument(anglais), is_laser_the_instrument(francais))
                self.assertEqual(is_on_topic(anglais), is_on_topic(francais))

    def test_a_french_process_project_still_passes(self):
        """Le second membre de la règle vaut dans les deux langues : caractériser une surface
        usinée au microscope ne fait pas d'un article d'usinage un article de métrologie."""
        for text in (
            "Micro-usinage par laser femtoseconde du verre borosilicate",
            "Texturation de surface par impulsions ultracourtes, caractérisée par microscopie "
            "à deux photons",
            "Découpe et gravure laser femtoseconde de supports de culture cellulaire",
        ):
            with self.subTest(text=text[:56]):
                self.assertFalse(is_laser_the_instrument(text))
                self.assertTrue(is_on_topic(text))

    def test_bare_words_are_deliberately_absent(self):
        """« spectroscopie », « microscopie » et « imagerie » nus écarteraient 137 projets ANR
        au lieu de 46, sans qu'on puisse montrer que les 91 de plus sont des instruments. Ce
        test fixe la décision : terme pour terme, jamais un filet plus large."""
        from lexicon import LASER_AS_INSTRUMENT_CUES
        for mot in ("spectroscopie", "microscopie", "imagerie", "spectroscopy", "microscopy"):
            with self.subTest(mot=mot):
                self.assertNotIn(mot, LASER_AS_INSTRUMENT_CUES)


class DocumentIntakeAppliesTheGuardTests(unittest.TestCase):
    def test_openalex_rejects_characterisation_only_titles(self):
        from openalex import _work_is_on_topic
        for text in INSTRUMENT_SEUL:
            with self.subTest(text=text[:60]):
                self.assertFalse(_work_is_on_topic(text))

    def test_openalex_keeps_process_monitoring_titles(self):
        from openalex import _work_is_on_topic
        for text in PROCEDE_SURVEILLE:
            with self.subTest(text=text[:60]):
                self.assertTrue(_work_is_on_topic(text))


if __name__ == "__main__":
    unittest.main()
