"""Tests pour le chantier 4 (fiabiliser la preuve) : séparation verbatim/reformulation
(is_verbatim) et dates individuelles (source_date), côté scrapers.py/db.py.

Le côté hybrid.py (extraction de la date depuis le HTML/PDF) est couvert dans
tests/test_hybrid.py::PublishedDateExtractionTests.
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
from db import classify_evidence_type
from hybrid import ContentBlock
from scrapers import _candidate, _offer_candidates, _upsert_market_candidate, _upsert_offer_candidate


class CandidateProvenanceTests(unittest.TestCase):
    def test_candidate_is_always_verbatim_and_carries_the_given_source_date(self):
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of medical stents is performed on a pilot line.",
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/medical", "Medical", block, source_date="2024-05-01")
        self.assertIs(True, fact["is_verbatim"])
        self.assertEqual("2024-05-01", fact["source_date"])
        self.assertIn(fact["quote"], block.text)  # a genuine substring, never a paraphrase

    def test_candidate_source_date_defaults_to_none_when_not_given(self):
        block = ContentBlock(
            heading="Stents",
            text="Femtosecond laser drilling of medical stents is performed on a pilot line.",
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/medical", "Medical", block)
        self.assertIsNone(fact["source_date"])

    def test_offer_candidates_are_verbatim_and_carry_source_date(self):
        block = ContentBlock(
            heading="Services",
            text="We provide femtosecond laser micromachining services for industrial customers.",
            path="main > article",
        )
        offers = _offer_candidates("Example", "https://example.test/services", "Services", block, page_type="service", source_date="2024-02-20")
        self.assertTrue(offers)
        for offer in offers:
            self.assertIs(True, offer["is_verbatim"])
            self.assertEqual("2024-02-20", offer["source_date"])


class UpsertPersistenceTests(unittest.TestCase):
    def _fresh_market_db(self, tmp: Path) -> Path:
        market_db = Path(tmp) / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
            patch.object(dbmod, "MARKET_DB", market_db),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
        return market_db

    def test_market_candidate_persists_source_date_and_is_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(
                heading="Stents", text="Femtosecond laser drilling of medical stents is performed on a pilot line.", path="main > a",
            )
            candidate = _candidate("Example", "https://example.test/medical", "Medical", block, source_date="2024-05-01")
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, candidate)
                row = db.execute("SELECT id,source_date,is_verbatim FROM evidence WHERE fact_key=?", (candidate["fact_key"],)).fetchone()
                source_row = db.execute("SELECT source_date,is_verbatim FROM evidence_sources WHERE evidence_id=?", (row["id"],)).fetchone()
            self.assertEqual("2024-05-01", row["source_date"])
            self.assertEqual(1, row["is_verbatim"])
            self.assertEqual("2024-05-01", source_row["source_date"])
            self.assertEqual(1, source_row["is_verbatim"])

    def test_market_candidate_update_keeps_existing_date_when_new_one_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(
                heading="Stents", text="Femtosecond laser drilling of medical stents is performed on a pilot line.", path="main > a",
            )
            first = _candidate("Example", "https://example.test/medical", "Medical", block, source_date="2024-05-01")
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, first)

            # A later pass with no source_date (e.g. cached blocks, no observation stamp
            # threaded through) and higher confidence must not blank out a date we already have.
            second = dict(first)
            second["source_date"] = None
            second["confidence"] = min(0.98, first["confidence"] + 0.05)
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, second)
                row = db.execute("SELECT source_date FROM evidence WHERE fact_key=?", (first["fact_key"],)).fetchone()
            self.assertEqual("2024-05-01", row["source_date"])

    def test_offer_candidate_persists_source_date_and_is_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(
                heading="Services", text="We provide femtosecond laser micromachining services for customers.", path="main > a",
            )
            offer = _offer_candidates("Example", "https://example.test/services", "Services", block, page_type="service", source_date="2024-02-20")[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, offer)
                row = db.execute("SELECT source_date,is_verbatim FROM offers WHERE fact_key=?", (offer["fact_key"],)).fetchone()
            self.assertEqual("2024-02-20", row["source_date"])
            self.assertEqual(1, row["is_verbatim"])


class OfferStructuralRoutingTests(unittest.TestCase):
    """§10.7 audit veille (30/08/2026, correction de §9.9) : field_confidence n'a pas la
    résolution nécessaire pour piloter la file de revue des offres -- review_status est
    désormais calculé sur des critères structurels (_offer_review_reasons), plus jamais
    'accepted' d'office à l'insertion."""

    def _fresh_market_db(self, tmp: Path) -> Path:
        market_db = Path(tmp) / "market.db"
        with (
            patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
            patch.object(dbmod, "MARKET_DB", market_db),
            patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
        ):
            dbmod.init_databases()
        return market_db

    def _confirmed_block(self) -> ContentBlock:
        # Predicate present ("we provide"), operation present (drilling), page_type will be
        # "service" (passed separately) -- carries none of the 4 non-source-count reasons, so
        # only the source-count criterion decides review vs accepted here.
        return ContentBlock(
            heading="Services",
            text="We provide femtosecond laser drilling services for medical customers.",
            path="main > article",
        )

    def test_brand_new_offer_with_a_single_source_is_routed_to_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            offer = _offer_candidates(
                "Example", "https://example.test/services", "Services", self._confirmed_block(), page_type="service",
            )[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, offer)
                row = db.execute("SELECT review_status FROM offers WHERE fact_key=?", (offer["fact_key"],)).fetchone()
            self.assertEqual("review", row["review_status"])

    def test_a_second_independent_source_promotes_the_offer_to_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            first = _offer_candidates(
                "Example", "https://example.test/services", "Services", self._confirmed_block(), page_type="service",
            )[0]
            second = _offer_candidates(
                "Example", "https://example.test/en/services", "Services (EN)", self._confirmed_block(), page_type="service",
            )[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, first)
                _upsert_offer_candidate(db, second)
                row = db.execute("SELECT review_status FROM offers WHERE fact_key=?", (first["fact_key"],)).fetchone()
            self.assertEqual("accepted", row["review_status"])

    def test_recrawling_the_same_url_does_not_count_as_a_second_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            offer = _offer_candidates(
                "Example", "https://example.test/services", "Services", self._confirmed_block(), page_type="service",
            )[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, offer)
                # Same URL, slightly higher confidence (e.g. re-crawled, refined match) --
                # must not be mistaken for independent corroboration.
                again = dict(offer)
                again["confidence"] = min(0.97, offer["confidence"] + 0.05)
                _upsert_offer_candidate(db, again)
                row = db.execute("SELECT review_status FROM offers WHERE fact_key=?", (offer["fact_key"],)).fetchone()
            self.assertEqual("review", row["review_status"])

    def test_quote_without_predicate_stays_in_review_even_with_two_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            block = ContentBlock(heading="Services", text="Femtosecond laser drilling services.", path="main > article")
            first = _offer_candidates("Example", "https://example.test/a", "Services", block, page_type="service")[0]
            second = _offer_candidates("Example", "https://example.test/b", "Services", block, page_type="service")[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, first)
                _upsert_offer_candidate(db, second)
                row = db.execute("SELECT review_status FROM offers WHERE fact_key=?", (first["fact_key"],)).fetchone()
            self.assertEqual("review", row["review_status"])

    def test_homepage_citation_stays_in_review_even_with_two_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = self._fresh_market_db(tmp)
            first = _offer_candidates(
                "Example", "https://example.test/services", "Services", self._confirmed_block(), page_type="service",
            )[0]
            # Second citation confirms a distinct source, but is itself a homepage citation --
            # reasons are recomputed from THIS pass's candidate, so it must not flip to accepted.
            second = _offer_candidates(
                "Example", "https://example.test/", "Home", self._confirmed_block(), page_type="service",
            )[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, first)
                _upsert_offer_candidate(db, second)
                row = db.execute("SELECT review_status FROM offers WHERE fact_key=?", (first["fact_key"],)).fetchone()
            self.assertEqual("review", row["review_status"])


class LegacySeedBackfillTests(unittest.TestCase):
    def test_manually_entered_rows_are_backfilled_as_not_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                stamp = dbmod.utc_now()
                with dbmod.connect(market_db) as db:
                    # Distinct market/component so the two rows are genuinely different facts
                    # (not just different rows for the same fact, which the dedup migration
                    # further down would legitimately merge into one).
                    # extraction_mode IS NULL: exactly the audit's signal for a hand-entered row.
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,source_url,quote,source_group,fingerprint,
                               review_status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        ("Example", "existing", "Médical", "Stents", "https://example.test/", "Une reformulation en français.", "grp", "manual-fp", "accepted", stamp, stamp),
                    )
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,source_url,quote,source_group,fingerprint,
                               review_status,created_at,updated_at,extraction_mode
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("Example", "existing", "Batteries", "Électrodes", "https://example.test/", "a verbatim excerpt", "grp2", "scraped-fp", "accepted", stamp, stamp, "block-rules"),
                    )
                # Re-running init_databases() (as the app does on every startup) must not
                # re-flip a row that was already correctly backfilled, nor touch scraped rows.
                dbmod.init_databases()

            with dbmod.connect(market_db) as db:
                manual = db.execute("SELECT is_verbatim FROM evidence WHERE fingerprint='manual-fp'").fetchone()
                scraped = db.execute("SELECT is_verbatim FROM evidence WHERE fingerprint='scraped-fp'").fetchone()
            self.assertEqual(0, manual["is_verbatim"])
            self.assertEqual(1, scraped["is_verbatim"])


class ClassifyEvidenceTypeTests(unittest.TestCase):
    def test_a_measured_number_with_a_technical_unit_is_a_proof(self):
        self.assertEqual("proof", classify_evidence_type("We drill 300 holes per second on titanium."))
        self.assertEqual("proof", classify_evidence_type("Achieves a spot size of 5 µm on glass."))
        self.assertEqual("proof", classify_evidence_type("Pulse duration down to 180 fs."))

    def test_a_recognized_certification_code_is_a_proof(self):
        self.assertEqual("proof", classify_evidence_type("Our cleanroom is certified ISO 13485."))
        self.assertEqual("proof", classify_evidence_type("AS9100 certified for aerospace components."))

    def test_generic_marketing_language_is_a_claim(self):
        self.assertEqual("claim", classify_evidence_type("We are experts in femtosecond laser micromachining."))
        self.assertEqual("claim", classify_evidence_type("Our team knows how to process advanced materials."))

    def test_empty_or_none_quote_is_a_claim_not_a_crash(self):
        self.assertEqual("claim", classify_evidence_type(""))
        self.assertEqual("claim", classify_evidence_type(None))


class CandidateStageArchitectureEvidenceTypeTests(unittest.TestCase):
    def test_candidate_carries_no_maturity_field_at_all(self):
        """Ce test vérifiait que `stage` ne concaténait plus architecture/matériau à l'étiquette
        de maturité (chantier 4). L'échelle de maturité ayant été retirée du marché le
        2026-10-06, il n'y a plus ni `stage` ni `maturity` à surveiller -- et c'est précisément
        cette absence qu'il faut verrouiller, pour qu'un recâblage ne passe pas inaperçu.

        L'architecture, elle, reste détectée dans son propre champ : c'était déjà l'acquis du
        chantier 4, et il survit au retrait.
        """
        block = ContentBlock(
            heading="TGV interposers",
            text=(
                "Femtosecond laser drilling of a TGV glass interposer for semiconductor packaging "
                "is performed in mass production."
            ),
            path="main > article",
        )
        fact = _candidate("Example", "https://example.test/semi", "Semi", block)
        self.assertIsNotNone(fact)
        for removed in ("stage", "maturity", "maturity_class"):
            self.assertNotIn(removed, fact)
        self.assertEqual("TGV", fact["architecture"])
        # "mass production" est bien lu, mais comme un bucket, plus comme un niveau.
        self.assertEqual("existing", fact["bucket"])

    def test_market_candidate_persists_architecture_and_evidence_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
            block = ContentBlock(
                heading="TGV",
                text=(
                    "Femtosecond laser drilling of a TGV glass interposer for semiconductor packaging "
                    "is performed at 300 holes per second in mass production."
                ),
                path="main > article",
            )
            candidate = _candidate("Example", "https://example.test/semi", "Semi", block)
            with dbmod.connect(market_db) as db:
                _upsert_market_candidate(db, candidate)
                row = db.execute(
                    "SELECT architecture,evidence_type,industrial_stage,maturity_level FROM evidence WHERE fact_key=?", (candidate["fact_key"],),
                ).fetchone()
            self.assertEqual("TGV", row["architecture"])
            self.assertEqual("proof", row["evidence_type"])
            # Les deux colonnes de maturité subsistent en base (les vider demanderait une
            # réécriture de table) mais ne sont plus jamais ALIMENTÉES : une ligne neuve les
            # laisse à NULL.
            self.assertIsNone(row["industrial_stage"])
            self.assertIsNone(row["maturity_level"])

    def test_offer_candidate_persists_evidence_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
            block = ContentBlock(
                heading="Services",
                text="We provide femtosecond laser micromachining at 300 holes per second for customers.",
                path="main > article",
            )
            offer = _offer_candidates("Example", "https://example.test/services", "Services", block, page_type="service")[0]
            with dbmod.connect(market_db) as db:
                _upsert_offer_candidate(db, offer)
                row = db.execute("SELECT evidence_type FROM offers WHERE fact_key=?", (offer["fact_key"],)).fetchone()
            self.assertEqual("proof", row["evidence_type"])


class IndustrialStageMigrationTests(unittest.TestCase):
    def test_legacy_concatenated_rows_are_split_into_architecture_and_a_clean_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                stamp = dbmod.utc_now()
                with dbmod.connect(market_db) as db:
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,operation,industrial_stage,source_url,quote,
                               source_group,fingerprint,review_status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "Example", "existing", "Semi-conducteurs", "Wafers", "Micro-usinage",
                            "Prototype | Architecture: TGV | Matériau: Saphir", "https://example.test/",
                            "quote", "grp", "legacy-fp", "accepted", stamp, stamp,
                        ),
                    )
                # Re-running init_databases() (as the app does on every startup) is exactly how
                # this migration actually fires -- it must also be idempotent on a second pass.
                dbmod.init_databases()
                dbmod.init_databases()

            with dbmod.connect(market_db) as db:
                row = db.execute("SELECT industrial_stage,architecture FROM evidence WHERE fingerprint='legacy-fp'").fetchone()
            self.assertEqual("Prototype", row["industrial_stage"])
            self.assertEqual("TGV", row["architecture"])

    def test_a_row_without_an_architecture_segment_is_just_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                stamp = dbmod.utc_now()
                with dbmod.connect(market_db) as db:
                    db.execute(
                        """INSERT INTO evidence(
                               actor_name,bucket,market,component,operation,industrial_stage,source_url,quote,
                               source_group,fingerprint,review_status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "Example", "existing", "Médical", "Stents", "Microperçage",
                            "Production | Matériau: Titane", "https://example.test/",
                            "quote", "grp2", "legacy-fp-2", "accepted", stamp, stamp,
                        ),
                    )
                dbmod.init_databases()

            with dbmod.connect(market_db) as db:
                row = db.execute("SELECT industrial_stage,architecture FROM evidence WHERE fingerprint='legacy-fp-2'").fetchone()
            self.assertEqual("Production", row["industrial_stage"])
            self.assertIsNone(row["architecture"])

    def test_placeholder_stage_values_are_normalized_by_bucket(self):
        """Regression test for the audit's ontologie marché finding: legacy rows carrying
        '<UNKNOWN>', a stringified 'None', 'commercialized' or a stray project name in
        industrial_stage must be normalized to a canonical MATURITY_RULES label, using the
        row's own bucket (never the quote) to decide which one."""
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            market_db = Path(tmp) / "market.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", market_db),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                stamp = dbmod.utc_now()
                # Distinct `component` per row: market_fact_key() is derived from
                # (actor,bucket,market,component,operation), and _migrate_evidence_fact_model()
                # (already exercised by other tests) consolidates rows that share a fact_key down
                # to one representative -- irrelevant to what this test checks, so each row here
                # must be its own distinct fact to survive that unrelated migration untouched.
                rows = [
                    ("existing", "Wafers-A", "<UNKNOWN>", "fp-existing-unknown"),
                    ("existing", "Wafers-B", "commercialized", "fp-existing-commercialized"),
                    ("radar", "Wafers-C", "<UNKNOWN>", "fp-radar-unknown"),
                    ("radar", "Wafers-D", "None", "fp-radar-none"),
                    ("radar", "Wafers-E", "projet LUMEN", "fp-radar-project-name"),
                ]
                with dbmod.connect(market_db) as db:
                    for bucket, component, stage, fingerprint in rows:
                        db.execute(
                            """INSERT INTO evidence(
                                   actor_name,bucket,market,component,operation,industrial_stage,source_url,quote,
                                   source_group,fingerprint,review_status,created_at,updated_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                "Example", bucket, "Semi-conducteurs", component, "Micro-usinage",
                                stage, "https://example.test/", "quote", "grp", fingerprint, "accepted", stamp, stamp,
                            ),
                        )
                # Same idempotency requirement as the concatenation migration above.
                dbmod.init_databases()
                dbmod.init_databases()

            with dbmod.connect(market_db) as db:
                stages = {
                    row["fingerprint"]: row["industrial_stage"]
                    for row in db.execute("SELECT fingerprint,industrial_stage FROM evidence WHERE fingerprint LIKE 'fp-%'")
                }
            self.assertEqual("Production", stages["fp-existing-unknown"])
            self.assertEqual("Production", stages["fp-existing-commercialized"])
            self.assertEqual("Maturité industrielle non déterminée", stages["fp-radar-unknown"])
            self.assertEqual("Maturité industrielle non déterminée", stages["fp-radar-none"])
            self.assertEqual("Maturité industrielle non déterminée", stages["fp-radar-project-name"])


if __name__ == "__main__":
    unittest.main()
