"""Tests pour le chantier 4 (fiabiliser la preuve) : séparation verbatim/reformulation
(is_verbatim) et dates individuelles (source_date), côté scrapers.py/db.py.

Le côté hybrid.py (extraction de la date depuis le HTML/PDF) est couvert dans
tests/test_hybrid.py::PublishedDateExtractionTests.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from hybrid import ContentBlock
from scrapers import _candidate, _offer_candidates, _upsert_market_candidate, _upsert_offer_candidate


class CandidateProvenanceTests(unittest.TestCase):
    def test_candidate_is_always_verbatim_and_carries_the_given_source_date(self):
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of medical stents is performed on a pilot line.",
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/medical", "Medical", block, source_date="2024-05-01")
        self.assertIs(True, fact["is_verbatim"])
        self.assertEqual("2024-05-01", fact["source_date"])
        self.assertIn(fact["quote"], block.text)  # a genuine substring, never a paraphrase

    def test_candidate_source_date_defaults_to_none_when_not_given(self):
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of medical stents is performed on a pilot line.",
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/medical", "Medical", block)
        self.assertIsNone(fact["source_date"])

    def test_offer_candidates_are_verbatim_and_carry_source_date(self):
        block = ContentBlock(
            heading="Services",
            text="We provide femtosecond laser micromachining services for industrial customers.",
            path="main > article",
        )
        offers = _offer_candidates("Example", "https://example.test/services", "Services", block, page_type="service", source_date="2024-02-20")
        self.assertTrue(offers)
        for offer in offers:
            self.assertIs(True, offer["is_verbatim"])
            self.assertEqual("2024-02-20", offer["source_date"])


class UpsertPersistenceTests(unittest.TestCase):
    def _fresh_market_db(self, tmp: Path) -> Path:
        market_db = Path(tmp) / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
            patch.object(dbmod, "MARKET_DB", market_db),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
        return market_db

    def test_market_candidate_persists_source_date_and_is_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(
                heading="Stents", text="Femtosecond laser drilling of medical stents on a pilot line.", path="main > a",
            )
            candidate = _candidate("Example", "https://example.test/medical", "Medical", block, source_date="2024-05-01")
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, candidate)
                row = db.execute("SELECT id,source_date,is_verbatim FROM evidence WHERE fact_key=?", (candidate["fact_key"],)).fetchone()
                source_row = db.execute("SELECT source_date,is_verbatim FROM evidence_sources WHERE evidence_id=?", (row["id"],)).fetchone()
            self.assertEqual("2024-05-01", row["source_date"])
            self.assertEqual(1, row["is_verbatim"])
            self.assertEqual("2024-05-01", source_row["source_date"])
            self.assertEqual(1, source_row["is_verbatim"])

    def test_market_candidate_update_keeps_existing_date_when_new_one_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(
                heading="Stents", text="Femtosecond laser drilling of medical stents on a pilot line.", path="main > a",
            )
            first = _candidate("Example", "https://example.test/medical", "Medical", block, source_date="2024-05-01")
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, first)

            # A later pass with no source_date (e.g. cached blocks, no observation stamp
            # threaded through) and higher confidence must not blank out a date we already have.
            second = dict(first)
            second["source_date"] = None
            second["confidence"] = min(0.98, first["confidence"] + 0.05)
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, second)
                row = db.execute("SELECT source_date FROM evidence WHERE fact_key=?", (first["fact_key"],)).fetchone()
            self.assertEqual("2024-05-01", row["source_date"])

    def test_offer_candidate_persists_source_date_and_is_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(
                heading="Services", text="We provide femtosecond laser micromachining services for customers.", path="main > a",
            )
            offer = _offer_candidates("Example", "https://example.test/services", "Services", block, page_type="service", source_date="2024-02-20")[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, offer)
                row = db.execute("SELECT source_date,is_verbatim FROM offers WHERE fact_key=?", (offer["fact_key"],)).fetchone()
            self.assertEqual("2024-02-20", row["source_date"])
            self.assertEqual(1, row["is_verbatim"])


class LegacySeedBackfillTests(unittest.TestCase):
    def test_manually_entered_rows_are_backfilled_as_not_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                stamp = dbmod.utc_now()
                with dbmod.connect(market_db) as db:
                    # Distinct market/component so the two rows are genuinely different facts
                    # (not just different rows for the same fact, which the dedup migration
                    # further down would legitimately merge into one).
                    # extraction_mode IS NULL: exactly the audit's signal for a hand-entered row.
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,source_url,quote,source_group,fingerprint,
                               review_status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        ("Example", "existing", "Médical", "Stents", "https://example.test/", "Une reformulation en français.", "grp", "manual-fp", "accepted", stamp, stamp),
                    )
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,source_url,quote,source_group,fingerprint,
                               review_status,created_at,updated_at,extraction_mode
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("Example", "existing", "Batteries", "Électrodes", "https://example.test/", "a verbatim excerpt", "grp2", "scraped-fp", "accepted", stamp, stamp, "block-rules"),
                    )
                # Re-running init_databases() (as the app does on every startup) must not
                # re-flip a row that was already correctly backfilled, nor touch scraped rows.
                dbmod.init_databases()

            with dbmod.connect(market_db) as db:
                manual = db.execute("SELECT is_verbatim FROM evidence WHERE fingerprint='manual-fp'").fetchone()
                scraped = db.execute("SELECT is_verbatim FROM evidence WHERE fingerprint='scraped-fp'").fetchone()
            self.assertEqual(0, manual["is_verbatim"])
            self.assertEqual(1, scraped["is_verbatim"])


if __name__ == "__main__":
    unittest.main()
