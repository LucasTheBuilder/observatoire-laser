from __future__ import annotations

import hashlib
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
    ("ALPHANOV", "France", "Centre technologique", 1, "https://www.alphanov.com"),
    ("MANUTECH USD", "France", "Plateforme technologique", 1, "https://www.manutech-usd.fr"),
    ("HEF", "France", "Groupe industriel", 1, "https://hef.group"),
    ("LASEA", "Belgique", "Intégrateur et services", 1, "https://www.lasea.eu"),
    ("IREPA LASER", "France", "Centre technologique", 0, "https://www.irepa-laser.com"),
    ("Pulsar Photonics", "Allemagne", "Production et développement", 0, "https://www.pulsar-photonics.de"),
    ("Lightmotif", "Pays-Bas", "Texturation de surfaces", 0, "https://www.lightmotif.nl"),
    ("FEMTO Engineering", "France", "Centre d’ingénierie", 0, "https://www.femto-engineering.fr"),
    ("Workshop of Photonics", "Lituanie", "Micro-usinage et production", 0, "https://wophotonics.com"),
    ("LightFab", "Allemagne", "Microfabrication du verre", 0, "https://lightfab.de"),
    ("Femtika", "Lituanie", "Microfabrication 3D", 0, "https://femtika.com"),
    ("Micreon", "Allemagne", "Micro-usinage laser", 0, "https://www.micreon.de"),
    ("OpTek Systems", "Royaume-Uni", "Production laser", 0, "https://optek.humaneticsgroup.com"),
    ("Blueacre Technology", "Irlande", "Dispositifs médicaux", 0, "https://blueacretechnology.com"),
    ("Oxford Lasers", "Royaume-Uni", "Micro-usinage laser", 0, "https://oxfordlasers.com"),
    ("3D-Micromac", "Allemagne", "Procédés industriels", 0, "https://3d-micromac.com"),
    ("Fraunhofer ILT", "Allemagne", "Institut technologique", 0, "https://www.ilt.fraunhofer.de/en.html"),
    ("Laser Zentrum Hannover", "Allemagne", "Institut technologique", 0, "https://www.lzh.de/en"),
    ("FEMTOprint", "Suisse", "Microfabrication du verre", 0, "https://www.femtoprint.ch"),
    ("LightPulse Laser Precision", "Allemagne", "Production sous contrat", 0, "https://www.light-pulse.de/en-gb/"),
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


def _language_from_url(url: str | None) -> str | None:
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
            (key, _language_from_url(representative["source_url"]), rep_id),
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
                    _language_from_url(row["source_url"]),
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
                _language_from_url(item["url"]), stamp, stamp,
            ),
        ).lastrowid
        assert new_id is not None  # guaranteed by sqlite3 right after a successful AUTOINCREMENT insert
        evidence_id = new_id
    db.execute(
        """INSERT OR IGNORE INTO evidence_sources(
               evidence_id,source_url,source_title,source_date,quote,language,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (evidence_id, item["url"], item["title"], item["date"], item["quote"], _language_from_url(item["url"]), source_fingerprint, stamp),
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
        for name, actor_id in actor_ids.items():
            priority = next(actor[3] for actor in ACTORS if actor[0] == name)
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
        })
        db.execute("UPDATE evidence SET fact_status=CASE WHEN review_status='accepted' THEN 'validated' WHEN review_status='rejected' THEN 'rejected' ELSE 'review' END WHERE fact_status IS NULL OR fact_status='' OR fact_status='review'")
        db.execute("UPDATE evidence SET last_seen_at=COALESCE(last_seen_at,updated_at,created_at)")
        db.execute("UPDATE offers SET last_seen_at=COALESCE(last_seen_at,updated_at,created_at)")
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
