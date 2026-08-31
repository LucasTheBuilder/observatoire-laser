"""Tests pour outbound_links et l'admission des sous-domaines (Lot 2 §2.4, audit veille §8.2,
30/08/2026) : "les liens sortants sont détruits, pas seulement ignorés" -- un sous-domaine du
même domaine racine (shop.example.com) était traité comme un tiers externe au même titre qu'un
vrai site tiers, et rien ne gardait trace des vrais liens externes pour la découverte
d'acteurs ("un hôte externe qui revient sur cinq sites différents est un candidat de qualité").
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
from hybrid import _root_domain, parse_document


class RootDomainTests(unittest.TestCase):
    def test_bare_domain_is_its_own_root(self):
        self.assertEqual("example.com", _root_domain("example.com"))

    def test_www_prefix_is_stripped(self):
        self.assertEqual("example.com", _root_domain("www.example.com"))

    def test_subdomain_shares_root_with_bare_domain(self):
        self.assertEqual(_root_domain("example.com"), _root_domain("shop.example.com"))
        self.assertEqual(_root_domain("example.com"), _root_domain("en.example.com"))

    def test_distinct_domains_have_distinct_roots(self):
        self.assertNotEqual(_root_domain("example.com"), _root_domain("otherexample.com"))


class SubdomainAdmissionTests(unittest.TestCase):
    def test_subdomain_link_is_admitted_into_crawl_links(self):
        html = """<html><head><title>Home</title></head><body><main>
          <p><a href="https://shop.example.test/products">Shop</a></p>
        </main></body></html>"""
        document = parse_document(html, "https://www.example.test/")
        urls = [link["url"] for link in document.links]
        self.assertIn("https://shop.example.test/products", urls)
        self.assertEqual([], document.outbound_links)

    def test_external_content_link_is_recorded_as_outbound_not_crawled(self):
        html = """<html><head><title>Partners</title></head><body><main>
          <p>We work with <a href="https://thirdparty.test/about">Third Party</a> on this project.</p>
        </main></body></html>"""
        document = parse_document(html, "https://www.example.test/")
        urls = [link["url"] for link in document.links]
        self.assertNotIn("https://thirdparty.test/about", urls)
        outbound_urls = [link["url"] for link in document.outbound_links]
        self.assertIn("https://thirdparty.test/about", outbound_urls)
        outbound = next(link for link in document.outbound_links if link["url"] == "https://thirdparty.test/about")
        self.assertEqual("thirdparty.test", outbound["host"])

    def test_external_navigation_link_is_neither_crawled_nor_recorded(self):
        # Nav-context external links (social media icons, legal-notice-of-a-third-party in the
        # footer...) are too noisy to signal a candidate actor.
        html = """<html><head><title>Home</title></head><body>
          <header><nav><a href="https://social.example/company">Follow us</a></nav></header>
          <main><p>Welcome.</p></main>
        </body></html>"""
        document = parse_document(html, "https://www.example.test/")
        outbound_urls = [link["url"] for link in document.outbound_links]
        self.assertNotIn("https://social.example/company", outbound_urls)
        crawl_urls = [link["url"] for link in document.links]
        self.assertNotIn("https://social.example/company", crawl_urls)


class OutboundLinksSchemaTests(unittest.TestCase):
    def test_outbound_links_table_dedupes_on_source_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
            with dbmod.connect(actors_db) as db:
                source_id = db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active) VALUES(?,?,1)", (actor_id, "https://example.test/partners"),
                ).lastrowid
                for _ in range(2):
                    db.execute(
                        """INSERT OR IGNORE INTO outbound_links(source_id,target_url,target_host,label,first_seen_at)
                           VALUES(?,?,?,?,?)""",
                        (source_id, "https://thirdparty.test/about", "thirdparty.test", "Third Party", dbmod.utc_now()),
                    )
            count = dbmod.scalar(actors_db, "SELECT COUNT(*) FROM outbound_links WHERE source_id=?", (source_id,))
            self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
