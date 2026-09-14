"""Tests pour hal.py : collecte thématique HAL (chantier /sources, 13/09/2026).

httpx.Client est remplacé par un double rejouant des réponses canned -- jamais d'appel réseau
réel ici. La forme de réponse (response.docs[].{title_s,en_abstract_s,abstract_s,doiId_s,uri_s,
submittedDate_s}) est celle vérifiée manuellement (navigateur) contre la vraie API
api.archives-ouvertes.fr avant d'écrire ce module.
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
import http_client
from hal import collect_hal_publications


def _hal_doc(
    title="Femtosecond laser micromachining of fused silica microchannels",
    abstract="We demonstrate ultrafast laser processing for glass microfabrication.",
    doi="10.1000/femto.2026",
    uri="https://hal.science/hal-05000001v1",
    submitted="2026-06-01 10:00:00",
):
    doc = {
        "title_s": [title], "en_abstract_s": [abstract], "uri_s": uri,
        "submittedDate_s": submitted,
    }
    if doi:
        doc["doiId_s"] = doi
    return doc


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    handler: staticmethod = staticmethod(lambda params: {"response": {"docs": []}})
    fails_on_query: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, **kwargs):
        params = params or {}
        if self.fails_on_query and self.fails_on_query in (params.get("q") or ""):
            return FakeResponse(None, status_code=500)
        return FakeResponse(self.handler(params))


class CollectHalPublicationsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.tech_db = tmp_path / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"),
            patch.object(dbmod, "MARKET_DB", tmp_path / "market.db"),
            patch.object(dbmod, "TECH_DB", self.tech_db),
        ):
            dbmod.init_databases()
        import hal
        self.tech_patch = patch.object(hal, "TECH_DB", self.tech_db)
        self.tech_patch.start()
        self.addCleanup(self.tech_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": []}})
        FakeClient.fails_on_query = None

    def test_on_topic_document_is_queued_as_unlinked(self):
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": [_hal_doc()]}})
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_hal_publications()
        self.assertGreaterEqual(result["added"], 1)
        self.assertEqual(result["errors"], 0)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT title,doi,source_url FROM unlinked_documents").fetchone()
        self.assertIn("Femtosecond laser micromachining", row["title"])
        self.assertEqual(row["doi"], "10.1000/femto.2026")

    def test_off_topic_document_is_not_queued(self):
        off_topic = _hal_doc(
            title="Predictive maintenance for railway rolling stock",
            abstract="Digital twin framework for railway safety, no laser process involved.",
        )
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": [off_topic]}})
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_hal_publications()
        self.assertEqual(result["added"], 0)
        with dbmod.connect(self.tech_db) as db:
            count = db.execute("SELECT COUNT(*) FROM unlinked_documents").fetchone()[0]
        self.assertEqual(count, 0)

    def test_document_without_doi_falls_back_to_uri_fingerprint(self):
        doc = _hal_doc(doi=None, uri="https://hal.science/hal-05000002v1")
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": [doc]}})
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_hal_publications()
        self.assertGreaterEqual(result["added"], 1)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT doi,source_url FROM unlinked_documents").fetchone()
        self.assertIsNone(row["doi"])
        self.assertEqual(row["source_url"], "https://hal.science/hal-05000002v1")

    def test_second_run_does_not_duplicate(self):
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": [_hal_doc()]}})
        with patch.object(http_client.httpx, "Client", FakeClient):
            first = collect_hal_publications()
            second = collect_hal_publications()
        self.assertGreaterEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        with dbmod.connect(self.tech_db) as db:
            count = db.execute("SELECT COUNT(*) FROM unlinked_documents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_query_failure_does_not_block_others(self):
        FakeClient.fails_on_query = "femtosecond laser micromachining"
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": [_hal_doc()]}})
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_hal_publications()
        self.assertGreater(result["errors"], 0)
        self.assertGreaterEqual(result["added"], 1)  # les 5 autres requêtes ont quand meme tourne

    def test_document_already_in_documents_table_is_never_requeued(self):
        # Meme regle de perimetre que scrapers.scrape_technology (13/09/2026) : un document deja
        # attribue a un acteur ne doit jamais revenir dans la file d'attente.
        with dbmod.connect(self.tech_db) as db:
            dbmod.upsert_document(
                db, document_type="publication",
                title="Femtosecond laser micromachining of fused silica microchannels",
                source_url="https://hal.science/hal-05000001v1",
                fingerprint_source="10.1000/femto.2026",
                actor_name="ALPHANOV",
            )
        FakeClient.handler = staticmethod(lambda params: {"response": {"docs": [_hal_doc()]}})
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_hal_publications()
        self.assertEqual(result["added"], 0)
        with dbmod.connect(self.tech_db) as db:
            unlinked_count = db.execute("SELECT COUNT(*) FROM unlinked_documents").fetchone()[0]
        self.assertEqual(unlinked_count, 0)


if __name__ == "__main__":
    unittest.main()
