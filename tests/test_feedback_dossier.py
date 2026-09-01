"""Dossier de retour d'expérience (phase 1 du plan agent d'analyse).

Deux propriétés valent d'être verrouillées.

1. Un rejet arrive avec la RÈGLE qui l'a produit -- motif typé, citation, et les termes du
   lexique qui ont déclenché chaque dimension. C'est ce qui rend une décision imputable.
2. Les faits de référence non retrouvés sont classés par CAUSE. ``compute_recall`` les confond
   tous en un seul taux ; or « jamais collecté », « trouvé mais mal étiqueté » et « trouvé puis
   écarté » n'ont ni la même origine ni le même correctif, et un lecteur qui ne verrait que le
   taux ne pourrait que deviner.
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

import db as dbmod
import feedback_dossier as fd
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
    for module in (dbmod, rq, fd):
        stack.enter_context(patch.object(module, "ACTORS_DB", actors_db, create=True))
        stack.enter_context(patch.object(module, "MARKET_DB", market_db, create=True))
        stack.enter_context(patch.object(module, "TECH_DB", tech_db, create=True))
    return stack


def _insert_evidence(
    db, *, fingerprint: str, actor_name: str = "Example", market: str = "Batteries",
    component: str = "Électrodes de batteries", operation: str = "Microdécoupe",
    source_url: str = "https://example.test/a", fact_status: str = "review",
    match_terms: str | None = None,
) -> int:
    row_id = db.execute(
        """INSERT INTO evidence(
               actor_name,bucket,market,component,operation,source_url,quote,source_group,fingerprint,
               review_status,fact_status,extraction_mode,relation_strength,field_confidence,
               match_terms,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, "existing", market, component, operation, source_url,
            "citation de test", "grp-" + fingerprint, fingerprint, "review", fact_status,
            "block-rules", "direct", 0.78, match_terms, dbmod.utc_now(), dbmod.utc_now(),
        ),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


class RejectionProvenanceTests(unittest.TestCase):
    def test_a_rejection_carries_the_rule_that_produced_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    evidence_id = _insert_evidence(
                        db, fingerprint="fp1",
                        match_terms='{"component": ["electrode"], "market": ["battery"]}',
                    )
                rq.decide_review_item(
                    "evidence", evidence_id, "reject",
                    reviewed_by="lucas", reject_reason="wrong_dimension",
                )
                dossier = fd.build_feedback_dossier()

        self.assertEqual(1, len(dossier["rejections"]))
        rejection = dossier["rejections"][0]
        self.assertEqual("wrong_dimension", rejection["reject_reason"])
        self.assertEqual("citation de test", rejection["quote"])
        self.assertEqual({"component": ["electrode"], "market": ["battery"]}, rejection["match_terms"])
        self.assertEqual("block-rules", rejection["extraction_mode"])
        self.assertEqual("direct", rejection["relation_strength"])
        self.assertTrue(rejection["row_still_exists"])
        self.assertEqual([{"reject_reason": "wrong_dimension", "count": 1}], dossier["aggregates"]["by_reason"])

    def test_term_scoreboard_separates_accepted_from_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    rejected = _insert_evidence(db, fingerprint="fp-r", match_terms='{"market": ["battery"]}')
                    accepted = _insert_evidence(db, fingerprint="fp-a", match_terms='{"market": ["battery"]}')
                rq.decide_review_item("evidence", rejected, "reject", reject_reason="off_topic")
                rq.decide_review_item("evidence", accepted, "accept")
                board = fd.build_feedback_dossier()["aggregates"]["by_term"]

        entry = next(row for row in board if row["term"] == "battery")
        self.assertEqual((1, 1, 2), (entry["accepted"], entry["rejected"], entry["total"]))
        self.assertEqual("market", entry["dimension"])

    def test_a_decision_whose_row_vanished_is_flagged_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    evidence_id = _insert_evidence(db, fingerprint="fp1")
                rq.decide_review_item("evidence", evidence_id, "reject", reject_reason="duplicate")
                with dbmod.connect(market_db) as db:
                    db.execute("DELETE FROM evidence WHERE id=?", (evidence_id,))
                dossier = fd.build_feedback_dossier()

        rejection = dossier["rejections"][0]
        self.assertFalse(rejection["row_still_exists"])
        self.assertIsNone(rejection["quote"])
        # L'instantané du journal reste : la décision ne disparaît pas avec sa ligne.
        self.assertEqual("Example", rejection["actor_name"])


class GoldenMissClassificationTests(unittest.TestCase):
    """Les trois causes, chacune sur son cas."""

    def _golden(self, db, *, source_url: str = "https://example.test/a") -> None:
        db.execute(
            """INSERT INTO golden_facts(actor_name,market,component,operation,source_url,expected_quote,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                "Example", "Batteries", "Électrodes de batteries", "Microdécoupe",
                source_url, "citation vérifiée à la main", dbmod.utc_now(),
            ),
        )

    def test_found_then_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    self._golden(db)
                    _insert_evidence(db, fingerprint="fp1", fact_status="review")
                misses = fd.classify_golden_misses()
        self.assertEqual(["trouve_puis_jete"], [m["kind"] for m in misses])
        self.assertIn("fact_status=review", misses[0]["detail"])

    def test_mislabelled_on_the_same_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    self._golden(db)
                    _insert_evidence(
                        db, fingerprint="fp1", component="Wafers", operation="Dicing",
                        fact_status="validated",
                    )
                misses = fd.classify_golden_misses()
        self.assertEqual(["mal_etiquete"], [m["kind"] for m in misses])
        self.assertIn("Wafers", misses[0]["detail"])

    def test_never_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    self._golden(db, source_url="https://example.test/jamais-crawlee")
                misses = fd.classify_golden_misses()
        self.assertEqual(["aucune_trace"], [m["kind"] for m in misses])

    def test_a_retrieved_fact_is_not_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                with dbmod.connect(market_db) as db:
                    self._golden(db)
                    _insert_evidence(db, fingerprint="fp1", fact_status="validated")
                dossier = fd.build_feedback_dossier()
        self.assertEqual(0, dossier["misses"]["missed"])
        self.assertEqual(1.0, dossier["misses"]["recall"])

    def test_recall_is_none_when_there_is_nothing_to_measure(self):
        """Un dénominateur nul ne vaut pas 0 % -- même règle que veille_metrics."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, market_db, tech_db = _setup(tmp)
            with _patched(actors_db, market_db, tech_db):
                dossier = fd.build_feedback_dossier()
        self.assertIsNone(dossier["misses"]["recall"])
        self.assertEqual(0, dossier["misses"]["golden_facts_total"])


if __name__ == "__main__":
    unittest.main()
