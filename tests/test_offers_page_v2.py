"""Page Offres & capacités, suites de l'audit du 29/09/2026 : procédés débloqués, doublons
Capacité/Service fusionnés, couverture par acteur, offres nommées (named_offers.py)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as appmod
import db as dbmod
import named_offers as nomod
from named_offers import extract_named_offers
from scrapers import _offer_review_reasons


def _block(heading: str, text: str, h1: str = "") -> dict:
    return {"heading": heading, "h1": h1, "text": text, "path": "main > section"}


class ProcessOnlyOfferTests(unittest.TestCase):
    def test_process_without_operation_is_no_longer_operation_null(self):
        candidate = {"quote": "We offer selective laser etching of fused silica.", "page_type": "service",
                     "url": "https://example.test/sle", "operation": None, "process": "SLE",
                     "official_url": "https://example.test/"}
        self.assertEqual([], _offer_review_reasons(candidate, 1))

    def test_neither_operation_nor_process_stays_operation_null(self):
        candidate = {"quote": "We offer femtosecond laser services.", "page_type": "service",
                     "url": "https://example.test/s", "operation": None, "process": None,
                     "official_url": "https://example.test/"}
        self.assertIn("operation_null", _offer_review_reasons(candidate, 1))


class NamedOfferExtractionTests(unittest.TestCase):
    OFFICIAL = "https://www.kmlt.test/"

    def extract(self, blocks, url="https://kmlt.test/en/services/laser-micro-cutting/", title=None, page_type="service"):
        return {row["name"]: row for row in extract_named_offers("KMLT", self.OFFICIAL, url, page_type, title, blocks)}

    def test_page_h1_and_operation_headings_are_named_offers(self):
        found = self.extract([
            _block("Laser Micro-Cutting", "Laser Micro-Cutting Our femtosecond laser cuts stents. More text.", h1="Laser Micro-Cutting"),
            _block("Laser Drilling", "Laser Drilling Precise holes down to 10 µm with ultrashort pulses."),
            _block("Advantages", "No burr, no heat affected zone."),
            _block("What is laser micro-cutting?", "It is a process."),
            _block("Life Sciences", "Microfluidic chips for diagnostics."),
        ])
        self.assertEqual({"Laser Micro-Cutting", "Laser Drilling"}, set(found))
        self.assertEqual("accepted", found["Laser Drilling"]["review_status"])
        # Description verbatim, sans la répétition de l'intertitre, coupée à la première phrase.
        self.assertEqual("Our femtosecond laser cuts stents.", found["Laser Micro-Cutting"]["description"])

    def test_product_names_and_model_codes_but_not_standards(self):
        found = self.extract([
            _block("MM-500 (Medical)", "Compact femtosecond platform."),
            _block("ISO 13485:2016 and ISO 9001:2015", "Certified quality system for ultrafast laser parts."),
        ], url="https://kmlt.test/products/mm", title="GL.evo laser machine | KMLT", page_type="product")
        self.assertIn("MM-500 (Medical)", found)
        self.assertIn("GL.evo laser machine", found)
        self.assertNotIn("ISO 13485:2016 and ISO 9001:2015", found)

    def test_slogans_fragments_and_listicles_are_not_names(self):
        found = self.extract([
            _block("Perfect markings thanks to laser marking and laser engraving", "Femtosecond marking."),
            _block("athermal, ultrafast laser drilling", "Femtosecond drilling."),
            _block("7 Benefits of Femtosecond Fiber Lasers", "Femtosecond lasers are great."),
        ], title="Services | KMLT")
        self.assertEqual(set(), set(found))

    def test_documents_statistics_and_sentences_are_not_names(self):
        found = self.extract([
            _block("BROCHURE LASER", "Femtosecond laser brochure."),
            _block("Fine laser cutting: OUR STATISTICS", "Femtosecond cutting."),
            _block("More about our ultra-small drilling processes.", "Femtosecond drilling."),
            _block("Glass laser cutting examples", "Femtosecond glass cutting."),
        ], title="Services | KMLT")
        self.assertEqual(set(), set(found))

    def test_page_outside_ultrafast_scope_goes_to_review(self):
        found = self.extract([_block("Laser Drilling", "Laser Drilling Nanosecond fibre laser drilling of steel.", h1="Laser Drilling")])
        self.assertEqual("review", found["Laser Drilling"]["review_status"])

    def test_other_domain_and_non_offer_page_types_are_ignored(self):
        blocks = [_block("Laser Drilling", "Femtosecond laser drilling.", h1="Laser Drilling")]
        self.assertEqual({}, self.extract(blocks, url="https://partner.test/drilling"))
        self.assertEqual({}, self.extract(blocks, page_type="news"))


class OffersApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.actors_db, self.market_db = tmp / "actors.db", tmp / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", self.actors_db),
            patch.object(dbmod, "MARKET_DB", self.market_db),
            patch.object(dbmod, "TECH_DB", tmp / "technology.db"),
        ):
            dbmod.init_databases()
        with dbmod.connect(self.actors_db) as db:
            db.execute("DELETE FROM actors")
        with patch.object(dbmod, "ACTORS_DB", self.actors_db):
            self.covered = dbmod.create_actor("Covered", "FR", "Test", "https://covered.test/")
            self.missing = dbmod.create_actor("Missing", "FR", "Test", "https://missing.test/")
        with dbmod.connect(self.actors_db) as db:
            db.execute("UPDATE actors SET review_status='verified'")
            db.execute(
                "INSERT INTO actor_sources(actor_id,url,source_kind,page_type,last_checked_at) VALUES(?,?,?,?,?)",
                (self.missing, "https://missing.test/services", "sitemap", "service", None),
            )
        with dbmod.connect(self.market_db) as db:
            for i, (offer_type, material, url) in enumerate([
                ("capability", None, "https://covered.test/a"),
                ("service", "Verre", "https://covered.test/b"),
            ]):
                offer_id = db.execute(
                    """INSERT INTO offers(actor_name,offer_type,capability,operation,material,industrial_stage,page_type,
                           source_url,quote,fact_key,fingerprint,review_status,created_at,updated_at)
                       VALUES('Covered',?, 'Microperçage','Microperçage',?,'Maturité industrielle non déterminée',?,?,
                              'We drill.',?,?,'accepted','2026-09-29','2026-09-29')""",
                    (offer_type, material, offer_type, url, f"k{i}", f"f{i}"),
                ).lastrowid
                db.execute(
                    "INSERT INTO offer_sources(offer_id,source_url,quote,fingerprint,created_at) VALUES(?,?,?,?,?)",
                    (offer_id, url, "We drill.", f"s{i}", "2026-09-29"),
                )
            db.execute(
                """INSERT INTO offers(actor_name,offer_type,capability,operation,page_type,source_url,quote,fact_key,
                       fingerprint,review_status,created_at,updated_at)
                   VALUES('Missing','service','Soudage','Soudage','service','https://missing.test/w','Weld.','m','m',
                          'review','2026-09-29','2026-09-29')""",
            )
        self.patches = [
            patch.object(appmod, "ACTORS_DB", self.actors_db),
            patch.object(appmod, "MARKET_DB", self.market_db),
            patch.object(nomod, "ACTORS_DB", self.actors_db),
            patch.object(nomod, "MARKET_DB", self.market_db),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_same_capability_on_service_and_capability_pages_is_one_row(self):
        rows = appmod.offers()
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("service", row["offer_type"])
        self.assertEqual(["capability", "service"], row["offer_types"])
        self.assertEqual("Verre", row["material"])
        self.assertEqual(2, row["proofs"])
        # Les preuves de la ligne fusionnée incluent celles des deux offres d'origine.
        self.assertEqual(2, len(appmod.offer_proofs(row["id"])))

    def test_coverage_explains_why_an_actor_has_nothing_on_screen(self):
        by_name = {row["name"]: row for row in appmod.offers_coverage()}
        self.assertEqual(2, by_name["Covered"]["offers_accepted"])
        self.assertEqual(0, by_name["Missing"]["offers_accepted"])
        self.assertEqual(1, by_name["Missing"]["offers_in_review"])
        self.assertEqual((1, 0), (by_name["Missing"]["offer_pages"], by_name["Missing"]["offer_pages_fetched"]))

    def test_named_offers_collector_writes_and_endpoint_serves_accepted_only(self):
        blocks = [
            _block("Laser Drilling", "Laser Drilling Femtosecond laser drilling of glass.", h1="Laser Drilling"),
            _block("Customized solutions", "We adapt to you."),
        ]
        with dbmod.connect(self.actors_db) as db:
            db.execute(
                """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,last_http_status,last_title,blocks_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.covered, "https://covered.test/drilling", "sitemap", "service", 200, "Drilling", json.dumps(blocks)),
            )
        first = nomod.collect_named_offers()
        again = nomod.collect_named_offers()
        self.assertEqual(1, first["added"])
        self.assertEqual(0, again["added"])
        served = appmod.named_offers()
        self.assertEqual(["Laser Drilling"], [row["name"] for row in served])
        self.assertEqual(["Microperçage"], served[0]["operations"])
        self.assertEqual(1, {r["name"]: r for r in appmod.offers_coverage()}["Covered"]["named_offers"])

    def test_an_offer_no_longer_extracted_goes_back_to_review_but_never_over_a_human(self):
        blocks = [_block("Laser Drilling", "Laser Drilling Femtosecond laser drilling of glass.", h1="Laser Drilling")]
        with dbmod.connect(self.actors_db) as db:
            source_id = db.execute(
                """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,last_http_status,last_title,blocks_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.covered, "https://covered.test/drilling", "sitemap", "service", 200, "Drilling", json.dumps(blocks)),
            ).lastrowid
        nomod.collect_named_offers()
        with dbmod.connect(self.market_db) as db:
            db.execute("UPDATE named_offers SET last_seen_at='2026-01-01T00:00:00+00:00'")
        with dbmod.connect(self.actors_db) as db:
            db.execute("UPDATE actor_sources SET blocks_json='[]' WHERE id=?", (source_id,))
        report = nomod.collect_named_offers()
        self.assertEqual(1, report["retired_to_review"])
        self.assertEqual([], appmod.named_offers())

        with dbmod.connect(self.market_db) as db:
            db.execute("UPDATE named_offers SET review_status='accepted',reviewed_at='2026-09-29',last_seen_at='2026-01-01'")
        self.assertEqual(0, nomod.collect_named_offers()["retired_to_review"])


if __name__ == "__main__":
    unittest.main()
