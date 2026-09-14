"""Tests de national_projects.collect_national_projects() : projets nationaux et régionaux.

Aucune requête réseau : les deux fonctions qui parlent aux API (ANR, UKRI Gateway to Research)
sont remplacées par des fixtures construites à partir de réponses RÉELLES, relevées le
13/09/2026 sur les deux services -- mêmes noms de champs, mêmes formes (champs multivalués de
l'ANR, `participantValues` de GtR, texte de substitution que GtR met à la place d'un résumé
absent). Même principe que test_cordis_integration.py, qui fabrique un petit ZIP CORDIS plutôt
que de télécharger les 140 Mo réels.

La troisième passe (mentions de programme dans les pages déjà collectées) ne demande pas de
réseau du tout : elle relit `actor_sources.blocks_json`, que ces tests remplissent à la main.
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

import db as dbmod
import national_projects as npmod

# --- Fixtures ANR ----------------------------------------------------------------------------
# Forme relevée sur le jeu "fr-esr-aap-anr-projets-retenus-participants-identifies".
ANR_ON_TOPIC = {
    "code_du_projet": "ANR-14-CE16-0008",
    "titre": "Découpe et surfacing femtoseconde de supports pour le bioengineering cellulaire",
    "acronyme": "SupCo",
    "resume": (
        "Le projet développe un procédé de texturation de surface par impulsions ultracourtes "
        "pour la préparation de supports de culture cellulaire. Les structures périodiques "
        "obtenues relèvent du régime LIPSS."
    ),
    "programme": "Appel à projets générique 2014",
    "lien_projet": "http://www.agence-nationale-recherche.fr/?Projet=ANR-14-CE16-0008",
    "date_de_debut": 2015,
    "libelle_de_partenaire": ["MANUTECH USD", "Laboratoire Hubert Curien", ""],
    "sigle_de_partenaire": ["MANUTECH-USD", "LabHC", ""],
    "coordinateur_du_projet": "MANUTECH USD",
}
ANR_OFF_TOPIC = {
    "code_du_projet": "ANR-12-SEED-0003",
    "titre": "Optimisation des performances thermiques des échangeurs diphasiques",
    "acronyme": "NUCLEI",
    "resume": "Étude des transferts thermiques en ébullition nucléée, sans procédé laser.",
    "programme": "SEED 2012",
    "lien_projet": "",
    "date_de_debut": 2013,
    "libelle_de_partenaire": ["MANUTECH USD"],
    "sigle_de_partenaire": [""],
    "coordinateur_du_projet": "MANUTECH USD",
}
# Ramené par le `search()` tolérant de l'API, mais aucun partenaire ne porte réellement l'alias.
ANR_FUZZY_NOISE = {
    "code_du_projet": "ANR-18-CE08-0042",
    "titre": "Micro-usinage femtoseconde de verres optiques",
    "acronyme": "VERROPT",
    "resume": "Procédé d'ablation par impulsions femtosecondes appliqué aux verres optiques.",
    "programme": "Appel à projets générique 2018",
    "lien_projet": "",
    "date_de_debut": 2019,
    "libelle_de_partenaire": ["Institut de Chimie de Clermont-Ferrand"],
    "sigle_de_partenaire": ["ICCF"],
    "coordinateur_du_projet": "Institut de Chimie de Clermont-Ferrand",
}

# Le cas IREIS, trouvé en production le 14/09/2026 : l'ANR inscrit l'acteur sous sa raison
# sociale complète, et le nom que la base suit ne vit que dans `sigle_de_partenaire`.
ANR_ACRONYM_ONLY = {
    "code_du_projet": "ANR-13-RMNP-0010",
    "titre": "Texturation topographique multiéchelles de pièces polymères par structuration laser",
    "acronyme": "TOPOINJECTION",
    "resume": (
        "Structuration par impulsions ultracourtes de moules d'injection pour texturer des "
        "pièces polymères. Les structures périodiques visées relèvent du régime LIPSS."
    ),
    "programme": "RMNP 2013",
    "lien_projet": "",
    "date_de_debut": 2014,
    "libelle_de_partenaire": ["INSTITUT DE RECHERCHES EN INGENIERIE DES SURFACES", "Laboratoire Hubert Curien"],
    "sigle_de_partenaire": ["IREIS", "LabHC"],
    "coordinateur_du_projet": "Monsieur Stéphane BENAYOUN (LABORATOIRE DE TRIBOLOGIE)",
}

# --- Fixtures UKRI Gateway to Research --------------------------------------------------------
GTR_ON_TOPIC = {
    "id": "2D8359F7-1643-4438-8FC2-0CEE64B249B2",
    "title": "Advanced fs laser machining and inscription system",
    "abstractText": (
        "Development of an ultrashort pulse laser micromachining platform for glass, based on "
        "selective laser etching of fused silica."
    ),
    "grantCategory": "Collaborative R&D",
    "leadFunder": "Innovate UK",
    "start": "2013-04-01",
    "identifiers": {"identifier": [{"value": "100653", "type": "RCUK"}]},
    "participantValues": {"participant": [
        {"organisationName": "OXFORD LASERS LIMITED", "role": "LEAD_PARTICIPANT"},
        {"organisationName": "ASTON UNIVERSITY", "role": "PARTICIPANT"},
    ]},
}
GTR_OFF_TOPIC = {
    "id": "4B4F0B39-A8C6-4552-9E4C-19A2B0DF0238",
    "title": "SLIDE: Savings at Lubricated Interfaces Deliver Efficiency",
    # Le texte que GtR sert à la place d'un résumé jamais saisi -- ne doit jamais finir en
    # citation, et ne doit jamais faire passer un projet pour pertinent.
    "abstractText": "Abstracts are not currently available in GtR for all funded research.",
    "leadFunder": "Innovate UK",
    "identifiers": {"identifier": [{"value": "101234", "type": "RCUK"}]},
    "participantValues": {"participant": []},
}
GTR_NO_REFERENCE = {
    "id": "AAAAAAAA-0000-0000-0000-000000000000",
    "title": "Ultrafast laser texturing of bearing surfaces",
    "abstractText": "Femtosecond laser surface texturing for reduced friction.",
    "leadFunder": "EPSRC",
    "identifiers": {"identifier": []},
    "participantValues": {"participant": []},
}

# Deux blocs séparés : le projet RAPID (laser ultra-rapide) en haut de page, et bien plus bas
# un financement structurel sans rapport. Les blocs intermédiaires ne sont pas décoratifs --
# ils mettent le second hors de la fenêtre de contexte du premier, comme sur une vraie page.
ALPHANOV_BLOCKS = [
    {
        "heading": "Projet RAPID DUALIS",
        "text": (
            "Le projet RAPID DUALIS, financé par la DGA, porte sur le micro-usinage par "
            "impulsions femtosecondes de composants optiques durcis."
        ),
    },
    {"heading": "Nos équipes", "text": "Quarante ingénieurs répartis sur trois plateformes."},
    {"heading": "Contact", "text": "Nous écrire, nous rendre visite, nous suivre."},
    {
        "heading": "Financements",
        # Programme nommé, mais rien de laser ultra-rapide à portée : ne doit rien écrire.
        "text": "ALPhANOV est soutenu par la Région Nouvelle-Aquitaine pour ses actions de formation.",
    },
]

# Le compromis de MENTION_CONTEXT_BLOCKS en miniature : un bloc "Financement" qui ne dit pas
# de quoi parle le projet, juste à côté du bloc qui le dit. C'est le cas Femtocell d'ALPhANOV.
FEMTOCELL_BLOCKS = [
    {
        "heading": "ALPhANOV's role",
        "text": (
            "ALPhANOV contributes its expertise in laser process control applied to battery "
            "manufacturing, using ultrashort pulse laser structuring of electrodes."
        ),
    },
    {"heading": "Funders", "text": "This project has been funded by the French government under the France 2030 program."},
]


class NationalProjectsTests(unittest.TestCase):
    def _databases(self, tmp: str) -> tuple[Path, Path]:
        actors_db, tech_db = Path(tmp) / "actors.db", Path(tmp) / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
            patch.object(dbmod, "TECH_DB", tech_db),
        ):
            dbmod.init_databases()
            with dbmod.connect(actors_db) as db:
                db.execute("DELETE FROM actors")
            ids = {
                name: dbmod.create_actor(name, country, "Test", url)
                for name, country, url in (
                    ("MANUTECH USD", "France", "https://www.manutech-usd.fr"),
                    ("Oxford Lasers", "Royaume-Uni", "https://oxfordlasers.com"),
                    ("ALPHANOV", "France", "https://www.alphanov.com"),
                )
            }
        self.actor_ids = ids
        return actors_db, tech_db

    def _seed_page(self, actors_db: Path, actor_id: int, url: str, blocks: list[dict]) -> None:
        with dbmod.connect(actors_db) as db:
            db.execute(
                "INSERT INTO actor_sources(actor_id,url,source_kind,blocks_json) VALUES(?,?,?,?)",
                (actor_id, url, "page", json.dumps(blocks, ensure_ascii=False)),
            )

    def _run(self, actors_db: Path, tech_db: Path, *, include_sources=("anr", "gtr", "mentions")) -> dict:
        def fake_anr(client, alias):
            return [ANR_ON_TOPIC, ANR_OFF_TOPIC, ANR_FUZZY_NOISE] if alias == "MANUTECH USD" else []

        def fake_org_ids(client, actor_name, alias):
            return ["7BD02E7C"] if alias == "OXFORD LASERS" else []

        def fake_projects(client, organisation_id):
            return [GTR_ON_TOPIC, GTR_OFF_TOPIC, GTR_NO_REFERENCE]

        with (
            patch.object(npmod, "ACTORS_DB", actors_db),
            patch.object(npmod, "TECH_DB", tech_db),
            patch.object(npmod, "anr_records_for_alias", fake_anr),
            patch.object(npmod, "gtr_organisation_ids", fake_org_ids),
            patch.object(npmod, "gtr_projects_for_organisation", fake_projects),
        ):
            return npmod.collect_national_projects(include_sources=include_sources)

    # --- ANR ---------------------------------------------------------------------------------

    def test_anr_writes_event_relation_and_scoped_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            result = self._run(actors_db, tech_db, include_sources=("anr",))

            self.assertEqual(1, result["projects_matched"])
            self.assertEqual(1, result["projects_off_topic"])  # NUCLEI, échangeurs thermiques
            self.assertEqual(0, result["errors"])

            with dbmod.connect(actors_db) as db:
                events = db.execute("SELECT * FROM actor_events").fetchall()
                relations = db.execute("SELECT * FROM actor_relations").fetchall()
            self.assertEqual(1, len(events))
            event = events[0]
            self.assertEqual("anr_project", event["event_type"])
            self.assertEqual("verified", event["review_status"])
            self.assertIn("SupCo", event["description"])
            # La date de l'ANR est une année, écrite telle quelle et jamais complétée.
            self.assertEqual("2015", event["event_date"])
            self.assertEqual(ANR_ON_TOPIC["lien_projet"], event["source_url"])

            # Le partenaire non suivi entre comme relation ; l'acteur lui-même, non.
            related = {row["related_name"] for row in relations}
            self.assertIn("Laboratoire Hubert Curien", related)
            self.assertNotIn("MANUTECH USD", related)

            with dbmod.connect(tech_db) as db:
                signal = db.execute(
                    "SELECT axis,project_name,funding_scope,funding_program,quote FROM technology_signals"
                ).fetchone()
            self.assertIsNotNone(signal)
            self.assertEqual("LIPSS", signal["axis"])
            self.assertEqual("SupCo", signal["project_name"])
            self.assertEqual("national", signal["funding_scope"])
            self.assertTrue(signal["funding_program"].startswith("ANR"))

    def test_anr_ignores_a_fuzzy_match_no_partner_actually_carries(self):
        """VERROPT est sur le sujet et revient de l'API, mais aucun de ses partenaires n'est
        l'acteur suivi : le filtre strict local doit l'écarter malgré la réponse du serveur."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            self._run(actors_db, tech_db, include_sources=("anr",))
            with dbmod.connect(actors_db) as db:
                descriptions = [row["description"] for row in db.execute("SELECT description FROM actor_events")]
            self.assertFalse(any("VERROPT" in text for text in descriptions))

    def test_anr_falls_back_to_the_canonical_page_when_the_link_is_empty(self):
        self.assertEqual(
            "https://anr.fr/Projet-ANR-12-SEED-0003",
            npmod._anr_project_url(ANR_OFF_TOPIC),
        )

    # --- UKRI --------------------------------------------------------------------------------

    def test_gtr_keeps_only_on_topic_projects_with_a_citable_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            result = self._run(actors_db, tech_db, include_sources=("gtr",))

            self.assertEqual(1, result["projects_matched"])
            self.assertEqual(0, result["errors"])

            with dbmod.connect(actors_db) as db:
                events = db.execute("SELECT * FROM actor_events").fetchall()
                relations = db.execute("SELECT related_name FROM actor_relations").fetchall()
            self.assertEqual(1, len(events))
            self.assertEqual("ukri_project", events[0]["event_type"])
            self.assertEqual("https://gtr.ukri.org/projects?ref=100653", events[0]["source_url"])
            self.assertIn("Innovate UK", events[0]["description"])
            self.assertEqual("2013-04-01", events[0]["event_date"])
            self.assertEqual({"ASTON UNIVERSITY"}, {row["related_name"] for row in relations})

            with dbmod.connect(tech_db) as db:
                signal = db.execute(
                    "SELECT funding_scope,funding_program,quote FROM technology_signals"
                ).fetchone()
            self.assertEqual("national", signal["funding_scope"])
            self.assertEqual("Innovate UK", signal["funding_program"])

    def test_gtr_placeholder_abstract_is_never_read_as_a_summary(self):
        self.assertEqual("", npmod._gtr_abstract(GTR_OFF_TOPIC))
        self.assertIn("ultrashort", npmod._gtr_abstract(GTR_ON_TOPIC))

    # --- Mentions de programme ----------------------------------------------------------------

    def test_program_mention_needs_both_a_programme_and_the_laser_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            self._seed_page(
                actors_db, self.actor_ids["ALPHANOV"],
                "https://www.alphanov.com/fr/projets", ALPHANOV_BLOCKS,
            )
            result = self._run(actors_db, tech_db, include_sources=("mentions",))

            self.assertEqual(1, result["pages_scanned"])
            self.assertEqual(1, result["mentions_added"])

            with dbmod.connect(actors_db) as db:
                events = db.execute("SELECT * FROM actor_events").fetchall()
            self.assertEqual(1, len(events))
            event = events[0]
            self.assertEqual("funding_program_mention", event["event_type"])
            # Signal faible : il passe par la file de relecture, contrairement à ANR/UKRI.
            self.assertEqual("pending", event["review_status"])
            self.assertIn("RAPID", event["description"])
            self.assertIn("national", event["description"])
            # Le dernier bloc nomme bien la Région, mais aucun bloc à sa portée ne parle de
            # laser ultra-rapide : il reste hors de la base.
            self.assertNotIn("Nouvelle-Aquitaine", event["description"])

    def test_the_topic_may_live_in_the_neighbouring_block(self):
        """Un bloc "Funders" ne dit jamais de quoi parle le projet -- le sujet est à côté.
        C'est toute la raison d'être de MENTION_CONTEXT_BLOCKS (voir sa note de mesure)."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            self._seed_page(
                actors_db, self.actor_ids["ALPHANOV"],
                "https://www.alphanov.com/en/collaborative-projects/femtocell", FEMTOCELL_BLOCKS,
            )
            result = self._run(actors_db, tech_db, include_sources=("mentions",))
            self.assertEqual(1, result["mentions_added"])

            with dbmod.connect(actors_db) as db:
                description = db.execute("SELECT description FROM actor_events").fetchone()["description"]
            self.assertIn("France 2030", description)
            self.assertIn("national", description)

            # Une mention n'écrit jamais de signal technologique : elle n'est pas encore relue.
            with dbmod.connect(tech_db) as db:
                self.assertEqual(0, db.execute("SELECT COUNT(*) FROM technology_signals").fetchone()[0])

    # --- Idempotence --------------------------------------------------------------------------

    def test_second_run_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            self._seed_page(
                actors_db, self.actor_ids["ALPHANOV"],
                "https://www.alphanov.com/fr/projets", ALPHANOV_BLOCKS,
            )
            first = self._run(actors_db, tech_db)
            second = self._run(actors_db, tech_db)

            self.assertGreater(first["events_added"], 0)
            self.assertGreater(first["mentions_added"], 0)
            self.assertGreater(first["signals_added"], 0)
            for key in ("events_added", "relations_added", "signals_added", "mentions_added"):
                self.assertEqual(0, second[key], key)
            self.assertEqual(0, second["errors"])


class AliasRecallTests(unittest.TestCase):
    """Les quatre défauts de rappel trouvés le 14/09/2026 en confrontant le collecteur aux
    registres réels. Tous faisaient perdre des projets qui existaient bel et bien."""

    def test_a_legal_suffix_never_blocks_a_match(self):
        """GtR connaît "Laser Micromachining Limited", la base suit "Laser Micromachining
        Ltd" : exiger le suffixe donnait 0 fiche retenue sur 25 renvoyées."""
        alias = npmod.match_alias("Laser Micromachining Ltd")
        self.assertEqual("LASER MICROMACHINING", alias)
        for spelling in ("Laser Micromachining Limited", "LASER MICROMACHINING LIMITED",
                         "Laser Micromachining Ltd"):
            with self.subTest(spelling=spelling):
                self.assertTrue(npmod._contains_whole_phrase(npmod._normalize_org_text(spelling), alias))
        # ...sans pour autant ouvrir la porte à une organisation voisine mais distincte.
        self.assertFalse(npmod._contains_whole_phrase(npmod._normalize_org_text("Laser Quantum Ltd"), alias))

    def test_a_parenthesised_acronym_leaves_the_alias(self):
        """"Manufacturing Technology Centre (MTC)" ne matchait que sa fiche exacte, pas les
        trois autres fiches GtR du même centre."""
        alias = npmod.match_alias("Manufacturing Technology Centre (MTC)")
        self.assertEqual("MANUFACTURING TECHNOLOGY CENTRE", alias)
        self.assertTrue(npmod._contains_whole_phrase(
            npmod._normalize_org_text("THE MANUFACTURING TECHNOLOGY CENTRE LIMITED"), alias))

    def test_a_three_letter_actor_is_queried(self):
        """TWI (9 fiches dans GtR) et HEF étaient écartés avant toute requête par le seuil
        de CORDIS, calibré pour un dump de 300 000 lignes, pas pour une API par nom."""
        actors = [{"id": 1, "name": "TWI"}, {"id": 2, "name": "HEF"}]
        self.assertEqual({"TWI": "TWI", "HEF": "HEF"}, npmod._tracked_aliases(actors))

    def test_an_alias_reduced_to_nothing_keeps_the_full_name(self):
        """Une raison sociale qui n'est QUE sa forme juridique ne doit pas devenir vide."""
        self.assertEqual("AG", npmod.match_alias("AG"))

    def test_an_actor_named_only_by_its_acronym_in_anr_is_matched(self):
        """Le cas IREIS : la requête serveur interroge `sigle_de_partenaire`, la vérification
        locale doit interroger le même champ -- sinon on refuse ce qu'on est allé chercher."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            with dbmod.connect(actors_db) as db:
                db.execute("DELETE FROM actors")
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                dbmod.create_actor("IREIS", "France", "Test", "https://www.ireis.fr")

            def fake_anr(client, alias):
                return [ANR_ACRONYM_ONLY] if alias == "IREIS" else []

            with (
                patch.object(npmod, "ACTORS_DB", actors_db),
                patch.object(npmod, "TECH_DB", tech_db),
                patch.object(npmod, "anr_records_for_alias", fake_anr),
            ):
                result = npmod.collect_national_projects(include_sources=("anr",))
            self.assertEqual(1, result["projects_matched"])

            with dbmod.connect(actors_db) as db:
                event = db.execute("SELECT description FROM actor_events").fetchone()
            self.assertIn("TOPOINJECTION", event["description"])

    _databases = NationalProjectsTests._databases


class GtrScopeTests(unittest.TestCase):
    """GtR n'est pas que le guichet national : "Horizon Europe Guarantee" et "EU" y sont de
    vrais leadFunder (6 projets chez TWI, 3 au MTC, relevé du 14/09/2026). Les étiqueter
    "projet national" serait faux sur l'étiquette la plus visible de la fiche."""

    def test_european_funders_are_not_national(self):
        for funder in ("Horizon Europe Guarantee", "EU", "horizon europe guarantee"):
            with self.subTest(funder=funder):
                self.assertEqual("europeen", npmod._gtr_scope(funder))

    def test_every_other_ukri_funder_is_national(self):
        for funder in ("EPSRC", "Innovate UK", "ISCF", "ATI", "APC", "UKRI FLF", "SPF", "UKRI"):
            with self.subTest(funder=funder):
                self.assertEqual("national", npmod._gtr_scope(funder))


