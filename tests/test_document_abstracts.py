"""Tests pour le résumé OpenAlex et la citation qui doit porter sa preuve.

Jusqu'au 14/09/2026 un document n'était classé que sur son titre : douze mots, où presque rien
ne tient. Mesuré sur les 302 publications du corpus, dont 260 ont un résumé chez OpenAlex :
370 étiquettes -> 790, et les documents sans aucune famille passent de 76 à 29.

Deux défauts n'ont pu apparaître qu'avec ces mille caractères de texte, et ces tests les fixent :
la citation tronquée avant son terme, et les faux amis franco-anglais du vocabulaire marché.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lexicon import _QUOTE_MAX, DOCUMENT_MARKETS, MARKETS, _contains_term, _quote, match_all_labels
from openalex import _abstract


class AbstractReconstructionTests(unittest.TestCase):
    """OpenAlex ne redistribue pas les résumés en texte suivi mais en index inversé
    (mot -> positions) ; la reconstruction est exacte et documentée par OpenAlex."""

    def test_rebuilds_the_text_in_order(self):
        work = {"abstract_inverted_index": {
            "Femtosecond": [0], "laser": [1], "drilling": [2], "of": [3], "glass": [4],
        }}
        self.assertEqual("Femtosecond laser drilling of glass", _abstract(work))

    def test_a_repeated_word_keeps_all_its_places(self):
        work = {"abstract_inverted_index": {"laser": [0, 3], "burst": [1], "and": [2], "pulse": [4]}}
        self.assertEqual("laser burst and laser pulse", _abstract(work))

    def test_no_abstract_is_an_empty_string_not_none(self):
        """upsert_document et upsert_document_technology_signal attendent une chaîne ; renvoyer
        None obligerait chaque appelant à la tester."""
        for work in ({}, {"abstract_inverted_index": None}, {"abstract_inverted_index": {}}):
            with self.subTest(work=work):
                self.assertEqual("", _abstract(work))

    def test_a_malformed_index_does_not_crash_the_collection(self):
        work = {"abstract_inverted_index": {"laser": ["x"], "drilling": [1], "glass": None}}
        self.assertEqual("drilling", _abstract(work))


class QuoteCarriesItsEvidenceTests(unittest.TestCase):
    def test_the_window_recentres_on_a_term_beyond_the_cap(self):
        """Le défaut réel : la phrase gagnait parce qu'elle contient le terme, puis se faisait
        couper juste avant lui. 8 citations sur 790 étaient dans ce cas."""
        texte = "A" * (_QUOTE_MAX + 200) + " roll-to-roll processing of battery electrodes"
        citation = _quote(texte, ("roll-to-roll",))
        self.assertTrue(_contains_term(citation, "roll-to-roll"))
        self.assertLessEqual(len(citation), _QUOTE_MAX + 1)  # +1 pour l'ellipse

    def test_a_truncated_window_says_that_it_is_truncated(self):
        texte = "B" * (_QUOTE_MAX + 200) + " ablation threshold"
        self.assertTrue(_quote(texte, ("ablation",)).startswith("…"))

    def test_a_short_sentence_is_returned_whole_and_unmarked(self):
        texte = "Femtosecond laser drilling of glass. Ablation of steel."
        citation = _quote(texte, ("ablation",))
        self.assertEqual("Ablation of steel.", citation)

    def test_the_sentence_with_the_most_terms_still_wins(self):
        texte = "Ablation of steel. Ablation and drilling of glass. Drilling of silicon."
        self.assertEqual("Ablation and drilling of glass.", _quote(texte, ("ablation", "drilling")))


class DocumentMarketVocabularyTests(unittest.TestCase):
    """Deux termes de MARKETS ne survivent pas à un résumé en anglais, et c'est une affaire de
    LANGUE : « spatial » désigne l'industrie spatiale en français et une géométrie en anglais."""

    def test_spatial_beam_shaping_is_not_the_space_industry(self):
        for texte in (
            "Challenges in spatial beam shaping for ultrafast laser surface texturing",
            "Beam shaping is performed using a Spatial Light Modulator (SLM)",
            "the relationship between temporal and spatial energy distribution",
        ):
            with self.subTest(texte=texte[:48]):
                self.assertNotIn("Spatial", {label for label, _ in match_all_labels(texte, DOCUMENT_MARKETS)})

    def test_the_real_space_industry_still_matches(self):
        for texte in ("laser texturing for spacecraft radiators", "satellite optics manufacturing"):
            with self.subTest(texte=texte):
                self.assertIn("Spatial", {label for label, _ in match_all_labels(texte, DOCUMENT_MARKETS)})

    def test_optical_microscopy_is_an_instrument_not_the_optics_market(self):
        for texte in (
            "Optical microscopy confirms that burst mode modifications produce voids",
            "Surface characterization by optical profilometry and SEM",
        ):
            with self.subTest(texte=texte[:48]):
                self.assertNotIn("Optique", {label for label, _ in match_all_labels(texte, DOCUMENT_MARKETS)})

    def test_the_optics_market_still_matches_on_its_own_words(self):
        for texte in ("target markets: medical devices and micro-optics", "glass processing for optics manufacturing"):
            with self.subTest(texte=texte):
                self.assertIn("Optique", {label for label, _ in match_all_labels(texte, DOCUMENT_MARKETS)})

    def test_the_machine_optics_is_not_the_optics_market(self):
        """Relecture du 06/10/2026 : les 24 publications classées Optique l'étaient toutes par
        l'optique DE LA MACHINE ou de mesure."""
        for texte in (
            "focused through an f-theta lens of 100 mm",
            "helical drilling optics and ultrashort laser pulses",
            "the design of the corresponding processing optics",
        ):
            with self.subTest(texte=texte):
                self.assertNotIn("Optique", {label for label, _ in match_all_labels(texte, DOCUMENT_MARKETS)})

    def test_market_extraction_keeps_the_full_vocabulary(self):
        """MARKETS sert aussi à l'extraction de faits marché, sur des pages d'acteurs où le
        contexte est tout autre. Le restreindre là-bas réécrirait des faits déjà validés."""
        self.assertIn("spatial", MARKETS["Spatial"]["any_of"])
        self.assertIn("optical", MARKETS["Optique"]["any_of"])

    def test_every_other_market_label_keeps_its_vocabulary(self):
        """Seuls Spatial, Optique et Photonique perdent ou changent des termes ; ailleurs, le
        resserrement du 06/10/2026 n'ajoute que des exclusions (« quantum efficiency »...)."""
        for label, rule in MARKETS.items():
            if label in {"Spatial", "Optique", "Photonique"}:
                continue
            with self.subTest(label=label):
                self.assertEqual(rule["any_of"], DOCUMENT_MARKETS[label]["any_of"])


if __name__ == "__main__":
    unittest.main()
