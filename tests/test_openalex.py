"""Tests pour l'intégration OpenAlex (chantier 3) : openalex.collect_openalex_publications().

httpx.Client est remplacé par un double rejouant des réponses canned -- jamais d'appel réseau
réel ici. Les formes de réponse (institutions/works) sont celles vérifiées manuellement contre
la vraie API api.openalex.org avant d'écrire ce module.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
import openalex
from openalex import (
    _co_institutions,
    _domain,
    _find_institution,
    _parse_work,
    _upsert_document,
    _work_is_on_topic,
    collect_openalex_publications,
)


class WorkIsOnTopicTests(unittest.TestCase):
    def test_femtosecond_title_is_on_topic(self):
        self.assertTrue(_work_is_on_topic("Femtosecond laser micromachining of fused silica"))

    def test_named_process_without_laser_wording_is_on_topic(self):
        self.assertTrue(_work_is_on_topic("Selective laser etching (SLE) of photonic glass components"))

    def test_unrelated_title_is_off_topic(self):
        self.assertFalse(_work_is_on_topic("Effects of graded dietary levels of microalgae on poultry growth"))

    def test_generic_process_axis_without_laser_wording_is_off_topic(self):
        # Regression: same real false positive found in cordis.py's fixtures -- a title
        # matching "Monitoring IA procédé" via "digital twin" alone, no laser mention.
        self.assertFalse(_work_is_on_topic(
            "AI-Enriched Safety Criteria Catalogue and Digital Twin Framework for Predictive Safety and Maintenance"
        ))


class DomainHelperTests(unittest.TestCase):
    def test_strips_scheme_and_www(self):
        self.assertEqual("alphanov.com", _domain("https://www.alphanov.com/en/"))
        self.assertEqual("ilt.fraunhofer.de", _domain("http://www.ilt.fraunhofer.de/en.html"))

    def test_bare_host_without_scheme(self):
        self.assertEqual("example.com", _domain("example.com"))


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    # keyed by request path ("/institutions" or "/works"); value is a function(params) -> dict
    handlers: dict[str, object] = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None, **kwargs):
        path = "/institutions" if url.endswith("/institutions") else "/works"
        handler = self.handlers.get(path)
        return FakeResponse(handler(params or {}) if handler else {"results": []})


class FindInstitutionTests(unittest.TestCase):
    def test_accepts_the_single_domain_matching_candidate(self):
        FakeClient.handlers = {
            "/institutions": lambda params: {"results": [
                {"id": "https://openalex.org/I1", "display_name": "ALPhANOV (France)", "homepage_url": "https://www.alphanov.com/"},
                {"id": "https://openalex.org/I2", "display_name": "Some Other Alphanov-ish Org", "homepage_url": "https://unrelated.example/"},
            ]}
        }
        with patch.object(openalex.httpx, "Client", FakeClient):
            with FakeClient() as client:
                result = _find_institution(client, "ALPHANOV", "https://www.alphanov.com")
        self.assertIsNotNone(result)
        self.assertEqual("https://openalex.org/I1", result["id"])

    def test_zero_domain_matches_returns_none(self):
        FakeClient.handlers = {
            "/institutions": lambda params: {"results": [
                {"id": "https://openalex.org/I2", "display_name": "Unrelated", "homepage_url": "https://unrelated.example/"},
            ]}
        }
        with FakeClient() as client:
            self.assertIsNone(_find_institution(client, "ALPHANOV", "https://www.alphanov.com"))

    def test_ambiguous_multiple_domain_matches_returns_none(self):
        # Same normalized domain returned twice (e.g. duplicate/near-duplicate OpenAlex records)
        # must not be resolved arbitrarily.
        FakeClient.handlers = {
            "/institutions": lambda params: {"results": [
                {"id": "https://openalex.org/I1", "display_name": "CEIT", "homepage_url": "https://www.ceit.es/en/"},
                {"id": "https://openalex.org/I1b", "display_name": "CEIT (dup)", "homepage_url": "http://ceit.es/"},
            ]}
        }
        with FakeClient() as client:
            self.assertIsNone(_find_institution(client, "CEIT", "https://www.ceit.es/en/"))

    def test_no_official_url_returns_none_without_a_request(self):
        with FakeClient() as client:
            self.assertIsNone(_find_institution(client, "Example", ""))

    def test_falls_back_to_the_domain_stem_when_the_display_name_finds_nothing(self):
        """Cas réel (10/09/2026) : « Sirris Laser Innovation Lab » ne renvoie RIEN sur
        /institutions, alors qu'OpenAlex connaît Sirris et ses 997 travaux. Le nom stocké est
        un nom d'usage, pas un nom d'institution."""
        interrogations: list[str] = []

        def institutions(params):
            interrogations.append(params["search"])
            if params["search"] != "sirris":
                return {"results": []}
            return {"results": [
                {"id": "https://openalex.org/I3", "display_name": "Sirris", "homepage_url": "https://www.sirris.be/"},
            ]}

        FakeClient.handlers = {"/institutions": institutions}
        with FakeClient() as client:
            result = _find_institution(client, "Sirris Laser Innovation Lab", "https://www.sirris.be/")
        self.assertIsNotNone(result)
        self.assertEqual("https://openalex.org/I3", result["id"])
        # Le nom complet reste essayé EN PREMIER : élargir n'est pas remplacer.
        self.assertEqual("Sirris Laser Innovation Lab", interrogations[0])

    def test_widening_the_search_never_widens_the_proof(self):
        """La formulation change, la preuve d'identité non : une fiche au mauvais domaine
        reste refusée, quel que soit le terme qui l'a trouvée."""
        FakeClient.handlers = {
            "/institutions": lambda params: {"results": [
                {"id": "https://openalex.org/I9", "display_name": "Femtika UAB", "homepage_url": "https://vu.lt/"},
            ]}
        }
        with FakeClient() as client:
            self.assertIsNone(_find_institution(client, "Femtika", "https://femtika.com"))

    def test_two_country_entities_sharing_a_domain_stay_ambiguous(self):
        """TRUMPF (Germany) et TRUMPF (United Kingdom) publient tous deux sous trumpf.com.
        Élargir la recherche ne doit pas donner l'occasion d'en choisir un au hasard."""
        FakeClient.handlers = {
            "/institutions": lambda params: {"results": [
                {"id": "https://openalex.org/I4", "display_name": "TRUMPF (Germany)", "homepage_url": "https://www.trumpf.com/"},
                {"id": "https://openalex.org/I5", "display_name": "TRUMPF (United Kingdom)", "homepage_url": "https://www.trumpf.com/en_GB/"},
            ]}
        }
        with FakeClient() as client:
            self.assertIsNone(_find_institution(client, "TRUMPF", "https://www.trumpf.com"))


