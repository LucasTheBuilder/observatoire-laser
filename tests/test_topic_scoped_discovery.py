"""Tests pour la découverte d'acteurs TOPIC-scoped (Lot 3 §3.2/§3.3, audit veille §4.D/§10.8,
30/08/2026, "élargir le périmètre hors Europe") : cordis.discover_topic_scoped_candidates() et
openalex.discover_global_actor_candidates() -- contrairement au passage actor-scoped existant
(qui ne trouve que les consortiums/co-auteurs d'un acteur DÉJÀ suivi, donc structurellement
européen aujourd'hui), ces deux fonctions ne dépendent d'aucun acteur suivi et peuvent donc
découvrir une organisation 100% hors du réseau connu, où qu'elle soit dans le monde.
"""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cordis
import db as dbmod
import openalex
from cordis import discover_topic_scoped_candidates
from openalex import _fetch_global_on_topic_works, discover_global_actor_candidates

PROJECT_COLUMNS = [
    "id", "acronym", "status", "title", "startDate", "endDate", "totalCost", "ecMaxContribution",
    "topics", "ecSignatureDate", "frameworkProgramme", "masterCall", "subCall", "fundingScheme",
    "nature", "objective", "contentUpdateDate", "rcn", "grantDoi", "keywords", "Human-validated",
    "legalBasis",
]
ORGANIZATION_COLUMNS = [
    "projectID", "projectAcronym", "organisationID", "vatNumber", "name", "shortName", "SME",
    "activityType", "street", "postCode", "city", "country", "nutsCode", "geolocation",
    "organizationURL", "contactForm", "contentUpdateDate", "rcn", "order", "role", "ecContribution",
    "netEcContribution", "totalCost", "endOfParticipation", "active",
]


