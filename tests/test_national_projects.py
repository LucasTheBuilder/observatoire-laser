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

import csv
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
#
# Colonnes relevées le 14/09/2026 sur les CSV que l'ANR publie sur data.gouv.fr (ANR_01
# DOS/DGDS et ANR_02 DGPIE) : deux fichiers par jeu, l'un décrivant les projets, l'autre leurs
# partenaires, joints sur `Projet.Code_Decision`.
ANR_PROJECT_COLUMNS = [
    "Projet.Code_Decision", "AAP.Edition", "Projet.Acronyme", "Projet.Titre.Francais",
    "Projet.Titre.Anglais", "Projet.Resume.Francais", "Projet.Resume.Anglais",
    "Programme.Acronyme", "Projet.Montant.AF.Aide_allouee.ANR", "Projet.T0 scientifique",
]
ANR_PARTNER_COLUMNS = [
    "Projet.Code_Decision", "Projet.Acronyme", "Projet.Partenaire.Code_Decision",
    "Projet.Partenaire.Est_coordinateur", "Projet.Partenaire.Nom_organisme",
    "Projet.Partenaire.Categorie_organisme", "Projet.Partenaire.Adresse.Ville",
    "Projet.Partenaire.Adresse.Pays", "Projet.Partenaire.Aide_allouee.ANR",
]

ANR_PROJECTS = [
    {
        "Projet.Code_Decision": "ANR-14-CE16-0008", "AAP.Edition": "2014", "Projet.Acronyme": "SupCo",
        "Projet.Titre.Francais": "Découpe et surfacing femtoseconde de supports pour le bioengineering cellulaire",
        "Projet.Titre.Anglais": "Femtosecond cutting and surfacing of cell bioengineering substrates",
        "Projet.Resume.Francais": (
            "Le projet développe un procédé de texturation de surface par impulsions "
            "ultracourtes. Les structures périodiques obtenues relèvent du régime LIPSS."
        ),
        "Programme.Acronyme": "CE16", "Projet.T0 scientifique": "2015-03-01",
    },
    {
        # Hors sujet : matche un acteur suivi, ne doit produire aucune ligne.
        "Projet.Code_Decision": "ANR-12-SEED-0003", "AAP.Edition": "2012", "Projet.Acronyme": "NUCLEI",
        "Projet.Titre.Francais": "Optimisation des performances thermiques des échangeurs diphasiques",
        "Projet.Resume.Francais": "Étude des transferts thermiques en ébullition nucléée, sans procédé laser.",
        "Programme.Acronyme": "SEED", "Projet.T0 scientifique": "2013-01-01",
    },
    {
        # Sur le sujet, mais aucun acteur suivi au consortium : invisible pour l'observatoire.
        "Projet.Code_Decision": "ANR-18-CE08-0042", "AAP.Edition": "2018", "Projet.Acronyme": "VERROPT",
        "Projet.Titre.Francais": "Micro-usinage femtoseconde de verres optiques",
        "Projet.Resume.Francais": "Procédé d'ablation par impulsions femtosecondes, régime LIPSS.",
        "Programme.Acronyme": "CE08", "Projet.T0 scientifique": "2019-01-01",
    },
]

ANR_PARTNERS = [
    {"Projet.Code_Decision": "ANR-14-CE16-0008", "Projet.Acronyme": "SupCo",
     "Projet.Partenaire.Est_coordinateur": "True", "Projet.Partenaire.Nom_organisme": "MANUTECH-USD"},
    {"Projet.Code_Decision": "ANR-14-CE16-0008", "Projet.Acronyme": "SupCo",
     "Projet.Partenaire.Est_coordinateur": "False", "Projet.Partenaire.Nom_organisme": "Laboratoire Hubert Curien"},
    {"Projet.Code_Decision": "ANR-12-SEED-0003", "Projet.Acronyme": "NUCLEI",
     "Projet.Partenaire.Est_coordinateur": "False", "Projet.Partenaire.Nom_organisme": "MANUTECH-USD"},
    {"Projet.Code_Decision": "ANR-18-CE08-0042", "Projet.Acronyme": "VERROPT",
     "Projet.Partenaire.Est_coordinateur": "True", "Projet.Partenaire.Nom_organisme": "Institut de Chimie de Clermont-Ferrand"},
]


