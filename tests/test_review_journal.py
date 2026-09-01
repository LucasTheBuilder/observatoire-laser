"""Journal append-only des décisions de revue (phase 0.2 du plan agent d'analyse).

Les colonnes de trace posées sur chaque table disent l'état ACTUEL d'un item. Le journal existe
pour les deux choses qu'elles ne peuvent pas faire, et ce sont ces deux-là que ces tests
verrouillent : garder l'HISTORIQUE d'un item décidé plusieurs fois, et SURVIVRE à la disparition
de la ligne d'origine (les migrations de dédoublonnage suppriment des lignes d'evidence).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as appmod
import db as dbmod
import review_journal as journal
import review_queue as rq


def _setup(tmp: str) -> tuple[Path, Path, Path]:
    paths = tuple(Path(tmp) / name for name in ("actors.db", "market.db", "technology.db"))
    with (
        patch.object(dbmod, "ACTORS_DB", paths[0]),
        patch.object(dbmod, "MARKET_DB", paths[1]),
        patch.object(dbmod, "TECH_DB", paths[2]),
    ):
        dbmod.init_databases()
    return paths  # type: ignore[return-value]


def _patched(actors_db: Path, market_db: Path, tech_db: Path) -> ExitStack:
    stack = ExitStack()
    for module in (dbmod, rq):
        stack.enter_context(patch.object(module, "ACTORS_DB", actors_db))
        stack.enter_context(patch.object(module, "MARKET_DB", market_db))
        stack.enter_context(patch.object(module, "TECH_DB", tech_db))
    return stack


def _insert_evidence(db, *, fingerprint: str = "fp1", actor_name: str = "Example") -> int:
    row_id = db.execute(
        """INSERT INTO evidence(
               actor_name,bucket,market,component,operation,source_url,quote,source_group,fingerprint,
               review_status,fact_status,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, "existing", "Batteries", "Électrodes de batteries", "Microdécoupe",
            "https://example.test/marches", "citation de test", "grp-" + fingerprint, fingerprint,
            "review", "review", dbmod.utc_now(), dbmod.utc_now(),
        ),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


class JournalWriteTests(unittest.TestCase):
    def test_a_decision_is_journalled_with_its_business_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    evidence_id = _insert_evidence(db)
                rq.decide_review_item(
                    "evidence", evidence_id, "reject",
                    reviewed_by="lucas", reject_reason="off_topic",
                )
                entries = journal.list_decisions()
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(("evidence", evidence_id, "reject"), (entry["queue"], entry["item_id"], entry["decision"]))
        self.assertEqual("off_topic", entry["reject_reason"])
        self.assertEqual("lucas", entry["decided_by"])
        # L'instantané doit rendre la ligne lisible sans jointure.
        self.assertEqual("Example", entry["actor_name"])
        self.assertIn("Batteries", entry["summary"])
        self.assertEqual("https://example.test/marches", entry["source_url"])

    def test_two_decisions_on_one_item_keep_both(self):
        """Le point de tout le journal : une colonne écraserait la première décision."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    evidence_id = _insert_evidence(db)
                rq.decide_review_item("evidence", evidence_id, "reject", reject_reason="wrong_actor")
                # Revirement : l'humain rouvre puis accepte.
                with dbmod.connect(market_db) as db:
                    db.execute("UPDATE evidence SET review_status='review' WHERE id=?", (evidence_id,))
                rq.decide_review_item("evidence", evidence_id, "accept")
                entries = journal.list_decisions(queue="evidence")
                with dbmod.connect(market_db) as db:
                    current = db.execute(
                        "SELECT reject_reason FROM evidence WHERE id=?", (evidence_id,)
                    ).fetchone()
        self.assertEqual(["accept", "reject"], sorted(e["decision"] for e in entries))
        # La colonne ne garde que le dernier état ; le motif du premier rejet n'existe plus que
        # dans le journal.
        self.assertIsNone(current["reject_reason"])
        self.assertIn("wrong_actor", [e["reject_reason"] for e in entries])

    def test_a_failed_decision_is_not_journalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with self.assertRaises(ValueError):
                    rq.decide_review_item("evidence", 999, "reject", reject_reason="off_topic")
                entries = journal.list_decisions()
        self.assertEqual([], entries, "le journal ne doit enregistrer que des décisions réelles")

    def test_journal_outlives_the_row_it_describes(self):
        """Une fusion de doublons SUPPRIME des lignes d'evidence (db._canonicalise_existing_evidence)
        et emporte les colonnes de trace avec elles."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    evidence_id = _insert_evidence(db)
                rq.decide_review_item("evidence", evidence_id, "reject", reject_reason="duplicate")
                with dbmod.connect(market_db) as db:
                    db.execute("DELETE FROM evidence WHERE id=?", (evidence_id,))
                entries = journal.list_decisions()
        self.assertEqual(1, len(entries))
        self.assertEqual("duplicate", entries[0]["reject_reason"])
        self.assertEqual("Example", entries[0]["actor_name"], "l'instantané survit à la ligne")


class LegacyPathAndSeedTests(unittest.TestCase):
    def test_the_market_review_screen_also_journals(self):
        """L'écran qui porte 123 des 152 items passe par les endpoints hérités, pas par
        decide_review_item : c'est le chemin qu'il ne faut surtout pas oublier de brancher."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    rejected_id = _insert_evidence(db, fingerprint="fp-reject")
                    accepted_id = _insert_evidence(db, fingerprint="fp-accept", actor_name="Autre")
                appmod.reject_market_review(
                    rejected_id, appmod.RejectMarketReviewRequest(reject_reason="unconvincing_citation")
                )
                appmod.accept_market_review(accepted_id)
                entries = {e["item_id"]: e for e in journal.list_decisions()}
        self.assertEqual("reject", entries[rejected_id]["decision"])
        self.assertEqual("unconvincing_citation", entries[rejected_id]["reject_reason"])
        self.assertEqual("accept", entries[accepted_id]["decision"])
        self.assertIsNone(entries[accepted_id]["reject_reason"])

    def test_seeding_is_idempotent_and_keeps_the_original_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    evidence_id = _insert_evidence(db)
                    db.execute(
                        """UPDATE evidence SET review_status='rejected', reviewed_by='lucas',
                               reviewed_at='2026-08-30T09:00:00Z', reject_reason='wrong_dimension'
                           WHERE id=?""",
                        (evidence_id,),
                    )
                first = journal.seed_from_columns()
                second = journal.seed_from_columns()
                entries = journal.list_decisions()
        self.assertEqual({"evidence": 1}, first)
        self.assertEqual({}, second, "un second amorçage ne doit rien dupliquer")
        self.assertEqual(1, len(entries))
        # Horodatage réel de la décision, pas celui de l'amorçage.
        self.assertEqual("2026-08-30T09:00:00Z", entries[0]["decided_at"])
        self.assertEqual("wrong_dimension", entries[0]["reject_reason"])


if __name__ == "__main__":
    unittest.main()
