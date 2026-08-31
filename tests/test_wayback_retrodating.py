"""Tests pour la rétro-datation Wayback CDX (wayback_retrodating.py, Lot 4 §18).

httpx.Client est remplacé par un double rejouant des réponses canned -- jamais d'appel réseau
réel ici. Le format de réponse CDX (tableau JSON, ligne d'en-tête puis lignes de données) et le
format de récupération "brut" d'un snapshot (URL en /web/{timestamp}id_/{url}) sont ceux
vérifiés manuellement (curl) contre les vraies web.archive.org avant d'écrire ce module.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import wayback_retrodating
from wayback_retrodating import (
    _find_first_appearance,
    _normalize_html_text,
    _search_term,
    retrodate_evidence_sources,
)

CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


class NormalizeHtmlTextTests(unittest.TestCase):
    def test_strips_tags_and_collapses_whitespace(self):
        html = "<div>Ultrafast   <b>laser</b>\n\ntechnology</div>"
        self.assertEqual(_normalize_html_text(html), "ULTRAFAST LASER TECHNOLOGY")


class SearchTermTests(unittest.TestCase):
    def test_truncates_and_uppercases(self):
        quote = "a" * 100
        term = _search_term(quote)
        self.assertEqual(len(term), 60)
        self.assertEqual(term, "A" * 60)


class FakeResponse:
    def __init__(self, *, json_payload=None, text="", status_code=200):
        self._json = json_payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeClient:
    cdx_rows: list = []
    snapshot_content: dict = {}
    fail_cdx = False
    fail_snapshot_timestamps: set = set()

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, follow_redirects=False, **kwargs):
        if "cdx/search" in url:
            if self.fail_cdx:
                return FakeResponse(status_code=500)
            return FakeResponse(json_payload=[CDX_HEADER, *self.cdx_rows])
        match = re.search(r"/web/(\d+)id_/", url)
        timestamp = match.group(1) if match else None
        if timestamp in self.fail_snapshot_timestamps:
            return FakeResponse(status_code=500)
        return FakeResponse(text=self.snapshot_content.get(timestamp, ""))


def _snapshots(*timestamps):
    return [(ts, "https://example.com/page") for ts in timestamps]


class FindFirstAppearanceTests(unittest.TestCase):
    def setUp(self):
        FakeClient.snapshot_content = {}
        FakeClient.fail_snapshot_timestamps = set()

    def test_absent_from_newest_snapshot_returns_none(self):
        FakeClient.snapshot_content = {"20200101": "nothing relevant"}
        client = FakeClient()
        result = _find_first_appearance(client, _snapshots("20200101"), "FEMTOSECOND LASER")
        self.assertIsNone(result)

    def test_present_in_oldest_snapshot_returns_oldest(self):
        FakeClient.snapshot_content = {
            "20180101": "we do FEMTOSECOND LASER micromachining",
            "20220101": "we do FEMTOSECOND LASER micromachining, updated page",
        }
        client = FakeClient()
        result = _find_first_appearance(client, _snapshots("20180101", "20220101"), "FEMTOSECOND LASER")
        self.assertEqual(result, "20180101")

    def test_bisects_to_the_first_snapshot_containing_the_term(self):
        # Term appears starting at the 5th of 8 snapshots (index 4) -- the term is genuinely
        # absent before that point, present from then on, exactly the "offer just appeared"
        # case this connector exists to detect.
        timestamps = [f"2020{month:02d}01" for month in range(1, 9)]
        FakeClient.snapshot_content = {
            ts: ("has FEMTOSECOND LASER now" if i >= 4 else "no relevant content")
            for i, ts in enumerate(timestamps)
        }
        client = FakeClient()
        result = _find_first_appearance(client, _snapshots(*timestamps), "FEMTOSECOND LASER")
        self.assertEqual(result, timestamps[4])

    def test_a_fetch_failure_on_a_snapshot_is_never_treated_as_a_match(self):
        FakeClient.snapshot_content = {"20220101": "has FEMTOSECOND LASER now"}
        FakeClient.fail_snapshot_timestamps = {"20220101"}
        client = FakeClient()
        # newest snapshot fetch fails -> None, never a fabricated date
        result = _find_first_appearance(client, _snapshots("20220101"), "FEMTOSECOND LASER")
        self.assertIsNone(result)


class RetrodateEvidenceSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.market_db = tmp_path / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"),
            patch.object(dbmod, "MARKET_DB", self.market_db),
            patch.object(dbmod, "TECH_DB", tmp_path / "technology.db"),
        ):
            dbmod.init_databases()
        self.market_patch = patch.object(wayback_retrodating, "MARKET_DB", self.market_db)
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        FakeClient.cdx_rows = []
        FakeClient.snapshot_content = {}
        FakeClient.fail_cdx = False
        FakeClient.fail_snapshot_timestamps = set()

    def _seed_evidence_source(self, *, source_url="https://example.com/page", quote="Femtosecond laser micromachining for medical devices", is_verbatim=1, first_appeared_at=None):
        with dbmod.connect(self.market_db) as db:
            stamp = dbmod.utc_now()
            evidence_id = db.execute(
                """INSERT INTO evidence(actor_name,bucket,source_url,quote,source_group,fingerprint,created_at,updated_at)
                   VALUES('Test Actor','existing',?,?,'grp','fp-evidence-1',?,?)""",
                (source_url, quote, stamp, stamp),
            ).lastrowid
            source_id = db.execute(
                """INSERT INTO evidence_sources(evidence_id,source_url,quote,is_verbatim,first_appeared_at,fingerprint,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (evidence_id, source_url, quote, is_verbatim, first_appeared_at, f"fp-source-{source_url}-{quote[:10]}", stamp),
            ).lastrowid
        return source_id

    def test_verbatim_quote_with_a_findable_term_gets_dated(self):
        source_id = self._seed_evidence_source()
        FakeClient.cdx_rows = [["urlkey", "20240115000000", "https://example.com/page", "text/html", "200", "d1", "100"]]
        FakeClient.snapshot_content = {"20240115000000": "Femtosecond laser micromachining for medical devices"}
        with patch.object(wayback_retrodating.httpx, "Client", FakeClient):
            result = retrodate_evidence_sources()
        self.assertEqual(result["dated"], 1)
        self.assertEqual(result["errors"], 0)
        with dbmod.connect(self.market_db) as db:
            row = db.execute("SELECT first_appeared_at,first_appeared_snapshot_url FROM evidence_sources WHERE id=?", (source_id,)).fetchone()
        self.assertEqual(row["first_appeared_at"], "2024-01-15")
        self.assertIn("20240115000000id_", row["first_appeared_snapshot_url"])

    def test_non_verbatim_quote_is_never_selected(self):
        self._seed_evidence_source(is_verbatim=0)
        with patch.object(wayback_retrodating.httpx, "Client", FakeClient):
            result = retrodate_evidence_sources()
        self.assertEqual(result["scanned"], 0)

    def test_already_dated_source_is_never_reselected(self):
        self._seed_evidence_source(first_appeared_at="2023-01-01")
        with patch.object(wayback_retrodating.httpx, "Client", FakeClient):
            result = retrodate_evidence_sources()
        self.assertEqual(result["scanned"], 0)

    def test_page_with_no_snapshots_is_counted_and_not_written(self):
        source_id = self._seed_evidence_source()
        FakeClient.cdx_rows = []  # header-only response, i.e. len(rows) < 2
        with patch.object(wayback_retrodating.httpx, "Client", FakeClient):
            result = retrodate_evidence_sources()
        self.assertEqual(result["no_snapshots"], 1)
        with dbmod.connect(self.market_db) as db:
            row = db.execute("SELECT first_appeared_at FROM evidence_sources WHERE id=?", (source_id,)).fetchone()
        self.assertIsNone(row["first_appeared_at"])

    def test_term_never_found_is_counted_and_not_written(self):
        source_id = self._seed_evidence_source()
        FakeClient.cdx_rows = [["urlkey", "20240115000000", "https://example.com/page", "text/html", "200", "d1", "100"]]
        FakeClient.snapshot_content = {"20240115000000": "completely unrelated content"}
        with patch.object(wayback_retrodating.httpx, "Client", FakeClient):
            result = retrodate_evidence_sources()
        self.assertEqual(result["not_found"], 1)
        with dbmod.connect(self.market_db) as db:
            row = db.execute("SELECT first_appeared_at FROM evidence_sources WHERE id=?", (source_id,)).fetchone()
        self.assertIsNone(row["first_appeared_at"])

    def test_cdx_failure_is_counted_as_an_error(self):
        self._seed_evidence_source()
        FakeClient.fail_cdx = True
        with patch.object(wayback_retrodating.httpx, "Client", FakeClient):
            result = retrodate_evidence_sources()
        self.assertEqual(result["errors"], 1)


if __name__ == "__main__":
    unittest.main()
