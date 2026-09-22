"""Tests pour les signaux de demande (demand_signals.py, Lot 4 §15) : TED + BOAMP.

httpx.Client est remplacé par un double rejouant des réponses canned -- jamais d'appel réseau
réel ici. Les formes de réponse (TED : notices[].{publication-number,notice-title,publication-
date,buyer-name,links} ; BOAMP v2 Explore : records[].record.fields.{idweb,objet,nomacheteur,
dateparution,url_avis}) sont celles vérifiées manuellement (curl) contre les vraies API avant
d'écrire ce module -- y compris le piège trouvé et écarté : l'ancien mirroir BOAMP
/api/records/1.0/search/ est figé vers 2015/2022, /api/explore/v2.0 est la bonne base.
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
import demand_signals
from demand_signals import (
    _parse_boamp_record,
    _parse_ted_notice,
    _ted_buyer_name,
    _ted_notice_title,
    collect_demand_signals,
)


def _ted_notice(
    number="195271-2026", title="Femtosecond laser micromachining system for research laboratory",
    buyer="Chalmers Tekniska Hogskola", published="2026-03-20+01:00",
):
    return {
        "publication-number": number,
        "notice-title": {"eng": title},
        "publication-date": published,
        "buyer-name": {"eng": [buyer]},
        "links": {"html": {"ENG": f"https://ted.europa.eu/en/notice/-/detail/{number}"}},
    }


def _boamp_fields(
    idweb="26-29738", objet="Achat d'une chaine laser femtoseconde compacte pour l'ICMMO",
    buyer="Universite Paris-Saclay", dateparution="2026-03-24",
):
    return {
        "idweb": idweb, "objet": objet, "nomacheteur": buyer, "dateparution": dateparution,
        "url_avis": f"https://www.boamp.fr/pages/avis/?q=idweb:{idweb}",
    }


class TedParsingTests(unittest.TestCase):
    def test_title_prefers_english(self):
        notice = _ted_notice()
        notice["notice-title"] = {"swe": "Sverige laser", "eng": "English title"}
        self.assertEqual(_ted_notice_title(notice), "English title")

    def test_title_falls_back_to_any_language_when_no_english(self):
        notice = _ted_notice()
        notice["notice-title"] = {"swe": "Only Swedish"}
        self.assertEqual(_ted_notice_title(notice), "Only Swedish")

    def test_buyer_name_picks_first_non_empty(self):
        notice = _ted_notice()
        notice["buyer-name"] = {"swe": [], "eng": ["Real Buyer"]}
        self.assertEqual(_ted_buyer_name(notice), "Real Buyer")

    def test_parse_returns_none_without_publication_number(self):
        notice = _ted_notice()
        del notice["publication-number"]
        self.assertIsNone(_parse_ted_notice(notice))

    def test_parse_extracts_expected_fields(self):
        item = _parse_ted_notice(_ted_notice())
        self.assertEqual(item["source"], "TED")
        self.assertEqual(item["external_id"], "195271-2026")
        self.assertEqual(item["published_at"], "2026-03-20")
        self.assertIn("ted.europa.eu", item["url"])


class BoampParsingTests(unittest.TestCase):
    def test_parse_returns_none_without_idweb(self):
        fields = _boamp_fields()
        del fields["idweb"]
        self.assertIsNone(_parse_boamp_record(fields))

    def test_parse_extracts_expected_fields(self):
        item = _parse_boamp_record(_boamp_fields())
        self.assertEqual(item["source"], "BOAMP")
        self.assertEqual(item["external_id"], "26-29738")
        self.assertEqual(item["buyer_name"], "Universite Paris-Saclay")
        self.assertEqual(item["published_at"], "2026-03-24")
        self.assertIn("idweb:26-29738", item["url"])


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
    ted_handler: staticmethod = staticmethod(lambda payload: {"notices": []})
    boamp_handler: staticmethod = staticmethod(lambda params: {"records": []})
    ted_fails = False
    boamp_fails = False

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, **kwargs):
        if self.ted_fails:
            return FakeResponse(None, status_code=500)
        return FakeResponse(self.ted_handler(json))

    def get(self, url, params=None, **kwargs):
        if self.boamp_fails:
            return FakeResponse(None, status_code=500)
        return FakeResponse(self.boamp_handler(params or {}))


class CollectDemandSignalsTests(unittest.TestCase):
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
        self.market_patch = patch.object(demand_signals, "MARKET_DB", self.market_db)
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        FakeClient.ted_handler = staticmethod(lambda payload: {"notices": []})
        FakeClient.boamp_handler = staticmethod(lambda params: {"records": []})
        FakeClient.ted_fails = False
        FakeClient.boamp_fails = False

    def test_on_topic_ted_notice_is_added(self):
        FakeClient.ted_handler = staticmethod(lambda payload: {"notices": [_ted_notice()]})
        with patch.object(demand_signals.httpx, "Client", FakeClient):
            result = collect_demand_signals(include_cofunding=False)
        self.assertGreaterEqual(result["ted_added"], 1)
        self.assertEqual(result["errors"], 0)
        with dbmod.connect(self.market_db) as db:
            row = db.execute("SELECT signal_type,source,buyer_name,title FROM demand_signals WHERE source='TED'").fetchone()
        self.assertEqual(row["signal_type"], "tender")
        self.assertEqual(row["buyer_name"], "Chalmers Tekniska Hogskola")

    def test_on_topic_boamp_record_is_added(self):
        FakeClient.boamp_handler = staticmethod(lambda params: {"records": [{"record": {"fields": _boamp_fields()}}]})
        with patch.object(demand_signals.httpx, "Client", FakeClient):
            result = collect_demand_signals(include_cofunding=False)
        self.assertGreaterEqual(result["boamp_added"], 1)
        with dbmod.connect(self.market_db) as db:
            row = db.execute("SELECT source,buyer_name,published_at FROM demand_signals WHERE source='BOAMP'").fetchone()
        self.assertEqual(row["buyer_name"], "Universite Paris-Saclay")
        self.assertEqual(row["published_at"], "2026-03-24")

    def test_off_topic_notice_is_not_added(self):
        FakeClient.ted_handler = staticmethod(lambda payload: {"notices": [_ted_notice(title="Supply of office paper and stationery")]})
        with patch.object(demand_signals.httpx, "Client", FakeClient):
            result = collect_demand_signals(include_cofunding=False)
        self.assertEqual(result["ted_added"], 0)
        with dbmod.connect(self.market_db) as db:
            count = db.execute("SELECT COUNT(*) FROM demand_signals").fetchone()[0]
        self.assertEqual(count, 0)

    def test_second_run_deduplicates_by_source_and_external_id(self):
        FakeClient.ted_handler = staticmethod(lambda payload: {"notices": [_ted_notice()]})
        with patch.object(demand_signals.httpx, "Client", FakeClient):
            first = collect_demand_signals(include_cofunding=False)
            second = collect_demand_signals(include_cofunding=False)
        self.assertGreaterEqual(first["ted_added"], 1)
        self.assertEqual(second["ted_added"], 0)
        with dbmod.connect(self.market_db) as db:
            count = db.execute("SELECT COUNT(*) FROM demand_signals WHERE source='TED'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_ted_failure_does_not_block_boamp(self):
        FakeClient.ted_fails = True
        FakeClient.boamp_handler = staticmethod(lambda params: {"records": [{"record": {"fields": _boamp_fields()}}]})
        with patch.object(demand_signals.httpx, "Client", FakeClient):
            result = collect_demand_signals(include_cofunding=False)
        self.assertGreater(result["errors"], 0)
        self.assertGreaterEqual(result["boamp_added"], 1)


if __name__ == "__main__":
    unittest.main()
