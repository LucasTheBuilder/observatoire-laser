"""Tests pour la file des publications sans acteur rattaché (db.unlinked_documents).

Règle de périmètre de Lucas (13/09/2026) : le corpus Technologie laser ne contient que ce qu'un
acteur du roster signe. Deux collecteurs, deux destinations — openalex.py cherche PAR institution
et remplit `documents`, scrapers.scrape_technology cherche PAR SUJET sur Crossref et remplit
cette file.

Le test qui compte est `test_a_tracked_signatory_sends_it_back_to_the_corpus` : la passe
thématique n'attribue jamais d'acteur, donc une publication d'un acteur suivi arrive ici
orpheline. Sans la résolution par institution, la règle de périmètre supprimerait précisément ce
qu'elle veut garder — c'est arrivé le 13/09/2026 pour une publication CNRS, rattrapée à la main.
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
from db import (
    connect,
    exclude_document,
    resolve_unlinked_document,
    scalar,
    upsert_document,
    upsert_unlinked_document,
)

DOI = "10.1016/j.optlastec.2026.999999"
TITRE = "Femtosecond laser drilling of through vias in borosilicate glass"
EMPREINTE = __import__("hashlib").sha256(DOI.casefold().encode()).hexdigest()


class UnlinkedDocumentsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patchers = [
            patch.object(dbmod, "ACTORS_DB", root / "actors.db"),
            patch.object(dbmod, "MARKET_DB", root / "market.db"),
            patch.object(dbmod, "TECH_DB", root / "technology.db"),
        ]
        for patcher in self._patchers:
            patcher.start()
        dbmod.init_databases()
        self.tech_db = dbmod.TECH_DB

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._tmp.cleanup()

    def _queue(self, db, doi=DOI, titre=TITRE):
        return upsert_unlinked_document(
            db, title=titre, source_url=f"https://doi.org/{doi}",
            fingerprint_source=doi, doi=doi, published_at="2026-05-01",
            abstract="Abstract de test.",
        )

    def test_a_new_publication_enters_the_queue(self):
        with connect(self.tech_db) as db:
            self.assertEqual(1, self._queue(db))
        self.assertEqual(1, scalar(self.tech_db, "SELECT COUNT(*) FROM unlinked_documents"))
        self.assertEqual(0, scalar(self.tech_db, "SELECT COUNT(*) FROM documents"))

    def test_seeing_it_again_counts_instead_of_duplicating(self):
        """`times_seen` est la seule mesure d'insistance dont dispose la page : une publication
        que six collectes ramènent pèse plus qu'une passante."""
        with connect(self.tech_db) as db:
            self.assertEqual(1, self._queue(db))
            self.assertEqual(0, self._queue(db))
            self.assertEqual(0, self._queue(db))
        row = dbmod.rows(self.tech_db, "SELECT times_seen,first_seen_at,last_seen_at FROM unlinked_documents")[0]
        self.assertEqual(3, row["times_seen"])
        self.assertEqual(1, scalar(self.tech_db, "SELECT COUNT(*) FROM unlinked_documents"))

    def test_a_publication_already_in_the_corpus_never_enters_the_queue(self):
        """Elle a déjà un acteur : la file n'a rien à en dire."""
        with connect(self.tech_db) as db:
            upsert_document(db, document_type="publication", title=TITRE,
                            source_url=f"https://doi.org/{DOI}", fingerprint_source=DOI,
                            actor_name="ALPHANOV", doi=DOI)
            self.assertEqual(0, self._queue(db))
        self.assertEqual(0, scalar(self.tech_db, "SELECT COUNT(*) FROM unlinked_documents"))

    def test_a_publication_already_judged_off_topic_never_enters_the_queue(self):
        """Une décision de lecture vaut aussi pour la file : un erratum ou une annonce de source
        écartée à la main ne doit pas revenir par cette porte-là."""
        with connect(self.tech_db) as db:
            exclude_document(db, fingerprint_source=DOI, title=TITRE,
                             reason="source : architecture MOPA", excluded_by="audit")
            self.assertEqual(0, self._queue(db))
        self.assertEqual(0, scalar(self.tech_db, "SELECT COUNT(*) FROM unlinked_documents"))

    def test_a_tracked_signatory_sends_it_back_to_the_corpus(self):
        with connect(self.tech_db) as db:
            self._queue(db)
            promu = resolve_unlinked_document(
                db, fingerprint=EMPREINTE,
                institutions=["Fraunhofer Institute for Laser Technology", "RWTH Aachen University"],
                countries=["DE"], actor_name="Fraunhofer ILT",
            )
        self.assertTrue(promu)
        self.assertEqual(0, scalar(self.tech_db, "SELECT COUNT(*) FROM unlinked_documents"))
        document = dbmod.rows(self.tech_db, "SELECT actor_name,title,doi,abstract FROM documents")[0]
        self.assertEqual("Fraunhofer ILT", document["actor_name"])
        self.assertEqual(TITRE, document["title"])
        self.assertEqual(DOI, document["doi"])
        # Le résumé suit la publication : il est déjà collecté, le reperdre obligerait à
        # reclasser le document sur son seul titre.
        self.assertEqual("Abstract de test.", document["abstract"])

    def test_without_a_tracked_signatory_it_stays_and_keeps_its_institutions(self):
        with connect(self.tech_db) as db:
            self._queue(db)
            promu = resolve_unlinked_document(
                db, fingerprint=EMPREINTE,
                institutions=["Shanghai Jiao Tong University"], countries=["CN"],
            )
        self.assertFalse(promu)
        self.assertEqual(0, scalar(self.tech_db, "SELECT COUNT(*) FROM documents"))
        row = dbmod.rows(self.tech_db, "SELECT institutions,countries,resolved_at FROM unlinked_documents")[0]
        self.assertEqual('["Shanghai Jiao Tong University"]', row["institutions"])
        self.assertEqual('["CN"]', row["countries"])
        self.assertIsNotNone(row["resolved_at"])

    def test_an_unresolved_row_says_so_rather_than_claiming_no_affiliation(self):
        """NULL et « liste vide » ne disent pas la même chose : l'un veut dire « pas encore
        demandé », l'autre « demandé, et OpenAlex ne dépose aucune affiliation »."""
        with connect(self.tech_db) as db:
            self._queue(db)
        row = dbmod.rows(self.tech_db, "SELECT institutions,countries,resolved_at FROM unlinked_documents")[0]
        self.assertIsNone(row["institutions"])
        self.assertIsNone(row["countries"])
        self.assertIsNone(row["resolved_at"])

    def test_resolving_an_unknown_fingerprint_is_a_no_op(self):
        with connect(self.tech_db) as db:
            self.assertFalse(resolve_unlinked_document(
                db, fingerprint="0" * 64, institutions=["X"], countries=["FR"], actor_name="ALPHANOV"))
        self.assertEqual(0, scalar(self.tech_db, "SELECT COUNT(*) FROM documents"))


if __name__ == "__main__":
    unittest.main()