class GtrTransportTests(unittest.TestCase):
    """GtR dit "rien trouvé" avec un 404, pas avec une liste vide (relevé du 14/09/2026 : 33
    des 77 acteurs suivis, tous non britanniques). Le compter comme une erreur faisait passer
    une collecte parfaitement nominale pour une collecte en panne."""

    def test_a_404_means_no_result_not_a_failure(self):
        class FakeResponse:
            status_code = 404

            def raise_for_status(self):  # ne doit jamais être appelée sur un 404
                raise AssertionError("un 404 ne doit pas être traité comme une panne")

            def json(self):
                raise AssertionError("un 404 n'a pas de corps JSON à lire")

        class FakeClient:
            def get(self, *args, **kwargs):
                return FakeResponse()

        self.assertEqual([], npmod.gtr_organisation_ids(FakeClient(), "ALPHANOV", "ALPHANOV"))
        self.assertEqual([], npmod.gtr_projects_for_organisation(FakeClient(), "any-id"))

    def test_a_real_http_error_still_raises(self):
        import httpx

        class FakeResponse:
            status_code = 500

            def raise_for_status(self):
                raise httpx.HTTPStatusError("boom", request=None, response=None)

        class FakeClient:
            def get(self, *args, **kwargs):
                return FakeResponse()

        with self.assertRaises(httpx.HTTPStatusError):
            npmod.gtr_organisation_ids(FakeClient(), "Oxford Lasers", "OXFORD LASERS")


