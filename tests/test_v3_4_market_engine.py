from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from db import application_key
from hybrid import ContentBlock, normalize_page_type
from scrapers import (
    COMPONENTS,
    _candidate,
    _match_label,
    _structured_neighbors,
    _upsert_market_candidate,
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
            text="Femtosecond laser surface texturing is used for medical stents.",
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


class PredicateAndContrastGuardTests(unittest.TestCase):
    """§10.6 audit veille (30/08/2026): a relation window must carry a predicate, and a
    contrastive marker (CONTRAST_CUES) must block attribution the same way negation does."""

    def test_pulsar_style_contrast_sentence_does_not_attribute_the_competitor_operation(self):
        # Reproduces the exact failure mode from the audit (Pulsar Photonics): a sentence
        # describing what CLASSIC/CONVENTIONAL competing methods do must not be attributed to
        # the actor just because it lexically contains market+component+operation terms.
        diagnostics: dict[str, int] = {}
        block = ContentBlock(
            heading="Ceramic substrates",
            h2="Medical",
            text=(
                "With the classic femtosecond laser dicing of thin ceramic substrates, "
                "mostly used are mechanical saws or conventional cutting tools."
            ),
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block, diagnostics=diagnostics)
        self.assertIsNone(candidate)
        self.assertGreaterEqual(diagnostics.get("relation_negated_rejected", 0), 1)

    def test_menu_fragment_with_no_predicate_is_rejected(self):
        # The audit's own aggregate finding: 76% of the facts it manually rejected as unreliable
        # carried no verb/assertive marker at all -- menu/list fragments, not real claims.
        diagnostics: dict[str, int] = {}
        block = ContentBlock(
            heading="Applications",
            h2="Medical",
            text="Applications: medical stents, femtosecond laser texturing, production customers.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block, diagnostics=diagnostics)
        self.assertIsNone(candidate)
        self.assertGreaterEqual(diagnostics.get("relation_no_predicate_rejected", 0), 1)
        self.assertNotIn("candidate_valid", diagnostics)

    def test_sentence_with_predicate_still_validates(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser texturing is used for medical stents for production customers.",
            path="main > article",
        )
        candidate = _candidate("Example", "https://example.test/medical", "Applications", block)
        self.assertIsNotNone(candidate)


class ApplicationKeyBucketTransitionTests(unittest.TestCase):
    def _fresh_market_db(self, tmp: str) -> Path:
        # init_databases() builds all three databases in one call, so ACTORS_DB and TECH_DB
        # must be redirected too -- otherwise it silently touches the project's real data/
        # files (actor upsert timestamps, migrations) even though this test only cares about
        # market.db.
        directory = Path(tmp)
        market_db = directory / "market.db"
        with patch.object(dbmod, "MARKET_DB", market_db), \
             patch.object(dbmod, "ACTORS_DB", directory / "actors.db"), \
             patch.object(dbmod, "TECH_DB", directory / "technology.db"):
            dbmod.init_databases()
        return market_db

    def test_radar_then_existing_merge_into_one_fact_and_log_the_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            app_key = application_key("Example", "Batteries", "Électrodes de batteries", "Microdécoupe")
            radar = {
                "actor": "Example", "bucket": "radar", "market": "Batteries",
                "component": "Électrodes de batteries", "operation": "Microdécoupe",
                "stage": "Prototype", "url": "https://example.test/radar", "title": "Radar page",
                "quote": "Prototype femtosecond laser microcutting of battery electrodes.",
                "fact_key": "example|radar|batteries|electrodes-de-batteries|microdecoupe",
                "application_key": app_key,
                "fingerprint": "fact-fp-1", "source_fingerprint": "src-fp-1",
                "block_heading": "Prototype", "block_path": "main > article", "mode": "block-rules",
                "confidence": 0.75, "fact_status": "validated",
            }
            existing = dict(
                radar, bucket="existing", stage="Production",
                url="https://example.test/existing", title="Production page",
                quote="Mass production femtosecond laser microcutting of battery electrodes.",
                fact_key="example|existing|batteries|electrodes-de-batteries|microdecoupe",
                fingerprint="fact-fp-2", source_fingerprint="src-fp-2", confidence=0.8,
            )

            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, radar)
                _upsert_market_candidate(db, existing)

            with dbmod.connect(market_db) as db:
                rows = db.execute("SELECT bucket FROM evidence WHERE application_key=?", (app_key,)).fetchall()
                self.assertEqual(1, len(rows))
                self.assertEqual("existing", rows[0]["bucket"])
                self.assertEqual(2, db.execute("SELECT COUNT(*) FROM evidence_sources").fetchone()[0])
                transitions = [
                    tuple(row) for row in db.execute(
                        "SELECT from_bucket,to_bucket FROM evidence_bucket_transitions ORDER BY id"
                    ).fetchall()
                ]
            # One transition for the initial insert (None -> radar), one for the upgrade (radar -> existing).
            self.assertEqual([(None, "radar"), ("radar", "existing")], transitions)

    def test_a_later_weaker_radar_signal_does_not_downgrade_an_existing_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            app_key = application_key("Example", "Batteries", "Électrodes de batteries", "Microdécoupe")
            existing = {
                "actor": "Example", "bucket": "existing", "market": "Batteries",
                "component": "Électrodes de batteries", "operation": "Microdécoupe",
                "stage": "Production", "url": "https://example.test/existing", "title": "Production page",
                "quote": "Mass production femtosecond laser microcutting of battery electrodes.",
                "fact_key": "example|existing|batteries|electrodes-de-batteries|microdecoupe",
                "application_key": app_key,
                "fingerprint": "fact-fp-1", "source_fingerprint": "src-fp-1",
                "block_heading": "Production", "block_path": "main > article", "mode": "block-rules",
                "confidence": 0.75, "fact_status": "validated",
            }
            # Higher confidence than the stored fact, so the field-update branch runs too --
            # the bucket must still not move backwards.
            later_radar = dict(
                existing, bucket="radar", stage="Prototype",
                url="https://example.test/radar-again", title="Prototype mention",
                quote="Prototype femtosecond laser microcutting of battery electrodes.",
                fact_key="example|radar|batteries|electrodes-de-batteries|microdecoupe",
                fingerprint="fact-fp-2", source_fingerprint="src-fp-2", confidence=0.9,
            )

            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, existing)
                _upsert_market_candidate(db, later_radar)

            with dbmod.connect(market_db) as db:
                row = db.execute("SELECT bucket FROM evidence WHERE application_key=?", (app_key,)).fetchone()
                self.assertEqual("existing", row["bucket"])
                transitions = [
                    row[0] for row in db.execute("SELECT to_bucket FROM evidence_bucket_transitions").fetchall()
                ]
            self.assertEqual(["existing"], transitions)


if __name__ == "__main__":
    unittest.main()
