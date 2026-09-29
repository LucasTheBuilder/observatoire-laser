"""Tests pour le connecteur brevets Lens.org (lens.py).

`_fetch_recent_patents` (la seule fonction qui parle réellement au réseau) est toujours patchée
ici -- jamais d'appel réseau réel. Les formes de réponse ci-dessous (champ "kind", pas
"kind_symbol" ; "extracted_name" en objet {"value": ...}) reproduisent ce qui a été observé le
28/09/2026 contre un vrai compte Lens (voir lens.py docstring) -- ce ne sont plus des suppositions.
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
import lens
from lens import _parse_result, _upsert_lens_patent, collect_lens_patents


def _lens_document(
    *, jurisdiction="EP", doc_number="7654321", kind="A1", date_published="2025-03-20",
    title_en="Method for femtosecond laser drilling", applicants=("TESTLASER SARL",),
    lens_id="123-456-789-012-345",
) -> dict:
    return {
        "lens_id": lens_id,
        "jurisdiction": jurisdiction,
        "doc_number": doc_number,
        "kind": kind,
        "date_published": date_published,
        "biblio": {
            "invention_title": [
                {"lang": "en", "text": title_en},
                {"lang": "de", "text": "Verfahren zum Laserbohren"},
            ],
            "parties": {
                "applicants": [{"extracted_name": {"value": name}} for name in applicants],
            },
        },
    }


def _search_response(*documents: dict) -> dict:
    return {"total": len(documents), "data": list(documents)}


class ParseResultTests(unittest.TestCase):
    def test_parses_publication_number_title_date_and_applicants(self):
        doc = _parse_result(_lens_document())
        self.assertEqual(doc["patent_number"], "EP7654321A1")
        self.assertEqual(doc["title"], "Method for femtosecond laser drilling")
        self.assertEqual(doc["published_at"], "2025-03-20")
        self.assertEqual(doc["applicants"], ["TESTLASER SARL"])
        self.assertIn("lens.org/lens/patent/123-456-789-012-345", doc["url"])

    def test_falls_back_to_any_title_when_no_english_one(self):
        raw = _lens_document()
        raw["biblio"]["invention_title"] = [{"lang": "fr", "text": "Procédé de perçage laser"}]
        doc = _parse_result(raw)
        self.assertEqual(doc["title"], "Procédé de perçage laser")

    def test_multiple_applicants_are_all_captured(self):
        doc = _parse_result(_lens_document(applicants=("TESTLASER SARL", "SOME UNTRACKED LAB")))
        self.assertEqual(doc["applicants"], ["TESTLASER SARL", "SOME UNTRACKED LAB"])

    def test_missing_doc_number_returns_none(self):
        raw = _lens_document()
        raw["doc_number"] = ""
        self.assertIsNone(_parse_result(raw))

    def test_missing_lens_id_falls_back_to_search_link(self):
        raw = _lens_document(lens_id="")
        doc = _parse_result(raw)
        self.assertIn("doc_number:7654321", doc["url"])

    def test_not_a_dict_returns_none(self):
        self.assertIsNone(_parse_result([]))  # type: ignore[arg-type]

    def test_applicant_name_as_plain_string_is_still_accepted(self):
        # Repli défensif (jamais observé en pratique) : extracted_name en chaîne nue plutôt
        # qu'en objet {"value": ...}.
        raw = _lens_document(applicants=())
        raw["biblio"]["parties"]["applicants"] = [{"extracted_name": "PLAIN STRING SARL"}]
        doc = _parse_result(raw)
        self.assertEqual(doc["applicants"], ["PLAIN STRING SARL"])


class CollectLensPatentsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        actors_db, market_db, tech_db = tmp_path / "actors.db", tmp_path / "market.db", tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", market_db), patch.object(dbmod, "TECH_DB", tech_db):
            dbmod.init_databases()
        self.actors_db = actors_db
        self.tech_db = tech_db
        self.actors_patch = patch.object(lens, "ACTORS_DB", actors_db)
        self.tech_patch = patch.object(lens, "TECH_DB", tech_db)
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
        self.key_patch = patch.object(lens, "LENS_API_KEY", "k")
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def test_returns_not_configured_without_credentials(self):
        with patch.object(lens, "LENS_API_KEY", ""):
            result = collect_lens_patents()
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["patents_added"], 0)

    def test_tracked_applicant_is_attributed_and_stored(self):
        with patch.object(lens, "_fetch_recent_patents", lambda client: _search_response(
            _lens_document(doc_number="1111111", applicants=("TESTLASER SARL",)),
        )):
            result = collect_lens_patents()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actors_matched"], 1)
        self.assertEqual(result["patents_added"], 1)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT actor_name,document_type,patent_number,source_url FROM documents").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")
        self.assertEqual(row["document_type"], "patent")
        self.assertEqual(row["patent_number"], "EP1111111A1")
        self.assertIn("lens.org", row["source_url"])

    def test_untracked_applicant_becomes_a_candidate(self):
        with patch.object(lens, "_fetch_recent_patents", lambda client: _search_response(
            _lens_document(doc_number="2222222", applicants=("UNTRACKED LASER LAB INC",)),
        )):
            result = collect_lens_patents()
        self.assertEqual(result["actors_matched"], 0)
        self.assertEqual(result["topic_scoped_candidates_added"], 1)
        with dbmod.connect(self.actors_db) as db:
            row = db.execute("SELECT name,review_status FROM actor_candidates").fetchone()
            source = db.execute("SELECT source_type FROM actor_candidate_sources").fetchone()
        self.assertEqual(row["name"], "UNTRACKED LASER LAB INC")
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(source["source_type"], "patent")

    def test_already_tracked_applicant_is_not_a_candidate(self):
        with patch.object(lens, "_fetch_recent_patents", lambda client: _search_response(
            _lens_document(doc_number="3333333", applicants=("TESTLASER SARL",)),
        )):
            result = collect_lens_patents()
        self.assertEqual(result["topic_scoped_candidates_added"], 0)

    def test_query_failure_is_reported_as_error_status(self):
        def boom(client):
            raise RuntimeError("429 Too Many Requests")
        with patch.object(lens, "_fetch_recent_patents", boom):
            result = collect_lens_patents()
        self.assertEqual(result["status"], "error")
        self.assertIn("429", result["message"])


class UpsertLensPatentTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.tech_db = tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"), patch.object(dbmod, "MARKET_DB", tmp_path / "market.db"), patch.object(dbmod, "TECH_DB", self.tech_db):
            dbmod.init_databases()
        self.addCleanup(self.tmpdir.cleanup)

    def test_unattributed_document_gets_attributed_by_a_later_call(self):
        doc = _parse_result(_lens_document(doc_number="5555555"))
        with dbmod.connect(self.tech_db) as db:
            inserted, attributed = _upsert_lens_patent(db, None, doc)
            self.assertEqual((inserted, attributed), (1, 0))
            inserted2, attributed2 = _upsert_lens_patent(db, "TESTLASER SARL", doc)
            self.assertEqual((inserted2, attributed2), (0, 1))
            row = db.execute("SELECT actor_name FROM documents WHERE patent_number='EP5555555A1'").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")


if __name__ == "__main__":
    unittest.main()
