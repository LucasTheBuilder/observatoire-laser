from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anthropic

import db as dbmod
from hybrid import AnthropicClient, ContentBlock, OllamaClient, get_ai_client
from scrapers import _ai_candidates, _upsert_vocabulary_candidate


class FakeToolBlock:
    def __init__(self, tool_input: dict) -> None:
        self.type = "tool_use"
        self.input = tool_input


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeAnthropicResponse:
    def __init__(self, tool_input: dict, input_tokens: int = 10, output_tokens: int = 5) -> None:
        self.content = [FakeToolBlock(tool_input)]
        self.usage = FakeUsage(input_tokens, output_tokens)


class FakeAiClient:
    """Minimal stand-in for OllamaClient/AnthropicClient's duck-typed contract."""

    def __init__(self, facts: list[dict]) -> None:
        self.model = "fake-model"
        self._facts = facts

    def available(self) -> bool:
        return True

    def ask_json(self, system: str, prompt: str) -> dict:
        return {"facts": self._facts}


class AnthropicClientAskJsonTests(unittest.TestCase):
    def test_ask_json_returns_tool_input_and_accumulates_usage_across_calls(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test-fake"}):
            client = AnthropicClient()
        fake_response = FakeAnthropicResponse({"facts": [{"market": "Médical"}]}, input_tokens=120, output_tokens=40)

        with patch.object(client._client.messages, "create", return_value=fake_response):
            result = client.ask_json("system prompt", "user prompt")
        self.assertEqual({"facts": [{"market": "Médical"}]}, result)
        self.assertEqual(120, client.total_input_tokens)
        self.assertEqual(40, client.total_output_tokens)

        with patch.object(client._client.messages, "create", return_value=fake_response):
            client.ask_json("system prompt", "user prompt")
        self.assertEqual(240, client.total_input_tokens)
        self.assertEqual(80, client.total_output_tokens)

    def test_without_api_key_is_unavailable_and_ask_json_is_a_no_op(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            client = AnthropicClient()
        self.assertFalse(client.available())
        self.assertEqual({}, client.ask_json("system", "prompt"))

    def test_api_error_degrades_to_empty_dict_without_raising(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test-fake"}):
            client = AnthropicClient()
        error = anthropic.APIError("boom", MagicMock(), body=None)
        with patch.object(client._client.messages, "create", side_effect=error):
            result = client.ask_json("system", "prompt")
        self.assertEqual({}, result)
        self.assertEqual(0, client.total_input_tokens)


class GetAiClientTests(unittest.TestCase):
    def test_defaults_to_ollama_when_unset(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("AI_PROVIDER", None)
            self.assertIsInstance(get_ai_client(), OllamaClient)

    def test_selects_anthropic_when_env_var_set(self):
        with patch.dict("os.environ", {"AI_PROVIDER": "anthropic"}):
            self.assertIsInstance(get_ai_client(), AnthropicClient)

    def test_is_case_insensitive_and_ignores_other_values(self):
        with patch.dict("os.environ", {"AI_PROVIDER": "Anthropic"}):
            self.assertIsInstance(get_ai_client(), AnthropicClient)
        with patch.dict("os.environ", {"AI_PROVIDER": "openai"}):
            self.assertIsInstance(get_ai_client(), OllamaClient)


class AiCandidatesKnownLabelTests(unittest.TestCase):
    def test_known_labels_still_require_the_deterministic_cross_check(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents for production customers.",
            path="main > article",
        )
        fake = FakeAiClient([{
            "block_index": 0, "market": "Médical", "component": "Stents", "operation": "Texturation",
            "quote": "Femtosecond laser surface texturing of medical stents for production customers.",
            "confidence": 0.9,
        }])
        candidates = _ai_candidates("Example", "https://example.test/medical", "Applications", [block], fake)
        self.assertEqual(1, len(candidates))
        self.assertEqual("market_application", candidates[0]["kind"])
        self.assertEqual("Médical", candidates[0]["market"])
        self.assertEqual("Stents", candidates[0]["component"])
        self.assertEqual("Texturation", candidates[0]["operation"])
        # _ai_mode_label() only special-cases isinstance(ollama, AnthropicClient); a plain
        # duck-typed fake (like Ollama itself) falls into the "ollama" branch.
        self.assertEqual("ollama:fake-model", candidates[0]["mode"])

    def test_known_label_rejected_when_not_independently_present_in_the_section(self):
        # The AI's own wording resolves to known labels, but the block text doesn't actually
        # support "Stents" independently -- the deterministic cross-check must still reject it.
        block = ContentBlock(
            heading="Medical devices",
            h2="Medical",
            text="Femtosecond laser surface texturing of implants for production customers.",
            path="main > article",
        )
        fake = FakeAiClient([{
            "block_index": 0, "market": "Médical", "component": "Stents", "operation": "Texturation",
            "quote": "Femtosecond laser surface texturing of implants for production customers.",
            "confidence": 0.9,
        }])
        candidates = _ai_candidates("Example", "https://example.test/medical", "Applications", [block], fake)
        self.assertEqual([], candidates)


class AiCandidatesOpenVocabularyTests(unittest.TestCase):
    def test_unresolved_component_is_queued_as_vocabulary_candidate_not_discarded(self):
        block = ContentBlock(
            heading="Aerospace pressure sensors",
            h2="Aéronautique",
            text="Femtosecond laser texturing of advanced pressure transducer housings for aerospace customers.",
            path="main > article",
        )
        fake = FakeAiClient([{
            "block_index": 0, "market": "Aéronautique", "component": "Boîtiers de capteurs de pression avancés",
            "operation": "Texturation",
            "quote": "Femtosecond laser texturing of advanced pressure transducer housings for aerospace customers.",
            "confidence": 0.9,
        }])
        candidates = _ai_candidates("Example", "https://example.test/aero", "Applications", [block], fake)
        self.assertEqual(1, len(candidates))
        self.assertEqual("vocabulary_candidate", candidates[0]["kind"])
        self.assertEqual(
            {"component": "Boîtiers de capteurs de pression avancés"},
            candidates[0]["proposed_labels"],
        )
        self.assertEqual({"market": "Aéronautique", "operation": "Texturation"}, candidates[0]["resolved_labels"])

    def test_a_fact_with_no_resolvable_dimension_and_nothing_proposed_is_dropped(self):
        block = ContentBlock(
            heading="Generic laser page",
            text="Femtosecond laser processing for various industrial customers.",
            path="main > article",
        )
        fake = FakeAiClient([{
            "block_index": 0, "market": "", "component": "", "operation": "",
            "quote": "Femtosecond laser processing for various industrial customers.",
            "confidence": 0.9,
        }])
        candidates = _ai_candidates("Example", "https://example.test/generic", "Applications", [block], fake)
        self.assertEqual([], candidates)


class VocabularyCandidateUpsertTests(unittest.TestCase):
    def test_repeated_upsert_dedupes_by_fingerprint_and_bumps_last_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            market_db = directory / "market.db"
            with patch.object(dbmod, "MARKET_DB", market_db), \
                 patch.object(dbmod, "ACTORS_DB", directory / "actors.db"), \
                 patch.object(dbmod, "TECH_DB", directory / "technology.db"):
                dbmod.init_databases()

            candidate = {
                "actor": "Example", "url": "https://example.test/aero", "title": "Applications",
                "quote": "Femtosecond laser texturing of advanced pressure transducer housings.",
                "block_heading": "Aerospace pressure sensors",
                "proposed_labels": {"component": "Boîtiers de capteurs de pression avancés"},
                "resolved_labels": {"market": "Aéronautique", "operation": "Texturation"},
                "fingerprint": "vocab-fp-1",
            }
            with dbmod.connect(market_db) as db:
                added_first = _upsert_vocabulary_candidate(db, candidate)
                added_second = _upsert_vocabulary_candidate(db, candidate)

            self.assertEqual(1, added_first)
            self.assertEqual(0, added_second)
            with dbmod.connect(market_db) as db:
                rows = db.execute("SELECT COUNT(*) FROM vocabulary_candidates").fetchone()
                self.assertEqual(1, rows[0])
                status = db.execute("SELECT review_status FROM vocabulary_candidates").fetchone()
                self.assertEqual("pending", status[0])


if __name__ == "__main__":
    unittest.main()
