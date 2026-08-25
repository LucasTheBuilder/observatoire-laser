from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ACTORS_DB = DATA_DIR / "actors.db"
MARKET_DB = DATA_DIR / "market.db"
TECH_DB = DATA_DIR / "technology.db"
BACKUP_RETENTION_COUNT = int(os.getenv("BACKUP_RETENTION_COUNT", "14"))


ACTORS = [
    ("ALPHANOV", "France", "Centre technologique - procédés laser & micro-usinage", 1, "https://www.alphanov.com"),
    ("MANUTECH USD", "France", "Plateforme technologique femtoseconde - texturation/fonctionnalisation", 1, "https://www.manutech-usd.fr"),
    ("HEF", "France", "Référence interne - groupe industriel", 1, "https://hef.group"),
    ("LASEA", "Belgique", "Systèmes femtoseconde & développement d'applications", 1, "https://www.lasea.eu"),
    ("IREPA LASER", "France", "Centre technologique - développement, industrialisation & production laser", 0, "https://www.irepa-laser.com"),
    ("Pulsar Photonics", "Allemagne", "Développement d'applications USP & fabrication sous contrat", 0, "https://www.pulsar-photonics.de"),
    ("Lightmotif", "Pays-Bas", "Micro-usinage USP & texturation - process development / contract manufacturing", 0, "https://www.lightmotif.nl"),
    ("FEMTO Engineering", "France", "Centre d'ingénierie - micro/nano-usinage femtoseconde", 0, "https://www.femto-engineering.fr"),
    ("Workshop of Photonics", "Lituanie", "Microfabrication femtoseconde - services, production & équipements", 0, "https://wophotonics.com"),
    ("LightFab", "Allemagne", "SLE / microfabrication 3D du verre", 0, "https://lightfab.de"),
    ("Femtika", "Lituanie", "Microfabrication 3D femtoseconde - équipements & contract manufacturing", 0, "https://femtika.com"),
    ("Micreon", "Allemagne", "Contract manufacturing en micro-usinage USP", 0, "https://www.micreon.de"),
    ("OpTek Systems", "Royaume-Uni", "Process development & contract laser micromachining", 0, "https://optek.humaneticsgroup.com"),
    ("Blueacre Technology", "Irlande", "Laser micromachining & Nitinol contract manufacturing - MedTech", 0, "https://blueacretechnology.com"),
    ("Oxford Lasers", "Royaume-Uni", "Contract laser micromachining & process development", 0, "https://oxfordlasers.com"),
    ("3D-Micromac", "Allemagne", "Développement de procédés laser & contract manufacturing", 0, "https://3d-micromac.com"),
    ("Fraunhofer ILT", "Allemagne", "Institut de recherche appliquée - procédés USP", 0, "https://www.ilt.fraunhofer.de/en.html"),
    ("Laser Zentrum Hannover", "Allemagne", "Institut technologique - micromachining USP", 0, "https://www.lzh.de/en"),
    ("FEMTOprint", "Suisse", "CDMO de microfabrication 3D du verre par femtoseconde/SLE", 0, "https://www.femtoprint.ch"),
    ("LightPulse Laser Precision", "Allemagne", "Développement / échantillonnage & micro-usinage USP", 0, "https://www.light-pulse.de/en-gb/"),
]

SEED_SOURCES = [
    ("ALPHANOV", "https://www.alphanov.com/en/collaborative-projects/femtocell-pilot-line-next-generation-gen4-batteries", "application"),
    ("HEF", "https://hef.group/en/glacier-project-femtosecond-laser-and-glass-cutting/", "application"),
    ("Pulsar Photonics", "https://www.pulsar-photonics.de/en/laser-contract-manufacturing/", "service"),
    ("LightFab", "https://lightfab.de/", "application"),
    ("FEMTO Engineering", "https://www.femto-engineering.fr/en/", "service"),
    ("MANUTECH USD", "https://www.manutech-usd.fr/en/", "official"),
]

# Seed values are now kept as canonical dimensions. Generation/application detail belongs in stage/quote,
# not in the component label, so seed and scraped evidence can deduplicate correctly.
LEGACY_SEED_GROUPS = (
    "ap-technologies-blueacre-acquisition-2026",
    "lightfab-glass-microfabrication",
    "femtocell-gen4-pilot-line",
    "glacier-optical-glass",
)

