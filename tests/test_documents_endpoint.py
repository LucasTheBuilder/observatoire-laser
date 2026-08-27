"""Tests pour /api/documents (page front "Technologies futures")."""

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


class DocumentsEndpointTests(unittest.TestCase):
    def _seed(self, tech_db: Path) -> None:
        stamp = dbmod.utc_now()
        with dbmod.connect(tech_db) as db:
            db.execute(
                """INSERT INTO documents(actor_name,document_type,title,source_url,published_at,doi,fingerprint,created_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("ALPHANOV", "publication", "Recent paper", "https://example.test/a", "2026-06-01", "10.1/a", "fp-a", stamp, stamp),
            )
            db.execute(
                """INSERT INTO documents(actor_name,document_type,title,source_url,published_at,doi,fingerprint,created_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (None, "publication", "Older unattributed paper", "https://example.test/b", "2020-01-01", "10.1/b", "fp-b", stamp, stamp),
            )
            db.execute(
                """INSERT INTO documents(actor_name,document_type,title,source_url,published_at,doi,fingerprint,created_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("IREPA LASER", "other", "Some other document", "https://example.test/c", None, None, "fp-c", stamp, stamp),
            )

    def test_returns_documents_most_recent_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                self._seed(tech_db)

            with patch.object(appmod, "TECH_DB", tech_db):
                # FastAPI's Query(default=...) sentinel is only resolved to a plain int when
                # the app is actually served through uvicorn; calling the route function
                # directly (as here) needs the value passed explicitly, same convention as
                # appmod.monthly(30) elsewhere in this test suite.
                result = appmod.documents(document_type=None, limit=200)

            self.assertEqual(3, len(result))
            # "Some other document" has no published_at, so it sorts by created_at (just now)
            # -- ahead of "Recent paper"'s real-but-older 2026-06-01 publication date. This is
            # the intended semantics: COALESCE(published_at, created_at) means "most recently
            # published, or if unknown, most recently collected" (the user asked for documents
            # "collectés récemment", not strictly "publiés récemment").
            titles_in_order = [row["title"] for row in result]
            self.assertEqual(
                ["Some other document", "Recent paper", "Older unattributed paper"],
                titles_in_order,
            )
            recent_paper = next(row for row in result if row["title"] == "Recent paper")
            self.assertEqual("ALPHANOV", recent_paper["actor_name"])
            older_paper = next(row for row in result if row["title"] == "Older unattributed paper")
            self.assertIsNone(older_paper["actor_name"])

    def test_filters_by_document_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                self._seed(tech_db)

            with patch.object(appmod, "TECH_DB", tech_db):
                result = appmod.documents(document_type="other", limit=200)

            self.assertEqual(1, len(result))
            self.assertEqual("Some other document", result[0]["title"])

    def test_limit_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                self._seed(tech_db)

            with patch.object(appmod, "TECH_DB", tech_db):
                result = appmod.documents(limit=1)

            self.assertEqual(1, len(result))


if __name__ == "__main__":
    unittest.main()
