"""Tests pour les pages carrières (Lot 2 §2.3, audit veille §4.A/§8.4, 30/08/2026) : la regex
en dur qui fermait careers?/recrutement/jobs? dans classify_source, non surchargeable par
profil, est descendue dans un profil configurable (ignore_terms) sans ces termes -- les pages
carrières sont désormais crawlées, typées 'careers', exclues du pipeline marché, et scannées
pour des signaux de recrutement techniques (process engineer -- ultrafast laser, etc.).
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
from hybrid import SECTION_SCORES, ContentBlock, classify_source
from scrapers import CAREER_ROLE_TERMS, _extract_career_signal, _select_market_sources, _upsert_career_event
from site_profiles import DEFAULT_SITE_PROFILE


class ClassifySourceCareersTests(unittest.TestCase):
    def test_careers_url_is_typed_careers_not_ignored(self):
        page_type, score = classify_source("https://example.test/careers/", "Careers")
        self.assertEqual("careers", page_type)
        self.assertEqual(SECTION_SCORES["careers"], score)

    def test_jobs_url_variants_are_recognized(self):
        for url, label in [
            ("https://example.test/jobs/", "Jobs"),
            ("https://example.test/en/career/", "Career"),
            ("https://example.test/recrutement/", "Recrutement"),
            ("https://example.test/karriere/", "Karriere"),
        ]:
            with self.subTest(url=url):
                page_type, _ = classify_source(url, label)
                self.assertEqual("careers", page_type)

    def test_default_profile_no_longer_ignores_careers_paths(self):
        # §8.4: ignore_paths used to list /careers, /career, /jobs directly -- removed so the
        # path-fragment gate doesn't block them before classify_source even runs.
        for fragment in ("/careers", "/career", "/jobs"):
            self.assertNotIn(fragment, DEFAULT_SITE_PROFILE["ignore_paths"])

    def test_job_shop_is_never_misclassified_as_careers(self):
        # Real production false positive (30/08/2026): "job shop" is established industry
        # terminology for a contract-manufacturing service (already matched by the SERVICE
        # rule below), not a careers signal -- the careers rule must only match "jobs" plural,
        # never the bare singular "job" that "job shop" contains.
        page_type, _ = classify_source(
            "https://kmlt.de/en/services/laser-marking/", "",
            title="Laser marking, laser engraving - laser job shop at KMLT GmbH",
        )
        self.assertEqual("service", page_type)
        page_type, _ = classify_source("https://example.test/jobshop/", "Job Shop")
        self.assertEqual("service", page_type)

    def test_plural_jobs_still_matches_careers(self):
        page_type, _ = classify_source("https://example.test/careers/", "Jobs")
        self.assertEqual("careers", page_type)

    def test_other_ignore_terms_still_work_by_default(self):
        page_type, _ = classify_source("https://example.test/privacy-policy/", "Privacy")
        self.assertEqual("ignore", page_type)
        page_type, _ = classify_source("https://example.test/contact/", "Contact")
        self.assertEqual("ignore", page_type)

    def test_profile_can_override_ignore_terms_unlike_the_old_hardcoded_regex(self):
        # §8.4's whole point: a profile must be able to reopen what the default closes --
        # something the old hardcoded regex made structurally impossible. Uses a URL/label
        # combination that trips ONLY the ignore_terms check (not the separate ignore_paths
        # fragment check, which "/contact/" would also trigger regardless of ignore_terms).
        custom_profile = dict(DEFAULT_SITE_PROFILE)
        custom_profile["ignore_terms"] = ()
        blocked_by_default, _ = classify_source("https://example.test/about-us/", "Legal Notice")
        self.assertEqual("ignore", blocked_by_default)
        page_type, _ = classify_source("https://example.test/about-us/", "Legal Notice", profile=custom_profile)
        self.assertNotEqual("ignore", page_type)


class SelectMarketSourcesExcludesCareersTests(unittest.TestCase):
    def test_careers_page_type_is_excluded_from_market_extraction(self):
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
                db.execute(
                    """INSERT INTO actor_sources(actor_id,url,active,page_type,last_http_status,content_hash)
                       VALUES(?,?,1,?,200,?)""",
                    (actor_id, "https://example.test/careers/", "careers", "hash1"),
                )
                db.execute(
                    """INSERT INTO actor_sources(actor_id,url,active,page_type,last_http_status,content_hash)
                       VALUES(?,?,1,?,200,?)""",
                    (actor_id, "https://example.test/applications/", "application", "hash2"),
                )
            import scrapers as scrapersmod
            with patch.object(scrapersmod, "ACTORS_DB", actors_db):
                sources = _select_market_sources(max_pages=10)
            urls = [s["url"] for s in sources]
            self.assertNotIn("https://example.test/careers/", urls)
            self.assertIn("https://example.test/applications/", urls)


class ExtractCareerSignalTests(unittest.TestCase):
    def test_role_combined_with_laser_term_is_captured(self):
        block = ContentBlock(
            heading="Process Engineer -- Ultrafast Laser Systems",
            text="We are looking for a Process Engineer specialized in ultrafast laser micromachining.",
            path="main > article",
        )
        signal = _extract_career_signal(block)
        self.assertIsNotNone(signal)
        self.assertIn("Process Engineer", signal)

    def test_generic_role_without_laser_term_is_not_captured(self):
        block = ContentBlock(
            heading="Sales Assistant",
            text="We are looking for a sales assistant to join our commercial team.",
            path="main > article",
        )
        self.assertIsNone(_extract_career_signal(block))

    def test_laser_mention_without_a_technical_role_is_not_captured(self):
        block = ContentBlock(
            heading="Accountant",
            text="Our femtosecond laser company is looking for an accountant.",
            path="main > article",
        )
        self.assertIsNone(_extract_career_signal(block))

    def test_all_career_role_terms_are_lowercase_for_matching(self):
        for term in CAREER_ROLE_TERMS:
            self.assertEqual(term, term.lower())


class UpsertCareerEventTests(unittest.TestCase):
    def _fresh_actors_db(self, tmp: str) -> tuple[Path, int]:
        actors_db = Path(tmp) / "actors.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
            actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/")
        return actors_db, actor_id

    def test_new_signal_is_inserted_as_pending_hiring_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = self._fresh_actors_db(tmp)
            with dbmod.connect(actors_db) as db:
                added = _upsert_career_event(db, actor_id, "Process Engineer -- Ultrafast Laser", "https://example.test/careers/")
            self.assertEqual(1, added)
            row = dbmod.rows(actors_db, "SELECT event_type,review_status,description FROM actor_events WHERE actor_id=?", (actor_id,))[0]
            self.assertEqual("hiring", row["event_type"])
            self.assertEqual("pending", row["review_status"])

    def test_two_distinct_titles_on_the_same_page_both_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = self._fresh_actors_db(tmp)
            with dbmod.connect(actors_db) as db:
                _upsert_career_event(db, actor_id, "Process Engineer -- Ultrafast Laser", "https://example.test/careers/")
                _upsert_career_event(db, actor_id, "Photonics Engineer -- R&D", "https://example.test/careers/")
            count = dbmod.scalar(actors_db, "SELECT COUNT(*) FROM actor_events WHERE actor_id=?", (actor_id,))
            self.assertEqual(2, count)

    def test_repeated_upsert_of_the_same_title_and_url_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, actor_id = self._fresh_actors_db(tmp)
            with dbmod.connect(actors_db) as db:
                first = _upsert_career_event(db, actor_id, "Process Engineer -- Ultrafast Laser", "https://example.test/careers/")
                second = _upsert_career_event(db, actor_id, "Process Engineer -- Ultrafast Laser", "https://example.test/careers/")
            self.assertEqual(1, first)
            self.assertEqual(0, second)


if __name__ == "__main__":
    unittest.main()
