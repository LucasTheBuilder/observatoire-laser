"""Dossier marketing (phase 1 de l'agent marketing).

Propriétés verrouillées : le confirmé et le « à confirmer » ne se mélangent jamais, seuls les
concurrents vérifiés et actifs comptent, les références citables ne pointent que vers du
confirmé, et ce que la base ne permet pas de dire ressort dans ``lacunes``.
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
            db.execute("UPDATE actors SET is_reference=0, competitive_class=NULL, active=0")
            for name, is_reference, active, status in (
                ("Nous SA", 1, 1, "verified"),
                ("Concurrent A", 0, 1, "verified"),
                ("Concurrent B", 0, 1, "verified"),
                ("Ecarte", 0, 1, "rejected"),
            ):
                db.execute(
                    "INSERT INTO actors(name,country,role,priority,official_url,active,review_status,is_reference,competitive_class,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, "France", "test", 1, f"https://{name[:4].lower()}.example/", active, status,
                     is_reference, "A1" if is_reference else "C1", dbmod.utc_now()),
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

    def test_confirmed_and_unconfirmed_competitors_are_counted_apart(self) -> None:
        confirmed_id = self._offer("Concurrent A", "Soudage test", "accepted")
        self._offer("Concurrent B", "Soudage test", "review")

        line = self._operation(build_marketing_dossier(today=TODAY), "Soudage test")

        self.assertEqual(1, line["concurrents_confirmes"])
        self.assertEqual(1, line["concurrents_a_confirmer"])
        self.assertEqual(["Concurrent A"], line["exemples"])
        self.assertEqual([f"offer:{confirmed_id}"], line["refs"])

    def test_our_status_distinguishes_confirmed_pending_and_absent(self) -> None:
        for operation in ("Op confirmee", "Op en revue", "Op absente"):
            self._offer("Concurrent A", operation, "accepted")
        self._offer("Nous SA", "Op confirmee", "accepted")
        self._offer("Nous SA", "Op en revue", "review")

        dossier = build_marketing_dossier(today=TODAY)

        self.assertEqual(["Nous SA"], dossier["perimetre"]["nous"])
        self.assertEqual("confirmé", self._operation(dossier, "Op confirmee")["nous"])
        self.assertEqual("à confirmer", self._operation(dossier, "Op en revue")["nous"])
        self.assertEqual("absent", self._operation(dossier, "Op absente")["nous"])

    def test_rejected_and_off_roster_actors_never_count(self) -> None:
        self._offer("Ecarte", "Op test", "accepted")
        self._offer("Inconnu hors liste", "Op test", "accepted")
        self._offer("Concurrent A", "Op test", "rejected")

        operations = build_marketing_dossier(today=TODAY)["operations"]

        self.assertEqual([], [line for line in operations if line["operation"] == "Op test" and
                              (line["concurrents_confirmes"] or line["concurrents_a_confirmer"])])

    def test_gaps_name_what_the_base_cannot_say(self) -> None:
        self._offer("Nous SA", "Op test", "accepted")

        constats = " ".join(gap["constat"] for gap in build_marketing_dossier(today=TODAY)["lacunes"])

        self.assertIn("Notre propre offre (Nous SA) est à peine décrite : 1 offre(s) confirmée(s)", constats)
        self.assertIn("Aucune taille de marché", constats)

    def test_class_a1_is_us_even_without_the_reference_flag(self) -> None:
        with dbmod.connect(self.paths["ACTORS_DB"]) as db:
            db.execute("UPDATE actors SET is_reference=0 WHERE name='Nous SA'")
        self._offer("Nous SA", "Op test", "accepted")

        dossier = build_marketing_dossier(today=TODAY)

        self.assertEqual(["Nous SA"], dossier["perimetre"]["nous"])
        line = self._operation(dossier, "Op test")
        self.assertEqual(("confirmé", 0), (line["nous"], line["concurrents_confirmes"]))

    def test_paths_are_read_at_call_time_from_the_db_module(self) -> None:
        self._offer("Concurrent A", "Op isolee", "accepted")
        self.assertTrue(any(line["operation"] == "Op isolee" for line in build_marketing_dossier(today=TODAY)["operations"]))


if __name__ == "__main__":
    unittest.main()
