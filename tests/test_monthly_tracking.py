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

            with dbmod.connect(market_db) as db:
                db.execute(
                    """INSERT INTO evidence(
                           actor_name,bucket,market,component,operation,industrial_stage,
                           source_url,source_title,quote,source_group,fingerprint,review_status,
                           created_at,updated_at,fact_key,evidence_kind,fact_status,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "ALPHANOV", "radar", "Batteries", "Électrodes de batteries", "Texturation",
                        "Prototype", "https://example.test/market", "Test market", "quote",
                        "test-group", "test-fingerprint", "accepted", stamp, stamp,
                        "test-fact-key", "market_application", "validated", stamp,
                    ),
                )
                db.execute(
                    """INSERT INTO offers(
                           actor_name,offer_type,capability,source_url,source_title,quote,
                           fact_key,fingerprint,review_status,created_at,updated_at,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "ALPHANOV", "service", "Texturation", "https://example.test/offer",
                        "Test offer", "quote", "offer-key", "offer-fingerprint", "accepted",
                        stamp, stamp, stamp,
                    ),
                )

            with dbmod.connect(tech_db) as db:
                db.execute(
                    """INSERT INTO documents(
                           document_type,title,source_url,fingerprint,created_at,last_seen_at
                       ) VALUES('publication',?,?,?,?,?)""",
                    ("Recent paper", "https://doi.org/10.test/example", "doc-fingerprint", stamp, stamp),
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
