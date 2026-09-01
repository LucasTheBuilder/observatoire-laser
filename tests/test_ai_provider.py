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
import hybrid as hybridmod
from hybrid import AnthropicClient, ContentBlock, OllamaClient, ai_cost_cap_reached, build_profile, get_ai_client
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

    def test_known_label_accepted_for_review_even_when_not_independently_confirmed(self):
        # Chantier 2 item 3 (invert the AI gate): the AI's own wording resolves to known
        # labels ("Stents"), but the block text doesn't actually support it independently via
        # the deterministic local-section lexicon. Under the old gate this silently dropped the
        # fact -- the audit's whole complaint (71 proposals -> 1 validated, because the AI could
        # only ever agree with the rules, never surface something they missed). Now it's queued
        # for human review instead: fact_status='review', never auto-published.
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
        self.assertEqual(1, len(candidates))
        self.assertEqual("market_application", candidates[0]["kind"])
        self.assertEqual("review", candidates[0]["fact_status"])
        self.assertEqual("Stents", candidates[0]["component"])

    def test_excluded_blocks_never_reach_the_ai(self):
        # Chantier 2 item 3: exclude_indices lets scrape_market() keep the AI focused on blocks
        # the deterministic lexicon rejected this pass, instead of re-processing ground it
        # already covered.
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
        candidates = _ai_candidates(
            "Example", "https://example.test/medical", "Applications", [block], fake, exclude_indices={0},
        )
        self.assertEqual([], candidates)


class AiCandidatesTruncationTests(unittest.TestCase):
    def test_facts_beyond_the_per_page_limit_are_counted_not_silently_dropped(self):
        # Bug fix (audit v8 §2.2): ai_facts_proposed used to count len(facts) while the loop
        # below only ever examined facts[:20] -- the two numbers could diverge without a
        # trace. 25 facts here must report 20 examined + 5 truncated, not 25 examined.
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents for production customers.",
            path="main > article",
        )
        facts = [{"block_index": 0, "quote": "not a real substring", "confidence": 0.9} for _ in range(25)]
        fake = FakeAiClient(facts)
        diagnostics: dict[str, int] = {}
        _ai_candidates("Example", "https://example.test/medical", "Applications", [block], fake, diagnostics)
        self.assertEqual(20, diagnostics.get("ai_facts_proposed"))
        self.assertEqual(5, diagnostics.get("ai_facts_truncated"))

    def test_facts_under_the_limit_report_no_truncation(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents for production customers.",
            path="main > article",
        )
        facts = [{"block_index": 0, "quote": "not a real substring", "confidence": 0.9} for _ in range(3)]
        fake = FakeAiClient(facts)
        diagnostics: dict[str, int] = {}
        _ai_candidates("Example", "https://example.test/medical", "Applications", [block], fake, diagnostics)
        self.assertEqual(3, diagnostics.get("ai_facts_proposed"))
        self.assertNotIn("ai_facts_truncated", diagnostics)


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

    def test_unknown_placeholder_is_treated_as_nothing_proposed_not_a_real_label(self):
        # Observed against the real Anthropic API: Claude Haiku sometimes answers a dimension
        # with the literal string "<UNKNOWN>" instead of leaving it blank, when a block
        # genuinely has no clear component/market/operation of its own (e.g. a process-only
        # publication snippet). That must not land in the vocabulary triage queue as if it
        # were a genuine proposal.
        block = ContentBlock(
            heading="Publications",
            text="Femtosecond laser processing enables novel manufacturing approaches for aerospace parts.",
            path="main > article",
        )
        diagnostics: dict[str, int] = {}
        fake = FakeAiClient([{
            "block_index": 0, "market": "Aéronautique", "component": "<UNKNOWN>", "operation": "Soudage",
            "quote": "Femtosecond laser processing enables novel manufacturing approaches for aerospace parts.",
            "confidence": 0.9,
        }])
        candidates = _ai_candidates("Example", "https://example.test/aero", "Applications", [block], fake, diagnostics)
        self.assertEqual([], candidates)
        self.assertEqual(1, diagnostics.get("ai_fact_nothing_proposed", 0))
        self.assertNotIn("ai_vocabulary_queued", diagnostics)