class SearchTermsTests(unittest.TestCase):
    def test_full_name_first_then_domain_stem(self):
        terms = openalex._search_terms("AIMEN Technology Centre", "https://www.aimen.es")
        self.assertEqual("AIMEN Technology Centre", terms[0])
        self.assertIn("aimen", terms)

    def test_no_duplicate_terms(self):
        # « Sirris » comme nom et « sirris » comme radical ne font qu'une interrogation.
        terms = openalex._search_terms("Sirris", "https://www.sirris.be/")
        self.assertEqual(len(terms), len({term.casefold() for term in terms}))

    def test_punctuation_is_not_a_search_term(self):
        terms = openalex._search_terms("Manufacturing Technology Centre (MTC)", "https://www.the-mtc.org/")
        self.assertIn("Manufacturing Technology", terms)
        self.assertNotIn("(", "".join(terms[1:]))


class FetchRecentWorksTests(unittest.TestCase):
    """Le plafond de 25 travaux les plus récents, tous sujets confondus, rendait les gros
    instituts invisibles : mesuré le 10/09/2026, aucune des 3 publications laser de CEIT ni
    des 3 de Fraunhofer ILT n'était dans les 25 premières de leur année."""

    def _pages(self, total: int, page_size: int = openalex.OPENALEX_WORKS_PER_PAGE):
        pages = [
            [{"id": f"W{i}"} for i in range(start, min(start + page_size, total))]
            for start in range(0, total, page_size)
        ]
        state = {"page": 0}

        def works(params):
            index = state["page"]
            state["page"] += 1
            if index >= len(pages):
                return {"results": [], "meta": {"next_cursor": None}}
            cursor = f"c{index + 1}" if index + 1 < len(pages) else None
            return {"results": pages[index], "meta": {"next_cursor": cursor}}

        return works

    def test_pagination_reads_past_the_first_page(self):
        FakeClient.handlers = {"/works": self._pages(176)}
        with FakeClient() as client:
            works = openalex._fetch_recent_works(client, "I1", "2025-01-01")
        self.assertEqual(176, len(works))

    def test_a_safety_cap_still_bounds_an_outlier(self):
        FakeClient.handlers = {"/works": self._pages(5000)}
        with FakeClient() as client:
            works = openalex._fetch_recent_works(client, "I1", "2025-01-01")
        self.assertEqual(openalex.OPENALEX_WORKS_PER_ACTOR, len(works))

    def test_a_failing_page_keeps_what_was_already_read(self):
        """Perdre la suite vaut mieux que perdre le début : l'ancienne version renvoyait [] sur
        la moindre erreur, ce qui effaçait aussi les pages déjà lues."""
        state = {"calls": 0}

        def works(params):
            state["calls"] += 1
            if state["calls"] > 1:
                raise RuntimeError("502 depuis OpenAlex")
            return {"results": [{"id": "W1"}], "meta": {"next_cursor": "c1"}}

        FakeClient.handlers = {"/works": works}
        with FakeClient() as client:
            self.assertEqual(1, len(openalex._fetch_recent_works(client, "I1", "2025-01-01")))


