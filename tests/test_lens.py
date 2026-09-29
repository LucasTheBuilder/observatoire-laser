"""Tests pour le connecteur brevets Lens.org (lens.py).

`_fetch_actor_patents` (la seule fonction qui parle réellement au réseau) est toujours patchée
ici -- jamais d'appel réseau réel. Les formes de réponse ci-dessous (champ "kind", pas
"kind_symbol" ; "extracted_name" en objet {"value": ...}) reproduisent ce qui a été observé le
28/09/2026 contre un vrai compte Lens (voir lens.py docstring) -- ce ne sont plus des suppositions.
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
import lens
from lens import _parse_result, _upsert_lens_patent, collect_lens_patents


def _lens_document(
    *, jurisdiction="EP", doc_number="7654321", kind="A1", date_published="2025-03-20",
    title_en="Method for femtosecond laser drilling", applicants=("TESTLASER SARL",),
    lens_id="123-456-789-012-345",
    abstract_en="A femtosecond laser drills micro-holes in a stainless steel fuel injector nozzle.",
) -> dict:
    return {
        "lens_id": lens_id,
        "jurisdiction": jurisdiction,
        "doc_number": doc_number,
        "kind": kind,
        "date_published": date_published,
        "abstract": [{"lang": "en", "text": abstract_en}] if abstract_en else [],
        "biblio": {
            "invention_title": [
                {"lang": "en", "text": title_en},
                {"lang": "de", "text": "Verfahren zum Laserbohren"},
            ],
            "parties": {
                "applicants": [{"extracted_name": {"value": name}} for name in applicants],
            },
        },
    }


def _search_response(*documents: dict) -> dict:
    return {"total": len(documents), "data": list(documents)}


class ParseResultTests(unittest.TestCase):
    def test_parses_publication_number_title_date_and_applicants(self):
        doc = _parse_result(_lens_document())
        self.assertEqual(doc["patent_number"], "EP7654321A1")
        self.assertEqual(doc["title"], "Method for femtosecond laser drilling")
        self.assertEqual(doc["published_at"], "2025-03-20")
        self.assertEqual(doc["applicants"], ["TESTLASER SARL"])
        self.assertIn("lens.org/lens/patent/123-456-789-012-345", doc["url"])

    def test_the_abstract_is_kept_english_first(self):
        raw = _lens_document(abstract_en="")
        raw["abstract"] = [
            {"lang": "de", "text": "Ein Femtosekundenlaser bohrt Löcher."},
            {"lang": "en", "text": "A femtosecond laser drills holes."},
        ]
        self.assertEqual("A femtosecond laser drills holes.", _parse_result(raw)["abstract"])

    def test_a_patent_without_abstract_parses_to_an_empty_one(self):
        self.assertEqual("", _parse_result(_lens_document(abstract_en=""))["abstract"])

    def test_falls_back_to_any_title_when_no_english_one(self):
        raw = _lens_document()
        raw["biblio"]["invention_title"] = [{"lang": "fr", "text": "Procédé de perçage laser"}]
        doc = _parse_result(raw)
        self.assertEqual(doc["title"], "Procédé de perçage laser")

    def test_multiple_applicants_are_all_captured(self):
        doc = _parse_result(_lens_document(applicants=("TESTLASER SARL", "SOME UNTRACKED LAB")))
        self.assertEqual(doc["applicants"], ["TESTLASER SARL", "SOME UNTRACKED LAB"])

    def test_missing_doc_number_returns_none(self):
        raw = _lens_document()
        raw["doc_number"] = ""
        self.assertIsNone(_parse_result(raw))

    def test_missing_lens_id_falls_back_to_search_link(self):
        raw = _lens_document(lens_id="")
        doc = _parse_result(raw)
        self.assertIn("doc_number:7654321", doc["url"])

    def test_not_a_dict_returns_none(self):
        self.assertIsNone(_parse_result([]))  # type: ignore[arg-type]

    def test_applicant_name_as_plain_string_is_still_accepted(self):
        # Repli défensif (jamais observé en pratique) : extracted_name en chaîne nue plutôt
        # qu'en objet {"value": ...}.
        raw = _lens_document(applicants=())
        raw["biblio"]["parties"]["applicants"] = [{"extracted_name": "PLAIN STRING SARL"}]
        doc = _parse_result(raw)
        self.assertEqual(doc["applicants"], ["PLAIN STRING SARL"])


class CollectLensPatentsTests(unittest.TestCase):
    """La collecte du 29/09/2026 : une requête par acteur suivi, et seul ce qu'un acteur suivi
    signe ET qui relève de l'usinage ultra-rapide entre dans le corpus -- classé, avec preuve."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        actors_db, market_db, tech_db = tmp_path / "actors.db", tmp_path / "market.db", tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", actors_db), patch.object(dbmod, "MARKET_DB", market_db), patch.object(dbmod, "TECH_DB", tech_db):
            dbmod.init_databases()
        self.actors_db = actors_db
        self.tech_db = tech_db
        for cible, valeur in (
            ("ACTORS_DB", actors_db), ("TECH_DB", tech_db), ("LENS_API_KEY", "k"),
            ("REQUEST_INTERVAL_SECONDS", 0),
        ):
            patcher = patch.object(lens, cible, valeur)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmpdir.cleanup)
        with dbmod.connect(actors_db) as db:
            # init_databases() sème le roster réel : on le met de côté, sans quoi chaque test
            # interrogerait (et compterait) une quarantaine d'acteurs.
            db.execute("UPDATE actors SET active=0")
            db.execute(
                "INSERT INTO actors(name,country,role,priority,official_url,updated_at,active) VALUES(?,?,?,?,?,?,1)",
                ("TESTLASER SARL", "France", "Centre technologique", 1, "https://www.testlaser.example", dbmod.utc_now()),
            )
        self.requetes: list[tuple[str, int]] = []

    def _collecte(self, *documents: dict, total: int | None = None) -> dict:
        def fetch(client, actor_name, offset=0):
            self.requetes.append((actor_name, offset))
            reponse = _search_response(*documents)
            if total is not None:
                reponse["total"] = total
            return reponse
        with patch.object(lens, "_fetch_actor_patents", fetch):
            return collect_lens_patents()

    def _documents(self):
        with dbmod.connect(self.tech_db) as db:
            return db.execute("SELECT actor_name,document_type,patent_number,source_url,abstract FROM documents").fetchall()

    def test_returns_not_configured_without_credentials(self):
        with patch.object(lens, "LENS_API_KEY", ""):
            result = collect_lens_patents()
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["patents_added"], 0)

    def test_each_tracked_actor_is_queried_by_name(self):
        self._collecte()
        self.assertEqual([("TESTLASER SARL", 0)], self.requetes)

    def test_an_ultrafast_machining_patent_signed_by_the_actor_is_stored_with_its_proof(self):
        result = self._collecte(_lens_document(
            doc_number="1111111", applicants=("TESTLASER SAS",),
            abstract_en="A femtosecond laser drills cooling holes in a nickel superalloy turbine blade.",
        ))
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, result["actors_matched"])
        self.assertEqual(1, result["patents_added"])
        [row] = self._documents()
        self.assertEqual("TESTLASER SARL", row["actor_name"])
        self.assertEqual("patent", row["document_type"])
        self.assertEqual("EP1111111A1", row["patent_number"])
        self.assertIn("lens.org", row["source_url"])
        self.assertIn("turbine blade", row["abstract"])
        self.assertGreater(result["technology_signals_added"], 0)
        with dbmod.connect(self.tech_db) as db:
            preuves = db.execute(
                "SELECT quote FROM technology_signal_sources WHERE source_url LIKE '%lens.org%'"
            ).fetchall()
        self.assertTrue(preuves, "un brevet retenu sort classé, chaque étiquette avec sa citation")

    def test_a_patent_the_actor_does_not_sign_is_not_written(self):
        """La recherche de Lens sur le déposant est floue : on revérifie sur la réponse."""
        result = self._collecte(_lens_document(
            doc_number="2222222", applicants=("OTHER PHOTONICS INC",),
            abstract_en="A femtosecond laser drills holes in glass.",
        ))
        self.assertEqual(1, result["not_signed"])
        self.assertEqual([], self._documents())

    def test_the_actor_name_must_match_as_a_whole_word(self):
        result = self._collecte(_lens_document(
            doc_number="2222223", applicants=("SUPERTESTLASER GMBH",),
            abstract_en="A femtosecond laser drills holes in glass.",
        ))
        self.assertEqual(1, result["not_signed"])

    def test_an_off_perimeter_patent_is_not_written(self):
        """Signé par l'acteur, mais sans rien d'ultra-rapide : hors périmètre (règle du 10/09)."""
        result = self._collecte(_lens_document(
            doc_number="3333333", applicants=("TESTLASER SARL",),
            title_en="Welding flux composition", abstract_en="A flux for submerged arc welding of steel.",
        ))
        self.assertEqual(1, result["off_topic"])
        self.assertEqual([], self._documents())

    def test_a_laser_source_patent_is_not_written(self):
        """Titre et résumé réels d'un brevet Amplitude ramené par la collecte du 29/09/2026 :
        is_on_topic() le laissait passer, il ne nomme ni opération ni pièce."""
        result = self._collecte(_lens_document(
            doc_number="5555555", applicants=("TESTLASER SARL",),
            title_en="STABILIZED FEMTOSECOND PULSED LASER AND STABILIZATION METHOD",
            abstract_en=(
                "The present invention relates to a high-power femtosecond pulsed laser, said laser "
                "comprising: a source able to generate a series of input laser pulses having an "
                "envelope frequency and a carrier frequency; chirped pulse amplification means"
            ),
        ))
        self.assertEqual(1, result["off_topic"])
        self.assertEqual([], self._documents())

    def test_a_machining_patent_in_patent_wording_is_kept(self):
        """« separating » est la découpe des brevets : absente du lexique des publications."""
        result = self._collecte(_lens_document(
            doc_number="6666666", applicants=("TESTLASER SARL",),
            title_en="METHOD FOR SEPARATING ULTRATHIN GLASS",
            abstract_en="Ultrashort laser pulses are focused into the glass along a separation line.",
        ))
        self.assertEqual(1, result["patents_added"])

    def test_no_actor_candidate_is_created_from_patents_any_more(self):
        self._collecte(_lens_document(
            doc_number="4444444", applicants=("TESTLASER SARL", "SMITH JOHN"),
            abstract_en="A femtosecond laser drills holes in glass.",
        ))
        with dbmod.connect(self.actors_db) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM actor_candidates").fetchone()[0])

    def test_a_large_filer_is_paginated(self):
        pleine_page = [
            _lens_document(doc_number=str(9000000 + i), applicants=("TESTLASER SARL",))
            for i in range(lens.RESULTS_LIMIT)
        ]
        self._collecte(*pleine_page, total=250)
        self.assertEqual([0, 100, 200], [offset for _nom, offset in self.requetes])

    def test_one_failing_actor_does_not_fail_the_run(self):
        with dbmod.connect(self.actors_db) as db:
            db.execute(
                "INSERT INTO actors(name,country,role,priority,official_url,updated_at,active) VALUES(?,?,?,?,?,?,1)",
                ("SECOND LASER", "France", "Fabricant", 1, "https://second.example", dbmod.utc_now()),
            )

        def fetch(client, actor_name, offset=0):
            if actor_name == "SECOND LASER":
                raise RuntimeError("400 Bad Request")
            return _search_response()
        with patch.object(lens, "_fetch_actor_patents", fetch):
            result = collect_lens_patents()
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, result["errors"])

    def test_every_query_failing_is_reported_as_error_status(self):
        def boom(client, actor_name, offset=0):
            raise RuntimeError("429 Too Many Requests")
        with patch.object(lens, "_fetch_actor_patents", boom):
            result = collect_lens_patents()
        self.assertEqual(result["status"], "error")
        self.assertIn("429", result["message"])


