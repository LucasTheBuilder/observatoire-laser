"""Tests pour la collecte presse (chantier 3) : press.collect_press_mentions().

httpx.Client est remplacé par un double rejouant un flux RSS canned -- jamais d'appel réseau
réel ici. La forme du flux (title/link/description/pubDate) est celle vérifiée manuellement
contre les deux flux réels (Laser Focus World, Photonics Spectra) avant d'écrire ce module.
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
import press
from press import (
    _contains_whole_word,
    _is_near_duplicate,
    _normalize,
    _parse_feed,
    _word_shingles,
    classify_press_event,
    collect_press_mentions,
)

SAMPLE_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Sample Feed</title>
<item>
  <title>ALPHANOV unveils new femtosecond laser platform</title>
  <link>https://example.test/news/alphanov-platform</link>
  <description>&lt;p&gt;French laser specialist ALPHANOV announced a new production line.&lt;/p&gt;</description>
  <pubDate>24 Aug 2026 04:00:00 GMT</pubDate>
</item>
<item>
  <title>Generic quantum computing news</title>
  <link>https://example.test/news/quantum</link>
  <description>No mention of any laser micromachining company here.</description>
  <pubDate>20 Aug 2026 04:00:00 GMT</pubDate>
</item>
<item>
  <title>Missing link item is skipped</title>
  <description>ALPHANOV mentioned again but no link tag.</description>
  <pubDate>19 Aug 2026 04:00:00 GMT</pubDate>
</item>
</channel></rss>"""


REWORDED_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Sample Feed 2</title>
<item>
  <title>ALPHANOV unveils new femtosecond laser production platform</title>
  <link>https://otheroutlet.test/wire/alphanov-launch</link>
  <description>French photonics firm ALPHANOV has announced a new production line for lasers.</description>
  <pubDate>24 Aug 2026 09:00:00 GMT</pubDate>
</item>
</channel></rss>"""


DISTINCT_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Sample Feed 3</title>
<item>
  <title>ALPHANOV appoints new chief financial officer</title>
  <link>https://otheroutlet.test/wire/alphanov-cfo</link>
  <description>ALPHANOV has named a new CFO to lead its finance team going forward.</description>
  <pubDate>24 Aug 2026 09:00:00 GMT</pubDate>
</item>
</channel></rss>"""


SIGNAL_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Sample Feed</title>
<item>
  <title>ALPHANOV files new patent for femtosecond beam shaping</title>
  <link>https://example.test/news/alphanov-patent</link>
  <description>ALPHANOV announced a new patent covering its beam shaping process.</description>
  <pubDate>24 Aug 2026 04:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class ClassifyPressEventTests(unittest.TestCase):
    def test_patent_keyword_is_classified_as_patent(self):
        self.assertEqual("patent", classify_press_event("Company files new patent", ""))
        self.assertEqual("patent", classify_press_event("Une société dépose un brevet", ""))

    def test_investment_keyword_is_classified_as_investment(self):
        self.assertEqual("investment", classify_press_event("Startup closes Series B funding round", ""))
        self.assertEqual("investment", classify_press_event("Une PME annonce une levée de fonds", ""))

    def test_recruitment_keyword_is_classified_as_recruitment(self):
        self.assertEqual("recruitment", classify_press_event("Company is hiring laser engineers", ""))
        self.assertEqual("recruitment", classify_press_event("La société recrute un ingénieur laser", ""))

    def test_no_keyword_falls_back_to_press_mention(self):
        self.assertEqual("press_mention", classify_press_event("Company unveils new laser platform", ""))

    def test_patent_and_investment_are_checked_before_the_frequent_recruitment_false_positive(self):
        # A patent announcement that also happens to mention hiring in passing must still be
        # typed as the rarer, more decision-relevant 'patent' signal -- not swallowed by the
        # much more common 'recruitment' false positive (see SIGNAL_KEYWORDS's ordering note).
        text = "Company files new patent, and by the way we are hiring too"
        self.assertEqual("patent", classify_press_event(text, ""))


class NormalizationTests(unittest.TestCase):
    def test_whole_word_matching(self):
        haystack = _normalize("ALPHANOV unveils new platform")
        self.assertTrue(_contains_whole_word(haystack, _normalize("ALPHANOV")))
        self.assertFalse(_contains_whole_word(haystack, _normalize("PHANOV")))

    def test_metalphanova_is_not_a_false_match(self):
        haystack = _normalize("METALPHANOVA GmbH raises funding")
        self.assertFalse(_contains_whole_word(haystack, _normalize("ALPHANOV")))


class ParseFeedTests(unittest.TestCase):
    def test_parses_title_link_description_and_date(self):
        items = _parse_feed(SAMPLE_FEED)
        self.assertEqual(2, len(items))  # third item has no <link>, skipped
        first = items[0]
        self.assertEqual("ALPHANOV unveils new femtosecond laser platform", first["title"])
        self.assertEqual("https://example.test/news/alphanov-platform", first["link"])
        self.assertIn("production line", first["description"])
        self.assertNotIn("<p>", first["description"])
        self.assertEqual("2026-08-24", first["event_date"])

    def test_malformed_xml_returns_empty_list(self):
        self.assertEqual([], _parse_feed(b"not xml at all"))


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        return None


class FakeClient:
    feed_content: bytes = SAMPLE_FEED
    fail_urls: set[str] = set()
    # Optional per-URL override, keyed by feed URL, for tests that need two feeds to serve
    # different content -- falls back to feed_content when a URL has no entry.
    feed_content_by_url: dict[str, bytes] = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        if url in self.fail_urls:
            raise RuntimeError("network error")
        return FakeResponse(self.feed_content_by_url.get(url, self.feed_content))


