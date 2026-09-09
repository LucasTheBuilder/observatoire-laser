"""Tests pour la classification axe + maturité des documents Crossref/OpenAlex (Lot 3 §3.5,
audit veille §4.C.2, 30/08/2026) : "branchement de lexiques existants" -- un document déjà
on-topic (filtré en amont par scrape_technology/openalex.py) n'avait jusqu'ici aucun axe
technologique. Réutilise exactement PROCESS_TECHNOLOGIES/MATURITY_RULES, comme cordis.py pour
les projets.
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
from scrapers import upsert_document_technology_signal


def _setup(tmp: str) -> Path:
    tech_db = Path(tmp) / "technology.db"
    with (
        patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", tech_db),
    ):
        dbmod.init_databases()
    return tech_db


class UpsertDocumentTechnologySignalTests(unittest.TestCase):
    def test_known_process_axis_is_detected_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) of fused silica microchannels", "",
                    "https://doi.org/10.1/example", "ALPHANOV",
                )
            self.assertEqual(1, added)
            row = dbmod.rows(tech_db, "SELECT axis,bucket,actor_names FROM technology_signals")[0]
            self.assertEqual("SLE", row["axis"])
            self.assertIn("ALPHANOV", row["actor_names"])

    def test_no_matching_axis_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Femtosecond laser micromachining of glass for medical devices", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(0, added)
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(0, count)

    def test_generic_axis_without_laser_context_is_excluded(self):
        # Same false-positive guard as is_on_topic(): "digital twin" alone (Monitoring IA
        # procédé's match term) must not create a signal without a real laser term present --
        # this is the exact false positive the audit found in production (a Tekniker safety
        # document with zero laser mentions).
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "AI-Enriched Safety Criteria Catalogue and Digital Twin Framework", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(0, added)

    def test_generic_axis_with_laser_context_is_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Femtosecond laser process monitoring via digital twin models", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(1, added)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("Monitoring IA procédé", row["axis"])

    def test_maturity_stage_is_detected_from_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) for mass production of glass wafers", "",
                    "https://doi.org/10.1/example",
                )
            row = dbmod.rows(tech_db, "SELECT maturity_stage,bucket FROM technology_signals")[0]
            self.assertEqual("Production", row["maturity_stage"])
            self.assertEqual("existing", row["bucket"])

    def test_unknown_maturity_defaults_to_radar(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) of photonic glass components", "",
                    "https://doi.org/10.1/example",
                )
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals")[0]
            self.assertEqual("radar", row["bucket"])

    def test_repeated_call_on_same_document_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                first = upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")
                second = upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")
            self.assertEqual(1, first)
            self.assertEqual(0, second)
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(1, count)

    def test_second_actor_on_same_document_merges_into_actor_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example", "ALPHANOV")
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example", "Fraunhofer ILT")
            row = dbmod.rows(tech_db, "SELECT actor_names FROM technology_signals")[0]
            import json
            self.assertEqual(["ALPHANOV", "Fraunhofer ILT"], sorted(json.loads(row["actor_names"])))

    def test_different_documents_with_the_same_axis_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/a")
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/b")
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(2, count)

    def test_multiple_axes_on_one_document_each_create_a_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) combined with LIPSS structuring of glass", "",
                    "https://doi.org/10.1/example",
                )
            self.assertEqual(2, added)
            axes = {row["axis"] for row in dbmod.rows(tech_db, "SELECT axis FROM technology_signals")}
            self.assertEqual({"SLE", "LIPSS"}, axes)


class DocumentTechnologySignalSourcesTests(unittest.TestCase):
    """Audit du 08/09/2026 : ces signaux n'écrivaient aucune ligne technology_signal_sources.
    Comme /api/tech-corpus/proofs joint les deux tables, les seules publications ayant un axe
    qualifié étaient exactement celles dont le panneau de preuves affichait "axe pas encore
    qualifié"."""

    def test_a_source_row_is_written_with_the_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "Selective laser etching (SLE) of fused silica microchannels", "",
                    "https://doi.org/10.1/example", "ALPHANOV",
                )
            source = dbmod.rows(tech_db, "SELECT signal_id,source_url,source_title,quote FROM technology_signal_sources")
            self.assertEqual(1, len(source))
            self.assertEqual("https://doi.org/10.1/example", source[0]["source_url"])
            signal_id = dbmod.scalar(tech_db, "SELECT id FROM technology_signals")
            self.assertEqual(signal_id, source[0]["signal_id"])

    def test_quote_is_the_matched_text_not_an_invention(self):
        # Sans abstract -- le cas de toute la base aujourd'hui -- le passage matché EST le
        # titre. La ligne doit le dire tel quel plutôt que de maquiller un titre en verbatim.
        title = "Selective laser etching (SLE) of fused silica microchannels"
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(db, title, "", "https://doi.org/10.1/example")
            quote = dbmod.scalar(tech_db, "SELECT quote FROM technology_signal_sources")
            self.assertEqual(title, quote)

    def test_abstract_sentence_is_preferred_over_the_title_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(
                    db, "A study of glass machining",
                    "Nothing relevant here. We rely on selective laser etching of fused silica throughout.",
                    "https://doi.org/10.1/example",
                )
            quote = dbmod.scalar(tech_db, "SELECT quote FROM technology_signal_sources")
            self.assertIn("selective laser etching", quote)

    def test_replaying_the_same_document_does_not_duplicate_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")
                upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")
            self.assertEqual(1, dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signal_sources"))

    def test_source_is_backfilled_on_a_signal_that_predates_this_fix(self):
        # Le rattrapage des signaux déjà en base : le signal existe, sa preuve non. Rejouer la
        # collecte doit écrire la ligne manquante, pas passer son tour parce que le signal
        # n'est plus "nouveau".
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                dbmod.upsert_technology_signal(
                    db,
                    fact_key=dbmod.technology_signal_key("SLE", "https://doi.org/10.1/example"),
                    axis="SLE", maturity_stage="Prototype", bucket="radar", actor_names=[],
                    source_url="https://doi.org/10.1/example", quote="SLE etching of glass",
                    field_confidence=0.7, source_title="SLE etching of glass",
                )
            self.assertEqual(0, dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signal_sources"))

            with dbmod.connect(tech_db) as db:
                added = upsert_document_technology_signal(db, "SLE etching of glass", "", "https://doi.org/10.1/example")

            self.assertEqual(0, added)  # aucun signal neuf
            self.assertEqual(1, dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signal_sources"))


class CrossrefDateTests(unittest.TestCase):
    """Crossref renvoie `[[2027, 4]]` pour un article rattaché à un numéro à paraître. Le
    "-".join d'origine produisait "2027-4" : mal trié lexicographiquement et affiché brut."""

    def test_full_date_is_zero_padded(self):
        from scrapers import _crossref_date
        self.assertEqual("2026-05-29", _crossref_date({"date-parts": [[2026, 5, 29]]}))

    def test_partial_date_keeps_its_precision_without_inventing_a_day(self):
        from scrapers import _crossref_date
        self.assertEqual("2027-04", _crossref_date({"date-parts": [[2027, 4]]}))

    def test_partial_date_sorts_after_an_earlier_full_date(self):
        from scrapers import _crossref_date
        self.assertGreater(_crossref_date({"date-parts": [[2027, 4]]}), _crossref_date({"date-parts": [[2026, 12, 1]]}))

    def test_year_only_and_missing_dates(self):
        from scrapers import _crossref_date
        self.assertEqual("2026", _crossref_date({"date-parts": [[2026]]}))
        self.assertIsNone(_crossref_date(None))
        self.assertIsNone(_crossref_date({"date-parts": [[]]}))


if __name__ == "__main__":
    unittest.main()
