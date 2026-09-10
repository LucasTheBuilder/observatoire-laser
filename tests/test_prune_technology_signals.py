"""Tests pour prune_off_topic_sources.prune_technology_signals() (audit du 08/09/2026).

Le nettoyage rétroactif couvrait actors.db et `documents` mais pas `technology_signals` : des
projets européens sans aucun rapport avec le laser (RE4DY, iDriving, EEETHOS) restaient
affichés sur la page Technologie laser alors que le filtre qui les aurait rejetés à la collecte
existait déjà. Ces tests fixent les deux critères de suppression et, surtout, ce qui ne doit
JAMAIS être supprimé : un projet on-topic, et un signal dont la source n'est pas CORDIS.
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

import db as dbmod
import prune_off_topic_sources as prune

PROJECT_COLUMNS = ["id", "acronym", "status", "title", "startDate", "endDate", "objective"]

# Un projet réellement ultrarapide, et un projet "digital twin" sans une seule occurrence du mot
# laser -- le faux positif exact trouvé en production (RE4DY, GA 101058384).
PROJECTS = [
    {
        "id": "900001", "acronym": "USPFAB", "status": "SIGNED",
        "title": "Boosting the adoption of Ultrashort Pulsed Laser large scale structuring",
        "objective": "The project develops ultrashort pulsed laser microstructuring with dynamic control beam shaping.",
    },
    {
        "id": "900002", "acronym": "DATAFAB", "status": "SIGNED",
        "title": "European Data as a Product Value Ecosystems for Resilient Factory 4.0",
        "objective": "Only businesses that can articulate their data, based on AI and digital twin solutions, will react rapidly.",
    },
]


def _fixture_zip(path: Path) -> Path:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=PROJECT_COLUMNS, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in PROJECTS:
        writer.writerow({column: row.get(column, "") for column in PROJECT_COLUMNS})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.csv", buffer.getvalue().encode("utf-8"))
    return path


def _signal(db, *, axis: str, source_url: str, project_name: str | None) -> int:
    added, signal_id = dbmod.upsert_technology_signal(
        db,
        fact_key=dbmod.technology_signal_key(axis, project_name or source_url),
        axis=axis, maturity_stage="Prototype", bucket="radar", actor_names=[],
        source_url=source_url, quote="citation", field_confidence=0.9,
        project_name=project_name, source_title=project_name or "Document",
    )
    dbmod.upsert_fact_source(
        db, "technology_signal_sources", signal_id,
        source_url=source_url, quote="citation", fingerprint=f"fp-{signal_id}",
    )
    return signal_id


class PruneTechnologySignalsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.tech_db = tmp / "technology.db"
        self.zip_path = _fixture_zip(tmp / "horizon.zip")
        self._patches = [
            patch.object(dbmod, "ACTORS_DB", tmp / "actors.db"),
            patch.object(dbmod, "MARKET_DB", tmp / "market.db"),
            patch.object(dbmod, "TECH_DB", self.tech_db),
            patch.object(prune, "TECH_DB", self.tech_db),
        ]
        for item in self._patches:
            item.start()
        dbmod.init_databases()

    def tearDown(self):
        for item in self._patches:
            item.stop()
        self._tmp.cleanup()

    def _axes(self) -> set[str]:
        return {row["axis"] for row in dbmod.rows(self.tech_db, "SELECT axis FROM technology_signals")}

    def test_off_topic_cordis_signal_is_removed_and_on_topic_one_is_kept(self):
        with dbmod.connect(self.tech_db) as db:
            _signal(db, axis="Beam shaping", source_url="https://cordis.europa.eu/project/id/900001", project_name="USPFAB")
            _signal(db, axis="Monitoring IA procédé", source_url="https://cordis.europa.eu/project/id/900002", project_name="DATAFAB")

        report = prune.prune_technology_signals(caches=[self.zip_path])

        self.assertEqual(1, report["signals_off_topic"])
        self.assertEqual(1, report["signals_removed"])
        self.assertEqual({"Beam shaping"}, self._axes())

    def test_removing_a_signal_takes_its_sources_with_it(self):
        with dbmod.connect(self.tech_db) as db:
            _signal(db, axis="Monitoring IA procédé", source_url="https://cordis.europa.eu/project/id/900002", project_name="DATAFAB")
        self.assertEqual(1, dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM technology_signal_sources"))

        prune.prune_technology_signals(caches=[self.zip_path])

        self.assertEqual(0, dbmod.scalar(self.tech_db, "SELECT COUNT(*) FROM technology_signal_sources"))

    def test_signal_sourced_outside_cordis_is_never_removed(self):
        # Femtocell est documenté sur le site d'ALPHANOV : son projet n'est dans aucun cache
        # CORDIS, et l'absence de preuve ne doit jamais valoir preuve d'absence.
        with dbmod.connect(self.tech_db) as db:
            _signal(
                db, axis="Fabrication roll-to-roll (batteries)",
                source_url="https://www.alphanov.com/en/collaborative-projects/femtocell", project_name="Femtocell",
            )

        report = prune.prune_technology_signals(caches=[self.zip_path])

        self.assertEqual(0, report["signals_removed"])
        self.assertEqual({"Fabrication roll-to-roll (batteries)"}, self._axes())

    def test_cordis_project_absent_from_cache_is_never_removed(self):
        with dbmod.connect(self.tech_db) as db:
            _signal(db, axis="SLE", source_url="https://cordis.europa.eu/project/id/424242", project_name="INCONNU")

        report = prune.prune_technology_signals(caches=[self.zip_path])

        self.assertEqual(0, report["signals_removed"])
        self.assertEqual({"SLE"}, self._axes())

    def test_document_signal_is_kept_while_its_document_exists(self):
        with dbmod.connect(self.tech_db) as db:
            dbmod.upsert_document(
                db, document_type="publication", title="SLE of fused silica",
                source_url="https://doi.org/10.1/kept", fingerprint_source="10.1/kept",
            )
            _signal(db, axis="SLE", source_url="https://doi.org/10.1/kept", project_name=None)

        report = prune.prune_technology_signals(caches=[self.zip_path])

        self.assertEqual(0, report["signals_removed"])
        self.assertEqual({"SLE"}, self._axes())

    def test_document_signal_orphaned_by_the_document_prune_is_removed(self):
        with dbmod.connect(self.tech_db) as db:
            _signal(db, axis="DLIP", source_url="https://doi.org/10.1/gone", project_name=None)

        report = prune.prune_technology_signals(caches=[self.zip_path])

        self.assertEqual(1, report["signals_orphaned"])
        self.assertEqual(set(), self._axes())

    def test_no_cordis_signal_does_not_require_the_cache(self):
        # Le cache CORDIS pèse ~37 Mo : ne pas l'exiger quand aucun signal n'en vient.
        with dbmod.connect(self.tech_db) as db:
            dbmod.upsert_document(
                db, document_type="publication", title="SLE of fused silica",
                source_url="https://doi.org/10.1/kept", fingerprint_source="10.1/kept",
            )
            _signal(db, axis="SLE", source_url="https://doi.org/10.1/kept", project_name=None)

        report = prune.prune_technology_signals(caches=[])

        self.assertEqual(0, report["signals_removed"])


if __name__ == "__main__":
    unittest.main()
