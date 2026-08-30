"""Tests pour le tableau de bord de la veille (Lot 1 §1.7, audit veille §10.11, 30/08/2026) :
8 indicateurs de santé de la collecte/extraction, persistés dans veille_metrics.
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
import veille_metrics as vm


def _setup(tmp: str) -> tuple[Path, Path, Path]:
    actors_db = Path(tmp) / "actors.db"
    market_db = Path(tmp) / "market.db"
    tech_db = Path(tmp) / "technology.db"
    with (
        patch.object(dbmod, "ACTORS_DB", actors_db),
        patch.object(dbmod, "MARKET_DB", market_db),
        patch.object(dbmod, "TECH_DB", tech_db),
    ):
        dbmod.init_databases()
    return actors_db, market_db, tech_db


def _insert_evidence(
    db, *, fingerprint: str, source_url: str = "https://example.test/a", review_status: str = "accepted",
    is_verbatim: int = 1, date_confidence: str | None = None, source_date: str | None = None, created_at: str = "2026-08-20",
) -> None:
    db.execute(
        """INSERT INTO evidence(
               actor_name,bucket,source_url,quote,source_group,fingerprint,review_status,
               is_verbatim,date_confidence,source_date,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "Example", "existing", source_url, "a verbatim excerpt", "grp", fingerprint, review_status,
            is_verbatim, date_confidence, source_date, created_at, created_at,
        ),
    )
    db.execute(
        "INSERT INTO evidence_sources(evidence_id,source_url,quote,fingerprint,created_at) VALUES(last_insert_rowid(),?,?,?,?)",
        (source_url, "a verbatim excerpt", f"{fingerprint}-src", created_at),
    )


