"""Tests pour les alertes de veille (Lot 1 §1.4 dernier morceau, audit veille §5.F, 30/08/2026) :
4 règles explicites (bucket_transition_existing, new_fact_high_value_actor, collection_incident,
ma_funding_event), idempotentes par fingerprint, et /api/digest?since= qui n'expose que le delta.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import alerts as alertsmod
import app as appmod
import db as dbmod


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


def _insert_evidence(db, *, fingerprint: str, actor_name: str = "Example", bucket: str = "existing", created_at: str = "2026-08-25") -> int:
    return db.execute(
        """INSERT INTO evidence(
               actor_name,bucket,market,component,operation,source_url,quote,source_group,fingerprint,
               review_status,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, bucket, "Médical", "Stents", "Découpe", "https://example.test/a",
            "a verbatim excerpt", "grp", fingerprint, "accepted", created_at, created_at,
        ),
    ).lastrowid


class BucketTransitionAlertTests(unittest.TestCase):
    def test_radar_to_existing_transition_produces_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
                db.execute(
                    "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
                    (evidence_id, "radar", "existing", "2026-08-25T10:00:00+00:00"),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertEqual(1, inserted.get("bucket_transition_existing"))
            rows = dbmod.rows(market_db, "SELECT alert_type,actor_name FROM alerts WHERE alert_type='bucket_transition_existing'")
            self.assertEqual(1, len(rows))
            self.assertEqual("Example", rows[0]["actor_name"])

    def test_initial_creation_is_not_an_alert(self):
        # §10.9: from_bucket IS NULL means the row was CREATED at 'existing', not a real
        # transition -- 104 of 109 evidence_bucket_transitions rows in production are exactly
        # this, and must not flood the digest as if they were real progress.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
                db.execute(
                    "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
                    (evidence_id, None, "existing", "2026-08-25T10:00:00+00:00"),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertNotIn("bucket_transition_existing", inserted)


class NewFactHighValueActorAlertTests(unittest.TestCase):
    def test_new_existing_fact_at_c1_actor_produces_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
                dbmod.update_actor_classification(actor_id, competitive_class="C1")
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", actor_name="Example")
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertEqual(1, inserted.get("new_fact_high_value_actor"))

    def test_new_existing_fact_at_non_high_value_actor_is_not_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
                dbmod.update_actor_classification(actor_id, competitive_class="T1")
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", actor_name="Example")
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertNotIn("new_fact_high_value_actor", inserted)

    def test_radar_fact_at_c1_actor_is_not_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
                dbmod.update_actor_classification(actor_id, competitive_class="C1")
            with dbmod.connect(market_db) as db:
                _insert_evidence(db, fingerprint="fp1", actor_name="Example", bucket="radar")
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertNotIn("new_fact_high_value_actor", inserted)


class CollectionIncidentAlertTests(unittest.TestCase):
    def test_needs_reprofile_flag_produces_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "UPDATE site_profiles SET needs_reprofile=1,last_profiled_at=?,last_error=? WHERE actor_id=?",
                    ("2026-08-25T10:00:00+00:00", "403 Forbidden", actor_id),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertEqual(1, inserted.get("collection_incident"))

    def test_healthy_actor_produces_no_incident_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertNotIn("collection_incident", inserted)


class MaFundingAlertTests(unittest.TestCase):
    def test_investment_event_produces_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    """INSERT INTO actor_events(actor_id,event_type,description,source_url,review_status,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (actor_id, "investment", "Example raises EUR 10M in Series B", "https://example.test/news", "verified", "2026-08-25T10:00:00+00:00"),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertEqual(1, inserted.get("ma_funding_event"))

    def test_rejected_event_is_not_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    """INSERT INTO actor_events(actor_id,event_type,description,source_url,review_status,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (actor_id, "investment", "false positive", "https://example.test/news", "rejected", "2026-08-25T10:00:00+00:00"),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertNotIn("ma_funding_event", inserted)

    def test_recruitment_event_is_not_an_ma_funding_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    """INSERT INTO actor_events(actor_id,event_type,description,source_url,review_status,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (actor_id, "recruitment", "Example is hiring engineers", "https://example.test/news", "verified", "2026-08-25T10:00:00+00:00"),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                inserted = alertsmod.capture_alerts()
            self.assertNotIn("ma_funding_event", inserted)


class CaptureAlertsIdempotenceTests(unittest.TestCase):
    def test_capturing_twice_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                evidence_id = _insert_evidence(db, fingerprint="fp1")
                db.execute(
                    "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
                    (evidence_id, "radar", "existing", "2026-08-25T10:00:00+00:00"),
                )
            with (
                patch.object(alertsmod, "ACTORS_DB", actors_db),
                patch.object(alertsmod, "MARKET_DB", market_db),
            ):
                alertsmod.capture_alerts()
                second = alertsmod.capture_alerts()
            self.assertNotIn("bucket_transition_existing", second)
            count = dbmod.scalar(market_db, "SELECT COUNT(*) FROM alerts")
            self.assertEqual(1, count)


class DigestEndpointTests(unittest.TestCase):
    def test_digest_only_returns_alerts_since_the_given_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with dbmod.connect(market_db) as db:
                db.execute(
                    "INSERT INTO alerts(alert_type,actor_name,summary,detail,source_url,fingerprint,event_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("collection_incident", "Old Actor", "old alert", None, None, "old-fp", "2026-08-01T00:00:00+00:00", dbmod.utc_now()),
                )
                db.execute(
                    "INSERT INTO alerts(alert_type,actor_name,summary,detail,source_url,fingerprint,event_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("collection_incident", "Recent Actor", "recent alert", None, None, "recent-fp", "2026-08-25T00:00:00+00:00", dbmod.utc_now()),
                )
            with patch.object(appmod, "MARKET_DB", market_db):
                result = appmod.digest(since="2026-08-20T00:00:00+00:00")
            self.assertEqual(1, result["total"])
            self.assertEqual(1, len(result["by_type"]["collection_incident"]))
            self.assertEqual("Recent Actor", result["by_type"]["collection_incident"][0]["actor_name"])

    def test_digest_defaults_to_the_last_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with patch.object(appmod, "MARKET_DB", market_db):
                result = appmod.digest(since=None)
            self.assertEqual(0, result["total"])
            self.assertIsNotNone(result["since"])


if __name__ == "__main__":
    unittest.main()
