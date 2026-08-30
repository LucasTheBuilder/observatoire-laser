"""Tests pour l'intégration CORDIS (chantier 3) : cordis.collect_cordis().

Construit un petit ZIP "HORIZON Projects" fabriqué à la main (mêmes colonnes/délimiteur que le
vrai jeu de données CORDIS, vérifiées manuellement contre le fichier réel avant d'écrire ce
module) plutôt que de télécharger/parser les ~140 Mo réels à chaque test.
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


PROJECTS = [
    {
        "id": "999001", "acronym": "GLASSFAB", "status": "SIGNED",
        "title": "Next-generation femtosecond glass microfabrication",
        "startDate": "2023-01-01", "endDate": "2026-12-31",
        "objective": (
            "This project develops selective laser etching (SLE) of fused silica for "
            "next-generation photonic components, building on femtosecond laser processing."
        ),
    },
    {
        "id": "999002", "acronym": "FHTEST", "status": "SIGNED",
        "title": "Applied research programme",
        "startDate": "2022-06-01", "endDate": "2025-05-31",
        "objective": "Generic applied research with no laser-specific process mentioned.",
    },
    # Matched to a tracked actor (ALPHANOV, via generalist-institute participation) but off
    # topic: must produce zero events/relations/signals -- see audit v8 §2.1 (Tekniker/CEIT
    # railway/textile/water-treatment projects polluting the observatory's network graph).
    {
        "id": "999003", "acronym": "RAILSAFE", "status": "SIGNED",
        "title": "Railway safety and predictive maintenance",
        "startDate": "2023-03-01", "endDate": "2026-02-28",
        "objective": "Predictive maintenance models for railway rolling stock, with no relation to laser processing.",
    },
]

ORGANIZATIONS = [
    {"projectID": "999001", "projectAcronym": "GLASSFAB", "organisationID": "1", "name": "ALPHANOV", "role": "coordinator", "country": "FR"},
    {"projectID": "999001", "projectAcronym": "GLASSFAB", "organisationID": "2", "name": "IREPA LASER SAS", "role": "participant", "country": "FR"},
    {"projectID": "999001", "projectAcronym": "GLASSFAB", "organisationID": "3", "name": "UNIVERSITY OF SOMEWHERE", "role": "participant", "country": "DE"},
    # Umbrella legal entity -- must never resolve to the tracked "Fraunhofer ILT" actor.
    {"projectID": "999002", "projectAcronym": "FHTEST", "organisationID": "4", "name": "FRAUNHOFER GESELLSCHAFT ZUR FORDERUNG DER ANGEWANDTEN FORSCHUNG EV", "role": "coordinator", "country": "DE"},
    {"projectID": "999003", "projectAcronym": "RAILSAFE", "organisationID": "5", "name": "ALPHANOV", "role": "participant", "country": "FR"},
    {"projectID": "999003", "projectAcronym": "RAILSAFE", "organisationID": "6", "name": "SOME RAILWAY INSTITUTE", "role": "coordinator", "country": "DE"},
]


class CordisIntegrationTests(unittest.TestCase):
    def _seed_actors(self, actors_db: Path) -> dict[str, int]:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
        ids = {}
        for name, url in (
            ("ALPHANOV", "https://www.alphanov.com"),
            ("IREPA LASER", "https://www.irepa-laser.com"),
            ("Fraunhofer ILT", "https://www.ilt.fraunhofer.de"),
        ):
            ids[name] = dbmod.create_actor(name, "France", "Test", url, priority=False)
        return ids

    def test_matches_tracked_actors_and_fills_the_three_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                actor_ids = self._seed_actors(actors_db)

            fixture_zip = Path(tmp) / "fixture.zip"
            _build_fixture_zip(fixture_zip, projects=PROJECTS, organizations=ORGANIZATIONS)

            with (
                patch.object(cordis, "ACTORS_DB", actors_db),
                patch.object(cordis, "TECH_DB", tech_db),
            ):
                result = cordis.collect_cordis(cache_path=fixture_zip)

            # ALPHANOV and IREPA LASER both matched (via the "IREPA" alias); Fraunhofer ILT
            # never matches its umbrella legal entity (see CORDIS_MATCH_BLOCKLIST). ALPHANOV
            # also matches RAILSAFE (off-topic), which must not produce any event/relation.
            self.assertEqual(2, result["actors_matched"])
            self.assertEqual(2, result["projects_matched"])
            self.assertEqual(1, result["projects_off_topic"])
            self.assertEqual(0, result["errors"])
            self.assertEqual(2, result["events_added"])  # one per matched actor on GLASSFAB only
            self.assertEqual(1, result["signals_added"])  # SLE detected in the objective

            with dbmod.connect(actors_db) as db:
                railsafe_events = db.execute(
                    "SELECT COUNT(*) FROM actor_events WHERE description LIKE '%RAILSAFE%'"
                ).fetchone()[0]
                railsafe_relations = db.execute(
                    "SELECT COUNT(*) FROM actor_relations WHERE note LIKE '%RAILSAFE%'"
                ).fetchone()[0]
            self.assertEqual(0, railsafe_events)
            self.assertEqual(0, railsafe_relations)

            with dbmod.connect(actors_db) as db:
                events = db.execute("SELECT actor_id,description,event_date,source_url FROM actor_events").fetchall()
                relations = db.execute("SELECT actor_id,related_name,related_actor_id FROM actor_relations").fetchall()

            self.assertEqual(2, len(events))
            actor_ids_with_events = {row["actor_id"] for row in events}
            self.assertEqual({actor_ids["ALPHANOV"], actor_ids["IREPA LASER"]}, actor_ids_with_events)
            for row in events:
                self.assertIn("GLASSFAB", row["description"])
                self.assertEqual("2023-01-01", row["event_date"])
                self.assertEqual("https://cordis.europa.eu/project/id/999001", row["source_url"])

            # ALPHANOV's relations: IREPA LASER SAS (resolves to the tracked actor) and the
            # university (an untracked but real consortium partner, related_actor_id=None).
            alphanov_relations = {row["related_name"]: row["related_actor_id"] for row in relations if row["actor_id"] == actor_ids["ALPHANOV"]}
            self.assertEqual(actor_ids["IREPA LASER"], alphanov_relations["IREPA LASER SAS"])
            self.assertIn("UNIVERSITY OF SOMEWHERE", alphanov_relations)
            self.assertIsNone(alphanov_relations["UNIVERSITY OF SOMEWHERE"])

            with dbmod.connect(tech_db) as db:
                signal = db.execute("SELECT axis,bucket,project_name,actor_names,quote FROM technology_signals").fetchone()
            self.assertEqual("SLE", signal["axis"])
            self.assertEqual("GLASSFAB", signal["project_name"])
            self.assertIn("ALPHANOV", signal["actor_names"])
            self.assertIn("IREPA LASER", signal["actor_names"])
            self.assertIn("selective laser etching", signal["quote"].lower())

    def test_second_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db)

            fixture_zip = Path(tmp) / "fixture.zip"
            _build_fixture_zip(fixture_zip, projects=PROJECTS, organizations=ORGANIZATIONS)

            with (
                patch.object(cordis, "ACTORS_DB", actors_db),
                patch.object(cordis, "TECH_DB", tech_db),
            ):
                first = cordis.collect_cordis(cache_path=fixture_zip)
                second = cordis.collect_cordis(cache_path=fixture_zip)

            self.assertGreater(first["events_added"], 0)
            self.assertEqual(0, second["events_added"])
            self.assertEqual(0, second["relations_added"])
            self.assertEqual(0, second["signals_added"])

            with dbmod.connect(actors_db) as db:
                event_count = db.execute("SELECT COUNT(*) FROM actor_events").fetchone()[0]
                relation_count = db.execute("SELECT COUNT(*) FROM actor_relations").fetchone()[0]
            self.assertEqual(first["events_added"], event_count)
            self.assertEqual(first["relations_added"], relation_count)

    def test_own_entity_registered_under_its_legal_name_is_not_a_relation(self):
        # Regression: CORDIS can register an actor's own entity under a full legal name that
        # never contains the alias as a substring (e.g. real data: LASEA is registered as
        # "Laser Engineering Applications SA", shortName "LASEA") -- must not create a
        # self-referential "LASEA -> Laser Engineering Applications SA" relation.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                actor_id = dbmod.create_actor("LASEA", "Belgique", "Test", "https://www.lasea.eu", priority=False)

            projects = [{
                "id": "999010", "acronym": "STAY2ME", "status": "SIGNED",
                "title": "Ultrashort-pulse silicon through-hole drilling",
                "startDate": "2024-01-01", "endDate": "2027-12-31",
                "objective": "A GHz-burst femtosecond laser drills silicon with ultrashort pulses at 2 micrometers.",
            }]
            organizations = [
                # Matches the "LASEA" alias via `name` (real-world equivalent: a French
                # subsidiary row) -- this is what makes _match_projects link this project to
                # the actor in the first place, mirroring the real STAY2ME data.
                {"projectID": "999010", "projectAcronym": "STAY2ME", "organisationID": "1",
                 "name": "LASEA FRANCE", "shortName": "", "role": "thirdParty", "country": "FR"},
                # The actor's own entity, registered under its full legal name -- only its
                # shortName carries the alias. This is the row the fix is about.
                {"projectID": "999010", "projectAcronym": "STAY2ME", "organisationID": "2",
                 "name": "LASER ENGINEERING APPLICATIONS SA", "shortName": "LASEA", "role": "participant", "country": "BE"},
                {"projectID": "999010", "projectAcronym": "STAY2ME", "organisationID": "3",
                 "name": "CENTRE NATIONAL DE LA RECHERCHE SCIENTIFIQUE CNRS", "shortName": "CNRS", "role": "coordinator", "country": "FR"},
            ]
            fixture_zip = Path(tmp) / "fixture.zip"
            _build_fixture_zip(fixture_zip, projects=projects, organizations=organizations)

            with (
                patch.object(cordis, "ACTORS_DB", actors_db),
                patch.object(cordis, "TECH_DB", tech_db),
            ):
                result = cordis.collect_cordis(cache_path=fixture_zip)

            self.assertEqual(1, result["events_added"])
            self.assertEqual(1, result["relations_added"])  # CNRS only, not LASEA itself
            with dbmod.connect(actors_db) as db:
                relations = {row["related_name"] for row in db.execute(
                    "SELECT related_name FROM actor_relations WHERE actor_id=?", (actor_id,)
                )}
            self.assertEqual({"CENTRE NATIONAL DE LA RECHERCHE SCIENTIFIQUE CNRS"}, relations)
            self.assertNotIn("LASER ENGINEERING APPLICATIONS SA", relations)
            self.assertNotIn("LASEA FRANCE", relations)

    def test_no_matches_returns_zero_counts_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                dbmod.create_actor("Nobody Matches SARL", "France", "Test", "https://nobody.example/", priority=False)

            fixture_zip = Path(tmp) / "fixture.zip"
            _build_fixture_zip(fixture_zip, projects=PROJECTS, organizations=ORGANIZATIONS)

            with (
                patch.object(cordis, "ACTORS_DB", actors_db),
                patch.object(cordis, "TECH_DB", tech_db),
            ):
                result = cordis.collect_cordis(cache_path=fixture_zip)

            self.assertEqual(0, result["actors_matched"])
            self.assertEqual(0, result["projects_matched"])
            self.assertEqual(0, result["events_added"])


class ProjectIsOnTopicTests(unittest.TestCase):
    def test_femtosecond_project_is_on_topic(self):
        self.assertTrue(cordis._project_is_on_topic({
            "title": "Next-generation femtosecond glass microfabrication",
            "objective": "Selective laser etching (SLE) of fused silica.",
        }))

    def test_named_process_without_laser_wording_is_on_topic(self):
        self.assertTrue(cordis._project_is_on_topic({
            "title": "Photonic components",
            "objective": "Uses selective laser etching (SLE) of glass for waveguide fabrication.",
        }))

    def test_unrelated_project_is_off_topic(self):
        self.assertFalse(cordis._project_is_on_topic({
            "title": "Railway safety and predictive maintenance",
            "objective": "Predictive maintenance models for railway rolling stock.",
        }))

    def test_generic_process_axis_without_laser_wording_is_off_topic(self):
        # Regression: "Monitoring IA procédé" (digital twin, process monitoring...) is
        # intentionally laser-agnostic in PROCESS_TECHNOLOGIES (technology_signals only ever
        # tags it on text that already passed a laser gate elsewhere). Used standalone here,
        # it false-positived on a real Tekniker document with no laser mention at all.
        self.assertFalse(cordis._project_is_on_topic({
            "title": "AI-enriched safety criteria catalogue",
            "objective": "A digital twin framework enables data-driven process optimization and in-line monitoring for predictive safety.",
        }))


class NormalizationHelperTests(unittest.TestCase):
    def test_whole_phrase_matching_respects_word_boundaries(self):
        self.assertTrue(cordis._contains_whole_phrase("ALPHANOV SAS", "ALPHANOV"))
        self.assertFalse(cordis._contains_whole_phrase("METALPHANOVA GMBH", "ALPHANOV"))

    def test_blocklisted_actor_is_excluded_from_aliases(self):
        aliases = cordis._actor_aliases([{"name": "Fraunhofer ILT"}, {"name": "ALPHANOV"}])
        self.assertNotIn("Fraunhofer ILT", aliases)
        self.assertIn("ALPHANOV", aliases)

    def test_short_alias_is_excluded_as_too_generic(self):
        aliases = cordis._actor_aliases([{"name": "HEF"}])
        self.assertNotIn("HEF", aliases)


if __name__ == "__main__":
    unittest.main()