def _write_csv(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue().encode("utf-8")


def _build_fixture_zip(path: Path, *, projects: list[dict[str, str]], organizations: list[dict[str, str]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.csv", _write_csv(PROJECT_COLUMNS, projects))
        archive.writestr("organization.csv", _write_csv(ORGANIZATION_COLUMNS, organizations))


class DiscoverTopicScopedCandidatesTests(unittest.TestCase):
    def _setup(self, tmp: str) -> Path:
        actors_db = Path(tmp) / "actors.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
            with dbmod.connect(actors_db) as db:
                db.execute("DELETE FROM actors")
        return actors_db

    def test_project_with_zero_tracked_actors_still_surfaces_candidates(self):
        # The exact gap this function fixes: a fully on-topic project whose ONLY participants
        # are organizations nowhere in the tracked roster -- e.g. non-European ones, matching
        # the user's original question about global discovery.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            zip_path = Path(tmp) / "cordis.zip"
            _build_fixture_zip(
                zip_path,
                projects=[{
                    "id": "888001", "acronym": "FARLASER", "status": "SIGNED",
                    "title": "Femtosecond laser micromachining for global manufacturing",
                    "objective": "Selective laser etching (SLE) of fused silica for advanced photonic components.",
                }],
                organizations=[
                    {"projectID": "888001", "projectAcronym": "FARLASER", "organisationID": "1", "name": "Korea Institute of Photonics", "role": "coordinator", "country": "KR", "organizationURL": "https://kip.example.kr/"},
                    {"projectID": "888001", "projectAcronym": "FARLASER", "organisationID": "2", "name": "Tokyo Laser Technologies", "role": "participant", "country": "JP", "organizationURL": "https://tlt.example.jp/"},
                ],
            )
            with patch.object(cordis, "ACTORS_DB", actors_db):
                added = discover_topic_scoped_candidates(zip_path, {})
            self.assertEqual(2, added)
            rows = dbmod.rows(actors_db, "SELECT name,country,suggested_official_url FROM actor_candidates ORDER BY name")
            self.assertEqual(2, len(rows))
            names = {r["name"] for r in rows}
            self.assertEqual({"Korea Institute of Photonics", "Tokyo Laser Technologies"}, names)
            korea_row = next(r for r in rows if r["name"] == "Korea Institute of Photonics")
            self.assertEqual("KR", korea_row["country"])
            self.assertEqual("https://kip.example.kr/", korea_row["suggested_official_url"])

    def test_off_topic_project_produces_no_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            zip_path = Path(tmp) / "cordis.zip"
            _build_fixture_zip(
                zip_path,
                projects=[{
                    "id": "888002", "acronym": "RAILSAFE2", "status": "SIGNED",
                    "title": "Railway safety and predictive maintenance",
                    "objective": "Predictive maintenance models for railway rolling stock, with no relation to laser processing.",
                }],
                organizations=[
                    {"projectID": "888002", "projectAcronym": "RAILSAFE2", "organisationID": "1", "name": "Some Railway Institute", "role": "coordinator", "country": "DE"},
                ],
            )
            with patch.object(cordis, "ACTORS_DB", actors_db):
                added = discover_topic_scoped_candidates(zip_path, {})
            self.assertEqual(0, added)

    def test_organization_matching_a_tracked_actor_alias_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                dbmod.create_actor("ALPHANOV", "France", "Test", "https://www.alphanov.com")
            zip_path = Path(tmp) / "cordis.zip"
            _build_fixture_zip(
                zip_path,
                projects=[{
                    "id": "888003", "acronym": "GLASSFAB2", "status": "SIGNED",
                    "title": "Femtosecond glass microfabrication",
                    "objective": "Selective laser etching (SLE) of fused silica for photonic components.",
                }],
                organizations=[
                    {"projectID": "888003", "projectAcronym": "GLASSFAB2", "organisationID": "1", "name": "ALPHANOV", "role": "coordinator", "country": "FR"},
                    {"projectID": "888003", "projectAcronym": "GLASSFAB2", "organisationID": "2", "name": "New Laser Startup", "role": "participant", "country": "US"},
                ],
            )
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actors = [dict(row) for row in dbmod.rows(actors_db, "SELECT id,name FROM actors WHERE active=1")]
            aliases = cordis._actor_aliases(actors)
            with patch.object(cordis, "ACTORS_DB", actors_db):
                added = discover_topic_scoped_candidates(zip_path, aliases)
            self.assertEqual(1, added)
            row = dbmod.rows(actors_db, "SELECT name FROM actor_candidates")[0]
            self.assertEqual("New Laser Startup", row["name"])

    def test_idempotent_on_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            zip_path = Path(tmp) / "cordis.zip"
            _build_fixture_zip(
                zip_path,
                projects=[{
                    "id": "888004", "acronym": "FARLASER2", "status": "SIGNED",
                    "title": "Femtosecond laser micromachining",
                    "objective": "Selective laser etching (SLE) of fused silica.",
                }],
                organizations=[
                    {"projectID": "888004", "projectAcronym": "FARLASER2", "organisationID": "1", "name": "Global Laser Corp", "role": "coordinator", "country": "US"},
                ],
            )
            with patch.object(cordis, "ACTORS_DB", actors_db):
                first = discover_topic_scoped_candidates(zip_path, {})
                second = discover_topic_scoped_candidates(zip_path, {})
            self.assertEqual(1, first)
            self.assertEqual(0, second)


FAKE_WORK_TEMPLATE = {
    "id": "https://openalex.org/W1", "doi": None,
    "title": "Femtosecond laser micromachining of advanced photonic glass",
    "publication_date": "2026-01-01",
    "primary_location": {"landing_page_url": "https://example.test/paper"},
    "authorships": [
        {"institutions": [{"id": "https://openalex.org/I1", "display_name": "Global Photonics Institute", "country_code": "US"}]},
    ],
}


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    handlers: dict[str, object] = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, **kwargs):
        handler = self.handlers.get("/works")
        return FakeResponse(handler(params or {}) if handler else {"results": []})


class DiscoverGlobalActorCandidatesTests(unittest.TestCase):
    def _setup(self, tmp: str) -> Path:
        actors_db = Path(tmp) / "actors.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
            with dbmod.connect(actors_db) as db:
                db.execute("DELETE FROM actors")
        return actors_db

    def test_global_search_surfaces_a_candidate_regardless_of_any_tracked_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            FakeClient.handlers = {"/works": lambda params: {"results": [FAKE_WORK_TEMPLATE]}}
            with patch.object(openalex, "ACTORS_DB", actors_db), patch.object(openalex.httpx, "Client", FakeClient):
                result = discover_global_actor_candidates()
            self.assertGreaterEqual(result["actor_candidates_added"], 1)
            row = dbmod.rows(actors_db, "SELECT name,country FROM actor_candidates")[0]
            self.assertEqual("Global Photonics Institute", row["name"])
            self.assertEqual("US", row["country"])

    def test_off_topic_work_produces_no_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            off_topic_work = dict(FAKE_WORK_TEMPLATE)
            off_topic_work["title"] = "Effects of graded dietary levels of microalgae on poultry growth"
            FakeClient.handlers = {"/works": lambda params: {"results": [off_topic_work]}}
            with patch.object(openalex, "ACTORS_DB", actors_db), patch.object(openalex.httpx, "Client", FakeClient):
                result = discover_global_actor_candidates()
            self.assertEqual(0, result["actor_candidates_added"])

    def test_already_tracked_actor_institution_is_not_surfaced_as_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                dbmod.create_actor("Global Photonics Institute", "US", "Test", "https://gpi.example/")
            FakeClient.handlers = {"/works": lambda params: {"results": [FAKE_WORK_TEMPLATE]}}
            with patch.object(openalex, "ACTORS_DB", actors_db), patch.object(openalex.httpx, "Client", FakeClient):
                result = discover_global_actor_candidates()
            self.assertEqual(0, result["actor_candidates_added"])

    def test_same_work_seen_twice_across_queries_is_only_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = self._setup(tmp)
            FakeClient.handlers = {"/works": lambda params: {"results": [FAKE_WORK_TEMPLATE]}}
            with patch.object(openalex, "ACTORS_DB", actors_db), patch.object(openalex.httpx, "Client", FakeClient):
                discover_global_actor_candidates()
            # TECHNOLOGY_QUERIES has multiple entries, but the fixture always returns the SAME
            # work id -- deduplicated by work id, so exactly one candidate occurrence per name.
            count = dbmod.scalar(actors_db, "SELECT COUNT(*) FROM actor_candidate_sources")
            self.assertEqual(1, count)


class FetchGlobalOnTopicWorksTests(unittest.TestCase):
    def test_network_failure_returns_empty_list(self):
        FakeClient.handlers = {"/works": lambda params: (_ for _ in ()).throw(RuntimeError("boom"))}
        with patch.object(openalex.httpx, "Client", FakeClient):
            with FakeClient() as client:
                result = _fetch_global_on_topic_works(client, "femtosecond laser micromachining", "2026-01-01")
        self.assertEqual([], result)


if __name__ == "__main__":
    unittest.main()
