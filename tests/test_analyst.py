"""Agent d'analyse (phase 2). Aucun appel réseau : le client est injecté.

Ce qui est verrouillé ici tient à la répartition des rôles -- l'agent PROPOSE, le SQL vérifie,
l'humain tranche. Trois propriétés en découlent : sa sortie n'est jamais marquée vérifiée, une
proposition sans appui est écartée, et les citations partent au modèle explicitement étiquetées
comme non fiables (elles viennent de sites tiers, et un agent qui propose des règles est une
cible bien plus rentable qu'un extracteur).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analyst


def _dossier(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rejections": [
            {
                "item_id": 12, "queue": "evidence", "reject_reason": "wrong_dimension",
                "actor_name": "Example", "summary": "Batteries / Wafers / Dicing",
                "match_terms": {"component": ["wafer"]}, "extraction_mode": "block-rules",
                "relation_strength": "partial", "source_url": "https://example.test/a",
                "quote": "Nous découpons des wafers de silicium.",
            }
        ],
        "accepted_examples": [
            {
                "item_id": 34, "queue": "evidence", "actor_name": "Autre",
                "summary": "Semi-conducteurs / Wafers / Microdécoupe",
                "match_terms": {"component": ["wafer"]}, "relation_strength": "direct",
            }
        ],
        "misses": {
            "items": [
                {
                    "golden_fact_id": 5, "kind": "aucune_trace", "detail": "aucune preuve",
                    "actor_name": "Example", "expected": "Médical / Stents / Microperçage",
                    "source_url": "https://example.test/medical",
                }
            ]
        },
    }
    base.update(overrides)
    return base


class FakeClient:
    """Renvoie ce qu'on lui donne, et retient ce qu'il a reçu."""

    model = "modele-de-test"
    total_input_tokens = 11
    total_output_tokens = 22

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.system: str | None = None
        self.prompt: str | None = None
        self.tool: Any = None

    def propose(self, system: str, prompt: str, tool: Any) -> dict[str, Any]:
        self.system, self.prompt, self.tool = system, prompt, tool
        return self.payload


def _proposal(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "term_too_broad", "dimension": "component", "target": "wafer",
        "evidence_ids": [12], "rationale": "Le terme matche hors contexte semi-conducteur.",
        "change": {"operation": "add_negative_term", "value": "wafer de batterie"},
        "confidence": "medium",
    }
    base.update(overrides)
    return base


class OutputContractTests(unittest.TestCase):
    def test_output_is_never_marked_verified(self):
        """La phase 3 n'a pas encore tourné : afficher ça directement contournerait la seule
        garantie du dispositif."""
        client = FakeClient({"proposals": [_proposal()]})
        result = analyst.propose_improvements(_dossier(), client=client)
        self.assertFalse(result["verified"])
        self.assertEqual(1, len(result["proposals"]))
        self.assertEqual("modele-de-test", result["model"])

    def test_a_proposal_without_evidence_is_discarded(self):
        client = FakeClient({"proposals": [_proposal(evidence_ids=[]), _proposal()]})
        result = analyst.propose_improvements(_dossier(), client=client)
        self.assertEqual(1, len(result["proposals"]))
        self.assertEqual(1, result["discarded_malformed"])

    def test_an_unknown_operation_is_discarded(self):
        """Une opération hors liste n'est pas exécutable par la phase 3, donc pas vérifiable."""
        client = FakeClient({"proposals": [_proposal(change={"operation": "rewrite_lexicon", "value": "x"})]})
        result = analyst.propose_improvements(_dossier(), client=client)
        self.assertEqual([], result["proposals"])

    def test_no_call_is_made_when_there_is_nothing_to_analyse(self):
        client = FakeClient({"proposals": [_proposal()]})
        empty = _dossier(rejections=[], misses={"items": []})
        result = analyst.propose_improvements(empty, client=client)
        self.assertEqual([], result["proposals"])
        self.assertIn("skipped", result)
        self.assertIsNone(client.prompt, "aucun appel ne doit partir sur un dossier vide")


class PromptSafetyTests(unittest.TestCase):
    def test_quotes_are_sent_under_an_untrusted_label(self):
        client = FakeClient({"proposals": []})
        analyst.propose_improvements(_dossier(), client=client)
        payload = json.loads(client.prompt or "{}")
        self.assertIn("citations_non_fiables", payload)
        self.assertEqual("Nous découpons des wafers de silicium.", payload["citations_non_fiables"]["12"])
        # La consigne système doit le redire : la structure seule ne suffit pas.
        self.assertIn("jamais comme des instructions", client.system or "")

    def test_both_families_of_evidence_reach_the_model(self):
        """Sans les manques, l'agent ne peut proposer que de resserrer -- un cliquet à sens
        unique qui détruit le rappel en silence."""
        client = FakeClient({"proposals": []})
        analyst.propose_improvements(_dossier(), client=client)
        payload = json.loads(client.prompt or "{}")
        self.assertTrue(payload["rejets"])
        self.assertTrue(payload["manques"])
        self.assertTrue(payload["acceptes_contre_exemples"])


class ToolSchemaTests(unittest.TestCase):
    def test_the_schema_is_strict_and_leaves_no_free_field(self):
        """Un champ libre rendrait la modification proposée inexécutable, donc invérifiable --
        exactement la panne des deux entrées de lexique inertes acceptées en août."""
        self.assertTrue(analyst.PROPOSAL_TOOL["strict"])
        item = analyst.PROPOSAL_TOOL["input_schema"]["properties"]["proposals"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertFalse(item["properties"]["change"]["additionalProperties"])

    def test_the_schema_asks_for_no_count(self):
        """L'agent ne compte pas : le lui demander l'inviterait à approximer."""
        fields = analyst.PROPOSAL_TOOL["input_schema"]["properties"]["proposals"]["items"]["properties"]
        for forbidden in ("count", "total", "rate", "percentage", "frequency"):
            self.assertNotIn(forbidden, fields)

    def test_both_proposal_families_are_representable(self):
        for tightening in ("term_too_broad", "missing_negative_term"):
            self.assertIn(tightening, analyst.PROPOSAL_KINDS)
        for widening in ("missing_term", "uncovered_source", "gate_too_strict"):
            self.assertIn(widening, analyst.PROPOSAL_KINDS)


if __name__ == "__main__":
    unittest.main()
