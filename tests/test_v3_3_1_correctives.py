from __future__ import annotations

import itertools
import sqlite3
import unittest
from collections import defaultdict

from hybrid import ContentBlock
from scrapers import (
    _candidate,
    _detect_maturity,
    _offer_candidates,
    _pop_crawl_item,
    _push_crawl_item,
    _upsert_market_candidate,
)
from site_profiles import get_site_profile, seed_urls


class CoverageFirstTests(unittest.TestCase):
    def test_second_project_does_not_monopolise_uncovered_families(self):
        profile = get_site_profile({"name": "ALPHANOV", "official_url": "https://www.alphanov.com"})
        heap = []
        queued = {}
        counter = itertools.count()
        for item in (
            {"url": "https://x.test/project-1", "page_type": "project", "score": 135, "depth": 1},
            {"url": "https://x.test/project-2", "page_type": "project", "score": 134, "depth": 1},
            {"url": "https://x.test/service", "page_type": "service", "score": 90, "depth": 1},
            {"url": "https://x.test/technology", "page_type": "technology", "score": 86, "depth": 1},
            {"url": "https://x.test/application", "page_type": "application", "score": 105, "depth": 1},
        ):
            _push_crawl_item(heap, queued, counter, item)

        visited = set()
        visited_by_type = defaultdict(int)
        first = _pop_crawl_item(heap, queued, visited, visited_by_type, profile)
        self.assertIsNotNone(first)
        first_item, _ = first
        visited.add(first_item["url"])
        visited_by_type[first_item["page_type"]] += 1

        second = _pop_crawl_item(heap, queued, visited, visited_by_type, profile)
        self.assertIsNotNone(second)
        second_item, _ = second
        # If project was consumed first, another project must lose its coverage boost.
        if first_item["page_type"] == "project":
            self.assertNotEqual(second_item["page_type"], "project")


class AlphanovProfileTests(unittest.TestCase):
    def test_profile_seeds_products_services_and_application_sectors(self):
        profile = get_site_profile({"name": "ALPHANOV", "official_url": "https://www.alphanov.com"})
        self.assertIn("/produits-et-services/", profile["priority_paths"])
        self.assertIn("/en/products-and-services/", profile["priority_paths"])
        self.assertIn("/secteurs-applicatifs/", profile["priority_paths"])
        self.assertIn("/en/application-sectors/", profile["priority_paths"])
        urls = seed_urls(profile, "https://www.alphanov.com")
        self.assertIn("https://www.alphanov.com/produits-et-services/procedes-laser", urls)
        self.assertIn("https://www.alphanov.com/en/products-and-services/laser-machining-and-micro-machining", urls)
        self.assertIn("https://www.alphanov.com/secteurs-applicatifs/lasers", urls)
        self.assertIn("https://www.alphanov.com/en/application-sectors/lasers", urls)


class StrictMarketVsOfferTests(unittest.TestCase):
    def test_process_or_material_never_becomes_market_component(self):
        block = ContentBlock(
            heading="Laser processes",
            h1="Laser processes",
            h2="Transparent materials",
            h3="",
            path="main > section",
            text=(
                "Our femtosecond laser processing of fused silica uses selective laser etching and high throughput "
                "processing for industrial customers."
            ),
        )
        market_fact = _candidate("ALPHANOV", "https://www.alphanov.com/en/products-and-services/test", "Laser processes", block)
        self.assertIsNone(market_fact)
        offers = _offer_candidates(
            "ALPHANOV", "https://www.alphanov.com/en/products-and-services/test", "Laser processes", block, page_type="service"
        )
        self.assertTrue(offers)
        self.assertTrue(any(item["capability"] == "SLE" for item in offers))

    def test_rich_service_block_returns_multiple_capabilities(self):
        block = ContentBlock(
            heading="Laser machining and micro-machining",
            h1="Laser processes",
            h2="Laser machining and micro-machining",
            h3="",
            path="main > section",
            text=(
                "We provide femtosecond laser micromachining, laser drilling, surface texturing and selective ablation "
                "as technical services and contract manufacturing."
            ),
        )
        offers = _offer_candidates(
            "ALPHANOV", "https://www.alphanov.com/en/products-and-services/laser-machining", "Laser machining", block, page_type="service"
        )
        capabilities = {item["capability"] for item in offers}
        self.assertIn("Micro-usinage", capabilities)
        self.assertIn("Microperçage", capabilities)
        self.assertIn("Texturation", capabilities)
        self.assertIn("Ablation", capabilities)


