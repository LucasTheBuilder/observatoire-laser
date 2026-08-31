"""Tests pour la découverte d'acteurs (Lot 3 §3.2, audit veille §4.D, 30/08/2026) :
actor_candidates alimenté par CORDIS (actor_relations non rattachés) et outbound_links (hôte
externe récurrent sur plusieurs sites d'acteurs), avec promotion/rejet vers un vrai acteur.
L'apport OpenAlex (institution co-autrice) est testé séparément dans test_actor_feeds-adjacent
coverage of openalex.py, pas ici : ce fichier couvre le module actor_discovery.py lui-même.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import actor_discovery as ad
import app as appmod
import db as dbmod


def _setup(tmp: str) -> Path:
    actors_db = Path(tmp) / "actors.db"
    with (
        patch.object(dbmod, "ACTORS_DB", actors_db),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
    ):
        dbmod.init_databases()
        with dbmod.connect(actors_db) as db:
            db.execute("DELETE FROM actors")
    return actors_db


class RootDomainTests(unittest.TestCase):
    def test_subdomain_shares_root_with_bare_domain(self):
        self.assertEqual(ad._root_domain("linkedin.com"), ad._root_domain("de.linkedin.com"))
        self.assertEqual(ad._root_domain("linkedin.com"), ad._root_domain("fr.linkedin.com"))

    def test_distinct_domains_have_distinct_roots(self):
        self.assertNotEqual(ad._root_domain("example.com"), ad._root_domain("otherexample.com"))

    def test_www_prefix_is_stripped(self):
        self.assertEqual("example.com", ad._root_domain("www.example.com"))


class UpsertActorCandidateTests(unittest.TestCase):
    def test_first_occurrence_creates_a_candidate_with_score_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                added = ad.upsert_actor_candidate(db, "New Laser Co", "cordis", context="consortium partner", source_url="https://cordis.europa.eu/project/id/123")
            self.assertEqual(1, added)
            row = dbmod.rows(actors_db, "SELECT name,score,review_status FROM actor_candidates")[0]
            self.assertEqual("New Laser Co", row["name"])
            self.assertEqual(1, row["score"])
            self.assertEqual("pending", row["review_status"])

    def test_second_source_type_raises_score_to_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                ad.upsert_actor_candidate(db, "New Laser Co", "cordis", source_url="https://cordis.europa.eu/project/id/123")
                ad.upsert_actor_candidate(db, "New Laser Co", "outbound_link", source_url="https://newlaserco.example/")
            row = dbmod.rows(actors_db, "SELECT score FROM actor_candidates")[0]
            self.assertEqual(2, row["score"])

    def test_repeated_same_source_and_url_does_not_raise_score_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                ad.upsert_actor_candidate(db, "New Laser Co", "cordis", source_url="https://cordis.europa.eu/project/id/123")
                added_again = ad.upsert_actor_candidate(db, "New Laser Co", "cordis", source_url="https://cordis.europa.eu/project/id/123")
            self.assertEqual(0, added_again)
            row = dbmod.rows(actors_db, "SELECT score FROM actor_candidates")[0]
            self.assertEqual(1, row["score"])

    def test_different_name_spelling_normalizes_to_the_same_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                ad.upsert_actor_candidate(db, "New Laser Co.", "cordis", source_url="https://a.test/")
                ad.upsert_actor_candidate(db, "NEW LASER CO", "outbound_link", source_url="https://b.test/")
            count = dbmod.scalar(actors_db, "SELECT COUNT(*) FROM actor_candidates")
            self.assertEqual(1, count)

    def test_country_and_suggested_url_are_captured_when_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                ad.upsert_actor_candidate(
                    db, "New Laser Co", "cordis", source_url="https://a.test/",
                    country="US", suggested_official_url="https://newlaserco.example/",
                )
            row = dbmod.rows(actors_db, "SELECT country,suggested_official_url FROM actor_candidates")[0]
            self.assertEqual("US", row["country"])
            self.assertEqual("https://newlaserco.example/", row["suggested_official_url"])

    def test_country_is_never_overwritten_by_a_later_occurrence_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                ad.upsert_actor_candidate(db, "New Laser Co", "cordis", source_url="https://a.test/", country="US")
                ad.upsert_actor_candidate(db, "New Laser Co", "outbound_link", source_url="https://b.test/")
            row = dbmod.rows(actors_db, "SELECT country FROM actor_candidates")[0]
            self.assertEqual("US", row["country"])

    def test_first_known_country_wins_when_a_later_occurrence_disagrees(self):
        # COALESCE never overwrites once known -- deliberately conservative rather than
        # arbitrating between two sources that disagree.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                ad.upsert_actor_candidate(db, "New Laser Co", "cordis", source_url="https://a.test/", country="US")
                ad.upsert_actor_candidate(db, "New Laser Co", "outbound_link", source_url="https://b.test/", country="JP")
            row = dbmod.rows(actors_db, "SELECT country FROM actor_candidates")[0]
            self.assertEqual("US", row["country"])


class DiscoverFromCordisTests(unittest.TestCase):
    def test_unmatched_relation_becomes_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Known Actor", "France", "Test", "https://known.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actor_relations(actor_id,related_actor_id,related_name,relation_type,source_url,created_at) VALUES(?,?,?,?,?,?)",
                    (actor_id, None, "Unmatched Consortium Partner", "partner", "https://cordis.europa.eu/project/id/999", dbmod.utc_now()),
                )
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(1, result["cordis_candidates"])
            row = dbmod.rows(actors_db, "SELECT name FROM actor_candidates")[0]
            self.assertEqual("Unmatched Consortium Partner", row["name"])

    def test_matched_relation_never_becomes_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Known Actor", "France", "Test", "https://known.test/")
                other_id = dbmod.create_actor("Other Known Actor", "Germany", "Test", "https://other.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actor_relations(actor_id,related_actor_id,related_name,relation_type,source_url,created_at) VALUES(?,?,?,?,?,?)",
                    (actor_id, other_id, "Other Known Actor", "partner", "https://cordis.europa.eu/project/id/999", dbmod.utc_now()),
                )
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(0, result["cordis_candidates"])

    def test_related_name_matching_a_known_actor_by_name_is_excluded_even_if_unmatched_in_db(self):
        # Safety net: a relation somehow left related_actor_id NULL despite the name matching a
        # tracked actor -- must never surface as a "new" candidate.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor_id = dbmod.create_actor("Known Actor", "France", "Test", "https://known.test/")
                dbmod.create_actor("Already Tracked Org", "Germany", "Test", "https://tracked.test/")
            with dbmod.connect(actors_db) as db:
                db.execute(
                    "INSERT INTO actor_relations(actor_id,related_actor_id,related_name,relation_type,source_url,created_at) VALUES(?,?,?,?,?,?)",
                    (actor_id, None, "Already Tracked Org", "partner", "https://cordis.europa.eu/project/id/999", dbmod.utc_now()),
                )
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(0, result["cordis_candidates"])


class DiscoverFromOutboundLinksTests(unittest.TestCase):
    def _insert_outbound(self, db, actor_id: int, source_url: str, target_url: str, target_host: str, label: str = "") -> None:
        source_id = db.execute("INSERT INTO actor_sources(actor_id,url,active) VALUES(?,?,1)", (actor_id, source_url)).lastrowid
        db.execute(
            "INSERT INTO outbound_links(source_id,target_url,target_host,label,first_seen_at) VALUES(?,?,?,?,?)",
            (source_id, target_url, target_host, label, dbmod.utc_now()),
        )

    def test_host_appearing_on_two_distinct_actors_becomes_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor1 = dbmod.create_actor("Actor One", "France", "Test", "https://one.test/")
                actor2 = dbmod.create_actor("Actor Two", "Germany", "Test", "https://two.test/")
            with dbmod.connect(actors_db) as db:
                self._insert_outbound(db, actor1, "https://one.test/partners", "https://partner-corp.example/about", "partner-corp.example", "Partner Corp")
                self._insert_outbound(db, actor2, "https://two.test/partners", "https://partner-corp.example/about", "partner-corp.example", "Partner Corp")
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(1, result["outbound_link_candidates"])
            row = dbmod.rows(actors_db, "SELECT name FROM actor_candidates")[0]
            self.assertEqual("Partner Corp", row["name"])

    def test_host_appearing_on_only_one_actor_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor1 = dbmod.create_actor("Actor One", "France", "Test", "https://one.test/")
            with dbmod.connect(actors_db) as db:
                self._insert_outbound(db, actor1, "https://one.test/partners", "https://solo-corp.example/", "solo-corp.example")
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(0, result["outbound_link_candidates"])

    def test_social_media_host_is_never_a_candidate_even_if_recurrent(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor1 = dbmod.create_actor("Actor One", "France", "Test", "https://one.test/")
                actor2 = dbmod.create_actor("Actor Two", "Germany", "Test", "https://two.test/")
            with dbmod.connect(actors_db) as db:
                self._insert_outbound(db, actor1, "https://one.test/", "https://linkedin.com/company/one", "linkedin.com")
                self._insert_outbound(db, actor2, "https://two.test/", "https://linkedin.com/company/two", "linkedin.com")
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(0, result["outbound_link_candidates"])

    def test_country_subdomain_of_a_denylisted_platform_is_also_excluded(self):
        # Real production bug (30/08/2026): de.linkedin.com slipped past the denylist because
        # the check was an exact host match, not a root-domain comparison -- LinkedIn's
        # country-specific subdomains (de./fr./...) are a different host string but the same
        # root domain, and the fix must catch those too, not just the bare "linkedin.com" case
        # already covered by test_social_media_host_is_never_a_candidate_even_if_recurrent.
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor1 = dbmod.create_actor("Actor One", "France", "Test", "https://one.test/")
                actor2 = dbmod.create_actor("Actor Two", "Germany", "Test", "https://two.test/")
            with dbmod.connect(actors_db) as db:
                self._insert_outbound(db, actor1, "https://one.test/", "https://de.linkedin.com/company/one", "de.linkedin.com")
                self._insert_outbound(db, actor2, "https://two.test/", "https://de.linkedin.com/company/two", "de.linkedin.com")
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(0, result["outbound_link_candidates"])

    def test_host_matching_a_known_actor_domain_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with patch.object(dbmod, "ACTORS_DB", actors_db):
                actor1 = dbmod.create_actor("Actor One", "France", "Test", "https://one.test/")
                actor2 = dbmod.create_actor("Actor Two", "Germany", "Test", "https://two.test/")
                dbmod.create_actor("Already Tracked", "France", "Test", "https://tracked.test/")
            with dbmod.connect(actors_db) as db:
                self._insert_outbound(db, actor1, "https://one.test/", "https://tracked.test/products", "tracked.test")
                self._insert_outbound(db, actor2, "https://two.test/", "https://tracked.test/products", "tracked.test")
            with patch.object(ad, "ACTORS_DB", actors_db):
                result = ad.discover_actor_candidates()
            self.assertEqual(0, result["outbound_link_candidates"])


class PromoteAndRejectCandidateTests(unittest.TestCase):
    def test_promote_creates_a_real_actor_with_candidate_review_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("New Laser Co", "NEW LASER CO", 2, dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                result = ad.promote_candidate(candidate_id, official_url="https://newlaserco.example/", country="France", role="Prestataire", reviewed_by="lucas")
            actor_row = dbmod.rows(actors_db, "SELECT name,review_status,official_url FROM actors WHERE id=?", (result["actor_id"],))[0]
            self.assertEqual("New Laser Co", actor_row["name"])
            self.assertEqual("candidate", actor_row["review_status"])
            self.assertEqual("https://newlaserco.example/", actor_row["official_url"])
            candidate_row = dbmod.rows(actors_db, "SELECT review_status,promoted_actor_id,reviewed_by FROM actor_candidates WHERE id=?", (candidate_id,))[0]
            self.assertEqual("promoted", candidate_row["review_status"])
            self.assertEqual(result["actor_id"], candidate_row["promoted_actor_id"])
            self.assertEqual("lucas", candidate_row["reviewed_by"])

    def test_promote_falls_back_to_suggested_official_url_and_country_when_not_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    """INSERT INTO actor_candidates(name,normalized_name,score,country,suggested_official_url,first_seen_at,last_seen_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("Korea Institute of Photonics", "KOREA INSTITUTE OF PHOTONICS", 1, "KR", "https://kip.example.kr/", dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                result = ad.promote_candidate(candidate_id, role="Prestataire")
            actor_row = dbmod.rows(actors_db, "SELECT country,official_url FROM actors WHERE id=?", (result["actor_id"],))[0]
            self.assertEqual("KR", actor_row["country"])
            self.assertEqual("https://kip.example.kr/", actor_row["official_url"])

    def test_promote_explicit_values_override_the_suggested_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    """INSERT INTO actor_candidates(name,normalized_name,score,country,suggested_official_url,first_seen_at,last_seen_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("Korea Institute of Photonics", "KOREA INSTITUTE OF PHOTONICS", 1, "KR", "https://kip.example.kr/", dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                result = ad.promote_candidate(
                    candidate_id, role="Prestataire", country="South Korea", official_url="https://real-site.example.kr/",
                )
            actor_row = dbmod.rows(actors_db, "SELECT country,official_url FROM actors WHERE id=?", (result["actor_id"],))[0]
            self.assertEqual("South Korea", actor_row["country"])
            self.assertEqual("https://real-site.example.kr/", actor_row["official_url"])

    def test_promote_without_official_url_and_no_suggestion_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("New Laser Co", "NEW LASER CO", 1, dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                with self.assertRaises(ValueError):
                    ad.promote_candidate(candidate_id, role="Prestataire", country="France")

    def test_promote_without_country_and_no_suggestion_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("New Laser Co", "NEW LASER CO", 1, dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                with self.assertRaises(ValueError):
                    ad.promote_candidate(candidate_id, role="Prestataire", official_url="https://newlaserco.example/")

    def test_promote_an_already_reviewed_candidate_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,review_status,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                    ("New Laser Co", "NEW LASER CO", 2, "rejected", dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                with self.assertRaises(ValueError):
                    ad.promote_candidate(candidate_id, official_url="https://newlaserco.example/", country="France", role="Prestataire")

    def test_reject_records_reason_and_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("Not Actually Relevant", "NOT ACTUALLY RELEVANT", 1, dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            with patch.object(ad, "ACTORS_DB", actors_db):
                ad.reject_candidate(candidate_id, reviewed_by="lucas", reject_reason="not a laser company")
            row = dbmod.rows(actors_db, "SELECT review_status,reject_reason,reviewed_by FROM actor_candidates WHERE id=?", (candidate_id,))[0]
            self.assertEqual("rejected", row["review_status"])
            self.assertEqual("not a laser company", row["reject_reason"])
            self.assertEqual("lucas", row["reviewed_by"])


class ActorCandidateEndpointTests(unittest.TestCase):
    def test_list_endpoint_returns_pending_candidates_with_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("New Laser Co", "NEW LASER CO", 1, dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
                db.execute(
                    "INSERT INTO actor_candidate_sources(candidate_id,source_type,context,source_url,created_at) VALUES(?,?,?,?,?)",
                    (candidate_id, "cordis", "consortium partner", "https://cordis.europa.eu/project/id/123", dbmod.utc_now()),
                )
            with patch.object(appmod, "ACTORS_DB", actors_db):
                result = appmod.actor_candidates_list(status="pending")
            self.assertEqual(1, len(result))
            self.assertEqual("New Laser Co", result[0]["name"])
            self.assertEqual(1, len(result[0]["sources"]))
            self.assertEqual("cordis", result[0]["sources"][0]["source_type"])

    def test_promote_endpoint_creates_a_real_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            with dbmod.connect(actors_db) as db:
                candidate_id = db.execute(
                    "INSERT INTO actor_candidates(name,normalized_name,score,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("New Laser Co", "NEW LASER CO", 1, dbmod.utc_now(), dbmod.utc_now()),
                ).lastrowid
            payload = appmod.PromoteCandidateRequest(official_url="https://newlaserco.example/", country="France", role="Prestataire", reviewed_by="lucas")
            with patch.object(ad, "ACTORS_DB", actors_db), patch.object(dbmod, "ACTORS_DB", actors_db):
                result = appmod.actor_candidate_promote(candidate_id, payload)
            self.assertEqual("promoted", result["status"])
            actor_row = dbmod.rows(actors_db, "SELECT name FROM actors WHERE id=?", (result["actor_id"],))[0]
            self.assertEqual("New Laser Co", actor_row["name"])

    def test_reject_endpoint_raises_400_for_unknown_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = _setup(tmp)
            payload = appmod.RejectCandidateRequest(reviewed_by="lucas", reject_reason="not relevant")
            with patch.object(ad, "ACTORS_DB", actors_db):
                with self.assertRaises(appmod.HTTPException) as ctx:
                    appmod.actor_candidate_reject(999, payload)
            self.assertEqual(400, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