class ActorQueryTests(unittest.TestCase):
    def test_the_query_names_the_actor_and_requires_an_ultrafast_term(self):
        envoye: dict = {}

        class Reponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"total": 0, "data": []}

        class Client:
            def post(self, url, json, headers):
                envoye.update(json)
                return Reponse()

        lens._fetch_actor_patents(Client(), 'Manufacturing Technology Centre (MTC) "x"', 200)
        requete = envoye["query"]["query_string"]["query"]
        self.assertIn('applicant.name:"Manufacturing Technology Centre x"', requete)
        self.assertIn("femtosecond", requete)
        self.assertIn("abstract", envoye["include"])
        self.assertEqual(200, envoye["from"])


class UpsertLensPatentTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmpdir.name)
        self.tech_db = tmp_path / "tech.db"
        with patch.object(dbmod, "ACTORS_DB", tmp_path / "actors.db"), patch.object(dbmod, "MARKET_DB", tmp_path / "market.db"), patch.object(dbmod, "TECH_DB", self.tech_db):
            dbmod.init_databases()
        self.addCleanup(self.tmpdir.cleanup)

    def test_unattributed_document_gets_attributed_by_a_later_call(self):
        doc = _parse_result(_lens_document(doc_number="5555555"))
        with dbmod.connect(self.tech_db) as db:
            inserted, attributed = _upsert_lens_patent(db, None, doc)
            self.assertEqual((inserted, attributed), (1, 0))
            inserted2, attributed2 = _upsert_lens_patent(db, "TESTLASER SARL", doc)
            self.assertEqual((inserted2, attributed2), (0, 1))
            row = db.execute("SELECT actor_name FROM documents WHERE patent_number='EP5555555A1'").fetchone()
        self.assertEqual(row["actor_name"], "TESTLASER SARL")


if __name__ == "__main__":
    unittest.main()
