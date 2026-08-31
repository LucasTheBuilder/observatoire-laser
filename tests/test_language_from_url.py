"""Tests pour language_from_url (Lot 2 §2.6, audit veille §10.6, 30/08/2026) : la fonction ne
lisait que les segments /en/, /de/ -- language était NULL pour 69% des faits manuellement
échantillonnés, notamment les sites monolingues ou à sous-domaine (ex: en.example.com).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import language_from_url


class LanguageFromUrlPathTests(unittest.TestCase):
    def test_known_path_segments_are_detected(self):
        cases = {
            "https://example.test/fr/applications": "fr",
            "https://example.test/en/applications": "en",
            "https://example.test/de/anwendungen": "de",
            "https://example.test/es/aplicaciones": "es",
            "https://example.test/it/applicazioni": "it",
            "https://example.test/nl/toepassingen": "nl",
            "https://example.test/lt/pritaikymas": "lt",
            "https://example.test/cs/aplikace": "cs",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(expected, language_from_url(url))

    def test_no_path_segment_returns_none(self):
        self.assertIsNone(language_from_url("https://example.test/applications"))

    def test_unrecognized_first_segment_returns_none(self):
        self.assertIsNone(language_from_url("https://example.test/products/laser"))


class LanguageFromUrlSubdomainTests(unittest.TestCase):
    def test_language_subdomain_is_detected(self):
        cases = {
            "https://en.example.test/products": "en",
            "https://de.example.test/produkte": "de",
            "https://fr.example.test/produits": "fr",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(expected, language_from_url(url))

    def test_non_language_subdomain_returns_none(self):
        # These must NOT be mistaken for a language code -- the exact-match lookup against
        # _LANGUAGE_URL_CODES already excludes them without a separate exclusion list.
        for subdomain in ("www", "shop", "docs", "blog", "m"):
            with self.subTest(subdomain=subdomain):
                self.assertIsNone(language_from_url(f"https://{subdomain}.example.test/products"))

    def test_bare_domain_without_subdomain_returns_none(self):
        self.assertIsNone(language_from_url("https://example.test/products"))

    def test_path_segment_takes_priority_over_subdomain(self):
        # A path segment is a more direct signal than the host -- if both are present and
        # disagree, the path wins.
        self.assertEqual("de", language_from_url("https://en.example.test/de/produkte"))


class LanguageFromUrlEdgeCaseTests(unittest.TestCase):
    def test_none_and_empty_url_return_none(self):
        self.assertIsNone(language_from_url(None))
        self.assertIsNone(language_from_url(""))

    def test_case_insensitive(self):
        self.assertEqual("fr", language_from_url("https://example.test/FR/applications"))
        self.assertEqual("en", language_from_url("https://EN.example.test/products"))


if __name__ == "__main__":
    unittest.main()
