"""Tests pour capabilities.py (chantier 5, durci par l'audit v8 priorité 4) : capability_spec,
l'enveloppe de capacités chiffrées extraite déterministiquement des pages
product/equipment/capability/service/about déjà crawlées.

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
from capabilities import (
    _extract_capabilities,
    _extract_certifications,
    _extract_cleanroom_class,
    _parse_number,
    collect_capability_specs,
)


def _block(text: str, heading: str = "") -> dict:
    return {"heading": heading, "h1": "", "h2": "", "h3": "", "text": text, "path": "main > p", "media_context": ""}


def _pages(*texts: str, url: str = "https://example.test/page") -> list[tuple[str, str]]:
    """Builds the (source_url, text) pairs _extract_* now expects -- one fixed URL for every
    text unless a test needs to tell two pages apart (see the source-tracking tests below)."""
    return [(url, text) for text in texts]


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
        fields = _extract_capabilities(_pages(
            "Available wavelengths: 1030 nm, 515 nm and 343 nm.",
            "Cable length 5000 nm reel (irrelevant).",
        ))
        self.assertEqual([343, 515, 1030], fields["wavelengths_nm"])

    def test_wavelength_requires_laser_context_word_in_same_block(self):
        # Audit v8 §2.4: a bare "nm" number on a generic equipment page (no laser/wavelength
        # wording nearby) must never be attributed as a laser wavelength -- same principle
        # already applied to feature size/part size below.
        with_context = _extract_capabilities(_pages("This laser operates at 1064 nm."))
        without_context = _extract_capabilities(_pages("Sensor resolution: 1064 nm."))
        self.assertEqual([1064], with_context["wavelengths_nm"])
        self.assertEqual([], without_context["wavelengths_nm"])

    def test_pulse_duration_takes_shortest(self):
        fields = _extract_capabilities(_pages("System A: 290 fs pulse duration.", "System B: 900 fs pulse duration."))
        self.assertEqual(290.0, fields["pulse_duration_fs"])

    def test_pulse_duration_outside_audit_range_is_rejected(self):
        # Audit veille §10.12 (0.8), 100-1500fs. Values outside are not written at all rather
        # than kept -- silence is preferable to a number nobody can defend.
        too_short = _extract_capabilities(_pages("Ultra-short 30 fs pulse duration."))
        too_long = _extract_capabilities(_pages("System operates at 5000 fs pulse duration."))
        self.assertIsNone(too_short["pulse_duration_fs"])
        self.assertIsNone(too_long["pulse_duration_fs"])

    def test_wavelength_far_from_any_known_laser_line_is_rejected(self):
        # Audit veille §9.4, real production example: an actor's page yielded
        # [200, 206, 250, 257, 258, 300, 330, 343, 515, 1030, 1064, 2000] -- only
        # 257/343/515/1030/1064 are plausible ultrafast laser lines, the rest match no known
        # gain-medium fundamental or harmonic.
        fields = _extract_capabilities(_pages(
            "Laser wavelengths available: 200 nm, 206 nm, 250 nm, 258 nm, 300 nm, 330 nm, "
            "343 nm, 515 nm, 1030 nm, 1064 nm, 2000 nm."
        ))
        self.assertEqual([258, 343, 515, 1030, 1064], fields["wavelengths_nm"])

    def test_wavelength_within_tolerance_of_a_known_line_is_accepted(self):
        # 258nm is within 5nm of the real 257nm (4th harmonic of 1030nm) line.
        fields = _extract_capabilities(_pages("Laser output at 258 nm."))
        self.assertEqual([258], fields["wavelengths_nm"])

    def test_tolerance_uses_plus_minus_marker(self):
        fields = _extract_capabilities(_pages("Positioning accuracy of ±2 µm on all axes."))
        self.assertEqual(2.0, fields["tolerance_um"])

    def test_feature_size_requires_context_word_in_same_block(self):
        # A bare µm value with no feature-size context must never be attributed to feature size.
        with_context = _extract_capabilities(_pages("Minimum feature size achievable: 8 µm."))
        without_context = _extract_capabilities(_pages("Cable diameter: 8 µm."))
        self.assertEqual(8.0, with_context["min_feature_size_um"])
        self.assertIsNone(without_context["min_feature_size_um"])

    def test_feature_size_outside_audit_range_is_rejected(self):
        # Audit veille §10.12 (0.8), 0.5-200µm.
        too_small = _extract_capabilities(_pages("Minimum feature size achievable: 0.05 µm."))
        too_large = _extract_capabilities(_pages("Minimum feature size achievable: 500 µm."))
        self.assertIsNone(too_small["min_feature_size_um"])
        self.assertIsNone(too_large["min_feature_size_um"])
        plausible = _extract_capabilities(_pages("Minimum feature size achievable: 10 µm."))
        self.assertEqual(10.0, plausible["min_feature_size_um"])

    def test_part_size_requires_context_word_and_takes_largest(self):
        fields = _extract_capabilities(_pages("X-Y travel of 300 mm.", "X-Y travel up to 600 mm on the large-format stage."))
        self.assertEqual(600.0, fields["max_part_size_mm"])

    def test_implausible_part_size_for_micromachining_is_rejected(self):
        # Audit veille §9.4/§10.12, real production examples: Femtika max_part_size_mm=2680
        # (2.68 m) and Oxford Lasers 983mm were both kept by the old 1500mm bound despite not
        # being plausible for this segment -- tightened to 600mm on 30/08/2026.
        fields = _extract_capabilities(_pages("Work envelope up to 2680 mm for large panels."))
        self.assertIsNone(fields["max_part_size_mm"])
        fields = _extract_capabilities(_pages("Work envelope up to 983 mm for large panels."))
        self.assertIsNone(fields["max_part_size_mm"])
        plausible = _extract_capabilities(_pages("Work envelope up to 300 mm for large panels."))
        self.assertEqual(300.0, plausible["max_part_size_mm"])

    def test_throughput_pattern_and_largest_kept(self):
        fields = _extract_capabilities(_pages("Standard line: 1200 parts/hour.", "Fast line: 3000 parts per hour."))
        self.assertEqual(3000.0, fields["throughput_units_per_h"])

    def test_batch_size_range_kept_as_verbatim_quote(self):
        fields = _extract_capabilities(_pages("We process from 1 to 100000 parts depending on the program."))
        self.assertIn("1", fields["batch_size_range"])
        self.assertIn("100000", fields["batch_size_range"])

    def test_materials_qualified_uses_shared_lexicon(self):
        fields = _extract_capabilities(_pages("Qualified on titanium, stainless steel and fused silica glass parts."))
        self.assertIn("Métal", fields["materials_qualified"])
        self.assertIn("Verre", fields["materials_qualified"])

    def test_no_signal_yields_all_none(self):
        fields = _extract_capabilities(_pages("Contact us for more information about our company history."))
        self.assertIsNone(fields["min_feature_size_um"])
        self.assertIsNone(fields["tolerance_um"])
        self.assertIsNone(fields["max_part_size_mm"])
        self.assertIsNone(fields["throughput_units_per_h"])
        self.assertIsNone(fields["pulse_duration_fs"])
        self.assertIsNone(fields["batch_size_range"])
        self.assertEqual([], fields["wavelengths_nm"])
        self.assertEqual([], fields["materials_qualified"])

    def test_each_field_is_sourced_from_the_page_that_actually_carries_it(self):
        # Audit v8 §2.4/priority 4: capability_spec used to share a single source_url across
        # every field, regardless of which page actually produced the winning value. Two
        # different pages here must each be credited for the field they actually contributed.
        page_texts = [
            ("https://example.test/product-a", "Minimum feature size achievable: 8 µm."),
            ("https://example.test/product-b", "X-Y travel of 600 mm on the large-format stage."),
        ]
        fields = _extract_capabilities(page_texts)
        self.assertEqual("https://example.test/product-a", fields["min_feature_size_um_source_url"])
        self.assertEqual("https://example.test/product-b", fields["max_part_size_mm_source_url"])

    def test_winning_value_from_a_later_page_is_sourced_to_that_page(self):
        # Not just "the first page wins by default": the source must track whichever page's
        # value was actually retained (here, the largest part size, found on the second page).
        page_texts = [
            ("https://example.test/product-a", "X-Y travel of 300 mm."),
            ("https://example.test/product-b", "X-Y travel up to 600 mm on the large-format stage."),
        ]
        fields = _extract_capabilities(page_texts)
        self.assertEqual(600.0, fields["max_part_size_mm"])
        self.assertEqual("https://example.test/product-b", fields["max_part_size_mm_source_url"])


class ExtractCertificationsTests(unittest.TestCase):
    def test_recognized_codes_are_matched_regardless_of_spacing(self):
        found = _extract_certifications(_pages("Our quality system is certified ISO9001 and ISO 13485 for medical devices."))
        self.assertEqual({"ISO 13485", "ISO 9001"}, set(found))

    def test_aerospace_and_export_control_codes(self):
        found = _extract_certifications(_pages("AS9100 certified, Nadcap accredited for special processes, ITAR registered."))
        self.assertEqual({"AS9100", "ITAR", "Nadcap"}, set(found))

    def test_unrelated_iso_number_is_not_a_certification(self):
        # ISO 8601 is a date format, not a quality/industry certification -- must never match.
        found = _extract_certifications(_pages("Dates on this page follow ISO 8601."))
        self.assertEqual({}, found)

    def test_each_certification_is_sourced_to_the_page_that_states_it(self):
        page_texts = [
            ("https://example.test/quality", "Our facility is certified ISO 9001."),
            ("https://example.test/medical", "Our medical line is certified ISO 13485."),
        ]
        found = _extract_certifications(page_texts)
        self.assertEqual("https://example.test/quality", found["ISO 9001"])
        self.assertEqual("https://example.test/medical", found["ISO 13485"])


class ExtractCleanroomClassTests(unittest.TestCase):
    def test_iso_class_requires_cleanroom_context(self):
        with_context, source = _extract_cleanroom_class(_pages("Machining is performed in an ISO 7 cleanroom."))
        without_context, no_source = _extract_cleanroom_class(_pages("Section ISO 7 of the quality manual covers calibration."))
        self.assertEqual("ISO 7", with_context)
        self.assertTrue(source)
        self.assertIsNone(without_context)
        self.assertIsNone(no_source)

    def test_iso_class_prefers_the_best_finest_across_blocks(self):
        result, _source = _extract_cleanroom_class(_pages("Standard cleanroom: ISO 8.", "Premium line operates in an ISO 5 cleanroom."))
        self.assertEqual("ISO 5", result)

    def test_federal_standard_209e_class_fallback(self):
        result, _source = _extract_cleanroom_class(_pages("Assembly takes place in a Class 10000 clean room."))
        self.assertEqual("Class 10000", result)

    def test_iso_scale_preferred_over_federal_when_both_present(self):
        result, _source = _extract_cleanroom_class(_pages("Legacy Class 1000 clean room, now rated ISO 6 cleanroom."))
        self.assertEqual("ISO 6", result)

    def test_no_cleanroom_context_at_all_yields_none(self):
        result, source = _extract_cleanroom_class(_pages("General manufacturing floor, no special classification."))
        self.assertIsNone(result)
        self.assertIsNone(source)

    def test_best_class_is_sourced_to_the_page_that_states_it(self):
        page_texts = [
            ("https://example.test/standard", "Standard cleanroom: ISO 8."),
            ("https://example.test/premium", "Premium line operates in an ISO 5 cleanroom."),
        ]
        result, source = _extract_cleanroom_class(page_texts)
        self.assertEqual("ISO 5", result)
        self.assertEqual("https://example.test/premium", source)


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
                    """SELECT min_feature_size_um,tolerance_um,source_url,
                              min_feature_size_um_source_url,tolerance_um_source_url
                       FROM capability_spec c JOIN actors a ON a.id=c.actor_id WHERE a.name='FEMTOprint'"""
                ).fetchone()
            self.assertEqual(8.0, row["min_feature_size_um"])
            self.assertEqual(2.0, row["tolerance_um"])
            self.assertTrue(row["source_url"])
            # Both fields came from the same (only) seeded page here, but through the per-field
            # column now -- not just the legacy shared `source_url`.
            self.assertEqual(row["source_url"], row["min_feature_size_um_source_url"])
            self.assertEqual(row["source_url"], row["tolerance_um_source_url"])

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

    def test_certification_found_on_about_page_is_written_to_actor_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "Micreon", "about", "Our facility is certified ISO 9001 and ISO 13485.")
                return collect_capability_specs()

            result = self._run(actors_db, run)
            self.assertEqual(2, result["certifications_added"])
            with dbmod.connect(actors_db) as db:
                values = {
                    row["value"]
                    for row in db.execute(
                        """SELECT value FROM actor_facts f JOIN actors a ON a.id=f.actor_id
                           WHERE a.name='Micreon' AND f.dimension='certification'"""
                    ).fetchall()
                }
            self.assertEqual({"ISO 9001", "ISO 13485"}, values)

    def test_cleanroom_class_found_on_service_page_is_written_as_differentiator(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "Yalosys AG", "service", "Precision machining is performed in an ISO 6 cleanroom.")
                return collect_capability_specs()

            result = self._run(actors_db, run)
            self.assertEqual(1, result["cleanroom_facts_added"])
            with dbmod.connect(actors_db) as db:
                row = db.execute(
                    """SELECT value FROM actor_facts f JOIN actors a ON a.id=f.actor_id
                       WHERE a.name='Yalosys AG' AND f.dimension='differentiator'"""
                ).fetchone()
            self.assertEqual("Salle blanche ISO 6", row["value"])

    def test_preexisting_manually_worded_certification_is_not_duplicated(self):
        # Real production case: an actor already had "ISO 9001:2015" entered by hand before
        # this collector existed. Our canonical "ISO 9001" must recognize that as the same
        # fact instead of adding a second, redundant entry to the fiche.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "LLT Applikation", "about", "Certified ISO 9001 quality management.")
                actor_id = dbmod.rows(actors_db, "SELECT id FROM actors WHERE name='LLT Applikation'")[0]["id"]
                with dbmod.connect(actors_db) as db:
                    db.execute(
                        "INSERT INTO actor_facts(actor_id,dimension,value,source_url,created_at) VALUES(?,?,?,?,?)",
                        (actor_id, "certification", "DIN EN ISO 9001:2015", "https://manual.example/entered-by-hand", dbmod.utc_now()),
                    )
                return collect_capability_specs()

            result = self._run(actors_db, run)
            self.assertEqual(0, result["certifications_added"])
            with dbmod.connect(actors_db) as db:
                count = db.execute(
                    """SELECT COUNT(*) FROM actor_facts f JOIN actors a ON a.id=f.actor_id
                       WHERE a.name='LLT Applikation' AND f.dimension='certification'"""
                ).fetchone()[0]
            self.assertEqual(1, count)

    def test_second_run_does_not_duplicate_certification_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"

            def run():
                self._seed(actors_db, "OpTek Systems", "about", "Certified ISO 9001 facility.")
                first = collect_capability_specs()
                second = collect_capability_specs()
                return first, second

            first, second = self._run(actors_db, run)
            self.assertEqual(1, first["certifications_added"])
            self.assertEqual(0, second["certifications_added"])
            with dbmod.connect(actors_db) as db:
                count = db.execute(
                    """SELECT COUNT(*) FROM actor_facts f JOIN actors a ON a.id=f.actor_id
                       WHERE a.name='OpTek Systems' AND f.dimension='certification'"""
                ).fetchone()[0]
            self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