# v3.3.1 no longer injects synthetic/curated market facts at startup.
# Market facts must come from the strict extraction pipeline; legacy seeds are kept only as review material.
SEED_EVIDENCE: list[dict[str, Any]] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path):
    """Open one transaction per ``with`` block: commit only if the block exits normally.

    Any exception raised inside the block skips ``connection.commit()`` entirely, so every
    write made earlier in that same block is rolled back too -- not just the statement that
    raised. This is intentional (each block is atomic), but a loop that writes many independent
    rows inside a single ``with connect(...)`` must catch per-item errors itself if one bad item
    should not discard everything written before it (see scrape_technology in scrapers.py).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _add_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Apply small, additive migrations without replacing the user's databases."""
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _slug(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "non-identifie"


def _normalize_page_type_value(value: str | None) -> str:
    raw = (value or "other").strip().casefold().replace(" ", "_")
    aliases = {
        "applications": "application", "projects": "project", "products": "product",
        "services": "service", "capabilities": "capability", "technologies": "technology",
        "markets": "market", "publications": "publication", "equipments": "equipment",
    }
    return aliases.get(raw, raw or "other")


def _normalize_existing_page_types(db: sqlite3.Connection) -> None:
    """One-way data migration: collapse legacy plural page types to canonical singular values."""
    for legacy, canonical in {
        "applications": "application", "projects": "project", "products": "product",
        "services": "service", "capabilities": "capability", "technologies": "technology",
        "markets": "market", "publications": "publication", "equipments": "equipment",
    }.items():
        db.execute("UPDATE actor_sources SET page_type=? WHERE LOWER(COALESCE(page_type,''))=?", (canonical, legacy))


# Public (no leading underscore): scrapers.py imports this rather than keeping its own copy,
# so language tagging stays identical between actor_sources/evidence_sources (written here) and
# evidence/offers (written by the crawler) instead of silently drifting apart.
def language_from_url(url: str | None) -> str | None:
    path = urlparse(url or "").path.casefold()
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    first = parts[0]
    if first in {"fr", "fr-fr", "france"}:
        return "fr"
    if first in {"en", "en-gb", "en-us"}:
        return "en"
    if first in {"de", "de-de"}:
        return "de"
    return None


def market_fact_key(actor: str, bucket: str, market: str, component: str, operation: str) -> str:
    """Language-independent canonical key for one market application fact."""
    return "|".join(_slug(value) for value in (actor, bucket, market, component, operation))


def application_key(actor: str, market: str, component: str, operation: str) -> str:
    """Language- and maturity-independent identity for one market application.

    Unlike ``market_fact_key``, this deliberately excludes the bucket, so an application that
    moves from radar to industrial production keeps the same evidence row instead of forking
    into a second fact. See ``evidence_bucket_transitions`` for the maturity history.
    """
    return "|".join(_slug(value) for value in (actor, market, component, operation))


BUCKET_RANK = {"existing": 2, "radar": 1, "pending": 0, "rejected": -1}


def offer_fact_key(actor: str, offer_type: str, capability: str, operation: str | None, laser_process: str | None) -> str:
    return "|".join(_slug(value) for value in (actor, offer_type, capability, operation or "", laser_process or ""))


def _canonicalise_existing_evidence(db: sqlite3.Connection) -> None:
    """Repair legacy labels and remove old curated seeds from automatic publication."""
    placeholders = ",".join("?" for _ in LEGACY_SEED_GROUPS)
    db.execute(f"UPDATE evidence SET review_status='review' WHERE source_group IN ({placeholders})", LEGACY_SEED_GROUPS)
    db.execute("UPDATE evidence SET component='Électrodes de batteries' WHERE component='Électrodes de batteries Gen4'")
    db.execute("UPDATE evidence SET component='Microcanaux' WHERE component='Composants et microcanaux en verre'")
    db.execute("UPDATE evidence SET component='Composants en verre' WHERE component='Composants en verre optique'")
    db.execute("UPDATE evidence SET operation='Microdécoupe' WHERE operation IN ('Microdécoupe femtoseconde','Découpe femtoseconde')")
    db.execute("UPDATE evidence SET operation='SLE' WHERE operation='SLE et microstructuration 3D'")
    db.execute("UPDATE evidence SET operation='Microdécoupe' WHERE actor_name='ALPHANOV' AND operation='Découpe et structuration'")


def _migrate_evidence_fact_model(db: sqlite3.Connection) -> None:
    """Backfill language-independent facts and move each URL/quote into a source-proof table."""
    _canonicalise_existing_evidence(db)
    rows = db.execute("SELECT * FROM evidence ORDER BY id").fetchall()
    if not rows:
        return

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = market_fact_key(row["actor_name"], row["bucket"], row["market"] or "", row["component"] or "", row["operation"] or "")
        grouped.setdefault(key, []).append(row)

    for key, members in grouped.items():
        # Prefer an accepted/high-confidence row as the representative fact.
        def rank(row: sqlite3.Row) -> tuple[int, float, int]:
            status_rank = {"accepted": 2, "review": 1, "rejected": 0}.get(row["review_status"], 0)
            confidence = float(row["field_confidence"] or 0) if "field_confidence" in row.keys() else 0.0
            return status_rank, confidence, -int(row["id"])

        representative = max(members, key=rank)
        rep_id = int(representative["id"])
        db.execute(
            "UPDATE evidence SET fact_key=?,evidence_kind='market_application',language=? WHERE id=?",
            (key, language_from_url(representative["source_url"]), rep_id),
        )
        for row in members:
            source_fingerprint = str(row["fingerprint"] or hashlib.sha256(f"{row['source_url']}|{row['quote']}".encode()).hexdigest())
            db.execute(
                """INSERT OR IGNORE INTO evidence_sources(
                       evidence_id,source_url,source_title,source_date,quote,language,block_heading,block_path,
                       extraction_mode,field_confidence,fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rep_id, row["source_url"], row["source_title"], row["source_date"], row["quote"],
                    language_from_url(row["source_url"]),
                    row["block_heading"] if "block_heading" in row.keys() else None,
                    row["block_path"] if "block_path" in row.keys() else None,
                    row["extraction_mode"] if "extraction_mode" in row.keys() else None,
                    row["field_confidence"] if "field_confidence" in row.keys() else None,
                    source_fingerprint, row["created_at"],
                ),
            )
            if int(row["id"]) != rep_id:
                db.execute("DELETE FROM evidence WHERE id=?", (row["id"],))


def _migrate_application_keys(db: sqlite3.Connection) -> None:
    """Backfill application_key and merge rows that only differed by bucket.

    Before this migration, ``market_fact_key`` embedded the bucket, so the same application
    moving from radar to industrial production created a second evidence row instead of
    updating the first. This merges those pairs, keeps the highest-maturity bucket as the
    canonical one, moves every source proof onto the surviving row, and records the merge as
    a transition (stamped with the migration time, since the real transition date predates
    this column and is not recoverable).
    """
    rows = db.execute("SELECT * FROM evidence WHERE evidence_kind='market_application'").fetchall()
    if not rows:
        return

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = application_key(row["actor_name"], row["market"] or "", row["component"] or "", row["operation"] or "")
        grouped.setdefault(key, []).append(row)

    stamp = utc_now()
    for key, members in grouped.items():
        if len(members) == 1:
            db.execute("UPDATE evidence SET application_key=? WHERE id=?", (key, members[0]["id"]))
            continue

        def rank(row: sqlite3.Row) -> tuple[int, float, int]:
            confidence = float(row["field_confidence"] or 0) if "field_confidence" in row.keys() else 0.0
            return (BUCKET_RANK.get(row["bucket"], -1), confidence, int(row["id"]))

        ordered = sorted(members, key=rank, reverse=True)
        representative = ordered[0]
        rep_id = int(representative["id"])
        db.execute("UPDATE evidence SET application_key=? WHERE id=?", (key, rep_id))

        for row in ordered[1:]:
            old_id = int(row["id"])
            db.execute("UPDATE evidence_sources SET evidence_id=? WHERE evidence_id=?", (rep_id, old_id))
            if row["bucket"] != representative["bucket"]:
                db.execute(
                    "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
                    (rep_id, row["bucket"], representative["bucket"], stamp),
                )
            db.execute("DELETE FROM evidence WHERE id=?", (old_id,))


def _upsert_seed_evidence(db: sqlite3.Connection, item: dict[str, Any], stamp: str) -> None:
    key = market_fact_key(item["actor"], item["bucket"], item["market"], item["component"], item["operation"])
    source_fingerprint = hashlib.sha256(f"{item['url']}|{item['quote']}".encode()).hexdigest()
    row = db.execute("SELECT id FROM evidence WHERE fact_key=?", (key,)).fetchone()
    if row:
        evidence_id = int(row["id"])
    else:
        fact_fingerprint = hashlib.sha256(key.encode()).hexdigest()
        new_id = db.execute(
            """INSERT INTO evidence(
                   actor_name,bucket,market,component,operation,industrial_stage,source_url,source_title,source_date,
                   quote,source_group,fingerprint,fact_key,evidence_kind,language,review_status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'market_application',?,'accepted',?,?)""",
            (
                item["actor"], item["bucket"], item["market"], item["component"], item["operation"], item["stage"],
                item["url"], item["title"], item["date"], item["quote"], key, fact_fingerprint, key,
                language_from_url(item["url"]), stamp, stamp,
            ),
        ).lastrowid
        assert new_id is not None  # guaranteed by sqlite3 right after a successful AUTOINCREMENT insert
        evidence_id = new_id
    db.execute(
        """INSERT OR IGNORE INTO evidence_sources(
               evidence_id,source_url,source_title,source_date,quote,language,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (evidence_id, item["url"], item["title"], item["date"], item["quote"], language_from_url(item["url"]), source_fingerprint, stamp),
    )


def init_databases() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect(ACTORS_DB) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS actors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                country TEXT NOT NULL,
                role TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1)),
                official_url TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                last_scraped_at TEXT,
                last_status TEXT NOT NULL DEFAULT 'never',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actor_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                related_actor_id INTEGER REFERENCES actors(id) ON DELETE SET NULL,
                related_name TEXT NOT NULL,
                relation_type TEXT NOT NULL CHECK(relation_type IN ('partner','supplier','client')),
                note TEXT,
                source_url TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS actor_relations_actor_idx ON actor_relations(actor_id);
            CREATE TABLE IF NOT EXISTS actor_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                dimension TEXT NOT NULL CHECK(dimension IN ('certification','differentiator')),
                value TEXT NOT NULL,
                source_url TEXT,
                review_status TEXT NOT NULL DEFAULT 'verified' CHECK(review_status IN ('pending','verified','rejected')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS actor_facts_actor_idx ON actor_facts(actor_id);
            CREATE TABLE IF NOT EXISTS actor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                event_date TEXT,
                source_url TEXT,
                review_status TEXT NOT NULL DEFAULT 'verified' CHECK(review_status IN ('pending','verified','rejected')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS actor_events_actor_idx ON actor_events(actor_id);
            CREATE TABLE IF NOT EXISTS actor_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                url TEXT NOT NULL UNIQUE,
                source_kind TEXT NOT NULL DEFAULT 'official',
                active INTEGER NOT NULL DEFAULT 1,
                content_hash TEXT,
                last_http_status INTEGER,
                last_checked_at TEXT,
                last_changed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                changed INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            CREATE TABLE IF NOT EXISTS site_profiles (
                actor_id INTEGER PRIMARY KEY REFERENCES actors(id) ON DELETE CASCADE,
                strategy TEXT NOT NULL CHECK(strategy IN ('adaptive','generic')),
                status TEXT NOT NULL DEFAULT 'pending',
                profile_json TEXT,
                profile_hash TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                generated_by TEXT NOT NULL DEFAULT 'bootstrap',
                version INTEGER NOT NULL DEFAULT 1,
                last_profiled_at TEXT,
                needs_reprofile INTEGER NOT NULL DEFAULT 0 CHECK(needs_reprofile IN (0,1)),
                failure_count INTEGER NOT NULL DEFAULT 0,
                health_score REAL NOT NULL DEFAULT 1,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS profile_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                generated_by TEXT,
                confidence REAL,
                message TEXT
            );
            """
        )
        _add_columns(db, "actors", {
            # competitive_class: C1/C2 (direct/partial competitor), T1 (technology centre),
            # A1 (internal reference, e.g. HEF/IREIS), etc. -- orthogonal to "role", which
            # stays free text. is_reference actors are excluded from competitive counts/views.
            "competitive_class": "TEXT",
            "is_reference": "INTEGER NOT NULL DEFAULT 0 CHECK(is_reference IN (0,1))",
            "parent_actor": "TEXT",
            "entity_note": "TEXT",
            # Human-validation queue for actors whose evidence is too thin to trust yet
            # (e.g. a newly-launched site found by the audit's counter-investigation).
            # 'verified' is the default so every actor added the normal way (or already
            # in the base before this column existed) counts as a real actor immediately.
            "review_status": "TEXT NOT NULL DEFAULT 'verified' CHECK(review_status IN ('candidate','verified','rejected','monitor'))",
            # actor_type is the *nature* of the organization (orthogonal to competitive_class,
            # which is the *relation* to us) -- a centre technologique and a prestataire
            # industriel can both be C1, but they are not the same kind of actor.
            "actor_type": (
                "TEXT CHECK(actor_type IN ("
                "'groupe_industriel','prestataire_industriel','societe_developpement_procedes',"
                "'societe_technologique_specialisee','centre_technologique','institut_recherche_appliquee',"
                "'laboratoire_academique','partenaire_adjacent'))"
            ),
            # JSON array of the business lines actually demonstrated, e.g. ["equipment","service"].
            # An actor selling both must have both, but only "service"/"process"/"research"
            # lines should ever feed the competitive score -- "equipment" alone never does.
            "business_models": "TEXT",
            # Analyst synthesis and human-validation metadata that market.db cannot supply --
            # see actor_facts/actor_events for the sourced, itemized facts (certifications,
            # differentiators, M&A events) that back this summary up.
            "strategic_summary": "TEXT",
            "last_verified_at": "TEXT",
        })
        _add_columns(db, "actor_sources", {
            "page_type": "TEXT",
            "source_score": "INTEGER NOT NULL DEFAULT 0",
            "discovery_depth": "INTEGER NOT NULL DEFAULT 0",
            "discovery_context": "TEXT",
            "discovery_reason": "TEXT",
            "parent_url": "TEXT",
            "extraction_mode": "TEXT",
            "structure_hash": "TEXT",
            "last_title": "TEXT",
            "last_error": "TEXT",
            "ambiguous": "INTEGER NOT NULL DEFAULT 0",
            "blocks_json": "TEXT",
        })
        _add_columns(db, "site_profiles", {
            "coverage_json": "TEXT NOT NULL DEFAULT '{}'",
            "coverage_ready": "INTEGER NOT NULL DEFAULT 0",
            "coverage_discovered": "INTEGER NOT NULL DEFAULT 0",
        })
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS actor_sources_actor_active_score_idx
                ON actor_sources(actor_id,active,source_score DESC);
            CREATE INDEX IF NOT EXISTS actor_sources_checked_idx
                ON actor_sources(last_checked_at);
            CREATE INDEX IF NOT EXISTS actors_active_priority_idx
                ON actors(active,priority DESC,name);
            """
        )
        _normalize_existing_page_types(db)
        stamp = utc_now()
        db.executemany(
            """INSERT INTO actors(name,country,role,priority,official_url,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET country=excluded.country, role=excluded.role,
               priority=excluded.priority, official_url=excluded.official_url, updated_at=excluded.updated_at""",
            [(*actor, stamp) for actor in ACTORS],
        )
        actor_ids = {row["name"]: row["id"] for row in db.execute("SELECT id,name FROM actors")}
        for name, url, source_kind in SEED_SOURCES:
            db.execute(
                "INSERT OR IGNORE INTO actor_sources(actor_id,url,source_kind) VALUES(?,?,?)",
                (actor_ids[name], url, source_kind),
            )
        # Read priority back from the actors table itself, not the static ACTORS list: this
        # loop must also cover actors added later via create_actor() (POST /api/actors), which
        # are not and never will be in ACTORS. Looking them up in ACTORS raised StopIteration
        # here and crashed every startup once a single manually-added actor existed.
        for row in db.execute("SELECT id,priority FROM actors"):
            actor_id, priority = row["id"], row["priority"]
            db.execute(
                """INSERT INTO site_profiles(actor_id,strategy,status,generated_by)
                   VALUES(?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET strategy=excluded.strategy""",
                (actor_id, "adaptive" if priority else "generic", "pending", "bootstrap"),
            )

    with connect(MARKET_DB) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK(bucket IN ('existing','radar','pending','rejected')),
                market TEXT,
                component TEXT,
                operation TEXT,
                industrial_stage TEXT,
                source_url TEXT NOT NULL,
                source_title TEXT,
                source_date TEXT,
                quote TEXT NOT NULL,
                source_group TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                review_status TEXT NOT NULL DEFAULT 'accepted' CHECK(review_status IN ('accepted','review','rejected')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_group_idx ON evidence(bucket,market,component,operation);
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            """
        )
        _add_columns(db, "evidence", {
            "block_heading": "TEXT",
            "block_path": "TEXT",
            "extraction_mode": "TEXT",
            "field_confidence": "REAL",
            "laser_process": "TEXT",
            "material": "TEXT",
            "performance": "TEXT",
            "maturity_level": "TEXT",
            "relation_strength": "TEXT",
            "relation_evidence": "TEXT",
            "source_role": "TEXT",
            "fact_key": "TEXT",
            "evidence_kind": "TEXT NOT NULL DEFAULT 'market_application'",
            "language": "TEXT",
            "fact_status": "TEXT NOT NULL DEFAULT 'review'",
            "last_seen_at": "TEXT",
            "application_key": "TEXT",
        })
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_title TEXT,
                source_date TEXT,
                quote TEXT NOT NULL,
                language TEXT,
                block_heading TEXT,
                block_path TEXT,
                extraction_mode TEXT,
                field_confidence REAL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_sources_fact_idx ON evidence_sources(evidence_id);

            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                offer_type TEXT NOT NULL,
                capability TEXT NOT NULL,
                operation TEXT,
                laser_process TEXT,
                material TEXT,
                performance TEXT,
                industrial_stage TEXT,
                page_type TEXT,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                fact_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                review_status TEXT NOT NULL DEFAULT 'accepted' CHECK(review_status IN ('accepted','review','rejected')),
                field_confidence REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS offers_actor_idx ON offers(actor_name,offer_type,capability);
            CREATE TABLE IF NOT EXISTS offer_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                language TEXT,
                block_heading TEXT,
                block_path TEXT,
                extraction_mode TEXT,
                field_confidence REAL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS offer_sources_fact_idx ON offer_sources(offer_id);

            CREATE TABLE IF NOT EXISTS evidence_bucket_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                from_bucket TEXT,
                to_bucket TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_bucket_transitions_evidence_idx
                ON evidence_bucket_transitions(evidence_id);

            CREATE TABLE IF NOT EXISTS vocabulary_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                block_heading TEXT,
                proposed_labels TEXT NOT NULL,
                resolved_labels TEXT NOT NULL DEFAULT '{}',
                review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','accepted','rejected')),
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS vocabulary_candidates_status_idx
                ON vocabulary_candidates(review_status, created_at);

            CREATE TABLE IF NOT EXISTS custom_lexicon_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL CHECK(dimension IN ('market','component','operation')),
                label TEXT NOT NULL,
                match_terms TEXT NOT NULL,
                source_vocabulary_candidate_id INTEGER REFERENCES vocabulary_candidates(id),
                created_at TEXT NOT NULL,
                UNIQUE(dimension, label)
            );
            """
        )
        _add_columns(db, "offers", {
            "last_seen_at": "TEXT",
        })
        _add_columns(db, "evidence_sources", {
            "relation_strength": "TEXT",
            "relation_evidence": "TEXT",
            "source_role": "TEXT",
        })
        _add_columns(db, "collection_runs", {
            "offers_added": "INTEGER NOT NULL DEFAULT 0",
            "sources_added": "INTEGER NOT NULL DEFAULT 0",
            "blocks_examined": "INTEGER NOT NULL DEFAULT 0",
            "laser_blocks": "INTEGER NOT NULL DEFAULT 0",
            "candidate_count": "INTEGER NOT NULL DEFAULT 0",
            "diagnostics_json": "TEXT NOT NULL DEFAULT '{}'",
            "ai_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "ai_output_tokens": "INTEGER NOT NULL DEFAULT 0",
        })
        db.execute("UPDATE evidence SET fact_status=CASE WHEN review_status='accepted' THEN 'validated' WHEN review_status='rejected' THEN 'rejected' ELSE 'review' END WHERE fact_status IS NULL OR fact_status='' OR fact_status='review'")
        db.execute("UPDATE evidence SET last_seen_at=COALESCE(last_seen_at,updated_at,created_at)")
        db.execute("UPDATE offers SET last_seen_at=COALESCE(last_seen_at,updated_at,created_at)")
        # Both migrations run on every startup, not just once: they are idempotent (a row that
        # already carries its canonical key is only re-derived, never duplicated) and this keeps
        # the evidence table self-healing if a row is ever inserted or edited outside the normal
        # upsert path (manual fix, restored backup) without a fact_key/application_key.
        _migrate_evidence_fact_model(db)
        _migrate_application_keys(db)
        db.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_fact_key_uq ON evidence(fact_key) WHERE fact_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_application_key_uq
                ON evidence(application_key) WHERE application_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS evidence_status_bucket_idx
                ON evidence(fact_status,evidence_kind,bucket,created_at);
            CREATE INDEX IF NOT EXISTS evidence_last_seen_idx
                ON evidence(last_seen_at);
            CREATE INDEX IF NOT EXISTS offers_status_created_idx
                ON offers(review_status,created_at);
            CREATE INDEX IF NOT EXISTS offers_last_seen_idx
                ON offers(last_seen_at);
            """
        )
        stamp = utc_now()
        for item in SEED_EVIDENCE:
            _upsert_seed_evidence(db, item, stamp)

    with connect(TECH_DB) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT,
                document_type TEXT NOT NULL CHECK(document_type IN ('publication','patent','project','other')),
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                published_at TEXT,
                doi TEXT,
                patent_number TEXT,
                abstract TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            """
        )
        _add_columns(db, "documents", {
            "last_seen_at": "TEXT",
        })
        db.execute("UPDATE documents SET last_seen_at=COALESCE(last_seen_at,created_at)")
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS documents_type_published_idx
                ON documents(document_type,published_at);
            CREATE INDEX IF NOT EXISTS documents_created_idx
                ON documents(created_at);
            """
        )


def _backup_dir() -> Path:
    """Resolved fresh on every call (DATA_DIR / "backups"), like ACTORS_DB/MARKET_DB/TECH_DB
    are used elsewhere in this module -- tests patch DATA_DIR to an isolated path, and a
    module-level constant computed once at import time would silently keep pointing at the
    real project's data/backups/ instead of following that patch."""
    return DATA_DIR / "backups"


