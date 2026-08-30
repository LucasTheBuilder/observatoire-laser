from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app as appmod
import db as dbmod
from scrapers import _stored_blocks


class MonthlyTrackingTests(unittest.TestCase):
    def test_monthly_reports_recent_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actors_db = root / "actors.db"
            market_db = root / "market.db"
            tech_db = root / "technology.db"

            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()

            stamp = dbmod.utc_now()
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "UPDATE actor_sources SET last_changed_at=? WHERE id=(SELECT MIN(id) FROM actor_sources)",
                    (stamp,),
                )

            # Revue chronologie du 30/08/2026 : un signal ne compte comme "new_*" (fresh) que si
            # sa date de publication est CONFIRMÉE ET récente (date_confidence='published',
            # is_backfill=0) -- pas seulement parce que la ligne vient d'être insérée. Ces trois
            # lignes simulent une page réellement publiée aujourd'hui, avec une vraie date
            # extraite (voir hybrid._extract_published_date) : le cas positif que ce test vérifiait
            # déjà avant ce correctif.
            today = stamp[:10]
            with dbmod.connect(market_db) as db:
                db.execute(
                    """INSERT INTO evidence(
                           actor_name,bucket,market,component,operation,industrial_stage,
                           source_url,source_title,quote,source_group,fingerprint,review_status,
                           created_at,updated_at,fact_key,evidence_kind,fact_status,last_seen_at,
                           source_date,date_confidence,is_backfill
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "ALPHANOV", "radar", "Batteries", "Électrodes de batteries", "Texturation",
                        "Prototype", "https://example.test/market", "Test market", "quote",
                        "test-group", "test-fingerprint", "accepted", stamp, stamp,
                        "test-fact-key", "market_application", "validated", stamp,
                        today, "published", 0,
                    ),
                )
                db.execute(
                    """INSERT INTO offers(
                           actor_name,offer_type,capability,source_url,source_title,quote,
                           fact_key,fingerprint,review_status,created_at,updated_at,last_seen_at,
                           source_date,date_confidence,is_backfill
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "ALPHANOV", "service", "Texturation", "https://example.test/offer",
                        "Test offer", "quote", "offer-key", "offer-fingerprint", "accepted",
                        stamp, stamp, stamp,
                        today, "published", 0,
                    ),
                )

            with dbmod.connect(tech_db) as db:
                db.execute(
                    """INSERT INTO documents(
                           document_type,title,source_url,fingerprint,created_at,last_seen_at,
                           published_at,date_confidence,is_backfill
                       ) VALUES('publication',?,?,?,?,?,?,?,?)""",
                    (
                        "Recent paper", "https://doi.org/10.test/example", "doc-fingerprint", stamp, stamp,
                        today, "published", 0,
                    ),
                )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                result = appmod.monthly(30)

            self.assertGreaterEqual(result["counts"]["changed_sources"], 1)
            self.assertEqual(result["counts"]["new_market"], 1)
            self.assertEqual(result["counts"]["new_offers"], 1)
            self.assertEqual(result["counts"]["technology"], 1)
            self.assertEqual(result["counts"]["new_market_backfill"], 0)
            self.assertEqual(result["counts"]["new_market_undated"], 0)

    def test_monthly_excludes_backfill_and_undated_from_new_counts(self):
        """Regression test for the chronology bug the 30/08/2026 audit flagged: a row inserted
        today (created_at=now) whose real content is old, or whose date is unknown, must NOT be
        counted as a fresh signal -- only surfaced separately as backfill/undated."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actors_db = root / "actors.db"
            market_db = root / "market.db"
            tech_db = root / "technology.db"

            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()

            stamp = dbmod.utc_now()

            with dbmod.connect(market_db) as db:
                # Case A: a page crawled for the very first time today, but its confirmed
                # publication date is from 2019 -- a documentary backfill, not a market move.
                db.execute(
                    """INSERT INTO evidence(
                           actor_name,bucket,market,component,operation,industrial_stage,
                           source_url,source_title,quote,source_group,fingerprint,review_status,
                           created_at,updated_at,fact_key,evidence_kind,fact_status,last_seen_at,
                           source_date,date_confidence,is_backfill
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "IREPA LASER", "existing", "Batteries", "Électrodes de batteries", "Texturation",
                        "Production", "https://example.test/backfill", "Old page", "quote",
                        "backfill-group", "backfill-fingerprint", "accepted", stamp, stamp,
                        "backfill-fact-key", "market_application", "validated", stamp,
                        "2019-03-01", "published", 1,
                    ),
                )
                # Case B: also inserted today, but no reliable publication date was ever found --
                # we know the ROW is new, not whether the FACT is.
                db.execute(
                    """INSERT INTO evidence(
                           actor_name,bucket,market,component,operation,industrial_stage,
                           source_url,source_title,quote,source_group,fingerprint,review_status,
                           created_at,updated_at,fact_key,evidence_kind,fact_status,last_seen_at,
                           source_date,date_confidence,is_backfill
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "IREPA LASER", "radar", "Batteries", "Électrodes de batteries", "Texturation",
                        "Prototype", "https://example.test/undated", "Undated page", "quote",
                        "undated-group", "undated-fingerprint", "accepted", stamp, stamp,
                        "undated-fact-key", "market_application", "validated", stamp,
                        stamp[:10], "observed_only", None,
                    ),
                )

            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(appmod, "TECH_DB", tech_db),
            ):
                result = appmod.monthly(30)

            # The whole point of this fix: neither row inflates "new_market".
            self.assertEqual(result["counts"]["new_market"], 0)
            self.assertEqual(result["counts"]["new_market_backfill"], 1)
            self.assertEqual(result["counts"]["new_market_undated"], 1)
            self.assertEqual([row["id"] for row in result["new_market_backfill"]], [1])
            self.assertEqual([row["id"] for row in result["new_market_undated"]], [2])

    def test_stored_blocks_require_recent_valid_payload(self):
        now = datetime.now(timezone.utc)
        source = {
            "blocks_json": json.dumps([{
                "heading": "Application",
                "text": "Femtosecond laser texturing of battery electrodes.",
                "path": "main > article",
                "h2": "Batteries",
            }]),
            "last_checked_at": now.isoformat(timespec="seconds"),
        }
        blocks = _stored_blocks(source)
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[0].heading, "Application")

        stale = dict(source, last_checked_at=(now - timedelta(days=2)).isoformat(timespec="seconds"))
        self.assertIsNone(_stored_blocks(stale))
        self.assertIsNone(_stored_blocks({"blocks_json": "{bad", "last_checked_at": source["last_checked_at"]}))


if __name__ == "__main__":
    unittest.main()