class AiCandidatesLowConfidenceTests(unittest.TestCase):
    """Audit du 01/09/2026 : `confidence < 0.72` était la première cause de perte du chemin IA
    (171 faits jetés sur 330 pertes cumulées, contre 21 `ai_fact_validated` au total), et le
    rejet portait sur un nombre auto-déclaré par le modèle. Comme rien de _ai_candidates n'est
    jamais auto-publié (fact_status='review'), il ne protégeait rien que la relecture humaine ne
    couvrait déjà : il ne coûtait que du rappel. Nouveau contrat fixé ici -- une confiance basse
    est OBSERVÉE (diagnostic + field_confidence, qui sert à prioriser la file), jamais un rejet.
    """

    def test_low_confidence_known_labels_reach_review_instead_of_being_dropped(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents for production customers.",
            path="main > article",
        )
        fake = FakeAiClient([{
            "block_index": 0, "market": "Médical", "component": "Stents", "operation": "Texturation",
            "quote": "Femtosecond laser surface texturing of medical stents for production customers.",
            "confidence": 0.55,
        }])
        diagnostics: dict[str, int] = {}
        candidates = _ai_candidates("Example", "https://example.test/medical", "Applications", [block], fake, diagnostics)
        self.assertEqual(1, len(candidates))
        self.assertEqual("market_application", candidates[0]["kind"])
        # Conservé, mais toujours derrière le garde humain -- jamais auto-publié.
        self.assertEqual("review", candidates[0]["fact_status"])
        # La valeur auto-déclarée est gardée telle quelle : review_queue._priority s'en sert
        # pour pondérer l'incertitude, au lieu qu'elle serve à supprimer en silence.
        self.assertEqual(0.55, candidates[0]["confidence"])
        # Observé sans être rejeté : les deux compteurs montent sur le même fait.
        self.assertEqual(1, diagnostics.get("ai_fact_low_confidence", 0))
        self.assertEqual(1, diagnostics.get("ai_fact_validated", 0))

    def test_low_confidence_unknown_label_now_reaches_the_vocabulary_branch(self):
        # Le seuil s'appliquait AVANT la branche vocabulaire : un libellé inconnu proposé avec
        # une confiance basse disparaissait sans laisser le moindre candidat à relire. C'est la
        # perte la plus coûteuse des deux, puisque le vocabulaire est la seule boucle
        # d'apprentissage réellement fermée de l'app (vocabulary_candidates ->
        # custom_lexicon_entries, rechargé à chaque collecte).
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
            "confidence": 0.55,
        }])
        diagnostics: dict[str, int] = {}
        candidates = _ai_candidates("Example", "https://example.test/aero", "Applications", [block], fake, diagnostics)
        self.assertEqual(1, len(candidates))
        self.assertEqual("vocabulary_candidate", candidates[0]["kind"])
        self.assertEqual(1, diagnostics.get("ai_fact_low_confidence", 0))
        self.assertEqual(1, diagnostics.get("ai_vocabulary_queued", 0))

    def test_non_verbatim_quote_is_still_a_hard_rejection(self):
        # Garde-fou de non-régression : retirer le seuil de confiance ne relâche pas la
        # citation verbatim, qui reste LE critère structurel de rejet (§10.7 : ce sont les
        # critères structurels qui décident, pas un score continu déguisé).
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents for production customers.",
            path="main > article",
        )
        fake = FakeAiClient([{
            "block_index": 0, "market": "Médical", "component": "Stents", "operation": "Texturation",
            "quote": "Nous reformulons librement ce que dit la page au lieu de la citer.",
            "confidence": 0.95,
        }])
        diagnostics: dict[str, int] = {}
        candidates = _ai_candidates("Example", "https://example.test/medical", "Applications", [block], fake, diagnostics)
        self.assertEqual([], candidates)
        self.assertEqual(1, diagnostics.get("ai_fact_quote_not_verbatim", 0))


