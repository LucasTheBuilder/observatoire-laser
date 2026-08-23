from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db as dbmod
from hybrid import ContentBlock
from scrapers import _candidate, _relation_evidence, _section_role


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

    def test_page_title_cannot_supply_market(self):
        block = ContentBlock(
            heading="Process",
            h1="Laser processing",
            h2="Process",
            h3="",
            path="main > section",
            text="Femtosecond laser cutting of stents is performed on a pilot line.",
        )
        fact = _candidate("Example", "https://example.test/medical", "Medical applications", block)
        self.assertIsNone(fact)


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


if __name__ == "__main__":
    unittest.main()