def _backup_one(path: Path, stamp: str) -> Path | None:
    """Copy one SQLite file to data/backups/<stem>_<stamp>.db. No-op if it doesn't exist yet."""
    if not path.exists():
        return None
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{path.stem}_{stamp}.db"
    shutil.copy2(path, backup_path)
    return backup_path


def _prune_backups(stem: str, keep: int = BACKUP_RETENTION_COUNT) -> None:
    """Keep only the ``keep`` most recent backups for one database (by filename, which sorts
    chronologically thanks to the ISO-like timestamp suffix)."""
    backup_dir = _backup_dir()
    if not backup_dir.exists():
        return
    candidates = sorted(backup_dir.glob(f"{stem}_*.db"), key=lambda p: p.name, reverse=True)
    for stale in candidates[keep:]:
        stale.unlink(missing_ok=True)


def backup_all_databases() -> list[Path]:
    """Snapshot the three SQLite databases to data/backups/ before a collection run.

    Best-effort and additive: a missing or unreadable database is skipped rather than aborting
    the others, and this never runs automatically at import time or app startup -- only right
    before a scrape job -- so a bad run always has a rollback point without the analyst having
    to remember to run reset_market_db.py first. Old backups beyond BACKUP_RETENTION_COUNT are
    pruned per database so data/backups/ doesn't grow unbounded.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    saved: list[Path] = []
    for path in (ACTORS_DB, MARKET_DB, TECH_DB):
        try:
            backup_path = _backup_one(path, stamp)
        except OSError:
            continue
        if backup_path:
            saved.append(backup_path)
            _prune_backups(path.stem)
    return saved


def reset_market_database(*, backup: bool = True) -> Path | None:
    """Rebuild market.db from an empty schema while preserving actors.db and technology.db.

    When ``backup`` is true the previous SQLite database is copied to ``data/backups`` with a
    timestamp before deletion. WAL/SHM sidecars are removed as well. This is intentionally an
    explicit maintenance action and is never executed automatically at application startup.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_one(MARKET_DB, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")) if backup else None
    if backup_path:
        _prune_backups(MARKET_DB.stem)
    for path in (MARKET_DB, Path(str(MARKET_DB) + "-wal"), Path(str(MARKET_DB) + "-shm")):
        if path.exists():
            path.unlink()
    init_databases()
    return backup_path


def rows(path: Path, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect(path) as db:
        return [dict(row) for row in db.execute(query, tuple(params)).fetchall()]


def scalar(path: Path, query: str, params: Iterable[Any] = ()) -> Any:
    with connect(path) as db:
        result = db.execute(query, tuple(params)).fetchone()
        return result[0] if result else None


def create_actor(name: str, country: str, role: str, official_url: str, priority: bool = False) -> int:
    """Add a new actor outside the static ACTORS seed list.

    Growing coverage currently means editing the hardcoded ACTORS list in this module and
    redeploying; this lets an analyst add one from the UI/API instead. Bootstraps a matching
    site_profiles row so the crawler picks the actor up on its next run, exactly like a
    seeded one. Raises ValueError on a blank/duplicate name or a non-http(s) URL.
    """
    name = name.strip()
    if not name:
        raise ValueError("Actor name is required")
    official_url = official_url.strip()
    if not official_url.startswith(("http://", "https://")):
        raise ValueError("official_url must be an absolute http(s) URL")
    stamp = utc_now()
    with connect(ACTORS_DB) as db:
        if db.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone():
            raise ValueError(f"An actor named '{name}' already exists")
        actor_id = db.execute(
            "INSERT INTO actors(name,country,role,priority,official_url,updated_at) VALUES(?,?,?,?,?,?)",
            (name, country.strip(), role.strip(), int(bool(priority)), official_url, stamp),
        ).lastrowid
        assert actor_id is not None
        db.execute(
            "INSERT INTO site_profiles(actor_id,strategy,status,generated_by) VALUES(?,?,?,?)",
            (actor_id, "adaptive" if priority else "generic", "pending", "manual"),
        )
    return actor_id


def set_actor_active(actor_id: int, active: bool) -> None:
    """Pause or resume an actor without deleting its history. Raises ValueError if unknown."""
    with connect(ACTORS_DB) as db:
        updated = db.execute(
            "UPDATE actors SET active=?,updated_at=? WHERE id=?", (int(bool(active)), utc_now(), actor_id)
        ).rowcount
    if not updated:
        raise ValueError(f"Actor {actor_id} not found")


ACTOR_TYPES = {
    "groupe_industriel", "prestataire_industriel", "societe_developpement_procedes",
    "societe_technologique_specialisee", "centre_technologique", "institut_recherche_appliquee",
    "laboratoire_academique", "partenaire_adjacent",
}
BUSINESS_MODELS = {"equipment", "service", "process", "research", "internal"}


def update_actor_classification(
    actor_id: int,
    *,
    name: str | None = None,
    country: str | None = None,
    role: str | None = None,
    official_url: str | None = None,
    priority: bool | None = None,
    competitive_class: str | None = None,
    is_reference: bool | None = None,
    parent_actor: str | None = None,
    entity_note: str | None = None,
    review_status: str | None = None,
    actor_type: str | None = None,
    business_models: list[str] | None = None,
    strategic_summary: str | None = None,
) -> None:
    """Patch an actor's editable fields: the plain descriptive ones (name, country, role,
    official_url, priority) plus the analytical fields (competitive class, actor type,
    business model(s), internal-reference flag, M&A parent/note, human-review status) added
    on top of them. Only fields explicitly passed (not None) are updated, so a caller can set
    a single field without clobbering the others.
    """
    updates: dict[str, object] = {}
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Actor name is required")
        updates["name"] = name
        with connect(ACTORS_DB) as db:
            clash = db.execute("SELECT id FROM actors WHERE name=? AND id<>?", (name, actor_id)).fetchone()
        if clash:
            raise ValueError(f"An actor named '{name}' already exists")
    if country is not None:
        updates["country"] = country
    if role is not None:
        updates["role"] = role
    if official_url is not None:
        official_url = official_url.strip()
        if not official_url.startswith(("http://", "https://")):
            raise ValueError("official_url must be an absolute http(s) URL")
        updates["official_url"] = official_url
    if priority is not None:
        updates["priority"] = int(bool(priority))
    if competitive_class is not None:
        updates["competitive_class"] = competitive_class
    if is_reference is not None:
        updates["is_reference"] = int(bool(is_reference))
    if parent_actor is not None:
        updates["parent_actor"] = parent_actor
    if entity_note is not None:
        updates["entity_note"] = entity_note
    if review_status is not None:
        if review_status not in {"candidate", "verified", "rejected", "monitor"}:
            raise ValueError(f"Invalid review_status: {review_status!r}")
        updates["review_status"] = review_status
    if actor_type is not None:
        if actor_type not in ACTOR_TYPES:
            raise ValueError(f"Invalid actor_type: {actor_type!r}")
        updates["actor_type"] = actor_type
    if business_models is not None:
        invalid = set(business_models) - BUSINESS_MODELS
        if invalid:
            raise ValueError(f"Invalid business_models: {sorted(invalid)!r}")
        updates["business_models"] = json.dumps(business_models)
    if strategic_summary is not None:
        updates["strategic_summary"] = strategic_summary
    if not updates:
        return
    updates["updated_at"] = utc_now()
    assignments = ",".join(f"{field}=?" for field in updates)
    with connect(ACTORS_DB) as db:
        updated = db.execute(
            f"UPDATE actors SET {assignments} WHERE id=?", (*updates.values(), actor_id)
        ).rowcount
    if not updated:
        raise ValueError(f"Actor {actor_id} not found")


def delete_actor(actor_id: int) -> None:
    """Permanently remove an actor and everything scraped for it (sources, site profile,
    relations cascade via ON DELETE CASCADE). Irreversible -- the caller is responsible for
    confirming this with a human first. Raises ValueError if the actor doesn't exist.
    """
    with connect(ACTORS_DB) as db:
        deleted = db.execute("DELETE FROM actors WHERE id=?", (actor_id,)).rowcount
    if not deleted:
        raise ValueError(f"Actor {actor_id} not found")


def add_actor_relation(
    actor_id: int,
    relation_type: str,
    related_name: str,
    *,
    related_actor_id: int | None = None,
    note: str | None = None,
    source_url: str | None = None,
) -> int:
    """Record a partner/supplier/client relation for the network map. ``related_name`` is
    always stored (even when ``related_actor_id`` points at a tracked actor) so the edge
    still renders a label if that actor is later renamed or removed.
    """
    if relation_type not in {"partner", "supplier", "client"}:
        raise ValueError(f"Invalid relation_type: {relation_type!r}")
    related_name = related_name.strip()
    if not related_name:
        raise ValueError("related_name is required")
    with connect(ACTORS_DB) as db:
        exists = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not exists:
            raise ValueError(f"Actor {actor_id} not found")
        return db.execute(
            """INSERT INTO actor_relations(actor_id,related_actor_id,related_name,relation_type,note,source_url,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (actor_id, related_actor_id, related_name, relation_type, note, source_url, utc_now()),
        ).lastrowid


def add_actor_fact(actor_id: int, dimension: str, value: str, *, source_url: str | None = None) -> int:
    """Record one sourced, itemized fact (certification or differentiator) that market.db
    cannot supply -- see the fiches-cibles integration plan. Distinct from actors.role/
    strategic_summary, which stay free text: this is queryable per dimension.
    """
    if dimension not in {"certification", "differentiator"}:
        raise ValueError(f"Invalid dimension: {dimension!r}")
    value = value.strip()
    if not value:
        raise ValueError("value is required")
    with connect(ACTORS_DB) as db:
        exists = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not exists:
            raise ValueError(f"Actor {actor_id} not found")
        return db.execute(
            "INSERT INTO actor_facts(actor_id,dimension,value,source_url,created_at) VALUES(?,?,?,?,?)",
            (actor_id, dimension, value, source_url, utc_now()),
        ).lastrowid


def add_actor_event(
    actor_id: int, event_type: str, description: str, *, event_date: str | None = None, source_url: str | None = None
) -> int:
    """Record one dated, sourced event (e.g. an acquisition) for an actor's fiche."""
    event_type = event_type.strip()
    if not event_type:
        raise ValueError("event_type is required")
    description = description.strip()
    if not description:
        raise ValueError("description is required")
    with connect(ACTORS_DB) as db:
        exists = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not exists:
            raise ValueError(f"Actor {actor_id} not found")
        return db.execute(
            "INSERT INTO actor_events(actor_id,event_type,description,event_date,source_url,created_at) VALUES(?,?,?,?,?,?)",
            (actor_id, event_type, description, event_date, source_url, utc_now()),
        ).lastrowid


def _normalize_domain(url: str) -> str:
    host = urlparse(url).netloc.casefold()
    return host[4:] if host.startswith("www.") else host


def _name_tokens(name: str) -> set[str]:
    stop = {"gmbh", "ag", "srl", "ltd", "sa", "sas", "sarl", "inc", "co", "laser", "lasers", "photonics", "technology", "technologies"}
    slug = _slug(name).replace("-", " ")
    return {token for token in slug.split() if token and token not in stop}


def find_actor_duplicate_candidates() -> list[dict[str, object]]:
    """Flag actor pairs that may be the same organization: exact domain match, a shared
    parent company, or near-identical names once legal suffixes (GmbH, Ltd...) are
    stripped. Read-only -- this only reports candidates, it never merges or deletes
    anything; a human decides. No address/registry data is collected today, so that
    signal from the audit isn't checked here.
    """
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT id,name,official_url,parent_actor FROM actors WHERE active=1")]
    candidates: list[dict[str, object]] = []
    for i, left in enumerate(actors):
        left_domain = _normalize_domain(left["official_url"])
        left_tokens = _name_tokens(left["name"])
        for right in actors[i + 1 :]:
            reasons = []
            if left_domain and left_domain == _normalize_domain(right["official_url"]):
                reasons.append("meme domaine officiel")
            if left["parent_actor"] and left["parent_actor"] == right["name"]:
                reasons.append(f"{left['name']} rattache a {right['name']}")
            elif right["parent_actor"] and right["parent_actor"] == left["name"]:
                reasons.append(f"{right['name']} rattache a {left['name']}")
            elif left["parent_actor"] and left["parent_actor"] == right["parent_actor"]:
                reasons.append(f"meme maison mere ({left['parent_actor']})")
            right_tokens = _name_tokens(right["name"])
            if left_tokens and right_tokens and left_tokens == right_tokens:
                reasons.append("noms identiques hors forme juridique")
            if reasons:
                candidates.append({
                    "actor_a": left["name"], "actor_a_id": left["id"],
                    "actor_b": right["name"], "actor_b_id": right["id"],
                    "reasons": reasons,
                })
    return candidates


def accept_vocabulary_candidate(candidate_id: int, dimension: str) -> dict[str, str]:
    """Promote one proposed label from a vocabulary candidate into the live custom lexicon.

    ``dimension`` selects which of the candidate's (possibly several) unresolved dimensions
    to promote -- a candidate proposing both an unknown component and an unknown market
    requires one call per dimension, so each can be reviewed on its own merits. The matching
    rule created is a single-term "any_of" using the accepted label's own text, mirroring
    how most existing lexicon entries already work (e.g. "Stents": any_of=("stent",)).
    Raises ValueError if the candidate doesn't exist or has no proposal for that dimension.
    """
    with connect(MARKET_DB) as db:
        row = db.execute("SELECT proposed_labels FROM vocabulary_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise ValueError(f"Vocabulary candidate {candidate_id} not found")
        proposed = json.loads(row["proposed_labels"])
        label = proposed.get(dimension)
        if not label:
            raise ValueError(f"No proposed label for dimension '{dimension}' on candidate {candidate_id}")
        stamp = utc_now()
        db.execute(
            """INSERT INTO custom_lexicon_entries(dimension,label,match_terms,source_vocabulary_candidate_id,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(dimension,label) DO NOTHING""",
            (dimension, label, json.dumps([label], ensure_ascii=False), candidate_id, stamp),
        )
        db.execute("UPDATE vocabulary_candidates SET review_status='accepted' WHERE id=?", (candidate_id,))
    return {"dimension": dimension, "label": label}


def reject_vocabulary_candidate(candidate_id: int) -> None:
    """Mark a vocabulary candidate reviewed-and-declined. Raises ValueError if unknown."""
    with connect(MARKET_DB) as db:
        updated = db.execute(
            "UPDATE vocabulary_candidates SET review_status='rejected' WHERE id=?", (candidate_id,)
        ).rowcount
    if not updated:
        raise ValueError(f"Vocabulary candidate {candidate_id} not found")
