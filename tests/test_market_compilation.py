"""Tests pour la compilation marché & produit (market_compilation.py, /api/market/compilation).

Les phrases viennent des cas relus à la main le 28/09/2026 sur la base réelle : chaque garde
du module y a son exemple, et celui-ci doit rester vrai.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as appmod
import db as dbmod
import market_compilation as mc
from lexicon import OPERATIONS


class ClassifyTests(unittest.TestCase):
    def test_named_market_and_product_are_attached_with_their_sentence(self):
        found = mc.classify(["Ultrashort pulsed laser ablation of zirconia-alumina composites for implant applications."])
        self.assertIn("Implants", found["product"])
        self.assertIn("implant applications", found["product"]["Implants"])

    def test_machine_optics_is_not_the_optics_market(self):
        found = mc.classify(["The study is performed using a 100 mm focusing length f-theta lens."])
        self.assertEqual({}, found["market"])
        found = mc.classify(["We offer laser helical drilling optics for cylindrical holes."])
        self.assertNotIn("Optique", found["market"])

    def test_micro_optics_opens_the_optics_market(self):
        found = mc.classify(["Target markets: medical devices, watchmaking, micro-optics."])
        self.assertIn("Optique", found["market"])
        self.assertIn("Médical", found["market"])

    def test_quantum_efficiency_is_not_quantum_technology(self):
        found = mc.classify(["The detectors reach a quantum efficiency no worse than 8% at 1310 nm."])
        self.assertNotIn("Quantum", found["market"])

    def test_electrode_needs_a_battery_context(self):
        self.assertNotIn(
            "Électrodes de batteries",
            mc.classify(["Spiky nickel electrodes for oxygen evolution catalysis by femtosecond laser structuring."])["product"],
        )
        self.assertIn(
            "Électrodes de batteries",
            mc.classify(["Femtosecond laser structuring of electrodes for improved Li-Ion battery performances."])["product"],
        )

    def test_negated_sentence_attaches_nothing(self):
        found = mc.classify(["Our process does not target medical devices."])
        self.assertEqual({}, found["market"])

    def test_laser_source_sentence_attaches_nothing(self):
        found = mc.classify(["A picosecond laser oscillator for seeding amplifiers, for applications in medical imaging."])
        self.assertEqual({}, found["market"])

    def test_actor_name_is_not_a_market(self):
        found = mc.classify(["Pulsar Photonics is developing a machine for laser drilling."], names=["Pulsar Photonics"])
        self.assertEqual({}, found["market"])

    def test_offer_sentence_must_name_the_offer_operation(self):
        texts = [
            "We perform laser cutting of stents for medical devices.",
            "Our polishing services serve the luxury industry.",
        ]
        found = mc.classify(texts, operation_rule=OPERATIONS["Microdécoupe"], max_len=320, reject_bibliography=True)
        self.assertIn("Médical", found["market"])
        self.assertNotIn("Luxe", found["market"])

    def test_bibliography_and_overlong_sentences_are_skipped_for_offers(self):
        citation = "Zibner, F., Gillner, A.: High precision laser cutting of nitinol for medical applications ICALEO 2013."
        menu = "Laser cutting " + "Micro and nano structuring with lasers " * 12 + "for medical devices."
        found = mc.classify([citation, menu], max_len=320, reject_bibliography=True)
        self.assertEqual({}, found["market"])


class CompilationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.market_db, self.tech_db = tmp / "market.db", tmp / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", tmp / "actors.db"),
            patch.object(dbmod, "MARKET_DB", self.market_db),
            patch.object(dbmod, "TECH_DB", self.tech_db),
        ):
            dbmod.init_databases()
        # Les bases réseedées portent déjà des lignes de démonstration : on part de zéro.
        with dbmod.connect(self.market_db) as db:
            for table in ("offer_sources", "offers", "evidence_sources", "evidence", "demand_signals"):
                db.execute(f"DELETE FROM {table}")
        with dbmod.connect(self.tech_db) as db:
            for table in ("technology_signal_sources", "technology_signals", "documents"):
                db.execute(f"DELETE FROM {table}")
        mc._cache.update({"key": None, "value": None})
        self._seed()

    def _offer(self, db, offer_id, status, operation, quote):
        stamp = dbmod.utc_now()
        db.execute(
            """INSERT INTO offers(id,actor_name,offer_type,capability,operation,source_url,quote,fact_key,
                                  fingerprint,review_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (offer_id, "ACME", "service", f"Capacité {offer_id}", operation, f"https://acme.test/{offer_id}",
             quote, f"fk{offer_id}", f"fp{offer_id}", status, stamp, stamp),
        )

    def _seed(self):
        stamp = dbmod.utc_now()
        with dbmod.connect(self.market_db) as db:
            self._offer(db, 1, "accepted", "Microdécoupe", "We perform laser cutting of stents for medical devices.")
            self._offer(db, 2, "review", "Microdécoupe", "We perform laser cutting of stents for medical devices.")
            self._offer(db, 3, "accepted", "Microdécoupe", "We perform laser cutting of many materials.")
            db.execute(
                """INSERT INTO demand_signals(signal_type,source,title,source_url,fingerprint,created_at)
                   VALUES('tender','BOAMP','Découpe laser femtoseconde','https://boamp.test/1','d1',?)""",
                (stamp,),
            )
        with dbmod.connect(self.tech_db) as db:
            db.execute(
                """INSERT INTO documents(actor_name,document_type,title,abstract,source_url,fingerprint,created_at)
                   VALUES('ACME','publication','Femtosecond texturing of titanium dental implants',NULL,
                          'https://doi.org/10.1/x','doc1',?)""",
                (stamp,),
            )
            db.execute(
                """INSERT INTO documents(actor_name,document_type,title,abstract,source_url,fingerprint,created_at)
                   VALUES('ACME','publication','Ablation thresholds of fused silica',NULL,
                          'https://doi.org/10.1/y','doc2',?)""",
                (stamp,),
            )

    def _call(self):
        with (
            patch.object(appmod, "MARKET_DB", self.market_db),
            patch.object(appmod, "TECH_DB", self.tech_db),
        ):
            return appmod.market_compilation()

    def test_only_accepted_offers_that_name_something_are_compiled(self):
        result = self._call()
        self.assertEqual(["offer:1"], [row["uid"] for row in result["offers"]])
        offer = result["offers"][0]
        self.assertEqual(["Médical"], offer["markets"])
        self.assertEqual(["Stents"], offer["products"])
        self.assertTrue(all(proof["sentence"] for proof in offer["proofs"]))

    def test_only_documents_that_name_something_are_compiled(self):
        technology = self._call()["technology"]
        self.assertEqual(1, len(technology))
        self.assertEqual([], technology[0]["markets"])
        self.assertEqual(["Implants"], technology[0]["products"])
        self.assertEqual("openalex", technology[0]["source_id"])

    def test_glossary_counts_what_reached_the_page(self):
        sources = {(s["id"], s["role"]): s for s in self._call()["sources"]}
        self.assertEqual(1, sources[("actor_websites", "Pages des acteurs suivis")]["count"])
        self.assertEqual(1, sources[("openalex", "Publications scientifiques")]["count"])
        self.assertEqual(1, sources[("boamp", "Signaux de demande")]["count"])
        self.assertEqual(0, sources[("ted", "Signaux de demande")]["count"])


if __name__ == "__main__":
    unittest.main()
