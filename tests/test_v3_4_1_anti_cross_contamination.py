from __future__ import annotations

import unittest

from hybrid import ContentBlock
from scrapers import _candidate, _structured_neighbors


class V341AntiCrossContaminationTests(unittest.TestCase):
    def test_sibling_cards_are_isolated_even_with_same_hierarchy(self):
        blocks = [
            ContentBlock(
                heading="Biomedical",
                h1="Applications",
                h2="Applications",
                text="Medical applications using femtosecond laser processing.",
                path="html > body > main > section.apps > div.card::repeated",
            ),
            ContentBlock(
                heading="TGV wafers",
                h1="Applications",
                h2="Applications",
                text="Femtosecond laser dicing is used for semiconductor wafers in advanced packaging.",
                path="html > body > main > section.apps > div.card::repeated",
            ),
        ]
        self.assertEqual([], _structured_neighbors(blocks, 0))
        self.assertEqual([], _structured_neighbors(blocks, 1))
        self.assertIsNone(_candidate(
            "Example", "https://example.test/applications", "Applications", blocks[0],
            structured_blocks=_structured_neighbors(blocks, 0),
        ))
        second = _candidate(
            "Example", "https://example.test/applications", "Applications", blocks[1],
            structured_blocks=_structured_neighbors(blocks, 1),
        )
        self.assertIsNotNone(second)
        self.assertEqual("Semi-conducteurs", second["market"])
        self.assertEqual("Wafers", second["component"])
        self.assertEqual("Dicing", second["operation"])

    def test_multi_application_parent_cannot_create_cartesian_fact(self):
        block = ContentBlock(
            heading="Applications",
            h2="Applications",
            text=(
                "Medical applications include implants and catheters. "
                "Semiconductor applications include wafers and substrates. "
                "Femtosecond laser dicing and surface texturing are available for precision manufacturing."
            ),
            path="html > body > main > section.applications",
        )
        diagnostics: dict[str, int] = {}
        candidate = _candidate(
            "Example", "https://example.test/applications", "Femtosecond laser applications", block,
            diagnostics=diagnostics,
        )
        self.assertIsNone(candidate)
        self.assertEqual(1, diagnostics.get("multi_context_block"))
        self.assertEqual(1, diagnostics.get("multi_context_rejected"))

    def test_same_dom_parent_can_still_form_structured_relation(self):
        blocks = [
            ContentBlock(
                heading="Battery electrodes",
                h2="Battery manufacturing",
                text="Femtosecond laser processing of battery electrodes.",
                path="html > body > main > section.battery > div.intro",
            ),
            ContentBlock(
                heading="Operations",
                h2="Battery manufacturing",
                text="Surface texturing is used for these electrodes.",
                path="html > body > main > section.battery > div.operations",
            ),
        ]
        neighbors = _structured_neighbors(blocks, 0)
        self.assertEqual([blocks[1]], neighbors)
        candidate = _candidate(
            "Example", "https://example.test/battery", "Femtosecond laser", blocks[0],
            structured_blocks=neighbors,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual("structured", candidate["relation_strength"])
        self.assertEqual("Batteries", candidate["market"])
        self.assertEqual("Électrodes de batteries", candidate["component"])
        self.assertEqual("Texturation", candidate["operation"])


if __name__ == "__main__":
    unittest.main()
