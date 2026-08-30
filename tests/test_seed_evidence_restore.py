"""Tests pour db.restore_seed_evidence() (audit veille §10.2, 30/08/2026).

SEED_EVIDENCE régénère les 40 faits réellement en production au moment de l'audit -- ces tests
ne vérifient pas leur contenu exact (ça bougera si la base change), seulement que le mécanisme
de recovery lui-même est correct : (1) init_databases() ne l'applique plus automatiquement --
une base fraîche (dont chaque test de la suite) ne doit jamais se retrouver avec ces faits sans
l'avoir demandé ; (2) restore_seed_evidence(), appelé explicitement, insère bien tout,
review_status='review' (jamais 'accepted' d'office) ; (3) idempotent sur un second appel.
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


class SeedEvidenceNotAutoAppliedTests(unittest.TestCase):
    def test_fresh_database_has_no_seed_evidence(self):
        # The exact regression this fix addresses: a brand new database (any test, any new
        # deployment) must never silently gain 40 hand-curated facts about specific actors it
        # never crawled itself.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                count = dbmod.scalar(market_db, "SELECT COUNT(*) FROM evidence")
            self.assertEqual(0, count)


class RestoreSeedEvidenceTests(unittest.TestCase):
    def _fresh_dbs(self, tmp: str) -> tuple[Path, Path, Path]:
        actors_db = Path(tmp) / "actors.db"
        market_db = Path(tmp) / "market.db"
        tech_db = Path(tmp) / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", market_db),
            patch.object(dbmod, "TECH_DB", tech_db),
        ):
            dbmod.init_databases()
        return actors_db, market_db, tech_db

    def test_restore_inserts_every_item_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, market_db, _ = self._fresh_dbs(tmp)
            with patch.object(dbmod, "MARKET_DB", market_db):
                inserted = dbmod.restore_seed_evidence()
            count = dbmod.scalar(market_db, "SELECT COUNT(*) FROM evidence")
            self.assertEqual(len(dbmod.SEED_EVIDENCE), inserted)
            self.assertEqual(len(dbmod.SEED_EVIDENCE), count)

    def test_restored_facts_enter_as_review_not_accepted(self):
        # Audit veille §10.2: a seed fact must never enter validated/accepted automatically --
        # it has to pass through real human review like anything else, unlike the pre-fix
        # behaviour that hardcoded review_status='accepted'.
        with tempfile.TemporaryDirectory() as tmp:
            _, market_db, _ = self._fresh_dbs(tmp)
            with patch.object(dbmod, "MARKET_DB", market_db):
                dbmod.restore_seed_evidence()
            statuses = {
                row["review_status"] for row in dbmod.rows(market_db, "SELECT DISTINCT review_status FROM evidence")
            }
            self.assertEqual({"review"}, statuses)

    def test_restore_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, market_db, _ = self._fresh_dbs(tmp)
            with patch.object(dbmod, "MARKET_DB", market_db):
                first = dbmod.restore_seed_evidence()
                second = dbmod.restore_seed_evidence()
            self.assertEqual(len(dbmod.SEED_EVIDENCE), first)
            self.assertEqual(0, second)
            count = dbmod.scalar(market_db, "SELECT COUNT(*) FROM evidence")
            self.assertEqual(len(dbmod.SEED_EVIDENCE), count)


if __name__ == "__main__":
    unittest.main()
