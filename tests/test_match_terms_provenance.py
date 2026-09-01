"""Traçabilité des termes déclencheurs (`match_terms`) -- prérequis de l'agent d'analyse.

_candidate()/_offer_candidates() ont toujours SU quels termes du lexique avaient déclenché
chaque dimension : ils s'en servaient pour choisir la phrase à citer (_quote), puis les
jetaient. Un rejet humain disait donc "ce fait est faux" sans jamais dire "à cause de quelle
règle" -- le lien manquant entre une décision et la règle à corriger.

Ces tests verrouillent les trois propriétés dont dépend cette exploitabilité :
1. les termes sont capturés PAR DIMENSION, et sont réellement présents dans le texte source ;
2. les faits partiels -- le plus gros contingent en base -- les portent aussi (leur chemin
   passe par _core_labels(), qui ne renvoie que les libellés : sans re-dérivation explicite ils
   seraient les seuls à arriver en revue sans provenance) ;
3. une réobservation sans terme résolu n'écrase jamais une provenance déjà connue.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scrapers
from hybrid import ContentBlock
from scrapers import _candidate, _match_terms_json, _offer_candidates


def _memory_db() -> sqlite3.Connection:
    """Connexion minimale : _ensure_market_fact_status_column complète le schéma, comme pour
    les autres tests qui écrivent directement dans evidence."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_name TEXT, bucket TEXT, market TEXT, component TEXT,
            operation TEXT, industrial_stage TEXT, source_url TEXT, source_title TEXT, source_date TEXT, quote TEXT,
            source_group TEXT, fingerprint TEXT UNIQUE, fact_key TEXT UNIQUE, evidence_kind TEXT, language TEXT,
            review_status TEXT, created_at TEXT, updated_at TEXT, block_heading TEXT, block_path TEXT,
            extraction_mode TEXT, field_confidence REAL, laser_process TEXT, material TEXT, performance TEXT,
            maturity_level TEXT, relation_strength TEXT, relation_evidence TEXT, source_role TEXT
        );
        CREATE TABLE evidence_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id INTEGER, source_url TEXT, source_title TEXT,
            source_date TEXT, quote TEXT, language TEXT, block_heading TEXT, block_path TEXT, extraction_mode TEXT,
            field_confidence REAL, relation_strength TEXT, relation_evidence TEXT, source_role TEXT,
            fingerprint TEXT UNIQUE, created_at TEXT
        );
        """
    )
    return conn


class CandidateMatchTermsTests(unittest.TestCase):
    def test_direct_relation_records_the_terms_that_fired_each_dimension(self):
        block = ContentBlock(
            heading="Electrodes",
            text="Femtosecond laser texturing of battery electrodes is performed at scale.",
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/applications/", "Applications", block)
        self.assertIsNotNone(fact)
        terms = fact["match_terms"]

        # Les trois dimensions de coeur sont résolues, donc chacune doit porter sa trace.
        self.assertEqual({"market", "component", "operation"}, set(terms) & {"market", "component", "operation"})
        # Un terme enregistré doit être un terme RÉELLEMENT trouvé dans le texte analysé, pas
        # le libellé canonique de la règle : c'est toute la différence entre "quelle règle a
        # tiré" et "quel mot l'a fait tirer".
        haystack = f"{block.heading} {block.text}".casefold()
        for found in terms.values():
            self.assertTrue(found)
            for term in found:
                self.assertIn(term.casefold(), haystack)

    def test_partial_fact_also_carries_its_terms(self):
        # Deux dimensions sur trois : ce chemin passe par _core_labels(), qui perd les termes.
        block = ContentBlock(
            heading="Titanium implants",
            text="Femtosecond laser surface texturing of titanium implants in qualified production.",
            path="main > article.card",
        )
        fact = _candidate("MANUTECH USD", "https://www.manutech-usd.fr/applications/", "Applications", block)
        self.assertIsNotNone(fact)
        self.assertEqual("partial", fact["relation_strength"])
        self.assertTrue(fact["match_terms"], "un fait partiel sans provenance est un rejet inexploitable")

    def test_offer_candidates_record_terms_by_dimension(self):
        block = ContentBlock(
            heading="Micro-usinage",
            text="Nous réalisons la microdécoupe et la gravure laser femtoseconde de pièces en titane.",
            path="main > section",
        )
        offers = _offer_candidates("Example", "https://example.test/services/", "Services", block, page_type="service")
        self.assertTrue(offers)
        self.assertTrue(any(offer["match_terms"] for offer in offers))


class MatchTermsPersistenceTests(unittest.TestCase):
    def test_terms_are_written_to_the_evidence_row_as_json(self):
        block = ContentBlock(
            heading="Electrodes",
            text="Femtosecond laser texturing of battery electrodes is performed at scale.",
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/applications/", "Applications", block)
        conn = _memory_db()
        scrapers._upsert_market_candidate(conn, fact)
        stored = conn.execute("SELECT match_terms FROM evidence").fetchone()["match_terms"]
        self.assertEqual(fact["match_terms"], json.loads(stored))

    def test_a_candidate_without_terms_never_clobbers_a_known_provenance(self):
        """COALESCE(?,match_terms) ne protège que si l'absence de terme vaut NULL, pas '{}'."""
        self.assertIsNone(_match_terms_json({}))
        self.assertIsNone(_match_terms_json({"match_terms": {}}))
        self.assertEqual(
            '{"operation": ["gravure"]}',
            _match_terms_json({"match_terms": {"operation": ["gravure"]}}),
        )


if __name__ == "__main__":
    unittest.main()
