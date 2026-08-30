from __future__ import annotations

import sys
import unittest
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import scrapers
from db import connect, scalar
from hybrid import ContentBlock, _pdf_metadata_date, classify_source, parse_document
from scrapers import (
    APPLICATION_ARCHITECTURES,
    PROCESS_TECHNOLOGIES,
    _candidate,
    _context_for_block,
    _match_label,
    adaptive_decision,
)

LASEA_PAGE = """
<!doctype html><html><head><title>LASEA Applications</title></head><body>
<header><nav>
  <a href="/applications/iol">Micromachining of intraocular lenses (IOLs)</a>
  <a href="/applications/microfluidics">Micro-welding for microfluidic devices</a>
  <a href="/products">Products</a><a href="/news">News</a>
</nav></header>
<main>
  <section class="applications">
    <article class="application-card"><h2>Intraocular lenses</h2>
      <p>Femtosecond laser micromachining of intraocular lenses for medical devices.</p>
    </article>
    <article class="application-card"><h2>Microfluidic devices</h2>
      <p>Femtosecond laser micro-welding of microfluidic devices in a qualified production service.</p>
    </article>
  </section>
</main></body></html>
"""


class HybridExtractionTests(unittest.TestCase):
    def test_navigation_is_discovery_only(self):
        document = parse_document(LASEA_PAGE, "https://www.lasea.eu/")
        self.assertTrue(any(link["context"] == "navigation" for link in document.links))
        self.assertTrue(all("Products News" not in block.text for block in document.blocks))

    def test_lasea_cards_are_never_cross_joined(self):
        document = parse_document(LASEA_PAGE, "https://www.lasea.eu/")
        self.assertEqual(2, len(document.blocks))
        candidates = [_candidate("LASEA", "https://www.lasea.eu/applications", document.title, block) for block in document.blocks]
        candidates = [candidate for candidate in candidates if candidate]
        self.assertTrue(any(candidate["market"] == "Médical" and candidate["component"] == "Lentilles intraoculaires (IOL)" for candidate in candidates))
        self.assertFalse(any(candidate["component"] == "Lentilles intraoculaires (IOL)" and candidate["operation"] == "Soudage" for candidate in candidates))

    def test_lasea_context_never_reads_neighboring_cards(self):
        document = parse_document(LASEA_PAGE, "https://www.lasea.eu/")
        direct, context = _context_for_block(document.title, document.blocks, 0)
        self.assertIn("intraocular lenses", direct.lower())
        self.assertNotIn("micro-welding", direct.lower())
        self.assertNotIn("micro-welding", context.lower())

    def test_manutech_tiny_item_wrappers_preserve_parent_content(self):
        html = """<html><head><title>MANUTECH Applications</title></head><body><main>
          <section class="elementor-widget application"><div class="item">Laser</div>
          <h2>Microfluidic glass components</h2>
          <p>Femtosecond laser welding of microfluidic devices in qualified production.</p>
          </section></main></body></html>"""
        document = parse_document(html, "https://www.manutech-usd.fr/en/applications/")
        self.assertEqual(1, len(document.blocks))
        self.assertIn("microfluidic devices", document.blocks[0].text.lower())
        self.assertNotEqual("empty", document.extraction_method)

    def test_priority_sections_and_unhelpful_links_are_classified(self):
        application, application_score = classify_source("https://www.manutech-usd.fr/applications-manutech/")
        project, project_score = classify_source("https://www.manutech-usd.fr/en/projects/")
        news, news_score = classify_source("https://www.manutech-usd.fr/actualites/")
        blog, _ = classify_source("https://www.manutech-usd.fr/en/blog/")
        contact, _ = classify_source("https://www.manutech-usd.fr/contact/")
        self.assertEqual(("application", "project", "news", "news", "ignore"),
                         (application, project, news, blog, contact))
        self.assertGreater(application_score, project_score)
        self.assertGreater(project_score, news_score)

    def test_local_contract_manufacturing_synonyms_classify_as_service(self):
        # A German/Italian/Spanish site describing contract manufacturing under its own
        # local term must be recognized just like the English "contract manufacturing" --
        # searching only in English/French missed MeKo, HAILTEC and KMLT (audit finding).
        german, _ = classify_source("https://example.de/lohnfertigung/")
        german_alt, _ = classify_source("https://example.de/auftragsfertigung/")
        italian, _ = classify_source("https://example.it/lavorazione-conto-terzi/")
        spanish, _ = classify_source("https://example.es/fabricacion-por-contrato/")
        self.assertEqual(("service", "service", "service", "service"), (german, german_alt, italian, spanish))

    def test_incomplete_market_evidence_remains_in_review(self):
        # Component ("Implants") and operation ("Texturation") are both present, but no market
        # term appears anywhere in the block and the URL is a generic "/applications/" hub (not
        # specific enough for _url_market_hint to supply one) -- chantier 2 item 2 accepts this
        # as a partial fact pending human review instead of discarding it outright.
        block = ContentBlock(
            heading="Titanium implants",
            text="Femtosecond laser surface texturing of titanium implants in qualified production.",
            path="main > article.card",
        )
        candidate = _candidate("MANUTECH USD", "https://www.manutech-usd.fr/applications/", "Applications", block)
        self.assertIsNotNone(candidate)
        self.assertEqual("partial", candidate["fact_status"])
        self.assertIsNone(candidate["market"])
        self.assertEqual("Implants", candidate["component"])
        self.assertEqual("Texturation", candidate["operation"])

    def test_tgv_requires_a_glass_or_via_context(self):
        self.assertIsNone(_match_label("The TGV train operates a high speed transport service.", PROCESS_TECHNOLOGIES))
        self.assertEqual("TGV", _match_label("Femtosecond laser TGV glass via drilling for interposer packaging.", APPLICATION_ARCHITECTURES))

    def test_first_mapping_checks_three_manutech_sections_immediately(self):
        homepage = """<html><head><title>MANUTECH USD</title></head><body><header><nav>
          <a href="/en/applications/">Applications</a><a href="/en/projects/">Projects</a>
          <a href="/en/blog/">News</a><a href="/en/contact/">Contact</a>
          </nav></header><main><div class="item">Short home</div></main></body></html>"""
        section = """<html><head><title>Femtosecond laser projects</title></head><body><main>
          <article class="application-card"><h2>Medical microfluidic devices</h2>
          <p>Femtosecond laser micro-welding of microfluidic devices for qualified production.</p>
          </article></main></body></html>"""

        class FakeResponse:
            def __init__(self, url: str):
                self.url = url
                self.status_code = 200
                self.text = section if any(term in url for term in ("applications", "projects", "blog")) else homepage
                self.content = self.text.encode("utf-8")
                self.headers: dict[str, str] = {}

            def raise_for_status(self):
                return None

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def get(self, url: str, headers=None):
                return FakeResponse(url)

        with TemporaryDirectory() as directory:
            directory_path = Path(directory)
            # Build a freshly-seeded template in an isolated location instead of touching the
            # project's real data/ files: init_databases() mutates whatever ACTORS_DB/MARKET_DB/
            # TECH_DB point to, including running destructive migrations (dedup merges), so it
            # must never run against the real paths from a test.
            template_actors_db = directory_path / "template_actors.db"
            with patch.object(dbmod, "ACTORS_DB", template_actors_db), \
                 patch.object(dbmod, "MARKET_DB", directory_path / "template_market.db"), \
                 patch.object(dbmod, "TECH_DB", directory_path / "template_technology.db"):
                dbmod.init_databases()

            isolated_db = directory_path / "actors.db"
            copy2(template_actors_db, isolated_db)
            with connect(isolated_db) as db:
                db.execute("UPDATE actors SET active=CASE WHEN name='MANUTECH USD' THEN 1 ELSE 0 END")
                actor_id = db.execute("SELECT id FROM actors WHERE name='MANUTECH USD'").fetchone()[0]
                db.execute("DELETE FROM actor_sources WHERE actor_id=?", (actor_id,))
                db.execute("""INSERT INTO actor_sources(actor_id,url,source_kind,page_type,source_score)
                              VALUES(?,?,?,?,?)""", (actor_id, "https://www.manutech-usd.fr/en/", "official", "homepage", 35))

            with patch.object(scrapers, "ACTORS_DB", isolated_db), \
                 patch.object(scrapers.OllamaClient, "available", return_value=False), \
                 patch("scrapers.httpx.Client", return_value=FakeClient()):
                result = scrapers.scrape_actors(max_pages_per_actor=4)

            with connect(isolated_db) as db:
                source_rows = list(db.execute("""SELECT page_type,last_http_status,ambiguous FROM actor_sources
                                                WHERE actor_id=? AND page_type IN ('application','project','news')""", (actor_id,)))
                profile = db.execute("SELECT status,needs_reprofile,coverage_ready,coverage_discovered FROM site_profiles WHERE actor_id=?", (actor_id,)).fetchone()

        self.assertEqual(4, result["scanned"])
        self.assertEqual({"application", "project", "news"}, {row["page_type"] for row in source_rows})
        self.assertTrue(all(row["last_http_status"] == 200 and row["ambiguous"] == 0 for row in source_rows))
        self.assertEqual(("ready", 0, 3, 3), tuple(profile))

    def test_generic_anomaly_switches_to_adaptive(self):
        self.assertEqual(("generic", False), adaptive_decision(False, 2, 12, 0, 2))
        self.assertEqual(("adaptive", True), adaptive_decision(False, 0, 0, 2, 2))

    def test_exactly_four_priority_profiles_are_seeded(self):
        with TemporaryDirectory() as directory:
            directory_path = Path(directory)
            actors_db = directory_path / "actors.db"
            with patch.object(dbmod, "ACTORS_DB", actors_db), \
                 patch.object(dbmod, "MARKET_DB", directory_path / "market.db"), \
                 patch.object(dbmod, "TECH_DB", directory_path / "technology.db"):
                dbmod.init_databases()
            count = scalar(actors_db, """SELECT COUNT(*) FROM site_profiles p JOIN actors a ON a.id=p.actor_id
                                          WHERE a.priority=1 AND p.strategy='adaptive'""")
        self.assertEqual(4, count)