class ParseWorkTests(unittest.TestCase):
    def test_strips_doi_prefix_and_uses_landing_page(self):
        work = {
            "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/xyz",
            "title": "A femtosecond laser paper", "publication_date": "2026-01-15",
            "primary_location": {"landing_page_url": "https://doi.org/10.1000/xyz"},
        }
        item = _parse_work(work)
        self.assertEqual("10.1000/xyz", item["doi"])
        self.assertEqual("https://doi.org/10.1000/xyz", item["url"])
        self.assertEqual("2026-01-15", item["published_at"])

    def test_missing_title_is_rejected(self):
        self.assertIsNone(_parse_work({"id": "https://openalex.org/W1"}))

    def test_falls_back_to_doi_url_then_openalex_id(self):
        no_landing = _parse_work({"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x", "title": "T"})
        self.assertEqual("https://doi.org/10.1/x", no_landing["url"])
        no_doi_no_landing = _parse_work({"id": "https://openalex.org/W1", "title": "T"})
        self.assertEqual("https://openalex.org/W1", no_doi_no_landing["url"])


class CoInstitutionsTests(unittest.TestCase):
    def test_co_institutions_excludes_the_searched_institution(self):
        work = {"authorships": [
            {"institutions": [{"id": "https://openalex.org/I1", "display_name": "ALPhANOV"}]},
            {"institutions": [{"id": "https://openalex.org/I2", "display_name": "Fraunhofer ILT", "country_code": "DE"}]},
        ]}
        names = _co_institutions(work, "I1")
        self.assertEqual([("Fraunhofer ILT", "DE")], names)

    def test_no_authorships_returns_empty_list(self):
        self.assertEqual([], _co_institutions({}, "I1"))

    def test_institution_without_display_name_is_skipped(self):
        work = {"authorships": [{"institutions": [{"id": "https://openalex.org/I2", "display_name": ""}]}]}
        self.assertEqual([], _co_institutions(work, "I1"))

    def test_institution_without_country_code_returns_none(self):
        work = {"authorships": [{"institutions": [{"id": "https://openalex.org/I2", "display_name": "Fraunhofer ILT"}]}]}
        self.assertEqual([("Fraunhofer ILT", None)], _co_institutions(work, "I1"))

    def test_multiple_distinct_co_institutions_are_all_returned(self):
        work = {"authorships": [
            {"institutions": [{"id": "https://openalex.org/I1", "display_name": "ALPhANOV"}]},
            {"institutions": [{"id": "https://openalex.org/I2", "display_name": "Fraunhofer ILT", "country_code": "DE"}]},
            {"institutions": [{"id": "https://openalex.org/I3", "display_name": "Tekniker", "country_code": "ES"}]},
        ]}
        self.assertEqual([("Fraunhofer ILT", "DE"), ("Tekniker", "ES")], _co_institutions(work, "I1"))


