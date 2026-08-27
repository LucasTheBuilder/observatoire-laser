"""Tests pour capabilities.py (chantier 5) : capability_spec, l'enveloppe de capacités
chiffrées extraite déterministiquement des pages product/equipment/capability déjà crawlées.

Les cas de collect_capability_specs() ici passent tous par le chemin blocks_json en cache
(actor_sources.blocks_json + last_checked_at déjà renseignés), qui court-circuite tout appel
réseau -- httpx.Client n'a donc jamais besoin d'être doublé. Le chemin "cache absent -> fetch
live" réutilise scrapers._fetch/hybrid.parse_document, déjà couverts ailleurs.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capabilities
import db as dbmod
from capabilities import _extract_capabilities, _parse_number, collect_capability_specs


def _block(text: str, heading: str = "") -> dict:
    return {"heading": heading, "h1": "", "h2": "", "h3": "", "text": text, "path": "main > p", "media_context": ""}


class ParseNumberTests(unittest.TestCase):
    def test_dot_decimal(self):
        self.assertEqual(1.5, _parse_number("1.5"))

    def test_comma_decimal(self):
        self.assertEqual(1.5, _parse_number("1,5"))

    def test_comma_thousands_with_dot_decimal(self):
        self.assertEqual(1500.5, _parse_number("1,500.5"))

    def test_not_a_number(self):
        self.assertIsNone(_parse_number("abc"))


class ExtractCapabilitiesTests(unittest.TestCase):
    def test_wavelengths_collected_and_out_of_range_rejected(self):
        fields = _extract_capabilities(["Available wavelengths: 1030 nm, 515 nm and 343 nm.", "Cable length 5000 nm reel (irrelevant)."])
        self.assertEqual([343, 515, 1030], fields["wavelengths_nm"])

    def test_pulse_duration_takes_shortest(self):
        fields = _extract_capabilities(["System A: 290 fs pulse duration.", "System B: 900 fs pulse duration."])
        self.assertEqual(290.0, fields["pulse_duration_fs"])

    def test_tolerance_uses_plus_minus_marker(self):
        fields = _extract_capabilities(["Positioning accuracy of ±2 µm on all axes."])
        self.assertEqual(2.0, fields["tolerance_um"])

    def test_feature_size_requires_context_word_in_same_block(self):
        # A bare µm value with no feature-size context must never be attributed to feature size.
        with_context = _extract_capabilities(["Minimum feature size achievable: 8 µm."])
        without_context = _extract_capabilities(["Cable diameter: 8 µm."])
        self.assertEqual(8.0, with_context["min_feature_size_um"])
        self.assertIsNone(without_context["min_feature_size_um"])

    def test_part_size_requires_context_word_and_takes_largest(self):
        fields = _extract_capabilities(["X-Y travel of 300 mm.", "X-Y travel up to 600 mm on the large-format stage."])
        self.assertEqual(600.0, fields["max_part_size_mm"])

    def test_throughput_pattern_and_largest_kept(self):
        fields = _extract_capabilities(["Standard line: 1200 parts/hour.", "Fast line: 3000 parts per hour."])
        self.assertEqual(3000.0, fields["throughput_units_per_h"])

    def test_batch_size_range_kept_as_verbatim_quote(self):
        fields = _extract_capabilities(["We process from 1 to 100000 parts depending on the program."])
        self.assertIn("1", fields["batch_size_range"])
        self.assertIn("100000", fields["batch_size_range"])

    def test_materials_qualified_uses_shared_lexicon(self):
        fields = _extract_capabilities(["Qualified on titanium, stainless steel and fused silica glass parts."])
        self.assertIn("Métal", fields["materials_qualified"])
        self.assertIn("Verre", fields["materials_qualified"])

    def test_no_signal_yields_all_none(self):
        fields = _extract_capabilities(["Contact us for more information about our company history."])
        self.assertIsNone(fields["min_feature_size_um"])
        self.assertIsNone(fields["tolerance_um"])
        self.assertIsNone(fields["max_part_size_mm"])
        self.assertIsNone(fields["throughput_units_per_h"])
        self.assertIsNone(fields["pulse_duration_fs"])
        self.assertIsNone(fields["batch_size_range"])
        self.assertEqual([], fields["wavelengths_nm"])
        self.assertEqual([], fields["materials_qualified"])


class CollectCapabilitySpecsTests(unittest.TestCase):
    def _seed(self, actors_db: Path, name: str, page_type: str, text: str, *, source_id_hint: str = "a") -> None:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors WHERE name=?", (name,))
        dbmod.create_actor(name, "France", "Test", f"https://{source_id_hint}.example/", priority=False)
        blocks_json = json.dumps([_block(text)], ensure_ascii=False)
        with dbmod.connect(actors_db) as db:
            actor_id = db.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone()["id"]
            db.execute(
                """INSERT INTO actor_sources(actor_id,url,page_type,active,last_http_status,last_checked_at,blocks_json)
                   VALUES(?,?,?,1,200,?,?)""",
                (actor_id, f"https://{source_id_hint}.example/{page_type}-{text[:8]}", page_type, dbmod.utc_now(), blocks_json),
            )

    def _run(self, actors_db: Path, fn):
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", actors_db.parent / "market.db"),
            patch.object(dbmod, "TECH_DB", actors_db.parent / "technology.db"),
            patch.object(capabilities, "ACTORS_DB", actors_db),
        ):
            dbmod.init_databases()
            return fn()

    def test_creates_capability_spec_row_from_cached_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "FEMTOprint", "product", "Minimum feature size achievable: 8 µm, positioning accuracy of ±2 µm.")
                return collect_capability_specs()

            result = self._run(actors_db, run)
            self.assertEqual(1, result["profiles_added"])
            self.assertEqual(0, result["errors"])
            with dbmod.connect(actors_db) as db:
                row = db.execute(
                    """SELECT min_feature_size_um,tolerance_um,source_url FROM capability_spec c
                       JOIN actors a ON a.id=c.actor_id WHERE a.name='FEMTOprint'"""
                ).fetchone()
            self.assertEqual(8.0, row["min_feature_size_um"])
            self.assertEqual(2.0, row["tolerance_um"])
            self.assertTrue(row["source_url"])

    def test_page_type_outside_scope_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "KMLT", "news", "Minimum feature size achievable: 8 µm.")
                return collect_capability_specs()

            result = self._run(actors_db, run)
            self.assertEqual(0, result["actors_with_pages"])
            with dbmod.connect(actors_db) as db:
                count = db.execute("SELECT COUNT(*) FROM capability_spec").fetchone()[0]
            self.assertEqual(0, count)

    def test_no_deterministic_signal_creates_no_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "MeKo", "equipment", "Contact us for more information about our company history.")
                return collect_capability_specs()

            result = self._run(actors_db, run)
            self.assertEqual(0, result["profiles_added"])
            with dbmod.connect(actors_db) as db:
                count = db.execute("SELECT COUNT(*) FROM capability_spec").fetchone()[0]
            self.assertEqual(0, count)

    def test_second_run_updates_rather_than_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "GFH", "product", "Positioning accuracy of ±5 µm.")
                first = collect_capability_specs()
                second = collect_capability_specs()
                return first, second

            first, second = self._run(actors_db, run)
            self.assertEqual(1, first["profiles_added"])
            self.assertEqual(0, second["profiles_added"])
            self.assertEqual(1, second["profiles_updated"])
            with dbmod.connect(actors_db) as db:
                count = db.execute("SELECT COUNT(*) FROM capability_spec").fetchone()[0]
            self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
