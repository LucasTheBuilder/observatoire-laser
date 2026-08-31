"""Tests pour le connecteur brevets EPO OPS (patent.py, Lot 3 §3.4).

httpx.Client est remplacé par un double rejouant des réponses canned -- jamais d'appel réseau
réel ici. AVERTISSEMENT (voir patent.py docstring) : la forme XML de ces fixtures suit le format
d'échange EPO (DOCDB) tel que documenté publiquement, mais n'a PAS encore été confrontée à une
vraie réponse de l'API (aucun compte développeur disponible au moment où ce module a été écrit)
-- à corriger si besoin dès qu'une clé/secret EPO OPS réels sont disponibles, avant de considérer
ce chantier terminé.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import patent
from patent import (
    _cql_escape,
    _parse_exchange_document,
    _upsert_patent_document,
    collect_patents,
)

NS = "http://www.epo.org/exchange"


def _exchange_document_xml(
    *, country="EP", doc_number="1234567", kind="A1", pub_date="20250115",
    title_en="Method for femtosecond laser drilling", applicants=("TESTLASER SARL",),
) -> str:
    applicant_xml = "".join(
        f'<applicant sequence="{i + 1}" data-format="docdb">'
        f"<applicant-name><name>{name}</name></applicant-name></applicant>"
        for i, name in enumerate(applicants)
    )
    return f"""
    <exchange-document xmlns="{NS}">
        <bibliographic-data>
            <publication-reference>
                <document-id document-id-type="docdb">
                    <country>{country}</country>
                    <doc-number>{doc_number}</doc-number>
                    <kind>{kind}</kind>
                    <date>{pub_date}</date>
                </document-id>
            </publication-reference>
            <invention-title lang="en">{title_en}</invention-title>
            <invention-title lang="de">Verfahren zum Laserbohren</invention-title>
            <parties>
                <applicants>{applicant_xml}</applicants>
            </parties>
        </bibliographic-data>
    </exchange-document>
    """


def _search_response_xml(*documents: str) -> bytes:
    joined = "".join(documents)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="{NS}">
        <ops:biblio-search>
            <ops:search-result>
                <exchange-documents>{joined}</exchange-documents>
            </ops:search-result>
        </ops:biblio-search>
    </ops:world-patent-data>
    """.encode()


class CqlEscapeTests(unittest.TestCase):
    def test_strips_double_quotes(self):
        self.assertEqual(_cql_escape('Le"ica'), "Leica")

    def test_leaves_plain_names_untouched(self):
        self.assertEqual(_cql_escape("TESTLASER SARL"), "TESTLASER SARL")


class ParseExchangeDocumentTests(unittest.TestCase):
    def test_parses_publication_number_title_date_and_applicants(self):
        el = ET.fromstring(_exchange_document_xml())
        doc = _parse_exchange_document(el)
        self.assertEqual(doc["patent_number"], "EP1234567A1")
        self.assertEqual(doc["title"], "Method for femtosecond laser drilling")
        self.assertEqual(doc["published_at"], "2025-01-15")
        self.assertEqual(doc["applicants"], ["TESTLASER SARL"])
        self.assertIn("pn%3DEP1234567A1", doc["url"])

    def test_falls_back_to_any_title_when_no_english_one(self):
        xml = _exchange_document_xml().replace('lang="en"', 'lang="fr"')
        el = ET.fromstring(xml)
        doc = _parse_exchange_document(el)
        self.assertTrue(doc["title"])

    def test_multiple_applicants_are_all_captured(self):
        el = ET.fromstring(_exchange_document_xml(applicants=("TESTLASER SARL", "SOME UNTRACKED LAB")))
        doc = _parse_exchange_document(el)
        self.assertEqual(doc["applicants"], ["TESTLASER SARL", "SOME UNTRACKED LAB"])

    def test_missing_doc_number_returns_none(self):
        xml = _exchange_document_xml(doc_number="")
        el = ET.fromstring(xml)
        self.assertIsNone(_parse_exchange_document(el))


class FakeResponse:
    def __init__(self, *, json_payload=None, content=b"", status_code=200):
        self._json = json_payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeClient:
    # search_handler(params: dict) -> bytes ; overridden per test.
    search_handler = staticmethod(lambda params: _search_response_xml())
    token_fails = False

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, **kwargs):
        if self.token_fails:
            return FakeResponse(status_code=401)
        return FakeResponse(json_payload={"access_token": "fake-token", "expires_in": "1200"})

    def get(self, url, params=None, headers=None, **kwargs):
        return FakeResponse(content=self.search_handler(params or {}))


class CollectPatentsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        actors_db = tmp_path / "actors.db"
        market_db = tmp_path / "market.db"
        tech_db = tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", market_db), patch.object(dbmod, "TECH_DB", tech_db):
            dbmod.init_databases()
        self.actors_db = actors_db
        self.tech_db = tech_db
        self.actors_patch = patch.object(patent, "ACTORS_DB", actors_db)
        self.tech_patch = patch.object(patent, "TECH_DB", tech_db)
        self.actors_patch.start()
        self.tech_patch.start()
        self.addCleanup(self.actors_patch.stop)
        self.addCleanup(self.tech_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        # 55 acteurs réels + le nôtre sont seedés par init_databases() -- sans ça, la pause
        # polie entre deux requêtes par acteur (PATENT_REQUEST_DELAY_SECONDS) ferait durer
        # chaque test une bonne minute.
        self.delay_patch = patch.object(patent, "PATENT_REQUEST_DELAY_SECONDS", 0.0)
        self.delay_patch.start()
        self.addCleanup(self.delay_patch.stop)
        with dbmod.connect(actors_db) as db:
            db.execute(
                "INSERT INTO actors(name,country,role,priority,official_url,updated_at,active) VALUES(?,?,?,?,?,?,1)",
                ("TESTLASER SARL", "France", "Centre technologique", 1, "https://www.testlaser.example", dbmod.utc_now()),
            )
        FakeClient.token_fails = False
        FakeClient.search_handler = staticmethod(lambda params: _search_response_xml())

    def test_returns_not_configured_without_credentials(self):
        with patch.object(patent, "EPO_OPS_KEY", ""), patch.object(patent, "EPO_OPS_SECRET", ""):
            result = collect_patents()
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["patents_added"], 0)

    def test_actor_scoped_patent_is_attributed_and_stored(self):
        def handler(params):
            cql = params.get("q", "")
            if "TESTLASER SARL" in cql:
                return _search_response_xml(_exchange_document_xml(doc_number="1111111", applicants=("TESTLASER SARL",)))
            return _search_response_xml()
        FakeClient.search_handler = staticmethod(handler)
        with patch.object(patent, "EPO_OPS_KEY", "k"), patch.object(patent, "EPO_OPS_SECRET", "s"), patch.object(patent.httpx, "Client", FakeClient):
            result = collect_patents()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actors_matched"], 1)
        self.assertEqual(result["patents_added"], 1)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT actor_name,document_type,patent_number FROM documents").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")
        self.assertEqual(row["document_type"], "patent")
        self.assertEqual(row["patent_number"], "EP1111111A1")

    def test_topic_scoped_untracked_applicant_becomes_a_candidate(self):
        def handler(params):
            cql = params.get("q", "")
            if cql.startswith("cpc="):
                return _search_response_xml(
                    _exchange_document_xml(doc_number="2222222", applicants=("UNTRACKED LASER LAB INC",))
                )
            return _search_response_xml()
        FakeClient.search_handler = staticmethod(handler)
        with patch.object(patent, "EPO_OPS_KEY", "k"), patch.object(patent, "EPO_OPS_SECRET", "s"), patch.object(patent.httpx, "Client", FakeClient):
            result = collect_patents()
        self.assertEqual(result["topic_scoped_candidates_added"], 1)
        with dbmod.connect(self.actors_db) as db:
            row = db.execute("SELECT name,review_status FROM actor_candidates").fetchone()
            source = db.execute("SELECT source_type FROM actor_candidate_sources").fetchone()
        self.assertEqual(row["name"], "UNTRACKED LASER LAB INC")
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(source["source_type"], "patent")

    def test_topic_scoped_already_tracked_applicant_is_not_a_candidate(self):
        def handler(params):
            cql = params.get("q", "")
            if cql.startswith("cpc="):
                return _search_response_xml(_exchange_document_xml(doc_number="3333333", applicants=("TESTLASER SARL",)))
            return _search_response_xml()
        FakeClient.search_handler = staticmethod(handler)
        with patch.object(patent, "EPO_OPS_KEY", "k"), patch.object(patent, "EPO_OPS_SECRET", "s"), patch.object(patent.httpx, "Client", FakeClient):
            result = collect_patents()
        self.assertEqual(result["topic_scoped_candidates_added"], 0)

    def test_same_patent_found_by_both_passes_is_stored_once_and_attributed(self):
        # Le double doit distinguer les requêtes : sinon les 55 vrais acteurs seedés par
        # init_databases() "trouveraient" tous le même brevet fixture avant TESTLASER SARL,
        # et l'attribution testée ici ne prouverait rien.
        def handler(params):
            cql = params.get("q", "")
            if "TESTLASER SARL" in cql or cql.startswith("cpc="):
                return _search_response_xml(_exchange_document_xml(doc_number="4444444", applicants=("TESTLASER SARL",)))
            return _search_response_xml()
        FakeClient.search_handler = staticmethod(handler)
        with patch.object(patent, "EPO_OPS_KEY", "k"), patch.object(patent, "EPO_OPS_SECRET", "s"), patch.object(patent.httpx, "Client", FakeClient):
            result = collect_patents()
        with dbmod.connect(self.tech_db) as db:
            rows = db.execute("SELECT actor_name FROM documents WHERE patent_number='EP4444444A1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_name"], "TESTLASER SARL")
        self.assertGreaterEqual(result["patents_attributed"], 0)

    def test_token_failure_is_reported_as_error_status(self):
        FakeClient.token_fails = True
        with patch.object(patent, "EPO_OPS_KEY", "k"), patch.object(patent, "EPO_OPS_SECRET", "s"), patch.object(patent.httpx, "Client", FakeClient):
            result = collect_patents()
        self.assertEqual(result["status"], "error")


class UpsertPatentDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.tech_db = tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"), patch.object(dbmod, "MARKET_DB", tmp_path / "market.db"), patch.object(dbmod, "TECH_DB", self.tech_db):
            dbmod.init_databases()
        self.addCleanup(self.tmpdir.cleanup)

    def test_unattributed_document_gets_attributed_by_a_later_call(self):
        doc = _parse_exchange_document(ET.fromstring(_exchange_document_xml(doc_number="5555555")))
        with dbmod.connect(self.tech_db) as db:
            inserted, attributed = _upsert_patent_document(db, None, doc)
            self.assertEqual((inserted, attributed), (1, 0))
            inserted2, attributed2 = _upsert_patent_document(db, "TESTLASER SARL", doc)
            self.assertEqual((inserted2, attributed2), (0, 1))
            row = db.execute("SELECT actor_name FROM documents WHERE patent_number='EP5555555A1'").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")


if __name__ == "__main__":
    unittest.main()
