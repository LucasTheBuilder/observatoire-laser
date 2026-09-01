"""Tests pour l'identité de groupe GLEIF/LEI (gleif.py, Lot 3 §3.6).

httpx.Client est remplacé par un double rejouant des réponses canned -- jamais d'appel réseau
réel ici. La forme des réponses (JSON:API, data.attributes.entity.legalName.name/status,
404 sur /direct-parent ou /ultimate-parent quand aucune relation n'est reportée) est celle
vérifiée manuellement contre la vraie api.gleif.org avant d'écrire ce module -- y compris les
LEI de GLEIF_LEI_ALIASES eux-mêmes, chacun croisé à la main (adresse/pays/nom commercial) avec
le site officiel de l'acteur correspondant.
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
import gleif
from gleif import GLEIF_LEI_ALIASES, collect_gleif_group_identity


def _record_payload(lei: str, *, legal_name: str, status: str = "ACTIVE") -> dict:
    return {"data": {"id": lei, "attributes": {"entity": {"legalName": {"name": legal_name}, "status": status}}}}


def _parent_payload(lei: str, legal_name: str) -> dict:
    return {"data": {"id": lei, "attributes": {"entity": {"legalName": {"name": legal_name}}}}}


class FakeResponse:
    def __init__(self, payload: dict | None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    # keyed by LEI; value: {"legal_name", "status", "direct_parent": name|None, "ultimate_parent": name|None}
    canned: dict[str, dict] = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        path = url.split("/lei-records/", 1)[1]
        if path.endswith("/direct-parent"):
            lei, relation = path[: -len("/direct-parent")], "direct_parent"
        elif path.endswith("/ultimate-parent"):
            lei, relation = path[: -len("/ultimate-parent")], "ultimate_parent"
        else:
            lei, relation = path, None
        entry = self.canned.get(lei)
        if entry is None:
            return FakeResponse(None, status_code=404)
        if relation is None:
            return FakeResponse(_record_payload(lei, legal_name=entry["legal_name"], status=entry.get("status", "ACTIVE")))
        parent_name = entry.get(relation)
        if not parent_name:
            return FakeResponse(None, status_code=404)
        return FakeResponse(_parent_payload(f"{lei}-{relation}", parent_name))


class CollectGleifGroupIdentityTests(unittest.TestCase):
    def _seed_actors(self, actors_db: Path, names: list[str]) -> None:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
        for name in names:
            dbmod.create_actor(name, "Test", "Test", f"https://{name.lower().replace(' ', '')}.example/", priority=False)

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.actors_db = tmp_path / "actors.db"
        # db.create_actor()/connect() resolve ACTORS_DB from db.py's OWN module namespace, not
        # gleif's imported copy -- both must stay patched for the whole test, not just during
        # init_databases(), or _seed_actors()/create_actor() silently fall through to the real
        # production data/actors.db (bind-mounted into the docker container) once the narrower
        # patch exits.
        self.dbmod_actors_patch = patch.object(dbmod, "ACTORS_DB", self.actors_db)
        self.dbmod_market_patch = patch.object(dbmod, "MARKET_DB", tmp_path / "market.db")
        self.dbmod_tech_patch = patch.object(dbmod, "TECH_DB", tmp_path / "technology.db")
        self.dbmod_actors_patch.start()
        self.dbmod_market_patch.start()
        self.dbmod_tech_patch.start()
        self.addCleanup(self.dbmod_actors_patch.stop)
        self.addCleanup(self.dbmod_market_patch.stop)
        self.addCleanup(self.dbmod_tech_patch.stop)
        dbmod.init_databases()
        self.actors_patch = patch.object(gleif, "ACTORS_DB", self.actors_db)
        self.actors_patch.start()
        self.addCleanup(self.actors_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        FakeClient.canned = {}

    def test_parent_group_is_written_when_gleif_reports_one(self):
        lei = GLEIF_LEI_ALIASES["Meliad"]
        self._seed_actors(self.actors_db, ["Meliad"])
        FakeClient.canned = {lei: {"legal_name": "MELIAD", "direct_parent": "NEWEDGE"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            result = collect_gleif_group_identity()
        self.assertEqual(1, result["matched"])
        self.assertEqual(1, result["profiles_added"])
        self.assertEqual(1, result["parent_groups_found"])
        self.assertEqual(0, result["errors"])
        with dbmod.connect(self.actors_db) as db:
            row = db.execute(
                """SELECT parent_group,lei,lei_active,lei_source_url
                   FROM actor_profile p JOIN actors a ON a.id=p.actor_id WHERE a.name='Meliad'"""
            ).fetchone()
        self.assertEqual("NEWEDGE", row["parent_group"])
        self.assertEqual(lei, row["lei"])
        self.assertEqual(1, row["lei_active"])
        self.assertIn(lei, row["lei_source_url"])

    def test_gleif_never_overwrites_a_national_registry_id(self):
        # Régression : GLEIF et firmographics écrivaient tous deux registry_id/registry_name/
        # registry_active, donc le dernier collecteur exécuté effaçait l'identifiant de l'autre.
        # Constaté en base avant correction (Meliad, acteur français, n'avait plus que son LEI).
        # Les deux identifiants doivent désormais coexister sur la même ligne.
        lei = GLEIF_LEI_ALIASES["Meliad"]
        self._seed_actors(self.actors_db, ["Meliad"])
        stamp = dbmod.utc_now()
        with dbmod.connect(self.actors_db) as db:
            actor_id = db.execute("SELECT id FROM actors WHERE name='Meliad'").fetchone()["id"]
            db.execute(
                """INSERT INTO actor_profile(actor_id,founded_year,registry_id,registry_name,
                       registry_active,source_url,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (actor_id, 2015, "812345678", "France (INSEE/RNE)", 1,
                 "https://recherche-entreprises.api.gouv.fr/x", stamp, stamp),
            )
        FakeClient.canned = {lei: {"legal_name": "MELIAD"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            collect_gleif_group_identity()
        with dbmod.connect(self.actors_db) as db:
            row = db.execute(
                """SELECT registry_id,registry_name,founded_year,lei,lei_active
                   FROM actor_profile p JOIN actors a ON a.id=p.actor_id WHERE a.name='Meliad'"""
            ).fetchone()
        self.assertEqual("812345678", row["registry_id"], "le SIREN a été écrasé par GLEIF")
        self.assertEqual("France (INSEE/RNE)", row["registry_name"])
        self.assertEqual(2015, row["founded_year"], "les firmographies nationales ont été perdues")
        self.assertEqual(lei, row["lei"])
        self.assertEqual(1, row["lei_active"])

    def test_falls_back_to_ultimate_parent_when_no_direct_parent(self):
        lei = GLEIF_LEI_ALIASES["LASEA"]
        self._seed_actors(self.actors_db, ["LASEA"])
        FakeClient.canned = {lei: {"legal_name": "LASER ENGINEERING APPLICATIONS", "ultimate_parent": "SOME GROUP"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            result = collect_gleif_group_identity()
        self.assertEqual(1, result["parent_groups_found"])
        with dbmod.connect(self.actors_db) as db:
            row = db.execute(
                "SELECT parent_group FROM actor_profile p JOIN actors a ON a.id=p.actor_id WHERE a.name='LASEA'"
            ).fetchone()
        self.assertEqual("SOME GROUP", row["parent_group"])

    def test_no_reported_relationship_leaves_parent_group_null_not_guessed(self):
        lei = GLEIF_LEI_ALIASES["3D-Micromac"]
        self._seed_actors(self.actors_db, ["3D-Micromac"])
        FakeClient.canned = {lei: {"legal_name": "3D-Micromac AG"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            result = collect_gleif_group_identity()
        self.assertEqual(1, result["matched"])
        self.assertEqual(0, result["parent_groups_found"])
        with dbmod.connect(self.actors_db) as db:
            row = db.execute(
                "SELECT parent_group,lei FROM actor_profile p JOIN actors a ON a.id=p.actor_id WHERE a.name='3D-Micromac'"
            ).fetchone()
        self.assertIsNone(row["parent_group"])
        self.assertEqual(lei, row["lei"])

    def test_a_later_run_without_a_relationship_does_not_erase_a_previously_known_parent_group(self):
        lei = GLEIF_LEI_ALIASES["Meliad"]
        self._seed_actors(self.actors_db, ["Meliad"])
        FakeClient.canned = {lei: {"legal_name": "MELIAD", "direct_parent": "NEWEDGE"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            collect_gleif_group_identity()
            # GLEIF later stops reporting the relationship (or a transient gap) -- must not
            # be interpreted as "parent removed" and wipe the previously captured value.
            FakeClient.canned = {lei: {"legal_name": "MELIAD"}}
            result = collect_gleif_group_identity()
        self.assertEqual(1, result["profiles_updated"])
        with dbmod.connect(self.actors_db) as db:
            row = db.execute(
                "SELECT parent_group FROM actor_profile p JOIN actors a ON a.id=p.actor_id WHERE a.name='Meliad'"
            ).fetchone()
        self.assertEqual("NEWEDGE", row["parent_group"])

    def test_second_run_updates_rather_than_duplicates(self):
        lei = GLEIF_LEI_ALIASES["Tekniker"]
        self._seed_actors(self.actors_db, ["Tekniker"])
        FakeClient.canned = {lei: {"legal_name": "FUNDACION TEKNIKER"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            first = collect_gleif_group_identity()
            second = collect_gleif_group_identity()
        self.assertEqual(1, first["profiles_added"])
        self.assertEqual(0, second["profiles_added"])
        self.assertEqual(1, second["profiles_updated"])
        with dbmod.connect(self.actors_db) as db:
            count = db.execute("SELECT COUNT(*) FROM actor_profile").fetchone()[0]
        self.assertEqual(1, count)

    def test_inactive_status_is_recorded(self):
        lei = GLEIF_LEI_ALIASES["KMLT"]
        self._seed_actors(self.actors_db, ["KMLT"])
        FakeClient.canned = {lei: {"legal_name": "KMLT GmbH", "status": "LAPSED"}}
        with patch.object(gleif.httpx, "Client", FakeClient):
            collect_gleif_group_identity()
        with dbmod.connect(self.actors_db) as db:
            row = db.execute(
                "SELECT lei_active FROM actor_profile p JOIN actors a ON a.id=p.actor_id WHERE a.name='KMLT'"
            ).fetchone()
        self.assertEqual(0, row["lei_active"])

    def test_actor_not_in_live_roster_is_skipped_without_error(self):
        with dbmod.connect(self.actors_db) as db:
            db.execute("DELETE FROM actors")  # none of the aliased actors exist any more
        with patch.object(gleif.httpx, "Client", FakeClient):
            result = collect_gleif_group_identity()
        self.assertEqual(0, result["matched"])
        self.assertEqual(0, result["errors"])

    def test_fetch_failure_is_counted_as_an_error_not_written(self):
        self._seed_actors(self.actors_db, ["LightFab"])
        FakeClient.canned = {}  # 404 on the base record itself -> raise_for_status raises
        with patch.object(gleif.httpx, "Client", FakeClient):
            result = collect_gleif_group_identity()
        self.assertEqual(0, result["matched"])
        self.assertEqual(1, result["errors"])
        with dbmod.connect(self.actors_db) as db:
            count = db.execute("SELECT COUNT(*) FROM actor_profile").fetchone()[0]
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
