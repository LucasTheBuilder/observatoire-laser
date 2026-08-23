from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hybrid import ContentBlock, normalize_page_type
from scrapers import (
    COMPONENTS,
    _candidate,
    _match_label,
    _structured_neighbors,
)


class V34MarketEngineTests(unittest.TestCase):
    def test_controlled_morphology_matches_common_component_plurals(self):
        self.assertEqual("Stents", _match_label("medical stents", COMPONENTS))
        self.assertEqual("Électrodes de batteries", _match_label("battery electrodes", COMPONENTS))
        self.assertEqual("Wafers", _match_label("silicon wafers", COMPONENTS))
        self.assertEqual("Cathéters", _match_label("medical catheters", COMPONENTS))

    def test_structured_relation_joins_only_same_editorial_group(self):
        blocks = [
            ContentBlock(
                heading="Stent manufacturing",
                h1="Applications",
                h2="Medical applications",
                h3="",
                text="Femtosecond laser processing of medical stents.",
                path="main > section.medical > div.intro",
            ),
            ContentBlock(
                heading="Surface operations",
                h1="Applications",
                h2="Medical applications",
                h3="",
                text="Surface texturing is offered for these components.",
                path="main > section.medical > div.operations",
            ),
            ContentBlock(
                heading="Battery electrodes",
                h1="Applications",
                h2="Battery applications",
                h3="",
                text="Electrode structuring for battery manufacturing.",
                path="main > section.battery > div.card",
            ),
        ]
        neighbors = _structured_neighbors(blocks, 0)
        self.assertEqual([blocks[1]], neighbors)
        diagnostics: dict[str, int] = {}
        candidate = _candidate(
            "Example",
            "https://example.test/applications",
            "Femtosecond laser applications",
            blocks[0],
            structured_blocks=neighbors,
            diagnostics=diagnostics,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual("structured", candidate["relation_strength"])
        self.assertEqual("Médical", candidate["market"])
        self.assertEqual("Stents", candidate["component"])
        self.assertEqual("Texturation", candidate["operation"])
        self.assertEqual(1, diagnostics["relation_structured"])

    def test_unknown_maturity_is_valid_radar_not_hidden_pending(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block)
        self.assertIsNotNone(candidate)
        self.assertEqual("radar", candidate["bucket"])
        self.assertEqual("unknown", candidate["maturity_class"])
        self.assertEqual("validated", candidate["fact_status"])
        self.assertIn("non déterminée", candidate["maturity"])

    def test_page_type_aliases_are_normalized(self):
        self.assertEqual("application", normalize_page_type("applications"))
        self.assertEqual("project", normalize_page_type("projects"))
        self.assertEqual("product", normalize_page_type("products"))
        self.assertEqual("news", normalize_page_type("news"))

    def test_rejection_telemetry_explains_missing_core_dimensions(self):
        diagnostics: dict[str, int] = {}
        block = ContentBlock(
            heading="Surface processing",
            text="Femtosecond laser surface texturing for precision manufacturing.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/page", "Laser applications", block, diagnostics=diagnostics)
        self.assertIsNone(candidate)
        self.assertEqual(1, diagnostics["blocks_examined"])
        self.assertEqual(1, diagnostics["laser_blocks"])
        self.assertEqual(1, diagnostics["missing_market"])
        self.assertEqual(1, diagnostics["missing_component"])
        self.assertEqual(1, diagnostics["relation_too_weak"])


class NegationGuardTests(unittest.TestCase):
    def test_explicit_denial_does_not_validate_the_relation(self):
        diagnostics: dict[str, int] = {}
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="We do not offer femtosecond laser texturing of medical stents.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block, diagnostics=diagnostics)
        self.assertIsNone(candidate)
        self.assertGreaterEqual(diagnostics.get("relation_negated_rejected", 0), 1)
        self.assertNotIn("candidate_valid", diagnostics)

    def test_contrastive_unlike_sentence_does_not_validate_the_relation(self):
        diagnostics: dict[str, int] = {}
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Unlike femtosecond laser texturing of medical stents, we specialize in metal stamping.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block, diagnostics=diagnostics)
        self.assertIsNone(candidate)
        self.assertGreaterEqual(diagnostics.get("relation_negated_rejected", 0), 1)

    def test_positive_sentence_without_negation_still_validates(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="We offer femtosecond laser texturing of medical stents for production customers.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block)
        self.assertIsNotNone(candidate)
        self.assertEqual("direct", candidate["relation_strength"])


if __name__ == "__main__":
    unittest.main()
