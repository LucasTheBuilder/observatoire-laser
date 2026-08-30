from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db as dbmod
from hybrid import ContentBlock
from scrapers import (
    COMPONENTS,
    MARKETS,
    OPERATIONS,
    PERFORMANCE_TERMS,
    PROCESS_TECHNOLOGIES,
    _candidate,
    _match_all_labels,
    _relation_evidence,
    _section_role,
)


class LocalRelationTests(unittest.TestCase):
    def test_alphanov_style_cross_section_contamination_is_rejected(self):
        block = ContentBlock(
            heading="Publications",
            h1="Lasers",
            h2="Publications",
            h3="",
            path="main > section.publications",
            text=(
                "Optical technologies are at the heart of our activity. "
                "LIPSS and DLIP for high throughput surface processing. "
                "Femtosecond laser surface texturing for advanced manufacturing."
            ),
        )
        self.assertEqual(_section_role(block), "publication")
        strength, _ = _relation_evidence(block)
        self.assertIsNone(strength)
        fact = _candidate(
            "ALPHANOV",
            "https://www.alphanov.com/en/application-sectors/lasers",
            "Lasers",
            block,
        )
        self.assertIsNone(fact)

    def test_publication_can_be_valid_when_relation_is_direct(self):
        block = ContentBlock(
            heading="Publications",
            h1="Laser processing",
            h2="Publications",
            h3="",
            path="main > section.publications > article",
            text=(
                "Femtosecond laser texturing of medical stent is demonstrated during process qualification."
            ),
        )
        fact = _candidate("Example", "https://example.test/publications/stents", "Publications", block)
        self.assertIsNotNone(fact)
        self.assertEqual(fact["market"], "Médical")
        self.assertEqual(fact["component"], "Stents")
        self.assertEqual(fact["operation"], "Texturation")
        self.assertEqual(fact["relation_strength"], "direct")
        self.assertEqual(fact["source_role"], "publication")
        self.assertEqual(fact["bucket"], "radar")

    def test_news_can_be_valid_when_relation_is_direct(self):
        block = ContentBlock(
            heading="Latest news",
            h1="News",
            h2="Latest news",
            h3="",
            path="main > section.news > article",
            text="Femtosecond laser drilling of medical catheter enters series production.",
        )
        fact = _candidate("Example", "https://example.test/news/catheter", "Latest news", block)
        self.assertIsNotNone(fact)
        self.assertEqual(fact["source_role"], "news")
        self.assertEqual(fact["bucket"], "existing")

    def test_related_project_can_be_valid_with_heading_market_context(self):
        block = ContentBlock(
            heading="Related projects",
            h1="Projects",
            h2="Medical",
            h3="Related projects",
            path="main > section.related-projects > article",
            text="The project develops femtosecond laser cutting of stent on a pilot line.",
        )
        fact = _candidate("Example", "https://example.test/projects/stent", "Projects", block)
        self.assertIsNotNone(fact)
        self.assertEqual(fact["market"], "Médical")
        self.assertEqual(fact["relation_strength"], "contextual")
        self.assertEqual(fact["source_role"], "project")

    def test_quantum_photonics_and_life_sciences_markets_are_recognized(self):
        # Dedicated application-hub verticals that a pure equipment/CDMO vendor commonly
        # publishes (e.g. FEMTOprint's applications/quantum.asp, /photonics.asp, /life-sciences.asp)
        # -- previously unrecognized, so pages about them never produced a market fact.
        cases = [
            ("Femtosecond laser micromachining is used for glass ion trap components in quantum computing systems.", "Quantum"),
            ("Femtosecond laser engraving is used for glass waveguide components in photonics interconnects.", "Photonique"),
            ("Femtosecond laser micromachining is used for microfluidic device components in life sciences applications.", "Sciences de la vie"),
        ]
        for text, expected_market in cases:
            with self.subTest(market=expected_market):
                block = ContentBlock(
                    heading="Applications", h1="Applications", h2=expected_market, h3="",
                    path="main > section.applications", text=text,
                )
                fact = _candidate("FEMTOprint", "https://www.femtoprint.ch/applications/example.asp", "Applications", block)
                self.assertIsNotNone(fact)
                self.assertEqual(fact["market"], expected_market)

    def test_optical_and_photonics_wording_is_not_flagged_ambiguous(self):
        # "optical fiber" and "photonics" are near-synonymous phrasing for the same application
        # in this industry's prose; before MARKET_SYNONYM_CLUSTERS this tripped the multi-market
        # ambiguity guard (no component canonically resolves Optique vs Photonique) and silently
        # dropped an otherwise valid direct fact.
        block = ContentBlock(
            heading="Applications", h1="Applications", h2="Photonics", h3="",
            path="main > section.applications",
            text="Femtosecond laser welding is used for optical fiber components in photonics interconnects.",
        )
        fact = _candidate("Example", "https://example.test/applications/photonics", "Applications", block)
        self.assertIsNotNone(fact)
        self.assertEqual(fact["market"], "Photonique")
        self.assertEqual(fact["component"], "Fibres optiques")

    def test_industrial_need_performance_terms_are_recognized(self):
        # These capture the customer's underlying industrial need (why the process is wanted),
        # not the laser's own spec -- an external audit found the whole dimension absent from
        # the lexicon (not just unmatched in current data). "yield"/"intégration" are kept to
        # compound phrases only, since the bare words over-match unrelated contexts.
        cases = [
            ("The process minimizes the heat affected zone (HAZ) for sensitive parts.", "Maîtrise thermique"),
            ("A debris-free, burr-free cut is achieved without post-processing.", "Propreté du procédé"),
            ("Low surface roughness is obtained thanks to optimized scanning.", "Rugosité maîtrisée"),
            ("Friction reduction and tribological performance were measured.", "Frottement maîtrisé"),
            ("The hydrophobic surface improves wettability for coating applications.", "Mouillabilité"),
            ("Production yield increased after process integration on the line.", "Rendement de production"),
            ("Cette découpe garantit un débit élevé et une cadence de production stable.", "Productivité"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                labels = {label for label, _ in _match_all_labels(text, PERFORMANCE_TERMS)}
                self.assertIn(expected, labels)

    def test_reinforced_market_terms_are_recognized(self):
        # An external audit flagged four under-covered verticals, verified against the live
        # lexicon before adding anything (electronics/packaging, glass-as-multi-market,
        # tooling/molds, energy beyond batteries+PV): Capteurs/Packaging avancé/TGV/microfluidic
        # components already existed, but these did not.
        cases = [
            ("Laser-based hydrogen electrolyzer components for fuel cell stacks.", MARKETS, "Hydrogène"),
            ("Precision electrical connector manufacturing.", COMPONENTS, "Connectique"),
            ("A passive component supplier for surface mount component packaging.", COMPONENTS, "Composants passifs"),
            ("Integrated photonics and photonic integrated circuit fabrication.", COMPONENTS, "Optique intégrée"),
            ("Microdisplay and display panel glass processing for AR headsets.", COMPONENTS, "Composants d'affichage"),
            ("Injection mold texturing for functional surfaces.", COMPONENTS, "Moules et outillage de précision"),
            ("Wire bonding and die attach for chip-scale micro-assembly.", OPERATIONS, "Micro-assemblage"),
            ("Improved lubrication and friction reduction on the textured mold surface.", PERFORMANCE_TERMS, "Frottement maîtrisé"),
        ]
        for text, lexicon, expected in cases:
            with self.subTest(expected=expected):
                labels = {label for label, _ in _match_all_labels(text, lexicon)}
                self.assertIn(expected, labels)

    def test_science_to_industry_process_technologies_are_recognized(self):
        # Two more axes flagged by the same audit as under-covered for tracking science ->
        # industry readiness signals (see the new technology_signals table in db.py).
        cases = [
            ("The system relies on dynamic beam shaping via a programmable laser beam for 3D surfaces.", "Beam shaping"),
            ("A digital twin enables data-driven process optimization and in-line monitoring.", "Monitoring IA procédé"),
            ("Direct laser interference patterning (DLIP) produces periodic functional surfaces.", "DLIP"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                labels = {label for label, _ in _match_all_labels(text, PROCESS_TECHNOLOGIES)}
                self.assertIn(expected, labels)

    def test_page_title_cannot_supply_market(self):
        # h1 ("Laser processing") is page-wide and deliberately excluded from market resolution
        # (see _relation_evidence step 2's comment), and _candidate() is called directly here
        # without a page_market (that's scrape_market()'s job, via _url_market_hint -- see
        # PageMarketHintTests) -- so market must stay unresolved either way. Component
        # ("Stents") and operation ("Microdécoupe") are still present, so chantier 2 item 2
        # keeps this as a partial fact rather than discarding it, but never with a guessed market.
        block = ContentBlock(
            heading="Process",
            h1="Laser processing",
            h2="Process",
            h3="",
            path="main > section",
            text="Femtosecond laser cutting of stents is performed on a pilot line.",
        )
        fact = _candidate("Example", "https://example.test/medical", "Medical applications", block)
        self.assertIsNotNone(fact)
        self.assertEqual("partial", fact["fact_status"])
        self.assertIsNone(fact["market"])


class MarketResetTests(unittest.TestCase):
    def test_reset_market_database_keeps_other_db_paths_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original_data = dbmod.DATA_DIR
            original_market = dbmod.MARKET_DB
            original_actors = dbmod.ACTORS_DB
            original_tech = dbmod.TECH_DB
            try:
                dbmod.DATA_DIR = root
                dbmod.MARKET_DB = root / "market.db"
                dbmod.ACTORS_DB = root / "actors.db"
                dbmod.TECH_DB = root / "technology.db"
                dbmod.init_databases()
                with dbmod.connect(dbmod.MARKET_DB) as conn:
                    conn.execute("INSERT INTO evidence(actor_name,bucket,market,component,operation,industrial_stage,source_url,quote,source_group,fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 ("Old","existing","Médical","Stents","Texturation","Production","https://old","old","old","old-fp",dbmod.utc_now(),dbmod.utc_now()))
                backup = dbmod.reset_market_database(backup=True)
                self.assertIsNotNone(backup)
                self.assertTrue(backup.exists())
                self.assertEqual(dbmod.scalar(dbmod.MARKET_DB, "SELECT COUNT(*) FROM evidence"), 0)
                self.assertTrue(dbmod.ACTORS_DB.exists())
                self.assertTrue(dbmod.TECH_DB.exists())
            finally:
                dbmod.DATA_DIR = original_data
                dbmod.MARKET_DB = original_market
                dbmod.ACTORS_DB = original_actors
                dbmod.TECH_DB = original_tech


class BackupRotationTests(unittest.TestCase):
    def test_backup_all_databases_snapshots_existing_dbs_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = (dbmod.DATA_DIR, dbmod.ACTORS_DB, dbmod.MARKET_DB, dbmod.TECH_DB)
            try:
                dbmod.DATA_DIR = root
                dbmod.ACTORS_DB = root / "actors.db"
                dbmod.MARKET_DB = root / "market.db"
                dbmod.TECH_DB = root / "technology.db"
                dbmod.init_databases()
                dbmod.TECH_DB.unlink()  # simulate a database that hasn't been created yet
                saved = dbmod.backup_all_databases()
                self.assertEqual(2, len(saved))
                self.assertTrue(all(path.exists() for path in saved))
                self.assertTrue(all(path.parent == root / "backups" for path in saved))
            finally:
                dbmod.DATA_DIR, dbmod.ACTORS_DB, dbmod.MARKET_DB, dbmod.TECH_DB = original

    def test_prune_backups_keeps_only_the_most_recent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backups_dir = root / "backups"
            backups_dir.mkdir()
            original_data_dir = dbmod.DATA_DIR
            try:
                dbmod.DATA_DIR = root
                for i in range(5):
                    (backups_dir / f"market_2026010{i}T000000Z.db").write_text("x")
                dbmod._prune_backups("market", keep=2)
                remaining = sorted(path.name for path in backups_dir.glob("market_*.db"))
                self.assertEqual(["market_20260103T000000Z.db", "market_20260104T000000Z.db"], remaining)
            finally:
                dbmod.DATA_DIR = original_data_dir


if __name__ == "__main__":
    unittest.main()
