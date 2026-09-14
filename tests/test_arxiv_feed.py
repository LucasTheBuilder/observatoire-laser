"""Tests pour arxiv_feed.py : collecte thématique arXiv (chantier /sources, 13/09/2026).

httpx.Client est remplacé par un double rejouant un flux Atom fabriqué à la main -- jamais
d'appel réseau réel ici. Le format (feed/entry/id/title/summary/published/arxiv:doi) est celui
documenté publiquement par arXiv (arxiv.org/help/api) mais n'a PAS pu être confronté à une
vraie réponse au moment d'écrire ce module -- l'IP partagée de cet environnement recevait déjà
"Rate exceeded." (HTTP 429) sur toute requête, y compris la toute première (voir arxiv_feed.py).
``time.sleep`` est neutralisé dans ces tests (la politesse inter-requêtes n'a rien à prouver ici).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import http_client
from arxiv_feed import collect_arxiv_preprints

ATOM_NS = 'xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"'


def _atom_entry(
    entry_id="https://arxiv.org/abs/2606.00001v1",
    title="Femtosecond laser micromachining of photonic waveguides",
    summary="We report ultrafast laser processing results for glass microfabrication.",
    published=None,
    doi=None,
):
    published = published or (date.today() - timedelta(days=5)).isoformat() + "T00:00:00Z"
    doi_tag = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return f"""
    <entry>
      <id>{entry_id}</id>
      <title>{title}</title>
      <summary>{summary}</summary>
      <published>{published}</published>
      <updated>{published}</updated>
      {doi_tag}
    </entry>
    """


def _atom_feed(entries: list[str]) -> bytes:
    body = "\n".join(entries)
    return f'<feed {ATOM_NS}>{body}</feed>'.encode("utf-8")


class FakeResponse:
    def __init__(self, content: bytes, status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    handler: staticmethod = staticmethod(lambda params: _atom_feed([]))
    fails_on_query: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, **kwargs):
        params = params or {}
        if self.fails_on_query and self.fails_on_query in (params.get("search_query") or ""):
            return FakeResponse(b"", status_code=500)
        return FakeResponse(self.handler(params))


class CollectArxivPreprintsTests(unittest.TestCase):
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
        import arxiv_feed
        self.tech_patch = patch.object(arxiv_feed, "TECH_DB", self.tech_db)
        self.tech_patch.start()
        self.addCleanup(self.tech_patch.stop)
        self.sleep_patch = patch.object(arxiv_feed.time, "sleep", lambda seconds: None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        FakeClient.handler = staticmethod(lambda params: _atom_feed([]))
        FakeClient.fails_on_query = None

    def test_on_topic_entry_is_queued_as_unlinked(self):
        FakeClient.handler = staticmethod(lambda params: _atom_feed([_atom_entry(doi="10.1000/femto.2026")]))
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_arxiv_preprints()
        self.assertGreaterEqual(result["added"], 1)
        self.assertEqual(result["errors"], 0)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT title,doi,source_url FROM unlinked_documents").fetchone()
        self.assertIn("Femtosecond laser micromachining", row["title"])
        self.assertEqual(row["doi"], "10.1000/femto.2026")

    def test_off_topic_entry_is_not_queued(self):
        off_topic = _atom_entry(
            title="Predictive maintenance for railway rolling stock",
            summary="Digital twin framework for railway safety, no laser process involved.",
        )
        FakeClient.handler = staticmethod(lambda params: _atom_feed([off_topic]))
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_arxiv_preprints()
        self.assertEqual(result["added"], 0)

    def test_entry_older_than_lookback_is_excluded(self):
        old_entry = _atom_entry(published=(date.today() - timedelta(days=400)).isoformat() + "T00:00:00Z")
        FakeClient.handler = staticmethod(lambda params: _atom_feed([old_entry]))
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_arxiv_preprints(lookback_days=60)
        self.assertEqual(result["added"], 0)

    def test_entry_without_doi_falls_back_to_arxiv_url_fingerprint(self):
        FakeClient.handler = staticmethod(
            lambda params: _atom_feed([_atom_entry(entry_id="https://arxiv.org/abs/2606.00002v1")])
        )
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_arxiv_preprints()
        self.assertGreaterEqual(result["added"], 1)
        with dbmod.connect(self.tech_db) as db:
            row = db.execute("SELECT doi,source_url FROM unlinked_documents").fetchone()
        self.assertIsNone(row["doi"])
        self.assertEqual(row["source_url"], "https://arxiv.org/abs/2606.00002v1")

    def test_second_run_does_not_duplicate(self):
        FakeClient.handler = staticmethod(lambda params: _atom_feed([_atom_entry(doi="10.1000/femto.2026")]))
        with patch.object(http_client.httpx, "Client", FakeClient):
            first = collect_arxiv_preprints()
            second = collect_arxiv_preprints()
        self.assertGreaterEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        with dbmod.connect(self.tech_db) as db:
            count = db.execute("SELECT COUNT(*) FROM unlinked_documents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_query_failure_does_not_block_others(self):
        FakeClient.fails_on_query = "femtosecond laser micromachining"
        FakeClient.handler = staticmethod(lambda params: _atom_feed([_atom_entry(doi="10.1000/femto.2026")]))
        with patch.object(http_client.httpx, "Client", FakeClient):
            result = collect_arxiv_preprints()
        self.assertGreater(result["errors"], 0)
        self.assertGreaterEqual(result["added"], 1)


if __name__ == "__main__":
    unittest.main()