class PublishedDateExtractionTests(unittest.TestCase):
    """Chantier 4 (fiabiliser la preuve) : source_date doit se remplir dès que la page en
    expose une, sans jamais en inventer une quand elle n'en expose pas."""

    def test_article_published_time_meta_tag_wins(self):
        html = """<html><head><title>News</title>
            <meta property="article:published_time" content="2024-03-15T10:00:00+01:00"></head>
            <body><main><h1>News</h1><p>Femtosecond laser news for industrial customers today.</p></main></body></html>"""
        document = parse_document(html, "https://example.test/news/laser-award")
        self.assertEqual("2024-03-15", document.published_date)

    def test_json_ld_date_published_is_read_when_no_meta_tag(self):
        html = """<html><head><title>Blog</title>
            <script type="application/ld+json">{"@type":"Article","datePublished":"2023-07-01","headline":"x"}</script>
            </head><body><main><h1>Blog</h1><p>Laser texturing capability update for our customers.</p></main></body></html>"""
        document = parse_document(html, "https://example.test/blog/update")
        self.assertEqual("2023-07-01", document.published_date)

    def test_url_date_pattern_is_the_last_resort(self):
        html = """<html><head><title>Article</title></head>
            <body><main><h1>Article</h1><p>Femtosecond laser processing news for the industry.</p></main></body></html>"""
        document = parse_document(html, "https://example.test/2022/11/03/laser-news")
        self.assertEqual("2022-11-03", document.published_date)

    def test_no_date_anywhere_leaves_it_none_rather_than_guessing(self):
        html = """<html><head><title>Page</title></head>
            <body><main><h1>Page</h1><p>Femtosecond laser processing for industrial customers.</p></main></body></html>"""
        document = parse_document(html, "https://example.test/applications/medical")
        self.assertIsNone(document.published_date)

    def test_implausible_year_is_rejected_not_returned(self):
        html = """<html><head><title>Page</title>
            <meta property="article:published_time" content="1899-01-01"></head>
            <body><main><h1>Page</h1><p>Femtosecond laser processing for industrial customers.</p></main></body></html>"""
        document = parse_document(html, "https://example.test/page")
        self.assertIsNone(document.published_date)

    def test_pdf_metadata_creation_date_is_parsed(self):
        self.assertEqual("2023-06-15", _pdf_metadata_date({"CreationDate": "D:20230615120000+02'00'"}))
        self.assertIsNone(_pdf_metadata_date({}))
        self.assertIsNone(_pdf_metadata_date({"CreationDate": "not a pdf date"}))


if __name__ == "__main__":
    unittest.main()
