"""Tests du helper partagé d'écriture des preuves (db.upsert_fact_source, étape 7 de l'audit).

Le patron « un fait + ses citations sourcées » était implémenté trois fois (evidence_sources,
offer_sources, technology_signal_sources), chacune avec sa copie du même INSERT OR IGNORE. Ces
tests verrouillent le contrat commun : dédoublonnage sur l'empreinte, comptage des insertions,
et tolérance aux champs non pertinents pour une table donnée.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from db import upsert_fact_source


class UpsertFactSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self._originals = (dbmod.DATA_DIR, dbmod.ACTORS_DB, dbmod.MARKET_DB, dbmod.TECH_DB)
        dbmod.DATA_DIR = root
        dbmod.ACTORS_DB = root / "actors.db"
        dbmod.MARKET_DB = root / "market.db"
        dbmod.TECH_DB = root / "technology.db"
        self.addCleanup(self._restore)
        dbmod.init_databases()

    def _restore(self):
        dbmod.DATA_DIR, dbmod.ACTORS_DB, dbmod.MARKET_DB, dbmod.TECH_DB = self._originals

    def _an_evidence_row(self, db) -> int:
        stamp = dbmod.utc_now()
        key = dbmod.market_fact_key("Acme", "radar", "Médical", "Stents", "Microdécoupe")
        return int(db.execute(
            """INSERT INTO evidence(actor_name,bucket,market,component,operation,source_url,quote,
                   source_group,fingerprint,fact_key,evidence_kind,created_at,updated_at)
               VALUES('Acme','radar','Médical','Stents','Microdécoupe','https://a.test','q',
                      ?,?,?, 'market_application',?,?)""",
            (key, key, key, stamp, stamp),
        ).lastrowid)

    def test_same_fingerprint_is_never_inserted_twice(self):
        with dbmod.connect(dbmod.MARKET_DB) as db:
            evidence_id = self._an_evidence_row(db)
            first = upsert_fact_source(
                db, "evidence_sources", evidence_id,
                source_url="https://a.test/p", quote="citation", fingerprint="fp-1",
            )
            second = upsert_fact_source(
                db, "evidence_sources", evidence_id,
                source_url="https://a.test/p", quote="citation", fingerprint="fp-1",
            )
            count = db.execute(
                "SELECT COUNT(*) FROM evidence_sources WHERE evidence_id=?", (evidence_id,)
            ).fetchone()[0]
        self.assertEqual(1, first, "la première écriture doit compter comme une insertion")
        self.assertEqual(0, second, "une empreinte déjà connue ne doit pas compter")
        self.assertEqual(1, count)

    def test_two_distinct_fingerprints_both_persist(self):
        with dbmod.connect(dbmod.MARKET_DB) as db:
            evidence_id = self._an_evidence_row(db)
            upsert_fact_source(db, "evidence_sources", evidence_id,
                               source_url="https://a.test/1", quote="q1", fingerprint="fp-1")
            upsert_fact_source(db, "evidence_sources", evidence_id,
                               source_url="https://a.test/2", quote="q2", fingerprint="fp-2")
            count = db.execute(
                "SELECT COUNT(*) FROM evidence_sources WHERE evidence_id=?", (evidence_id,)
            ).fetchone()[0]
        self.assertEqual(2, count)

    def test_fields_absent_from_the_target_table_are_ignored(self):
        # relation_strength n'existe que sur evidence_sources : le passer à une table qui ne
        # le porte pas ne doit pas faire échouer l'écriture, c'est ce qui permet aux trois
        # appelants de partager une seule signature.
        with dbmod.connect(dbmod.TECH_DB) as db:
            signal_id = int(db.execute(
                """INSERT INTO technology_signals(axis,maturity_stage,bucket,actor_names,source_url,
                       quote,fact_key,fingerprint,created_at,updated_at)
                   VALUES('LIPSS','R&D','radar','[]','https://a.test','q','k','fp',?,?)""",
                (dbmod.utc_now(), dbmod.utc_now()),
            ).lastrowid)
            added = upsert_fact_source(
                db, "technology_signal_sources", signal_id,
                source_url="https://a.test/p", quote="citation", fingerprint="fp-ts",
                relation_strength="direct",  # absent de cette table
                block_heading="Bloc",        # absent de cette table
            )
            row = db.execute(
                "SELECT source_url,quote FROM technology_signal_sources WHERE signal_id=?", (signal_id,)
            ).fetchone()
        self.assertEqual(1, added)
        self.assertEqual("https://a.test/p", row["source_url"])

    def test_unknown_table_is_refused(self):
        # Le nom de table entre dans le SQL : il ne doit jamais venir d'ailleurs que de la
        # table blanche _FACT_SOURCE_TABLES.
        with dbmod.connect(dbmod.MARKET_DB) as db, self.assertRaises(ValueError):
            upsert_fact_source(db, "evidence_sources; DROP TABLE evidence", 1,
                               source_url="u", quote="q", fingerprint="f")


if __name__ == "__main__":
    unittest.main()
