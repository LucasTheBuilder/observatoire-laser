"""Tests pour le chantier 2 de l'audit collecte : débloquer l'entonnoir d'extraction marché.

Couvre les 5 correctifs, dans l'ordre où ils apparaissent dans l'audit :
1. L'URL/le fil d'Ariane comme porteur de marché (_url_market_hint, relation "page_context").
2. Les faits à 2 dimensions acceptés comme fact_status='partial' (_partial_candidate_dims).
3. L'inversion de la garde IA (_ai_candidates : exclude_indices, plus de garde
   ai_known_label_not_independently_confirmed) et la file de revue /api/market/review.
4. Le budget par acteur (MARKET_PAGES_PER_ACTOR) et le saut des pages inchangées
   (market_extracted_hash) dans _select_market_sources.
5. Le lexique composants élargi.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as appmod
import db as dbmod
import scrapers
from hybrid import ContentBlock
from scrapers import (
    COMPONENTS,
    MARKET_PAGES_PER_ACTOR,
    _candidate,
    _is_noise_block,
    _match_label,
    _partial_candidate_dims,
    _relation_evidence,
    _select_market_sources,
    _url_market_hint,
)


class UrlMarketHintTests(unittest.TestCase):
    def test_unambiguous_application_path_supplies_the_market(self):
        self.assertEqual("Médical", _url_market_hint("https://example.test/applications/medical/"))

    def test_generic_hub_path_supplies_nothing(self):
        self.assertIsNone(_url_market_hint("https://example.test/applications/"))

    def test_two_markets_in_the_path_is_ambiguous_and_supplies_nothing(self):
        # A page whose breadcrumb spans several markets (e.g. a cross-sector case study) must
        # never impose an arbitrary single market on all of its blocks.
        self.assertIsNone(_url_market_hint("https://example.test/case-studies/medical-and-automotive/"))

    def test_title_alone_can_also_supply_the_market(self):
        self.assertEqual("Batteries", _url_market_hint("https://example.test/en/page-42", "Battery electrode manufacturing"))


class PageContextRelationTests(unittest.TestCase):
    def test_candidate_accepts_a_page_market_when_component_and_operation_are_local(self):
        # No market word anywhere in the block; the page itself (/applications/medical/) is
        # the only source of "Médical" -- exactly the audit's worked example.
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of stents is performed on a dedicated pilot line.",
            path="main > article",
        )
        fact = _candidate(
            "Example", "https://example.test/applications/medical/", "Medical applications",
            block, page_market=_url_market_hint("https://example.test/applications/medical/", "Medical applications"),
        )
        self.assertIsNotNone(fact)
        self.assertEqual("validated", fact["fact_status"])
        self.assertEqual("page_context", fact["relation_strength"])
        self.assertEqual("Médical", fact["market"])
        self.assertEqual("Stents", fact["component"])
        self.assertEqual("Microperçage", fact["operation"])
        # Weaker signal than an in-block relation -- must never reach a "direct"-level score.
        self.assertLess(fact["confidence"], 0.78)

    def test_page_context_never_overrides_an_in_block_market(self):
        # The block itself states "Batteries" directly -- step 1 (direct) must win over the
        # page-level hint, which would otherwise have supplied "Médical".
        block = ContentBlock(
            heading="Electrodes",
            text="Femtosecond laser texturing of battery electrodes is performed at scale.",
            path="main > article",
        )
        fact = _candidate(
            "Example", "https://example.test/applications/medical/", "Medical applications",
            block, page_market="Médical",
        )
        self.assertIsNotNone(fact)
        self.assertEqual("Batteries", fact["market"])
        self.assertNotEqual("page_context", fact["relation_strength"])

    def test_relation_evidence_page_context_is_the_last_resort(self):
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of stents is performed on a dedicated pilot line.",
            path="main > article",
        )
        strength, text = _relation_evidence(block, page_market="Médical")
        self.assertEqual("page_context", strength)
        self.assertIn("stents", text.lower())

        strength_without_hint, _ = _relation_evidence(block)
        self.assertIsNone(strength_without_hint)


class PartialCandidateTests(unittest.TestCase):
    def test_two_of_three_dimensions_yields_a_partial_fact(self):
        block = ContentBlock(
            heading="Titanium implants",
            text="Femtosecond laser surface texturing of titanium implants in qualified production.",
            path="main > article.card",
        )
        fact = _candidate("MANUTECH USD", "https://www.manutech-usd.fr/applications/", "Applications", block)
        self.assertIsNotNone(fact)
        self.assertEqual("partial", fact["fact_status"])
        self.assertEqual("pending", fact["bucket"])
        self.assertIsNone(fact["market"])
        self.assertLess(fact["confidence"], 0.70)

    def test_only_one_dimension_is_still_rejected(self):
        block = ContentBlock(
            heading="Implants",
            text="Femtosecond laser processing enables advanced implants for demanding programs.",
            path="main > article",
        )
        # "Implants" (component) present; no operation, no market -- only 1 of 3 dimensions.
        self.assertIsNone(_partial_candidate_dims(block, block.text, None))

    def test_all_three_dimensions_defer_to_the_strict_relation_path_not_partial(self):
        # When every dimension IS present, _partial_candidate_dims must not short-circuit the
        # stricter relation validator (it only ever returns for EXACTLY 2 of 3).
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of medical stents is performed on a pilot line.",
            path="main > article",
        )
        self.assertIsNone(_partial_candidate_dims(block, block.text, None))

    def test_publication_role_never_yields_a_partial_fact(self):
        # Same guard _relation_evidence already applies to structured (cross-block) evidence --
        # a bibliography/news block is not a business claim, complete or partial.
        block = ContentBlock(
            heading="Publications",
            h2="Publications",
            text="Optical technologies are at the heart of our activity, texturing surfaces for research.",
            path="main > section.publications",
        )
        fact = _candidate("ALPHANOV", "https://www.alphanov.com/publications", "Publications", block)
        self.assertIsNone(fact)


class MarketSourceSelectionUnchangedHashTests(unittest.TestCase):
    def test_unchanged_pages_are_skipped_changed_and_new_pages_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                actor_id = dbmod.create_actor(
                    "Test Actor", "France", "Test", "https://example.test/", priority=False,
                )

            with dbmod.connect(actors_db) as db:
                db.execute(
                    """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,active,source_score,content_hash,market_extracted_hash)
                       VALUES(?,?,?,?,1,?,?,?)""",
                    (actor_id, "https://example.test/unchanged", "discovered", "application", 50, "hash-a", "hash-a"),
                )
                db.execute(
                    """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,active,source_score,content_hash,market_extracted_hash)
                       VALUES(?,?,?,?,1,?,?,?)""",
                    (actor_id, "https://example.test/changed", "discovered", "application", 50, "hash-new", "hash-old"),
                )
                db.execute(
                    """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,active,source_score)
                       VALUES(?,?,?,?,1,?)""",
                    (actor_id, "https://example.test/never-analyzed", "discovered", "application", 50),
                )

            with patch.object(scrapers, "ACTORS_DB", actors_db):
                selected = {row["url"] for row in _select_market_sources(max_pages=10)}

            self.assertNotIn("https://example.test/unchanged", selected)
            self.assertIn("https://example.test/changed", selected)
            self.assertIn("https://example.test/never-analyzed", selected)


class MarketBudgetTests(unittest.TestCase):
    def test_default_budget_scales_with_active_actor_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                dbmod.create_actor("Actor One", "France", "Test", "https://one.example/", priority=False)
                dbmod.create_actor("Actor Two", "France", "Test", "https://two.example/", priority=False)
                dbmod.create_actor("Actor Three", "France", "Test", "https://three.example/", priority=False)

            captured: dict[str, int] = {}

            def fake_select(max_pages: int = 350) -> list[dict]:
                captured["max_pages"] = max_pages
                return []

            with (
                patch.object(scrapers, "ACTORS_DB", actors_db),
                patch.object(scrapers, "MARKET_DB", market_db),
                patch.object(scrapers, "_select_market_sources", fake_select),
            ):
                scrapers.scrape_market()

            self.assertEqual(MARKET_PAGES_PER_ACTOR * 3, captured["max_pages"])


class ComponentsLexiconExpansionTests(unittest.TestCase):
    def test_new_component_terms_are_matched(self):
        self.assertEqual("Vias traversants (TSV)", _match_label("through-silicon via etching for interposers", COMPONENTS))
        self.assertEqual("Séparateurs de batteries", _match_label("battery separator production for cell manufacturers", COMPONENTS))
        self.assertEqual("Cellules photovoltaïques", _match_label("solar cell texturing for higher efficiency", COMPONENTS))
        self.assertEqual("Boîtiers de montres", _match_label("watch case finishing for luxury clients", COMPONENTS))

    def test_doe_requires_a_laser_or_optics_context(self):
        self.assertIsNone(_match_label("The DOE announced new funding for the program.", COMPONENTS))
        self.assertEqual(
            "Éléments optiques diffractifs (DOE)",
            _match_label("Femtosecond laser fabrication of a diffractive optical element (DOE).", COMPONENTS),
        )


class EvidenceReviewQueueTests(unittest.TestCase):
    def _seed_evidence(self, market_db: Path, *, fact_status: str) -> int:
        stamp = dbmod.utc_now()
        with dbmod.connect(market_db) as db:
            cursor = db.execute(
                """INSERT INTO evidence(
                       actor_name,bucket,market,component,operation,industrial_stage,source_url,source_title,
                       quote,source_group,fingerprint,fact_key,application_key,evidence_kind,review_status,
                       created_at,updated_at,fact_status,last_seen_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'market_application',?,?,?,?,?)""",
                (
                    "Example", "pending", None, "Stents", "Microperçage", "A confirmer",
                    "https://example.test/x", "Title", "quote text", "grp", f"fp-{fact_status}-{stamp}",
                    f"fk-{fact_status}-{stamp}", f"ak-{fact_status}-{stamp}", "review", stamp, stamp,
                    fact_status, stamp,
                ),
            )
            return int(cursor.lastrowid)

    def test_accept_promotes_to_validated_and_reject_keeps_fact_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                accepted_id = self._seed_evidence(market_db, fact_status="partial")
                rejected_id = self._seed_evidence(market_db, fact_status="review")

                result = dbmod.accept_evidence_review(accepted_id)
                self.assertEqual("validated", result["fact_status"])
                dbmod.reject_evidence_review(rejected_id)

                with dbmod.connect(market_db) as db:
                    accepted_row = db.execute("SELECT fact_status,review_status FROM evidence WHERE id=?", (accepted_id,)).fetchone()
                    rejected_row = db.execute("SELECT fact_status,review_status FROM evidence WHERE id=?", (rejected_id,)).fetchone()
                self.assertEqual(("validated", "accepted"), (accepted_row["fact_status"], accepted_row["review_status"]))
                # fact_status is left as-is for audit trail; only review_status flips.
                self.assertEqual(("review", "rejected"), (rejected_row["fact_status"], rejected_row["review_status"]))

                with self.assertRaises(ValueError):
                    dbmod.accept_evidence_review(999999)
                with self.assertRaises(ValueError):
                    dbmod.accept_evidence_review(accepted_id)  # already validated

    def test_market_review_endpoint_lists_only_pending_review_and_partial_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                partial_id = self._seed_evidence(market_db, fact_status="partial")
                self._seed_evidence(market_db, fact_status="review")

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(dbmod, "MARKET_DB", market_db),
            ):
                pending = appmod.market_review()
                self.assertEqual(2, len(pending))
                self.assertEqual({"partial", "review"}, {row["fact_status"] for row in pending})

                appmod.accept_market_review(partial_id)

                pending_after = appmod.market_review()
                self.assertEqual(1, len(pending_after))
                accepted = appmod.market_review(status="accepted")
                self.assertEqual(1, len(accepted))


class NoiseBlockTests(unittest.TestCase):
    # Audit v8 §2.3/§2.6 (priority 6): _is_noise_block let bibliography citations and menu
    # debris through once the mandatory triplet was relaxed -- these reproduce the audit's own
    # examples (a DOI reference list at Fraunhofer ILT, "Navigation ..." at Kirana, "Read
    # more..." at Workshop of Photonics).

    def test_doi_reference_list_entry_is_noise(self):
        block = ContentBlock(
            heading="Publications",
            text="https://doi.org/10.2961/jlmn.2015.02.0022 Fornaroli, C., Holtkamp, J., Gillner, A. et al.",
            path="main > section.publications",
        )
        self.assertTrue(_is_noise_block(block))

    def test_bare_navigation_breadcrumb_is_noise(self):
        block = ContentBlock(heading="", text="Navigation R&D Femtosecond Micromachining", path="nav.menu")
        self.assertTrue(_is_noise_block(block))

    def test_bare_read_more_link_is_noise(self):
        block = ContentBlock(heading="", text="Read more", path="a.read-more")
        self.assertTrue(_is_noise_block(block))

    def test_read_more_inside_a_real_sentence_is_not_noise(self):
        # The menu-fragment terms are only trusted combined with "short + no sentence
        # punctuation" -- the same phrase inside actual prose must not be flagged.
        block = ContentBlock(
            heading="Applications",
            text=(
                "Our femtosecond laser micromachining process delivers sub-micron precision for "
                "medical stents in volume production; read more about our qualified processes below."
            ),
            path="main > article",
        )
        self.assertFalse(_is_noise_block(block))

    def test_legitimate_short_claim_without_punctuation_is_not_noise(self):
        block = ContentBlock(heading="", text="Femtosecond laser micromachining for medical implants", path="main > h2")
        self.assertFalse(_is_noise_block(block))


if __name__ == "__main__":
    unittest.main()