class CollectPressMentionsTests(unittest.TestCase):
    def setUp(self):
        # feed_content_by_url is a mutable class attribute on FakeClient -- reset before every
        # test so a per-URL override set by one test can never leak into the next.
        FakeClient.feed_content_by_url = {}

    def _seed_actors(self, actors_db: Path, names: list[str]) -> None:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
        for name in names:
            dbmod.create_actor(name, "France", "Test", f"https://{name.lower().replace(' ', '')}.example/", priority=False)

    def test_matches_are_recorded_as_pending_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV", "Some Other Actor"])

                FakeClient.feed_content = SAMPLE_FEED
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_press_mentions()

                self.assertEqual(2, result["feeds_ok"])
                self.assertEqual(0, result["errors"])
                # Both feeds serve identical canned content in this test, so the second feed's
                # article hits the same (actor_id, source_url) dedup key as the first and is
                # correctly skipped -- real feeds would have distinct URLs and both would count.
                self.assertEqual(1, result["events_added"])

                with dbmod.connect(actors_db) as db:
                    events = list(db.execute("SELECT actor_id,description,event_date,source_url,review_status FROM actor_events"))
                self.assertEqual(1, len(events))
                for event in events:
                    self.assertEqual("pending", event["review_status"])
                    self.assertEqual("2026-08-24", event["event_date"])
                    self.assertIn("ALPHANOV", event["description"])

    def test_second_run_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                FakeClient.feed_content = SAMPLE_FEED
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    first = collect_press_mentions()
                    second = collect_press_mentions()
                self.assertGreater(first["events_added"], 0)
                self.assertEqual(0, second["events_added"])

    def test_short_generic_actor_name_is_never_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["HEF"])
                FakeClient.feed_content = SAMPLE_FEED
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_press_mentions()
                self.assertEqual(0, result["events_added"])

    def test_signal_feed_is_stored_with_its_classified_event_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                FakeClient.feed_content = SIGNAL_FEED
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_press_mentions()
                self.assertEqual(1, result["events_added"])
                with dbmod.connect(actors_db) as db:
                    event = db.execute("SELECT event_type,description FROM actor_events").fetchone()
                self.assertEqual("patent", event["event_type"])
                self.assertTrue(event["description"].startswith("Brevet ("))

    def test_one_feed_failing_does_not_block_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                FakeClient.feed_content = SAMPLE_FEED
                FakeClient.fail_urls = {list(press.FEED_URLS.values())[0]}
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_press_mentions()
                self.assertEqual(1, result["feeds_ok"])
                self.assertEqual(1, result["errors"])
                self.assertEqual(1, result["events_added"])


class NearDuplicateTests(unittest.TestCase):
    def test_reworded_title_is_a_near_duplicate(self):
        original = "ALPHANOV unveils new femtosecond laser platform"
        reworded = "ALPHANOV unveils new femtosecond laser production platform"
        self.assertTrue(_is_near_duplicate(reworded, [f"Mention presse (Outlet) : {original}"]))

    def test_unrelated_article_about_the_same_actor_is_not_a_near_duplicate(self):
        existing = "Mention presse (Outlet) : ALPHANOV appoints new chief financial officer"
        new_title = "ALPHANOV unveils new femtosecond laser platform"
        self.assertFalse(_is_near_duplicate(new_title, [existing]))

    def test_empty_title_is_never_a_duplicate(self):
        self.assertFalse(_is_near_duplicate("", ["Mention presse (Outlet) : Something"]))

    def test_no_existing_events_is_never_a_duplicate(self):
        self.assertFalse(_is_near_duplicate("ALPHANOV unveils new femtosecond laser platform", []))

    def test_shingle_size_falls_back_to_whole_title_for_short_titles(self):
        # Fewer words than the shingle size (3) must not crash or silently match everything.
        shingles = _word_shingles("ALPHANOV wins award")
        self.assertEqual(1, len(shingles))


class CollectPressMentionsNearDuplicateTests(unittest.TestCase):
    def setUp(self):
        FakeClient.feed_content_by_url = {}

    def _seed_actors(self, actors_db: Path, names: list[str]) -> None:
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
        for name in names:
            dbmod.create_actor(name, "France", "Test", f"https://{name.lower().replace(' ', '')}.example/", priority=False)

    def test_same_story_reworded_by_a_second_outlet_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                feed_urls = list(press.FEED_URLS.values())
                FakeClient.feed_content_by_url = {feed_urls[0]: SAMPLE_FEED, feed_urls[1]: REWORDED_FEED}
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_press_mentions()

                # Two distinct source_urls (real duplicate would be caught by exact dedup
                # already) carrying near-identical titles about the same actor: only the first
                # is kept, the second is recognized as the same underlying story.
                self.assertEqual(1, result["events_added"])
                self.assertGreaterEqual(result["near_duplicates_skipped"], 1)
                with dbmod.connect(actors_db) as db:
                    count = db.execute("SELECT COUNT(*) FROM actor_events").fetchone()[0]
                self.assertEqual(1, count)

    def test_two_genuinely_different_stories_are_both_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
                patch.object(press, "ACTORS_DB", actors_db),
            ):
                dbmod.init_databases()
                self._seed_actors(actors_db, ["ALPHANOV"])
                feed_urls = list(press.FEED_URLS.values())
                FakeClient.feed_content_by_url = {feed_urls[0]: SAMPLE_FEED, feed_urls[1]: DISTINCT_FEED}
                FakeClient.fail_urls = set()
                with patch.object(press.httpx, "Client", FakeClient):
                    result = collect_press_mentions()

                self.assertEqual(2, result["events_added"])
                self.assertEqual(0, result["near_duplicates_skipped"])


if __name__ == "__main__":
    unittest.main()
