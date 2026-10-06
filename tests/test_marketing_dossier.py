"""Dossier marketing (phase 1 de l'agent marketing), lecture neutre du marché.

Propriétés verrouillées : le confirmé et le « à confirmer » ne se mélangent jamais, seuls les
acteurs suivis (vérifiés, actifs, hors partenaires de référence) comptent, aucun acteur n'y joue
le rôle de « nous », et ce que la base ne permet pas de dire ressort dans ``lacunes``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from marketing_dossier import build_marketing_dossier

TODAY = date(2026, 10, 1)


class MarketingDossierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.paths = {"ACTORS_DB": tmp / "actors.db", "MARKET_DB": tmp / "market.db", "TECH_DB": tmp / "technology.db"}
        self._stack = ExitStack()
        for name, path in self.paths.items():
            self._stack.enter_context(patch.object(dbmod, name, path))
        dbmod.init_databases()
        with dbmod.connect(self.paths["ACTORS_DB"]) as db:
            db.execute("UPDATE actors SET active=0")
            for name, is_reference, competitive_class, status in (
                ("Partenaire source", 1, None, "verified"),
                ("Acteur A", 0, "C1", "verified"),
                ("Acteur B", 0, "C2", "verified"),
                ("Ancienne reference", 0, "A1", "verified"),
                ("Ecarte", 0, "C1", "rejected"),
            ):
                db.execute(
                    "INSERT INTO actors(name,country,role,priority,official_url,active,review_status,is_reference,competitive_class,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, "France", "test", 1, f"https://{name[:6].lower().replace(' ', '')}.example/", 1, status,
                     is_reference, competitive_class, dbmod.utc_now()),
                )

    def tearDown(self) -> None:
        self._stack.close()
        self._tmp.cleanup()

    def _offer(self, actor: str, operation: str, status: str) -> int:
        with dbmod.connect(self.paths["MARKET_DB"]) as db:
            return int(db.execute(
                "INSERT INTO offers(actor_name,offer_type,capability,operation,source_url,quote,fact_key,fingerprint,"
                "review_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (actor, "service", operation, operation, "https://example.test/o", "q", f"{actor}|{operation}|{status}",
                 f"{actor}|{operation}|{status}", status, dbmod.utc_now(), dbmod.utc_now()),
            ).lastrowid)

    def _operation(self, dossier: dict, operation: str) -> dict:
        return next(line for line in dossier["operations"] if line["operation"] == operation)

    def test_confirmed_and_unconfirmed_actors_are_counted_apart(self) -> None:
        confirmed_id = self._offer("Acteur A", "Soudage test", "accepted")
        self._offer("Acteur B", "Soudage test", "review")

        line = self._operation(build_marketing_dossier(today=TODAY), "Soudage test")

        self.assertEqual(1, line["acteurs_confirmes"])
        self.assertEqual(1, line["acteurs_a_confirmer"])
        self.assertEqual(["Acteur A"], line["exemples"])
        self.assertEqual([f"offer:{confirmed_id}"], line["refs"])

    def test_rejected_off_roster_and_reference_partners_never_count(self) -> None:
        for actor in ("Ecarte", "Inconnu hors liste", "Partenaire source"):
            self._offer(actor, "Op test", "accepted")
        self._offer("Acteur A", "Op test", "rejected")

        operations = build_marketing_dossier(today=TODAY)["operations"]

        self.assertEqual([], [line for line in operations if line["operation"] == "Op test" and
                              (line["acteurs_confirmes"] or line["acteurs_a_confirmer"])])

    def test_no_actor_plays_us_a_former_internal_reference_is_tracked_like_the_others(self) -> None:
        self._offer("Ancienne reference", "Op test", "accepted")

        dossier = build_marketing_dossier(today=TODAY)

        self.assertNotIn("nous", dossier["perimetre"])
        line = self._operation(dossier, "Op test")
        self.assertNotIn("nous", line)
        self.assertEqual(["Ancienne reference"], line["exemples"])

    def test_gaps_name_what_the_base_cannot_say(self) -> None:
        constats = " ".join(gap["constat"] for gap in build_marketing_dossier(today=TODAY)["lacunes"])

        self.assertIn("Aucune taille de marché", constats)
        self.assertNotIn("Notre propre offre", constats)

    def test_paths_are_read_at_call_time_from_the_db_module(self) -> None:
        self._offer("Acteur A", "Op isolee", "accepted")
        self.assertTrue(any(line["operation"] == "Op isolee" for line in build_marketing_dossier(today=TODAY)["operations"]))


if __name__ == "__main__":
    unittest.main()