class CaptureVeilleMetricsTests(unittest.TestCase):
    def test_fresh_database_gives_none_for_every_market_db_indicator(self):
        # init_databases() seeds a static roster of actors/actor_sources (never crawled), so
        # unvisited_discovery_rate is legitimately 1.0 here -- but evidence/offers/
        # evidence_sources/offer_sources are genuinely empty on a fresh MARKET_DB, so every
        # indicator computed from them must report None (nothing to measure), never a
        # fabricated 0.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertEqual(1.0, values["unvisited_discovery_rate"])
            for indicator in vm.VEILLE_METRICS_THRESHOLDS:
                if indicator == "unvisited_discovery_rate":
                    continue
                self.assertIsNone(values[indicator], f"{indicator} should be None on a fresh MARKET_DB, got {values[indicator]}")

    def test_all_eight_indicators_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                vm.capture_veille_metrics(period="2026-08")
            rows = dbmod.rows(market_db, "SELECT indicator FROM veille_metrics WHERE period='2026-08'")
            self.assertEqual(set(vm.VEILLE_METRICS_THRESHOLDS), {row["indicator"] for row in rows})

    def test_capture_is_idempotent_within_the_same_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                vm.capture_veille_metrics(period="2026-08")
                vm.capture_veille_metrics(period="2026-08")
            count = dbmod.scalar(market_db, "SELECT COUNT(*) FROM veille_metrics WHERE period='2026-08'")
            self.assertEqual(len(vm.VEILLE_METRICS_THRESHOLDS), count)

    def test_collection_yield_counts_distinct_productive_urls_over_pages_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                for i in range(4):
                    db.execute(
                        "INSERT INTO actor_sources(actor_id,url,active,last_http_status) VALUES(?,?,1,200)",
                        (actor_id, f"https://example.test/page{i}"),
                    )
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", source_url="https://example.test/page0")
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertEqual(0.25, values["collection_yield"])

    def test_unvisited_discovery_rate_counts_null_last_checked_at(self):
        # init_databases() seeds a static roster with its own actor_sources (all unvisited) --
        # the expected ratio is computed against that real baseline instead of assuming a clean
        # slate, so this test doesn't silently drift if the seed roster changes size.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            baseline_total = dbmod.scalar(actors_db, "SELECT COUNT(*) FROM actor_sources WHERE active=1")
            baseline_unvisited = dbmod.scalar(actors_db, "SELECT COUNT(*) FROM actor_sources WHERE active=1 AND last_checked_at IS NULL")
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active,last_checked_at) VALUES(?,?,1,?)",
                    (actor_id, "https://example.test/visited", "2026-08-20"),
                )
                for i in range(3):
                    db.execute(
                        "INSERT INTO actor_sources(actor_id,url,active,last_checked_at) VALUES(?,?,1,NULL)",
                        (actor_id, f"https://example.test/unvisited{i}"),
                    )
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            expected = (baseline_unvisited + 3) / (baseline_total + 4)
            self.assertAlmostEqual(expected, values["unvisited_discovery_rate"], places=4)

    def test_crawl_error_rate_counts_non_2xx_3xx_among_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active,last_checked_at,last_http_status) VALUES(?,?,1,?,200)",
                    (actor_id, "https://example.test/ok", "2026-08-20"),
                )
                db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active,last_checked_at,last_http_status) VALUES(?,?,1,?,403)",
                    (actor_id, "https://example.test/blocked", "2026-08-20"),
                )
                db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active,last_checked_at,last_http_status) VALUES(?,?,1,?,NULL)",
                    (actor_id, "https://example.test/not-attempted", None),
                )
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertEqual(0.5, values["crawl_error_rate"])

    def test_non_verbatim_share_counts_is_verbatim_zero_among_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", is_verbatim=1)
                _insert_evidence(db, fingerprint="fp2", is_verbatim=1)
                _insert_evidence(db, fingerprint="fp3", is_verbatim=0)
                _insert_evidence(db, fingerprint="fp4", is_verbatim=0)
                # Not accepted -- must not count in either numerator or denominator.
                _insert_evidence(db, fingerprint="fp5", is_verbatim=0, review_status="review")
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertEqual(0.5, values["non_verbatim_share"])

    def test_published_date_reliability_over_evidence_and_offers_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", date_confidence="published")
                _insert_evidence(db, fingerprint="fp2", date_confidence="observed_only")
                db.execute(
                    """INSERT INTO offers(
                           actor_name,offer_type,capability,source_url,source_title,quote,fact_key,fingerprint,
                           review_status,field_confidence,date_confidence,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "Example", "service", "Drilling", "https://example.test/services", "Services", "we drill",
                        "fact-key-1", "offer-fp-1", "accepted", 0.8, "published", "2026-08-20", "2026-08-20",
                    ),
                )
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertAlmostEqual(2 / 3, values["published_date_reliability"], places=4)

    def test_detection_latency_is_median_days_between_source_date_and_created_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", source_date="2026-08-01", created_at="2026-08-11")
                _insert_evidence(db, fingerprint="fp2", source_date="2026-08-01", created_at="2026-08-21")
                # No source_date -- excluded from the latency computation entirely.
                _insert_evidence(db, fingerprint="fp3", source_date=None, created_at="2026-08-25")
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertEqual(15.0, values["detection_latency_days"])

    def test_source_concentration_uses_top_10_percent_of_distinct_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                # 10 distinct source URLs, one producing 5 facts, the other 9 producing 1 each --
                # top 10% = 1 URL = the dominant one = 5 / (5+9) of all facts.
                for i in range(5):
                    _insert_evidence(db, fingerprint=f"dominant-{i}", source_url="https://example.test/dominant")
                for i in range(9):
                    _insert_evidence(db, fingerprint=f"minor-{i}", source_url=f"https://example.test/minor{i}")
            with (
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                values = vm.capture_veille_metrics(period="2026-08")
            self.assertAlmostEqual(5 / 14, values["source_concentration_top10pct"], places=4)


class VeilleMetricsThresholdsTests(unittest.TestCase):
    def test_every_indicator_has_a_threshold_direction(self):
        for indicator, (direction, _threshold) in vm.VEILLE_METRICS_THRESHOLDS.items():
            self.assertIn(direction, ("gt", "lt"), f"{indicator} has an invalid alert direction")


class VeilleMetricsEndpointTests(unittest.TestCase):
    def test_no_snapshot_yet_raises_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(appmod, "MARKET_DB", market_db):
                with self.assertRaises(appmod.HTTPException) as ctx:
                    appmod.veille_metrics_latest()
            self.assertEqual(404, ctx.exception.status_code)

    def test_latest_snapshot_reports_alert_flags_from_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                # crawl_error_rate alert threshold is ("gt", 0.10) -- 0.9 must be flagged.
                db.execute(
                    "INSERT INTO veille_metrics(period,indicator,value,captured_at) VALUES(?,?,?,?)",
                    ("2026-08", "crawl_error_rate", 0.9, dbmod.utc_now()),
                )
                db.execute(
                    "INSERT INTO veille_metrics(period,indicator,value,captured_at) VALUES(?,?,?,?)",
                    ("2026-08", "collection_yield", 0.5, dbmod.utc_now()),
                )
            with patch.object(appmod, "MARKET_DB", market_db):
                result = appmod.veille_metrics_latest()
            self.assertEqual("2026-08", result["period"])
            by_indicator = {row["indicator"]: row for row in result["indicators"]}
            self.assertTrue(by_indicator["crawl_error_rate"]["alert"])
            self.assertFalse(by_indicator["collection_yield"]["alert"])
            # Never-captured indicators still appear, with value=None and alert=False (not an
            # error condition to flag -- see capture_veille_metrics' None-vs-0 discipline).
            self.assertIsNone(by_indicator["non_verbatim_share"]["value"])
            self.assertFalse(by_indicator["non_verbatim_share"]["alert"])

    def test_capture_endpoint_persists_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with (
                patch.object(appmod, "ACTORS_DB", actors_db),
                patch.object(appmod, "MARKET_DB", market_db),
                patch.object(vm, "ACTORS_DB", actors_db),
                patch.object(vm, "MARKET_DB", market_db),
            ):
                appmod.veille_metrics_capture()
                result = appmod.veille_metrics_latest()
            self.assertEqual(len(vm.VEILLE_METRICS_THRESHOLDS), len(result["indicators"]))


if __name__ == "__main__":
    unittest.main()
