"""Une offre déclarée par l'acteur sur sa propre page service/capability/product/equipment
n'exige plus de seconde URL (29/09/2026) -- et rejudge_offers.py applique la règle courante
aux offres déjà en base, dans les deux sens."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import rejudge_offers
from hybrid import ContentBlock
from scrapers import _offer_candidates, _offer_review_reasons, _upsert_offer_candidate

BLOCK = ContentBlock(
    heading="Services",
    text="We provide femtosecond laser drilling services for medical customers.",
    path="main > article",
)


def _offer(url: str, page_type: str, official_url: str | None) -> dict:
    offer = _offer_candidates("Example", url, "Services", BLOCK, page_type=page_type)[0]
    offer["official_url"] = official_url
    return offer


class FirstPartyOfferRuleTests(unittest.TestCase):
    def test_own_service_page_needs_no_second_source(self):
        offer = _offer("https://www.example.test/services", "service", "https://example.test/")
        self.assertEqual([], _offer_review_reasons(offer, 1))

    def test_subdomain_of_official_site_counts_as_first_party(self):
        offer = _offer("https://shop.example.test/capabilities", "capability", "https://www.example.test")
        self.assertEqual([], _offer_review_reasons(offer, 1))

    def test_parent_company_domain_is_not_first_party(self):
        offer = _offer("https://lpkf.com/services", "service", "https://lasermicronics.lpkf.com/")
        self.assertEqual(["single_unconfirmed_source"], _offer_review_reasons(offer, 1))

    def test_other_domain_still_needs_a_second_source(self):
        offer = _offer("https://partner.test/services", "service", "https://example.test/")
        self.assertEqual(["single_unconfirmed_source"], _offer_review_reasons(offer, 1))

    def test_technology_page_still_needs_a_second_source(self):
        offer = _offer("https://example.test/technology", "technology", "https://example.test/")
        self.assertEqual(["single_unconfirmed_source"], _offer_review_reasons(offer, 1))

    def test_unknown_official_url_keeps_the_old_rule(self):
        offer = _offer("https://example.test/services", "service", None)
        self.assertEqual(["single_unconfirmed_source"], _offer_review_reasons(offer, 1))

    def test_other_reasons_still_apply_on_a_first_party_page(self):
        block = ContentBlock(heading="Services", text="Femtosecond laser drilling services.", path="main > a")
        offer = _offer_candidates("Example", "https://example.test/services", "Services", block, page_type="service")[0]
        offer["official_url"] = "https://example.test/"
        self.assertEqual(["no_predicate"], _offer_review_reasons(offer, 1))


class RejudgeOffersTests(unittest.TestCase):
    def test_rejudge_moves_offers_both_ways_and_never_touches_human_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db = Path(tmp) / "actors.db", Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actors(name,country,role,priority,official_url,updated_at) VALUES(?,?,?,?,?,?)",
                    ("Example", "FR", "competitor", 0, "https://example.test/", "2026-09-29"),
                )

            own_page = _offer("https://example.test/services", "service", None)
            tech_page = _offer_candidates(
                "Example", "https://example.test/tech", "Tech", ContentBlock(
                    heading="Tech", text="We perform femtosecond laser cutting of glass.", path="main > b",
                ), page_type="technology",
            )[0]
            human = _offer_candidates(
                "Example", "https://example.test/services2", "Services", ContentBlock(
                    heading="Services", text="We offer femtosecond laser welding of glass.", path="main > c",
                ), page_type="service",
            )[0]
            with dbmod.connect(market_db) as db:
                for offer in (own_page, tech_page, human):
                    _upsert_offer_candidate(db, offer)
                # Ancienne règle : une offre accepted d'office qu'aucun motif actuel ne justifie plus.
                db.execute("UPDATE offers SET review_status='accepted' WHERE fact_key=?", (tech_page["fact_key"],))
                db.execute(
                    "UPDATE offers SET review_status='rejected',reviewed_at='2026-09-01',reviewed_by='analyst' WHERE fact_key=?",
                    (human["fact_key"],),
                )

            with patch.object(rejudge_offers, "ACTORS_DB", actors_db), patch.object(rejudge_offers, "MARKET_DB", market_db):
                dry = rejudge_offers.rejudge_offers(dry_run=True)
                with dbmod.connect(market_db) as db:
                    unchanged = db.execute("SELECT review_status FROM offers WHERE fact_key=?", (own_page["fact_key"],)).fetchone()[0]
                report = rejudge_offers.rejudge_offers()

            self.assertEqual("review", unchanged)
            self.assertEqual({"review->accepted": 1, "accepted->review": 1}, dry["transitions"])
            self.assertEqual(dry["transitions"], report["transitions"])
            with dbmod.connect(market_db) as db:
                status = dict(db.execute("SELECT fact_key,review_status FROM offers").fetchall())
            self.assertEqual("accepted", status[own_page["fact_key"]])
            self.assertEqual("review", status[tech_page["fact_key"]])
            self.assertEqual("rejected", status[human["fact_key"]])


if __name__ == "__main__":
    unittest.main()