def _write_anr_csv(path: Path, columns: list[str], rows: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


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

    def _anr_fixture_files(self, tmp: str, *, projects=None, partners=None) -> tuple[list[Path], list[Path]]:
        """Les deux listes de fichiers que `anr_cached_files` rendrait après téléchargement."""
        return (
            [_write_anr_csv(Path(tmp) / "projets.csv", ANR_PROJECT_COLUMNS,
                            ANR_PROJECTS if projects is None else projects)],
            [_write_anr_csv(Path(tmp) / "partenaires.csv", ANR_PARTNER_COLUMNS,
                            ANR_PARTNERS if partners is None else partners)],
        )

    def _run(self, actors_db: Path, tech_db: Path, *, include_sources=("anr", "gtr", "mentions"),
             anr_files=None) -> dict:
        files = anr_files if anr_files is not None else self._anr_fixture_files(str(actors_db.parent))

        def fake_anr_files(client):
            return files

        def fake_org_ids(client, actor_name, alias):
            return ["7BD02E7C"] if alias == "OXFORD LASERS" else []

        def fake_projects(client, organisation_id):
            return [GTR_ON_TOPIC, GTR_OFF_TOPIC, GTR_NO_REFERENCE]

        with (
            patch.object(npmod, "ACTORS_DB", actors_db),
            patch.object(npmod, "TECH_DB", tech_db),
            patch.object(npmod, "anr_cached_files", fake_anr_files),
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
            # `Projet.T0 scientifique` est une vraie date, un des gains du passage du jeu
            # archivé (qui ne donnait qu'une année) aux jeux vivants de l'ANR.
            self.assertEqual("2015-03-01", event["event_date"])
            self.assertEqual("https://anr.fr/Projet-ANR-14-CE16-0008", event["source_url"])

            # Le partenaire non suivi entre comme relation ; l'acteur lui-même, non.
            related = {row["related_name"] for row in relations}
            self.assertIn("Laboratoire Hubert Curien", related)
            self.assertNotIn("MANUTECH-USD", related)

            with dbmod.connect(tech_db) as db:
                signal = db.execute(
                    "SELECT axis,project_name,funding_scope,funding_program,quote FROM technology_signals"
                ).fetchone()
            self.assertIsNotNone(signal)
            self.assertEqual("LIPSS", signal["axis"])
            self.assertEqual("SupCo", signal["project_name"])
            self.assertEqual("national", signal["funding_scope"])
            self.assertTrue(signal["funding_program"].startswith("ANR"))

    def test_a_project_without_a_tracked_partner_stays_out(self):
        """VERROPT est parfaitement sur le sujet, mais aucun acteur suivi n'est à son
        consortium : l'observatoire suit des acteurs, pas un domaine."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db, tech_db = self._databases(tmp)
            self._run(actors_db, tech_db, include_sources=("anr",))
            with dbmod.connect(actors_db) as db:
                descriptions = [row["description"] for row in db.execute("SELECT description FROM actor_events")]
            self.assertFalse(any("VERROPT" in text for text in descriptions))

    def test_the_source_url_is_the_canonical_anr_page(self):
        """Les CSV de l'ANR ne portent aucune colonne de lien : l'URL vient du gabarit du
        site, vérifié sur un code valide et un code invalide (voir ANR_PROJECT_URL_TEMPLATE)."""
        self.assertEqual("https://anr.fr/Projet-ANR-12-SEED-0003",
                         npmod._anr_project_url("ANR-12-SEED-0003"))

    def test_both_languages_feed_the_topic_filter(self):
        """Beaucoup de projets ne remplissent qu'un des deux résumés ; le lexique est plus
        riche en anglais. Les quatre champs comptent."""
        anglais_seul = {
            "Projet.Code_Decision": "ANR-20-TEST-0001", "Projet.Titre.Anglais":
                "Ultrashort pulse laser structuring of battery electrodes, LIPSS regime",
        }
        self.assertIn("Ultrashort", npmod._anr_text(anglais_seul))
        francais_seul = {"Projet.Resume.Francais": "Structuration par impulsions ultracourtes."}
        self.assertIn("ultracourtes", npmod._anr_text(francais_seul))

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

    def test_the_live_anr_files_name_the_organisation_directly(self):
        """Le jeu ANR vivant n'a PAS de colonne de sigle -- contrairement au jeu archivé, où
        IREIS ne vivait que là. Il nomme l'organisation telle qu'elle signe ("IREIS",
        "HEF R&D - IREIS", "MANUTECH-USD", "IREPA LASER" : relevé du 14/09/2026), donc
        l'appariement se joue entièrement sur le nom d'organisation."""
        for spelling, actor in (
            ("IREIS", "IREIS"), ("HEF R&D - IREIS", "IREIS"),
            ("MANUTECH-USD", "MANUTECH USD"), ("GIE Manutech-USD", "MANUTECH USD"),
            ("IREPA LASER", "IREPA LASER"),
        ):
            with self.subTest(spelling=spelling):
                self.assertTrue(npmod._contains_whole_phrase(
                    npmod._normalize_org_text(spelling), npmod.match_alias(actor)))

    _databases = NationalProjectsTests._databases


class AnrSourceFreshnessTests(unittest.TestCase):
    """Deux pannes SILENCIEUSES du 14/09/2026, toutes deux « 200 OK, données incomplètes ».

    La première : le module interrogeait un jeu archivé, figé en 2016. La seconde : le
    resolveur de ressources ne gardait qu'un fichier CSV par jeu, alors que l'ANR découpe
    chaque jeu en ères (2005-2009 / depuis-2010) -- il ne lisait donc qu'une ère. Aucune des
    deux ne levait d'erreur ; toutes deux se voyaient uniquement en comptant les résultats.
    """

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, payload):
            self._payload = payload

        def get(self, *args, **kwargs):
            return AnrSourceFreshnessTests._FakeResponse(self._payload)

    # Forme relevée le 14/09/2026 sur data.gouv.fr : les deux ères, dans l'ordre réel de
    # publication (2010+ d'abord), plus des formats que la collecte doit ignorer.
    CATALOGUE = {"resources": [
        {"format": "pdf", "title": "notice.pdf", "url": "https://x/notice.pdf"},
        {"format": "xlsx", "title": "anr-dgds-depuis-2010-projets.xlsx", "url": "https://x/a.xlsx"},
        {"format": "csv", "title": "anr-dgds-depuis-2010-projets.csv", "url": "https://x/2010-projets.csv"},
        {"format": "csv", "title": "anr-dgds-depuis-2010-partenaires.csv", "url": "https://x/2010-partenaires.csv"},
        {"format": "csv", "title": "anr-dgds-2005-2009-projets.csv", "url": "https://x/2005-projets.csv"},
        {"format": "csv", "title": "anr-dgds-2005-2009-partenaires.csv", "url": "https://x/2005-partenaires.csv"},
    ]}

    def test_every_era_is_collected_not_just_the_last_one(self):
        projects, partners = npmod._anr_resource_urls(self._FakeClient(self.CATALOGUE), "peu-importe")
        self.assertEqual(["https://x/2010-projets.csv", "https://x/2005-projets.csv"], projects)
        self.assertEqual(["https://x/2010-partenaires.csv", "https://x/2005-partenaires.csv"], partners)

    def test_non_csv_resources_are_ignored(self):
        projects, partners = npmod._anr_resource_urls(self._FakeClient(self.CATALOGUE), "peu-importe")
        self.assertFalse([url for url in projects + partners if not url.endswith(".csv")])

    def test_the_archived_dataset_is_no_longer_referenced(self):
        """Garde-fou de régression : le jeu archivé du portail MESR s'arrête en 2016 et son
        API de requêtage est tentante. Rien dans le module ne doit y renvoyer."""
        source = (ROOT / "national_projects.py").read_text(encoding="utf-8")
        self.assertNotIn("fr-esr-aap-anr-projets-retenus-participants-identifies", source.split('"""', 2)[2])


class GtrDateAndAmountTests(unittest.TestCase):
    """Deux informations que GtR publie ailleurs qu'on ne les cherche d'abord.

    `start` au niveau du projet est vide sur une grande partie des fiches -- aucun projet
    d'Oxford Lasers ne le porte (relevé du 15/09/2026) --, mais la période de financement est
    toujours là, dans le lien `FUND` et en millisecondes epoch. Sans ce repli, aucun projet
    UKRI n'aurait de date et l'histogramme par année les ignorerait tous.
    """

    def test_the_fund_link_supplies_the_missing_start_date(self):
        project = {"links": {"link": [
            {"rel": "PI_PER", "start": 1600000000000},
            {"rel": "FUND", "start": 1138752000000, "end": 1233360000000},
        ]}}
        self.assertEqual("2006-02-01", npmod._gtr_start(project))

    def test_the_earliest_funding_period_wins(self):
        """Un projet reconduit porte plusieurs liens FUND : c'est son début qu'on veut."""
        project = {"links": {"link": [
            {"rel": "FUND", "start": 1711926000000},
            {"rel": "FUND", "start": 1138752000000},
        ]}}
        self.assertEqual("2006-02-01", npmod._gtr_start(project))

    def test_an_explicit_start_is_preferred_to_the_fund_link(self):
        project = {"start": "2013-04-01", "links": {"link": [{"rel": "FUND", "start": 1138752000000}]}}
        self.assertEqual("2013-04-01", npmod._gtr_start(project))

    def test_no_date_at_all_stays_none(self):
        self.assertIsNone(npmod._gtr_start({"links": {"link": [{"rel": "PI_PER", "start": 1138752000000}]}}))
        self.assertIsNone(npmod._gtr_start({}))

    def test_the_grant_is_summed_only_when_every_share_is_published(self):
        """Un total partiel est plus trompeur qu'une absence : il se lit comme un total."""
        complet = {"participantValues": {"participant": [{"grantOffer": 100.0}, {"grantOffer": 50.0}]}}
        self.assertEqual(150.0, npmod._gtr_amount(complet))
        partiel = {"participantValues": {"participant": [{"grantOffer": 100.0}, {}]}}
        self.assertIsNone(npmod._gtr_amount(partiel))
        self.assertIsNone(npmod._gtr_amount({"participantValues": {"participant": []}}))


class AnrDateAndAmountTests(unittest.TestCase):
    def test_the_call_edition_stands_in_for_a_missing_start_date(self):
        """`Projet.T0 scientifique` manque sur les projets anciens ; l'édition de l'appel est
        alors la seule information temporelle publiée, écrite telle quelle."""
        self.assertEqual("2021-04-20", npmod._anr_start(
            {"Projet.T0 scientifique": "2021-04-20", "AAP.Edition": "2021"}))
        self.assertEqual("2013", npmod._anr_start({"Projet.T0 scientifique": "", "AAP.Edition": "2013"}))
        self.assertIsNone(npmod._anr_start({}))

    def test_the_project_total_is_preferred_to_the_sum_of_shares(self):
        projet = {"Projet.Montant.AF.Aide_allouee.ANR": "517116.00"}
        parts = [{"Projet.Partenaire.Aide_allouee.ANR": "1.00"}]
        self.assertAlmostEqual(517116.0, npmod._anr_amount(projet, parts))

    def test_shares_are_summed_only_when_all_are_published(self):
        parts = [{"Projet.Partenaire.Aide_allouee.ANR": "100.0"},
                 {"Projet.Partenaire.Aide_allouee.ANR": "50.5"}]
        self.assertAlmostEqual(150.5, npmod._anr_amount({}, parts))
        incomplet = [{"Projet.Partenaire.Aide_allouee.ANR": "100.0"}, {}]
        self.assertIsNone(npmod._anr_amount({}, incomplet))

    def test_an_unreadable_amount_is_absent_not_zero(self):
        """Un projet dont on ignore le montant n'est pas un projet financé zéro euro."""
        self.assertIsNone(npmod._amount("n/a"))
        self.assertIsNone(npmod._amount(""))
        self.assertEqual(0.0, npmod._amount("0"))


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
