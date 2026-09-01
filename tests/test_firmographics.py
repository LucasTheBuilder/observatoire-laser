"""Tests pour le socle firmographique (chantiers 3/5) : firmographics.collect_french_registry().

Le client HTTP réel n'est jamais touché ici -- httpx.Client est remplacé par un double qui
rejoue des réponses canned, indexées par SIREN, construites à partir de la vraie forme de
réponse de recherche-entreprises.api.gouv.fr (vérifiée manuellement avant d'écrire ce module).
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
import firmographics
from firmographics import FRENCH_REGISTRY_ALIASES, _parse_registry_row, collect_french_registry


def _canned_result(siren: str, *, date_creation: str, nature_juridique: str, tranche: str, active: bool = True) -> dict:
    return {
        "siren": siren,
        "nom_raison_sociale": "WHATEVER",
        "date_creation": date_creation,
        "nature_juridique": nature_juridique,
        "tranche_effectif_salarie": tranche,
        "etat_administratif": "A" if active else "C",
    }


class ParseRegistryRowTests(unittest.TestCase):
    def test_extracts_founded_year_and_codes(self):
        row = _canned_result("493635817", date_creation="2006-11-24", nature_juridique="9220", tranche="22")
        fields = _parse_registry_row(row)
        self.assertEqual(2006, fields["founded_year"])
        self.assertEqual("9220", fields["legal_form_code"])
        self.assertEqual("22", fields["headcount_bracket_code"])
        self.assertEqual("493635817", fields["registry_id"])
        self.assertTrue(fields["registry_active"])

    def test_missing_or_malformed_date_yields_no_founded_year(self):
        self.assertIsNone(_parse_registry_row(_canned_result("1", date_creation="", nature_juridique="", tranche=""))["founded_year"])
        self.assertIsNone(_parse_registry_row({"siren": "1"})["founded_year"])

    def test_ceased_entity_is_flagged_inactive(self):
        row = _canned_result("1", date_creation="2000-01-01", nature_juridique="5710", tranche="11", active=False)
        self.assertFalse(_parse_registry_row(row)["registry_active"])


class FakeResponse:
    def __init__(self, results: list[dict]):
        self._results = results
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"results": self._results}


class FakeClient:
    # keyed by the "q" query param, mirroring how collect_french_registry() calls the API.
    canned: dict[str, list[dict]] = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, **kwargs):
        siren = (params or {}).get("q", "")
        return FakeResponse(self.canned.get(siren, []))


class CollectFrenchRegistryTests(unittest.TestCase):
    def _seed_actors(self, actors_db: Path, names: list[str]) -> None:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
        for name in names:
            dbmod.create_actor(name, "France", "Test", f"https://{name.lower().replace(' ', '')}.example/", priority=False)

    def test_creates_a_profile_row_per_matched_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(firmographics, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV", "MANUTECH USD", "IREPA LASER", "Some Untracked Actor"])

                FakeClient.canned = {
                    FRENCH_REGISTRY_ALIASES["ALPHANOV"]: [_canned_result(
                        FRENCH_REGISTRY_ALIASES["ALPHANOV"], date_creation="2006-11-24", nature_juridique="9220", tranche="22",
                    )],
                    FRENCH_REGISTRY_ALIASES["MANUTECH USD"]: [_canned_result(
                        FRENCH_REGISTRY_ALIASES["MANUTECH USD"], date_creation="2012-08-01", nature_juridique="5720", tranche="12",
                    )],
                    FRENCH_REGISTRY_ALIASES["IREPA LASER"]: [_canned_result(
                        FRENCH_REGISTRY_ALIASES["IREPA LASER"], date_creation="1995-08-02", nature_juridique="9220", tranche="31",
                    )],
                }
                with patch.object(firmographics, "connector_client", lambda *a, **k: FakeClient()):
                    result = collect_french_registry()

                self.assertEqual(3, result["matched"])
                self.assertEqual(3, result["profiles_added"])
                self.assertEqual(0, result["errors"])

                with dbmod.connect(actors_db) as db:
                    alphanov = db.execute(
                        """SELECT founded_year,legal_form_code,headcount_bracket_code,registry_id,registry_active,source_url
                           FROM actor_profile f JOIN actors a ON a.id=f.actor_id WHERE a.name='ALPHANOV'"""
                    ).fetchone()
                    untracked = db.execute(
                        "SELECT 1 FROM actor_profile f JOIN actors a ON a.id=f.actor_id WHERE a.name='Some Untracked Actor'"
                    ).fetchone()

                self.assertEqual(2006, alphanov["founded_year"])
                self.assertEqual("9220", alphanov["legal_form_code"])
                self.assertEqual("22", alphanov["headcount_bracket_code"])
                self.assertEqual(FRENCH_REGISTRY_ALIASES["ALPHANOV"], alphanov["registry_id"])
                self.assertEqual(1, alphanov["registry_active"])
                self.assertIn(FRENCH_REGISTRY_ALIASES["ALPHANOV"], alphanov["source_url"])
                # An actor with no configured SIREN alias must never get a fabricated profile.
                self.assertIsNone(untracked)

    def test_second_run_updates_rather_than_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(firmographics, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                FakeClient.canned = {
                    FRENCH_REGISTRY_ALIASES["ALPHANOV"]: [_canned_result(
                        FRENCH_REGISTRY_ALIASES["ALPHANOV"], date_creation="2006-11-24", nature_juridique="9220", tranche="22",
                    )],
                }
                with patch.object(firmographics, "connector_client", lambda *a, **k: FakeClient()):
                    first = collect_french_registry()
                    second = collect_french_registry()

                self.assertEqual(1, first["profiles_added"])
                self.assertEqual(0, second["profiles_added"])
                self.assertEqual(1, second["profiles_updated"])
                with dbmod.connect(actors_db) as db:
                    count = db.execute("SELECT COUNT(*) FROM actor_profile").fetchone()[0]
                self.assertEqual(1, count)

    def test_actor_not_in_live_roster_is_skipped_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(firmographics, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")  # none of the aliased actors exist any more
                FakeClient.canned = {}
                with patch.object(firmographics, "connector_client", lambda *a, **k: FakeClient()):
                    result = collect_french_registry()
                self.assertEqual(0, result["matched"])
                self.assertEqual(0, result["errors"])

    def test_mismatched_siren_in_response_is_treated_as_an_error_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(firmographics, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                # API returns a result, but for a different SIREN -- must never be trusted.
                FakeClient.canned = {
                    FRENCH_REGISTRY_ALIASES["ALPHANOV"]: [_canned_result(
                        "000000000", date_creation="2006-11-24", nature_juridique="9220", tranche="22",
                    )],
                }
                with patch.object(firmographics, "connector_client", lambda *a, **k: FakeClient()):
                    result = collect_french_registry()
                self.assertEqual(0, result["matched"])
                self.assertEqual(1, result["errors"])
                with dbmod.connect(actors_db) as db:
                    count = db.execute("SELECT COUNT(*) FROM actor_profile").fetchone()[0]
                self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