class FundingProgramLexiconTests(unittest.TestCase):
    """Le lexique des guichets : chaque libellé a une échelle, et les sigles courts exigent
    un contexte -- c'est ce qui distingue RAPID le régime d'aide de "rapid prototyping"."""

    def test_every_program_declares_a_scope(self):
        self.assertEqual(set(npmod.FUNDING_PROGRAMS), set(npmod.FUNDING_PROGRAM_SCOPES))
        for scope, _country in npmod.FUNDING_PROGRAM_SCOPES.values():
            self.assertIn(scope, npmod.SCOPE_LABELS)

    def test_rapid_needs_a_defence_context(self):
        with_context = npmod.detect_program_mentions(
            "Projet RAPID soutenu par la DGA : perçage par laser femtoseconde."
        )
        self.assertEqual(["RAPID (AID/DGA)"], [label for label, _ in with_context])
        self.assertEqual([], npmod.detect_program_mentions(
            "Rapid prototyping of femtosecond laser optics for internal use."
        ))

    def test_regional_and_european_scopes_are_not_confused(self):
        self.assertEqual("regional", npmod.FUNDING_PROGRAM_SCOPES["FEDER / ERDF"][0])
        self.assertEqual("regional", npmod.FUNDING_PROGRAM_SCOPES["Appel à projets régional"][0])
        self.assertEqual("europeen", npmod.FUNDING_PROGRAM_SCOPES["Interreg"][0])


if __name__ == "__main__":
    unittest.main()