class MarketInferenceGuardTests(unittest.TestCase):
    def test_component_alone_never_infers_a_market_without_explicit_text(self):
        # MARKET_INFERENCE (component -> market) only disambiguates when an explicit market
        # term is ALSO present in the text (see _candidate's "inferred_market in
        # explicit_markets" check); it must never manufacture a market fact from a component
        # alone, or "stent" would silently prove a Medical-market claim with no textual
        # evidence of "medical" anywhere on the page (audit P1: no market inference).
        block = ContentBlock(
            heading="Precision components",
            h1="Precision components",
            h2="",
            h3="",
            path="main > section",
            text="Our femtosecond laser drilling process produces precision stents for demanding manufacturing programs.",
        )
        fact = _candidate("ACME", "https://example.test/components", "Precision components", block)
        self.assertIsNone(fact)


class LocalSynonymMaturityTests(unittest.TestCase):
    def test_german_italian_spanish_contract_manufacturing_terms_read_as_production(self):
        # MeKo, HAILTEC and Kirana describe contract manufacturing in their own language
        # (Lohnfertigung, Auftragsfertigung, lavorazione conto terzi) rather than English --
        # a maturity detector that only knows "contract manufacturing" misses them entirely.
        for text in (
            "Wir bieten Lohnfertigung fuer medizinische Bauteile.",
            "Spezialisiert auf Auftragsfertigung von Praezisionsteilen.",
            "Offriamo lavorazione conto terzi per componenti ottici.",
            "Ofrecemos fabricación por contrato de piezas de precisión.",
        ):
            bucket, stage = _detect_maturity(text)
            self.assertEqual("existing", bucket, msg=text)
            self.assertEqual("Production", stage, msg=text)


class DuplicateProofDedupTests(unittest.TestCase):
    def test_same_quote_from_two_blocks_gets_the_same_source_fingerprint(self):
        # Two different DOM blocks (different path/heading) can expose the exact same
        # sentence -- e.g. a page that repeats a paragraph in a mobile and desktop layout.
        # The fingerprint must key on the fact + url + quote, not the block, so the second
        # occurrence collapses onto the first via INSERT OR IGNORE instead of showing as a
        # duplicate proof in the UI.
        text = (
            "We provide femtosecond laser micromachining and laser texturing as a technical "
            "service for industrial customers."
        )
        block_a = ContentBlock(heading="Services", h1="Services", h2="", h3="", path="main > section:nth-child(1)", text=text)
        block_b = ContentBlock(heading="Services (mobile)", h1="Services", h2="Mobile", h3="", path="aside > section:nth-child(3)", text=text)
        url = "https://www.example.test/en/services"
        offers_a = {item["capability"]: item for item in _offer_candidates("ACME", url, "Services", block_a, page_type="service")}
        offers_b = {item["capability"]: item for item in _offer_candidates("ACME", url, "Services", block_b, page_type="service")}
        self.assertTrue(offers_a)
        shared = set(offers_a) & set(offers_b)
        self.assertTrue(shared)
        for capability in shared:
            self.assertEqual(offers_a[capability]["source_fingerprint"], offers_b[capability]["source_fingerprint"])


class MultilingualFactTests(unittest.TestCase):
    def _db(self):
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
                field_confidence REAL, relation_strength TEXT, relation_evidence TEXT, source_role TEXT, fingerprint TEXT UNIQUE, created_at TEXT
            );
            """
        )
        return conn

    def test_fr_and_en_sources_create_one_fact_two_proofs(self):
        conn = self._db()
        common = {
            "kind": "market_application",
            "actor": "ALPHANOV",
            "bucket": "radar",
            "market": "Batteries",
            "component": "Électrodes de batteries",
            "operation": "Microdécoupe",
            "process": None,
            "material": None,
            "performance": None,
            "maturity": "Pré-industrialisation",
            "stage": "Ligne pilote",
            "group": "alphanov|radar|batteries|electrodes-de-batteries|microdecoupe",
            "fact_key": "alphanov|radar|batteries|electrodes-de-batteries|microdecoupe",
            "fingerprint": "fact-fingerprint",
            "block_heading": "Femtocell",
            "block_path": "main > article",
            "mode": "block-rules",
            "confidence": 0.9,
        }
        fr = dict(common, url="https://www.alphanov.com/fr/projet/femtocell", title="Femtocell FR", quote="Laser femtoseconde pour électrodes de batteries.", source_fingerprint="src-fr")
        en = dict(common, url="https://www.alphanov.com/en/project/femtocell", title="Femtocell EN", quote="Femtosecond laser for battery electrodes.", source_fingerprint="src-en")
        _upsert_market_candidate(conn, fr)
        _upsert_market_candidate(conn, en)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_sources").fetchone()[0], 2)
        languages = {row[0] for row in conn.execute("SELECT language FROM evidence_sources")}
        self.assertEqual(languages, {"fr", "en"})


if __name__ == "__main__":
    unittest.main()
