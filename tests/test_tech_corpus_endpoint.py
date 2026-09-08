"""Tests pour /api/tech-corpus et /api/tech-corpus/proofs (page front "Technologie laser").

Ce endpoint fusionne deux tables au schéma différent -- `documents` (publications, brevets) et
`technology_signals` (projets européens, plus les axes validés des documents). Les cas qui
comptent sont donc les cas de jointure : un projet ne doit apparaître qu'une fois même s'il
porte plusieurs axes, et un signal d'axe rattaché à une publication ne doit pas se transformer
en ligne de corpus supplémentaire.
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

import app as appmod
import db as dbmod

PUB_URL = "https://doi.org/10.1/pub-dlip"
PROJECT_URL = "https://cordis.europa.eu/project/id/101058409"
OFFSITE_PROJECT_URL = "https://www.alphanov.com/en/collaborative-projects/femtocell"


class TechCorpusEndpointTests(unittest.TestCase):
    def _seed(self, tech_db: Path) -> None:
        stamp = dbmod.utc_now()
        with dbmod.connect(tech_db) as db:
            for actor, kind, title, url, published, doi, patent, fingerprint in (
                ("Fraunhofer IWS", "publication", "DLIP surface texturing", PUB_URL, "2026-07-13", "10.1/pub-dlip", None, "fp-1"),
                (None, "publication", "Unqualified femtosecond welding paper", "https://doi.org/10.1/plain", "2027-4", "10.1/plain", None, "fp-2"),
                ("ALPHANOV", "patent", "Procédé de texturation d'électrodes", "https://patents.test/ep1", "2026-02-18", None, "EP1234567A1", "fp-3"),
            ):
                db.execute(
                    """INSERT INTO documents(actor_name,document_type,title,source_url,published_at,doi,
                                             patent_number,fingerprint,created_at,last_seen_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (actor, kind, title, url, published, doi, patent, fingerprint, stamp, stamp),
                )

            # Un projet européen portant DEUX axes -> deux lignes technology_signals.
            for axis in ("Beam shaping", "Monitoring IA procédé"):
                dbmod.upsert_technology_signal(
                    db, fact_key=f"{axis}|OPeraTIC", axis=axis, maturity_stage="Pré-industrialisation",
                    bucket="radar", actor_names=["AIMEN", "LASEA"], source_url=PROJECT_URL,
                    quote=f"citation {axis}", field_confidence=0.8, project_name="OPeraTIC",
                    source_title="OPeraTIC sur CORDIS",
                )
            # Un projet dont la source n'est pas CORDIS.
            dbmod.upsert_technology_signal(
                db, fact_key="Roll-to-roll|Femtocell", axis="Fabrication roll-to-roll (batteries)",
                maturity_stage="Pré-industrialisation", bucket="radar", actor_names=["ALPHANOV"],
                source_url=OFFSITE_PROJECT_URL, quote="citation femtocell", field_confidence=0.8,
                project_name="Femtocell",
            )
            # Un signal SANS project_name : c'est l'axe validé de la publication ci-dessus,
            # pas une entrée de corpus à part entière.
            dbmod.upsert_technology_signal(
                db, fact_key="DLIP|" + PUB_URL, axis="DLIP",
                maturity_stage="Maturité industrielle non déterminée", bucket="radar",
                actor_names=[], source_url=PUB_URL, quote="citation dlip", field_confidence=0.7,
            )
            # Un signal rejeté ne doit jamais sortir.
            dbmod.upsert_technology_signal(
                db, fact_key="SLE|Rejete", axis="SLE", maturity_stage="Prototype", bucket="radar",
                actor_names=[], source_url="https://cordis.europa.eu/project/id/999", quote="q",
                field_confidence=0.1, project_name="Rejeté",
            )
            db.execute("UPDATE technology_signals SET review_status='rejected' WHERE project_name='Rejeté'")

    def _corpus(self, tech_db: Path) -> list[dict]:
        with patch.object(appmod, "TECH_DB", tech_db):
            return appmod.tech_corpus()

    def _with_db(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        actors_db, market_db, tech_db = base / "actors.db", base / "market.db", base / "technology.db"
        with (
            patch.object(dbmod, "ACTORS_DB", actors_db),
            patch.object(dbmod, "MARKET_DB", market_db),
            patch.object(dbmod, "TECH_DB", tech_db),
        ):
            dbmod.init_databases()
            self._seed(tech_db)
        return tmp, tech_db

    def test_merges_documents_and_projects_without_duplicating(self):
        tmp, tech_db = self._with_db()
        with tmp:
            corpus = self._corpus(tech_db)

            # 3 documents + 2 projets (OPeraTIC dédoublonné sur ses 2 axes, Femtocell) = 5.
            # Le signal d'axe de la publication n'ajoute AUCUNE ligne, et le rejeté non plus.
            self.assertEqual(5, len(corpus))
            self.assertEqual(5, len({row["uid"] for row in corpus}), "les uid doivent être uniques")
            kinds = sorted(row["kind"] for row in corpus)
            self.assertEqual(["brevet", "projet", "projet", "pub", "pub"], kinds)
            self.assertNotIn("Rejeté", [row["title"] for row in corpus])

    def test_project_carries_all_its_axes_and_actors(self):
        tmp, tech_db = self._with_db()
        with tmp:
            project = next(row for row in self._corpus(tech_db) if row["title"] == "OPeraTIC")

            self.assertEqual({"Beam shaping", "Monitoring IA procédé"}, set(project["axes"]))
            self.assertEqual({"AIMEN", "LASEA"}, set(project["actors"]))
            self.assertEqual("Pré-industrialisation", project["maturity"])
            self.assertEqual("CORDIS · GA 101058409", project["reference"])
            self.assertEqual(2, len(project["signal_ids"]), "les deux signaux alimentent le panneau de preuves")
            # Aucune date de projet n'existe en base : on ne doit pas en inventer une.
            self.assertIsNone(project["published_at"])
            self.assertIsNotNone(project["observed_at"])

    def test_project_outside_cordis_is_not_credited_to_cordis(self):
        tmp, tech_db = self._with_db()
        with tmp:
            project = next(row for row in self._corpus(tech_db) if row["title"] == "Femtocell")
            self.assertEqual("Projet européen · alphanov.com", project["reference"])

    def test_publication_axis_comes_from_its_accepted_signal(self):
        tmp, tech_db = self._with_db()
        with tmp:
            corpus = self._corpus(tech_db)
            qualified = next(row for row in corpus if row["title"] == "DLIP surface texturing")
            plain = next(row for row in corpus if row["title"].startswith("Unqualified"))

            self.assertEqual(["DLIP"], qualified["axes"])
            self.assertEqual(["Fraunhofer IWS"], qualified["actors"])
            self.assertEqual("DOI : 10.1/pub-dlip", qualified["reference"])
            self.assertTrue(qualified["signal_ids"])

            # Aucun axe n'est deviné : sans signal validé, le document sort sans axe plutôt
            # qu'avec une inférence de lexique non relue.
            self.assertEqual([], plain["axes"])
            self.assertIsNone(plain["maturity"])
            self.assertEqual([], plain["signal_ids"])
            self.assertEqual([], plain["actors"])

    def test_patent_reference_uses_patent_number(self):
        tmp, tech_db = self._with_db()
        with tmp:
            patent = next(row for row in self._corpus(tech_db) if row["kind"] == "brevet")
            self.assertEqual("Brevet EP1234567A1", patent["reference"])

    def test_partial_publication_dates_sort_correctly(self):
        # "2027-4" (date OpenAlex incomplète) doit se classer entre mars et décembre 2027, pas
        # après, comme le ferait une comparaison de chaînes brute.
        self.assertLess(appmod._corpus_sort_key("2027-4"), appmod._corpus_sort_key("2027-12-01"))
        self.assertGreater(appmod._corpus_sort_key("2027-4"), appmod._corpus_sort_key("2027-03-30"))
        self.assertEqual("", appmod._corpus_sort_key(None))

    def test_corpus_is_sorted_most_recent_first(self):
        tmp, tech_db = self._with_db()
        with tmp:
            corpus = self._corpus(tech_db)
            keys = [appmod._corpus_sort_key(row["published_at"] or row["observed_at"]) for row in corpus]
            self.assertEqual(sorted(keys, reverse=True), keys)


class TechCorpusProofsTests(unittest.TestCase):
    def _seed(self, tech_db: Path) -> list[int]:
        ids = []
        with dbmod.connect(tech_db) as db:
            for axis in ("Beam shaping", "Monitoring IA procédé"):
                _, signal_id = dbmod.upsert_technology_signal(
                    db, fact_key=f"{axis}|OPeraTIC", axis=axis, maturity_stage="Pré-industrialisation",
                    bucket="radar", actor_names=["AIMEN"], source_url=PROJECT_URL,
                    quote=f"citation {axis}", field_confidence=0.8, project_name="OPeraTIC",
                )
                ids.append(signal_id)
                dbmod.upsert_fact_source(
                    db, "technology_signal_sources", signal_id,
                    source_url=PROJECT_URL, source_title="OPeraTIC sur CORDIS",
                    quote=f"citation {axis}", language="en", field_confidence=0.8,
                    # fingerprint est NOT NULL UNIQUE : sans lui l'INSERT OR IGNORE passe
                    # silencieusement et la preuve n'est jamais écrite.
                    fingerprint=hashlib.sha256(f"{axis}|{PROJECT_URL}".encode()).hexdigest(),
                )
        return ids

    def _with_db(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        with (
            patch.object(dbmod, "ACTORS_DB", base / "actors.db"),
            patch.object(dbmod, "MARKET_DB", base / "market.db"),
            patch.object(dbmod, "TECH_DB", base / "technology.db"),
        ):
            dbmod.init_databases()
            ids = self._seed(base / "technology.db")
        return tmp, base / "technology.db", ids

    def test_returns_quotes_for_every_signal_of_a_project(self):
        tmp, tech_db, ids = self._with_db()
        with tmp, patch.object(appmod, "TECH_DB", tech_db):
            proofs = appmod.tech_corpus_proofs(",".join(str(i) for i in ids))

            self.assertEqual(2, len(proofs))
            self.assertEqual({"Beam shaping", "Monitoring IA procédé"}, {p["axis"] for p in proofs})
            self.assertTrue(all(p["quote"] for p in proofs))

    def test_empty_and_invalid_inputs(self):
        tmp, tech_db, _ = self._with_db()
        with tmp, patch.object(appmod, "TECH_DB", tech_db):
            self.assertEqual([], appmod.tech_corpus_proofs(""))
            with self.assertRaises(appmod.HTTPException):
                appmod.tech_corpus_proofs("abc")
            with self.assertRaises(appmod.HTTPException):
                appmod.tech_corpus_proofs(",".join(str(i) for i in range(60)))

    def test_rejected_signals_never_expose_their_quotes(self):
        tmp, tech_db, ids = self._with_db()
        with tmp:
            with dbmod.connect(tech_db) as db:
                db.execute("UPDATE technology_signals SET review_status='rejected' WHERE id=?", (ids[0],))
            with patch.object(appmod, "TECH_DB", tech_db):
                proofs = appmod.tech_corpus_proofs(",".join(str(i) for i in ids))

            self.assertEqual(1, len(proofs))
            self.assertNotIn(ids[0], {p["signal_id"] for p in proofs})


if __name__ == "__main__":
    unittest.main()