class UpsertDocumentTests(unittest.TestCase):
    def _fresh_tech_db(self, tmp: str) -> Path:
        tech_db = Path(tmp) / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", tech_db),
        ):
            dbmod.init_databases()
        return tech_db

    def test_new_document_is_inserted_and_attributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = self._fresh_tech_db(tmp)
            item = {"title": "T", "url": "https://doi.org/10.1/x", "doi": "10.1/x", "published_at": "2026-01-01"}
            with dbmod.connect(tech_db) as db:
                inserted, attributed = _upsert_document(db, "ALPHANOV", item)
                row = db.execute("SELECT actor_name FROM documents WHERE fingerprint IS NOT NULL").fetchone()
            self.assertEqual((1, 0), (inserted, attributed))
            self.assertEqual("ALPHANOV", row["actor_name"])

    def test_existing_unattributed_document_gets_attributed_not_duplicated(self):
        # Simulates a document already found by the generic, unattributed scrape_technology().
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = self._fresh_tech_db(tmp)
            item = {"title": "T", "url": "https://doi.org/10.1/x", "doi": "10.1/x", "published_at": "2026-01-01"}
            fingerprint = hashlib.sha256(item["doi"].casefold().encode()).hexdigest()
            with dbmod.connect(tech_db) as db:
                db.execute(
                    """INSERT INTO documents(actor_name,document_type,title,source_url,published_at,doi,fingerprint,created_at,last_seen_at)
                       VALUES(NULL,'publication',?,?,?,?,?,?,?)""",
                    (item["title"], item["url"], item["published_at"], item["doi"], fingerprint, "2025-01-01", "2025-01-01"),
                )
            with dbmod.connect(tech_db) as db:
                inserted, attributed = _upsert_document(db, "ALPHANOV", item)
                count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                row = db.execute("SELECT actor_name FROM documents").fetchone()
            self.assertEqual((0, 1), (inserted, attributed))
            self.assertEqual(1, count)  # no duplicate row
            self.assertEqual("ALPHANOV", row["actor_name"])

    def test_already_attributed_document_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = self._fresh_tech_db(tmp)
            item = {"title": "T", "url": "https://doi.org/10.1/x", "doi": "10.1/x", "published_at": "2026-01-01"}
            with dbmod.connect(tech_db) as db:
                _upsert_document(db, "ALPHANOV", item)
                inserted, attributed = _upsert_document(db, "MANUTECH USD", item)
                row = db.execute("SELECT actor_name FROM documents").fetchone()
            self.assertEqual((0, 0), (inserted, attributed))
            self.assertEqual("ALPHANOV", row["actor_name"])  # never overwritten