class AiCandidatesMalformedBreakdownTests(unittest.TestCase):
    """Audit du 01/09/2026 : `ai_fact_malformed` regroupait quatre échecs distincts, donc on
    savait QUE le modèle produit du non-exploitable (20.6% des faits examinés, 32.5% sur la
    seule passe à l'échelle réelle) sans jamais savoir POURQUOI. Ces sous-compteurs séparent la
    forme de la réponse (champ absent / type faux -> schéma typé) de l'index de bloc (le modèle
    désigne un bloc qu'il n'a pas reçu -> enum sur block_index).

    Contrat vérifié partout ici : le total `ai_fact_malformed` monte TOUJOURS en plus du
    sous-compteur, pour rester comparable aux passes antérieures.
    """

    TEXT = "Femtosecond laser surface texturing of medical stents for production customers."

    def _diagnostics_for(self, fact: dict | object) -> dict:
        block = ContentBlock(heading="Medical stents", h2="Medical", text=self.TEXT, path="main > article")
        diagnostics: dict[str, int] = {}
        candidates = _ai_candidates(
            "Example", "https://example.test/medical", "Applications", [block],
            FakeAiClient([fact]), diagnostics,
        )
        self.assertEqual([], candidates)
        # Le roll-up historique reste alimenté quel que soit le sous-cas.
        self.assertEqual(1, diagnostics.get("ai_fact_malformed", 0))
        return diagnostics

    def test_a_fact_that_is_not_an_object_is_counted_as_such(self):
        diagnostics = self._diagnostics_for("juste une chaîne au lieu d'un objet")
        self.assertEqual(1, diagnostics.get("ai_fact_not_a_mapping", 0))

    def test_missing_block_index_is_counted_as_invalid(self):
        diagnostics = self._diagnostics_for({"quote": self.TEXT, "confidence": 0.9})
        self.assertEqual(1, diagnostics.get("ai_fact_block_index_invalid", 0))

    def test_non_numeric_block_index_is_counted_as_invalid(self):
        diagnostics = self._diagnostics_for({"block_index": "premier", "quote": self.TEXT, "confidence": 0.9})
        self.assertEqual(1, diagnostics.get("ai_fact_block_index_invalid", 0))

    def test_block_index_the_model_never_received_is_counted_separately(self):
        # L'hypothèse principale : un seul bloc a été envoyé (index 0), le modèle en désigne un
        # autre. Distinguer ce cas d'un champ absent est tout l'intérêt du découpage -- c'est
        # lui qui se corrige par un enum, et lui seul qui explique que le taux de malformés
        # grimpe avec le nombre de blocs envoyés.
        diagnostics = self._diagnostics_for({"block_index": 7, "quote": self.TEXT, "confidence": 0.9})
        self.assertEqual(1, diagnostics.get("ai_fact_block_index_unknown", 0))
        self.assertEqual(0, diagnostics.get("ai_fact_block_index_invalid", 0))

    def test_missing_quote_is_counted_separately(self):
        diagnostics = self._diagnostics_for({"block_index": 0, "confidence": 0.9})
        self.assertEqual(1, diagnostics.get("ai_fact_quote_missing", 0))

    def test_non_numeric_confidence_is_counted_separately(self):
        diagnostics = self._diagnostics_for({"block_index": 0, "quote": self.TEXT, "confidence": "élevée"})
        self.assertEqual(1, diagnostics.get("ai_fact_confidence_invalid", 0))

    def test_a_well_formed_fact_triggers_no_malformed_counter_at_all(self):
        # Non-régression : le découpage ne doit rejeter personne de plus qu'avant.
        block = ContentBlock(heading="Medical stents", h2="Medical", text=self.TEXT, path="main > article")
        diagnostics: dict[str, int] = {}
        candidates = _ai_candidates(
            "Example", "https://example.test/medical", "Applications", [block],
            FakeAiClient([{
                "block_index": 0, "market": "Médical", "component": "Stents", "operation": "Texturation",
                "quote": self.TEXT, "confidence": 0.9,
            }]),
            diagnostics,
        )
        self.assertEqual(1, len(candidates))
        self.assertNotIn("ai_fact_malformed", diagnostics)
        self.assertEqual(
            [], [key for key in diagnostics if key.startswith(("ai_fact_block_index", "ai_fact_quote_missing",
                                                              "ai_fact_confidence_invalid", "ai_fact_not_a_mapping"))],
        )


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


class AiCostCapTests(unittest.TestCase):
    """Circuit breaker for Lot 1 §1.4 (audit veille, "prérequis de l'automatisation"): once a
    run's estimated Anthropic spend crosses AI_COST_CAP_USD_PER_RUN, both AI call sites
    (_ai_candidates for market facts, build_profile's assist pass) must fall back to
    deterministic-only rather than keep calling the API unbounded on an unattended scheduled run."""

    def test_ollama_is_never_capped_regardless_of_usage(self):
        # Local model, no metered cost -- the cap only exists to bound Anthropic spend.
        client = OllamaClient()
        self.assertFalse(ai_cost_cap_reached(client))

    def test_anthropic_under_cap_is_not_capped(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test-fake"}):
            client = AnthropicClient()
        client.total_input_tokens = 10
        client.total_output_tokens = 10
        self.assertFalse(ai_cost_cap_reached(client))

    def test_anthropic_over_cap_is_capped(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test-fake"}):
            client = AnthropicClient()
        client.total_input_tokens = 1_000_000
        client.total_output_tokens = 1_000_000
        with patch.object(hybridmod, "AI_COST_CAP_USD_PER_RUN", 0.001):
            self.assertTrue(ai_cost_cap_reached(client))

    def test_ai_candidates_short_circuits_once_capped(self):
        block = ContentBlock(
            heading="Medical stents",
            h2="Medical",
            text="Femtosecond laser surface texturing of medical stents for production customers.",
            path="main > article",
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test-fake"}):
            client = AnthropicClient()
        client.total_input_tokens = 1_000_000
        client.total_output_tokens = 1_000_000
        diagnostics: dict[str, int] = {}
        with patch.object(hybridmod, "AI_COST_CAP_USD_PER_RUN", 0.001), patch.object(client, "ask_json") as mocked:
            candidates = _ai_candidates(
                "Example", "https://example.test/medical", "Applications", [block], client, diagnostics,
            )
        mocked.assert_not_called()
        self.assertEqual([], candidates)
        self.assertEqual(1, diagnostics.get("ai_cost_cap_reached"))

    def test_build_profile_falls_back_to_deterministic_once_capped(self):
        actor = {"name": "Example", "official_url": "https://example.test/", "priority": True}
        site_profile = {"ollama_profile_assist": True}
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test-fake"}):
            client = AnthropicClient()
        client.total_input_tokens = 1_000_000
        client.total_output_tokens = 1_000_000
        with patch.object(hybridmod, "AI_COST_CAP_USD_PER_RUN", 0.001), patch.object(client, "ask_json") as mocked:
            _, generated_by, _ = build_profile(actor, [], client, site_profile=site_profile)
        mocked.assert_not_called()
        self.assertEqual("deterministic", generated_by)


if __name__ == "__main__":
    unittest.main()
