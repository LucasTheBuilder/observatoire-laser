"""Tests pour le connecteur brevets Google Patents Public Datasets (google_patents.py).

`_run_query` (la seule fonction qui importe réellement google-cloud-bigquery et parle au réseau)
est toujours patchée ici -- jamais d'appel BigQuery réel, et le paquet n'a pas besoin d'être
installé pour faire tourner ces tests (voir requirements-google-patents.txt : volontairement
hors de l'environnement par défaut). AVERTISSEMENT (voir google_patents.py docstring) : la forme
des lignes ci-dessous suit le schéma BigQuery publié par Google tel que consulté le 22/09/2026,
mais n'a pas encore été confrontée à un vrai projet GCP facturable -- à corriger si besoin dès
qu'un projet est disponible, avant de considérer ce chantier terminé.
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
import google_patents
from google_patents import _parse_row, _upsert_google_patent, collect_google_patents


def _bq_row(
    *, publication_number="EP-7654321-A1", publication_date=20250320,
    title_en="Method for femtosecond laser drilling", assignees=("TESTLASER SARL",),
) -> dict:
    return {
        "publication_number": publication_number,
        "publication_date": publication_date,
        "title_localized": [
            {"language": "en", "text": title_en},
            {"language": "de", "text": "Verfahren zum Laserbohren"},
        ],
        "assignee_harmonized": [{"name": name, "country_code": "FR"} for name in assignees],
    }


class ParseRowTests(unittest.TestCase):
    def test_parses_publication_number_title_date_and_assignees(self):
        doc = _parse_row(_bq_row())
        self.assertEqual(doc["patent_number"], "EP-7654321-A1")
        self.assertEqual(doc["title"], "Method for femtosecond laser drilling")
        self.assertEqual(doc["published_at"], "2025-03-20")
        self.assertEqual(doc["applicants"], ["TESTLASER SARL"])
        self.assertEqual(doc["url"], "https://patents.google.com/patent/EP7654321A1")

    def test_falls_back_to_any_title_when_no_english_one(self):
        raw = _bq_row()
        raw["title_localized"] = [{"language": "fr", "text": "Procédé de perçage laser"}]
        doc = _parse_row(raw)
        self.assertEqual(doc["title"], "Procédé de perçage laser")

    def test_missing_publication_number_returns_none(self):
        raw = _bq_row()
        raw["publication_number"] = None
        self.assertIsNone(_parse_row(raw))

    def test_missing_publication_date_leaves_published_at_none(self):
        raw = _bq_row()
        raw["publication_date"] = None
        doc = _parse_row(raw)
        self.assertIsNone(doc["published_at"])

    def test_multiple_assignees_are_all_captured(self):
        doc = _parse_row(_bq_row(assignees=("TESTLASER SARL", "SOME UNTRACKED LAB")))
        self.assertEqual(doc["applicants"], ["TESTLASER SARL", "SOME UNTRACKED LAB"])


class CollectGooglePatentsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        actors_db, market_db, tech_db = tmp_path / "actors.db", tmp_path / "market.db", tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", market_db), patch.object(dbmod, "TECH_DB", tech_db):
            dbmod.init_databases()
        self.actors_db = actors_db
        self.tech_db = tech_db
        self.actors_patch = patch.object(google_patents, "ACTORS_DB", actors_db)
        self.tech_patch = patch.object(google_patents, "TECH_DB", tech_db)
        self.actors_patch.start()
        self.tech_patch.start()
        self.addCleanup(self.actors_patch.stop)
        self.addCleanup(self.tech_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        with dbmod.connect(actors_db) as db:
            db.execute(
                "INSERT INTO actors(name,country,role,priority,official_url,updated_at,active) VALUES(?,?,?,?,?,?,1)",
                ("TESTLASER SARL", "France", "Centre technologique", 1, "https://www.testlaser.example", dbmod.utc_now()),
            )
        # Configuré par défaut pour ces tests ; test_returns_not_configured_* le désactive
        # explicitement. bigquery_available forcé à True : ces tests patchent _run_query
        # directement, ils n'ont jamais besoin du vrai paquet installé.
        self.creds_patch = patch.object(google_patents, "GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake-creds.json")
        self.project_patch = patch.object(google_patents, "GOOGLE_CLOUD_PROJECT", "fake-project")
        self.available_patch = patch.object(google_patents, "_bigquery_available", lambda: True)
        self.creds_patch.start()
        self.project_patch.start()
        self.available_patch.start()
        self.addCleanup(self.creds_patch.stop)
        self.addCleanup(self.project_patch.stop)
        self.addCleanup(self.available_patch.stop)

    def test_returns_not_configured_without_credentials(self):
        with patch.object(google_patents, "GOOGLE_APPLICATION_CREDENTIALS", ""):
            result = collect_google_patents()
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["patents_added"], 0)

    def test_returns_not_configured_without_the_optional_package(self):
        with patch.object(google_patents, "_bigquery_available", lambda: False):
            result = collect_google_patents()
        self.assertEqual(result["status"], "not_configured")
        self.assertIn("google-cloud-bigquery", result["message"])

    def test_tracked_assignee_is_attributed_and_stored(self):
        with patch.object(google_patents, "_run_query", lambda lookback_days: [
            _bq_row(publication_number="EP-1111111-A1", assignees=("TESTLASER SARL",)),
        ]):
            result = collect_google_patents()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actors_matched"], 1)
        self.assertEqual(result["patents_added"], 1)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT actor_name,document_type,patent_number,source_url FROM documents").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")
        self.assertEqual(row["document_type"], "patent")
        self.assertEqual(row["patent_number"], "EP-1111111-A1")
        self.assertIn("patents.google.com", row["source_url"])

    def test_untracked_assignee_becomes_a_candidate(self):
        with patch.object(google_patents, "_run_query", lambda lookback_days: [
            _bq_row(publication_number="EP-2222222-A1", assignees=("UNTRACKED LASER LAB INC",)),
        ]):
            result = collect_google_patents()
        self.assertEqual(result["actors_matched"], 0)
        self.assertEqual(result["topic_scoped_candidates_added"], 1)
        with dbmod.connect(self.actors_db) as db:
            row = db.execute("SELECT name,review_status FROM actor_candidates").fetchone()
            source = db.execute("SELECT source_type FROM actor_candidate_sources").fetchone()
        self.assertEqual(row["name"], "UNTRACKED LASER LAB INC")
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(source["source_type"], "patent")

    def test_already_tracked_assignee_is_not_a_candidate(self):
        with patch.object(google_patents, "_run_query", lambda lookback_days: [
            _bq_row(publication_number="EP-3333333-A1", assignees=("TESTLASER SARL",)),
        ]):
            result = collect_google_patents()
        self.assertEqual(result["topic_scoped_candidates_added"], 0)

    def test_query_failure_is_reported_as_error_status(self):
        def boom(lookback_days):
            raise RuntimeError("query rejected: exceeds maximum_bytes_billed")
        with patch.object(google_patents, "_run_query", boom):
            result = collect_google_patents()
        self.assertEqual(result["status"], "error")
        self.assertIn("maximum_bytes_billed", result["message"])


class UpsertGooglePatentTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.tech_db = tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"), patch.object(dbmod, "MARKET_DB", tmp_path / "market.db"), patch.object(dbmod, "TECH_DB", self.tech_db):
            dbmod.init_databases()
        self.addCleanup(self.tmpdir.cleanup)

    def test_unattributed_document_gets_attributed_by_a_later_call(self):
        doc = _parse_row(_bq_row(publication_number="EP-5555555-A1"))
        with dbmod.connect(self.tech_db) as db:
            inserted, attributed = _upsert_google_patent(db, None, doc)
            self.assertEqual((inserted, attributed), (1, 0))
            inserted2, attributed2 = _upsert_google_patent(db, "TESTLASER SARL", doc)
            self.assertEqual((inserted2, attributed2), (0, 1))
            row = db.execute("SELECT actor_name FROM documents WHERE patent_number='EP-5555555-A1'").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")


if __name__ == "__main__":
    unittest.main()