class CollectOpenAlexTests(unittest.TestCase):
    def test_end_to_end_matches_and_stores_publications(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            tech_db = Path(tmp) / "technology.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", tech_db),
                patch.object(openalex, "ACTORS_DB", actors_db),
                patch.object(openalex, "TECH_DB", tech_db),
            ):
                dbmod.init_databases()
                with dbmod.connect(actors_db) as db:
                    db.execute("DELETE FROM actors")
                dbmod.create_actor("ALPHANOV", "France", "Test", "https://www.alphanov.com", priority=False)
                dbmod.create_actor("Unmatched Actor", "France", "Test", "https://unmatched.example/", priority=False)

                def institutions_handler(params):
                    if "ALPHANOV" in params.get("search", ""):
                        return {"results": [{"id": "https://openalex.org/I1", "display_name": "ALPhANOV", "homepage_url": "https://www.alphanov.com/"}]}
                    return {"results": []}

                def works_handler(params):
                    return {"results": [
                        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a", "title": "Femtosecond laser micromachining of fused silica", "type": "article", "publication_date": "2026-01-01",
                         "primary_location": {"landing_page_url": "https://doi.org/10.1/a"},
                         # §4.D audit veille (Lot 3 §3.2): a co-authoring institution on an
                         # on-topic work must surface as an actor_candidates row.
                         "authorships": [
                             {"institutions": [{"id": "https://openalex.org/I1", "display_name": "ALPhANOV"}]},
                             {"institutions": [{"id": "https://openalex.org/I9", "display_name": "New Photonics Lab"}]},
                         ]},
                        # Off-topic: same institution, but nothing to do with lasers -- must be
                        # counted and skipped, not stored (audit v8 §2.1). Its co-institution
                        # must NOT surface as a candidate either -- off-topic works are skipped
                        # before candidate extraction runs.
                        {"id": "https://openalex.org/W2", "doi": None, "title": "Effects of graded dietary levels of microalgae on poultry growth", "type": "article", "publication_date": "2026-02-01",
                         "primary_location": {},
                         "authorships": [{"institutions": [{"id": "https://openalex.org/I8", "display_name": "Off Topic Institute"}]}]},
                        # Lexicalement irréprochable, et pourtant pas une publication : un
                        # erratum de Tekniker, un dépôt de données d'IREPA et les
                        # « Supplementary Data » d'un article déjà en base sont entrés ainsi le
                        # 10/09/2026, la dernière en doublon de son propre article.
                        {"id": "https://openalex.org/W3", "doi": "https://doi.org/10.1/c", "title": "Supplementary Data for Paper \"Femtosecond laser drilling of glass\"", "type": "supplementary-materials", "publication_date": "2026-03-01",
                         "primary_location": {"landing_page_url": "https://doi.org/10.1/c"},
                         "authorships": [{"institutions": [{"id": "https://openalex.org/I1", "display_name": "ALPhANOV"}]}]},
                    ]}

                FakeClient.handlers = {"/institutions": institutions_handler, "/works": works_handler}
                with patch.object(openalex.httpx, "Client", FakeClient):
                    result = collect_openalex_publications()

                self.assertEqual(1, result["actors_matched"])
                self.assertEqual(1, result["documents_added"])
                # Le hors-sujet et le mauvais type se comptent ensemble : deux écartés.
                self.assertEqual(2, result["documents_off_topic"])
                self.assertEqual(1, result["actor_candidates_added"])
                self.assertEqual(0, result["errors"])

                with dbmod.connect(tech_db) as db:
                    docs = list(db.execute("SELECT actor_name,title,doi FROM documents ORDER BY title"))
                self.assertEqual(1, len(docs))
                self.assertEqual("Femtosecond laser micromachining of fused silica", docs[0]["title"])
                self.assertTrue(all(d["actor_name"] == "ALPHANOV" for d in docs))

                with dbmod.connect(actors_db) as db:
                    candidates = list(db.execute("SELECT name FROM actor_candidates"))
                self.assertEqual(["New Photonics Lab"], [c["name"] for c in candidates])


if __name__ == "__main__":
    unittest.main()
