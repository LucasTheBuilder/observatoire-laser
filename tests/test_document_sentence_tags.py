"""Tags marché et pièce des documents de la page Technologie laser (06/10/2026).

Lus sur le texte entier, ils prenaient l'optique de la machine pour le marché de l'optique ;
ils exigent désormais une phrase qui les nomme, et cette phrase devient la citation.
reclassify_document_tags.py applique la même règle aux tags déjà en base.
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
import reclassify_document_tags as reclassify
from scrapers import technology_signal_key, upsert_document_technology_signal

LENS_ABSTRACT = (
    "Glass welding with a 40 MHz femtosecond laser is studied. "
    "Focusing is achieved using an f-theta lens with a 50 mm focal length."
)
IMPLANT_TITLE = "Femtosecond laser texturing of titanium dental implants"


class SentenceTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.tech_db = tmp / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", tmp / "actors.db"),
            patch.object(dbmod, "MARKET_DB", tmp / "market.db"),
            patch.object(dbmod, "TECH_DB", self.tech_db),
        ):
            dbmod.init_databases()
        with dbmod.connect(self.tech_db) as db:
            db.execute("DELETE FROM technology_signal_sources")
            db.execute("DELETE FROM technology_signals")
            db.execute("DELETE FROM documents")

    def _tags(self, url: str) -> dict[tuple[str, str], list[str]]:
        tags: dict[tuple[str, str], list[str]] = {}
        for row in dbmod.rows(
            self.tech_db,
            """SELECT t.dimension,t.axis,s.quote FROM technology_signals t
                 LEFT JOIN technology_signal_sources s ON s.signal_id=t.id
                WHERE t.source_url=? AND t.dimension IN ('market','component')""",
            (url,),
        ):
            tags.setdefault((row["dimension"], row["axis"]), []).append(row["quote"])
        return tags

    def test_the_machine_lens_no_longer_tags_the_optics_market(self):
        with dbmod.connect(self.tech_db) as db:
            upsert_document_technology_signal(db, "40MHz femtosecond laser burst to weld glass", LENS_ABSTRACT, "https://doi.org/10.1/lens")
        self.assertEqual({}, self._tags("https://doi.org/10.1/lens"))

    def test_the_quote_is_the_sentence_that_names_the_tag(self):
        with dbmod.connect(self.tech_db) as db:
            upsert_document_technology_signal(db, IMPLANT_TITLE, "Laser parameters were varied.", "https://doi.org/10.1/imp")
        self.assertEqual({("component", "Implants"): [IMPLANT_TITLE], ("market", "Médical"): [IMPLANT_TITLE]}, self._tags("https://doi.org/10.1/imp"))

    def _seed_old_tags(self):
        """Ce qu'écrivait l'ancienne lecture du texte entier : un faux Optique, un vrai
        Implants mais avec une citation-fenêtre, plus le document lui-même."""
        stamp = dbmod.utc_now()
        with dbmod.connect(self.tech_db) as db:
            for url, title, abstract in (
                ("https://doi.org/10.1/lens", "40MHz femtosecond laser burst to weld glass", LENS_ABSTRACT),
                ("https://doi.org/10.1/imp", IMPLANT_TITLE, ""),
            ):
                db.execute(
                    """INSERT INTO documents(actor_name,document_type,title,abstract,source_url,fingerprint,created_at)
                       VALUES('ACME','publication',?,?,?,?,?)""",
                    (title, abstract, url, url, stamp),
                )
            for signal_id, axis, dimension, url, quote, reviewed in (
                (1, "Optique", "market", "https://doi.org/10.1/lens", "f-theta lens with a 50 mm focal length", None),
                (2, "Implants", "component", "https://doi.org/10.1/imp", "texturing of titanium dental implants", None),
                (3, "Médical", "market", "https://doi.org/10.1/lens", "relu et gardé par un humain", stamp),
            ):
                db.execute(
                    """INSERT INTO technology_signals(id,axis,maturity_stage,bucket,actor_names,source_url,quote,fact_key,
                                                      fingerprint,review_status,created_at,updated_at,dimension,reviewed_at)
                       VALUES(?,?,?,?,'[]',?,?,?,?,'accepted',?,?,?,?)""",
                    (signal_id, axis, "R&D", "radar", url, quote, technology_signal_key(axis, url), f"f{signal_id}", stamp, stamp, dimension, reviewed),
                )
                db.execute(
                    """INSERT INTO technology_signal_sources(signal_id,source_url,quote,fingerprint,created_at)
                       VALUES(?,?,?,?,?)""",
                    (signal_id, url, quote, f"s{signal_id}", stamp),
                )

    def test_reclassification_removes_requotes_and_spares_human_reviews(self):
        self._seed_old_tags()
        with (
            patch.object(reclassify, "TECH_DB", self.tech_db),
            patch.object(reclassify, "backup_all_databases", lambda: []),
        ):
            report = reclassify.reclassify_document_tags()
        self.assertEqual([("market", "Optique")], [(r["dimension"], r["label"]) for r in report["removed"]])
        lens_tags = self._tags("https://doi.org/10.1/lens")
        self.assertNotIn(("market", "Optique"), lens_tags)
        self.assertIn(("market", "Médical"), lens_tags, "une ligne relue par un humain ne bouge jamais")
        self.assertEqual({("component", "Implants"): [IMPLANT_TITLE], ("market", "Médical"): [IMPLANT_TITLE]}, self._tags("https://doi.org/10.1/imp"))

    def test_dry_run_writes_nothing(self):
        self._seed_old_tags()
        with patch.object(reclassify, "TECH_DB", self.tech_db):
            report = reclassify.reclassify_document_tags(dry_run=True)
        self.assertEqual(1, len(report["removed"]))
        self.assertIn(("market", "Optique"), self._tags("https://doi.org/10.1/lens"))


if __name__ == "__main__":
    unittest.main()
