"""Couche de persistance de l'Observatoire : schéma SQLite, migrations et fonctions CRUD.

Ce fichier ne fait AUCUN appel réseau (contrairement à scrapers.py) : c'est uniquement la
couche base de données. Il gère trois fichiers SQLite séparés (chacun avec sa propre
connexion/transaction, voir connect()) :

- ACTORS_DB (actors.db) : la liste des acteurs suivis (entreprises/labos concurrents ou
  partenaires), leurs pages web connues (actor_sources) et l'état du profil de crawl de
  chacun (site_profiles) -- alimenté par scrapers.scrape_actors().
- MARKET_DB (market.db) : les "faits marché" extraits du contenu des sites (evidence : quelle
  entreprise fait quoi, pour quel marché/composant/opération), les offres/capacités
  concurrentes (offers), et les preuves sourcées qui les justifient (evidence_sources,
  offer_sources) -- alimenté par scrapers.scrape_market().
- TECH_DB (technology.db) : les publications/documents scientifiques collectés (documents) et
  les signaux de maturité technologique transverses (technology_signals) -- alimenté par
  scrapers.scrape_technology().

init_databases() crée (ou met à jour, via des migrations additives) le schéma des trois
bases à chaque démarrage de l'application (voir app.py: lifespan). Le reste du fichier fournit
des fonctions utilitaires : connexion/transaction (connect), clés d'identité déterministes
pour dédupliquer les faits (market_fact_key, application_key, offer_fact_key,
technology_signal_key), sauvegardes (backup_all_databases), et des opérations CRUD sur les
acteurs (create_actor, update_actor_classification, delete_actor, ...).
"""

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


# Liste "en dur" des acteurs suivis dès le premier démarrage : (nom, pays, rôle, priority,
# official_url). `priority=1` marque les acteurs jugés les plus importants (crawl plus
# profond/plus large, voir site_profiles.crawl_budget). init_databases() insère cette liste
# à chaque démarrage via un UPSERT (ON CONFLICT DO UPDATE), donc la modifier ici et redémarrer
# l'appli met à jour les acteurs existants sans dupliquer de lignes. Des acteurs additionnels
# peuvent aussi être ajoutés depuis l'UI via create_actor(), sans toucher à ce fichier.
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

# Quelques URLs de pages connues, injectées d'office dans actor_sources au démarrage (avant
# même le premier crawl) pour garantir que ces pages "à forte valeur" seront visitées.
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
    """Horodatage ISO 8601 en UTC, à la seconde près -- utilisé partout comme created_at/updated_at."""
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
    """Normalise une chaîne en un "slug" ASCII minuscule et sans accents (ex: "Électrodes" ->
    "electrodes"). Utilisé comme brique de base des clés déterministes (market_fact_key,
    application_key, ...) pour que deux libellés qui ne diffèrent que par la casse/les accents
    produisent la même clé -- donc la même ligne en base au lieu de deux lignes dupliquées."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "non-identifie"


def _normalize_page_type_value(value: str | None) -> str:
    """Ramène un type de page au singulier canonique (ex: "applications" -> "application")."""
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


# Ces quatre fonctions "*_key" sont le cœur du mécanisme anti-duplication de l'app : elles
# transforment les champs métier d'un fait (acteur, marché, composant...) en une chaîne
# stable ("actor|bucket|market|component|operation") qui sert de clé UNIQUE en base
# (voir evidence_fact_key_uq, evidence_application_key_uq, offers.fact_key). Que le même fait
# soit observé une fois ou cent fois (dans une langue ou une autre, sur une page ou une autre),
# il produit toujours la même clé et vient donc mettre à jour la même ligne au lieu d'en créer
# une nouvelle -- c'est cette valeur qu'on cherche avec `WHERE fact_key=?` / `WHERE
# application_key=?` dans scrapers._upsert_market_candidate / _upsert_offer_candidate.
def market_fact_key(actor: str, bucket: str, market: str | None, component: str | None, operation: str | None) -> str:
    """Language-independent canonical key for one market application fact.

    market/component/operation accept None because a "partial" fact (see scrapers._candidate,
    chantier 2 item 2) may have only 2 of the 3 core dimensions -- _slug(None) falls back to a
    stable "non-identifie" placeholder, so two partial facts still dedupe correctly as long as
    the dimensions they DO share are identical.
    """
    return "|".join(_slug(value) for value in (actor, bucket, market, component, operation))


def application_key(actor: str, market: str | None, component: str | None, operation: str | None) -> str:
    """Language- and maturity-independent identity for one market application.

    Unlike ``market_fact_key``, this deliberately excludes the bucket, so an application that
    moves from radar to industrial production keeps the same evidence row instead of forking
    into a second fact. See ``evidence_bucket_transitions`` for the maturity history.
    """
    return "|".join(_slug(value) for value in (actor, market, component, operation))


# Ordre de maturité croissante : sert à savoir si un nouveau "bucket" observé pour un fait
# représente une progression (ex: radar -> existing) ou une régression qu'il ne faut pas
# appliquer automatiquement (voir scrapers._upsert_market_candidate: `upgrades_bucket`).
BUCKET_RANK = {"existing": 2, "radar": 1, "pending": 0, "rejected": -1}


def offer_fact_key(actor: str, offer_type: str, capability: str, operation: str | None, laser_process: str | None) -> str:
    """Même principe que market_fact_key, mais pour une "offre" (offers) -- une capacité/
    prestation d'un acteur qui n'est pas forcément rattachée à un marché/composant précis."""
    return "|".join(_slug(value) for value in (actor, offer_type, capability, operation or "", laser_process or ""))


# Chantier 4 (fiabiliser la preuve) : distinguer une déclaration marketing ("nous savons faire
# X") d'une preuve concrète ("300 trous/seconde sur titane", "certifié ISO 13485") -- l'audit
# note qu'aujourd'hui les deux sont stockés à égalité. Volontairement étroit et déterministe
# (comme le reste du pipeline d'extraction) : seuls deux signaux vérifiables sans ambiguïté
# comptent comme "proof" -- un chiffre accompagné d'une unité technique, ou un code de
# certification reconnu. Une "référence client nommée" (le troisième signal cité par l'audit)
# est délibérément omise : la détecter fiablement demanderait une vraie reconnaissance d'entité
# nommée, pas une regex, et un faux positif ferait passer une déclaration pour une preuve.
# 'third_party' n'est jamais renvoyé ici -- evidence/offers ne contiennent aujourd'hui que du
# contenu scrapé sur le site de l'acteur lui-même (premier parti par construction) ; la valeur
# reste réservée pour une source qui ne l'est pas (voir cordis.py/press.py, d'autres tables).
_PROOF_NUMBER_RE = re.compile(
    r"\d[\d.,]*\s*(%|°c|µm|nm|mm|cm|kg|g|w|kw|mw|hz|khz|mhz|ghz|fs|ps|ns|mj|µj|j/cm2|"
    r"pieces?|pi[eè]ces?|parts?|units?|unit[ée]s?|holes?|trous?|per second|/s|ppm|rpm)\b",
    re.IGNORECASE,
)
_CERTIFICATION_RE = re.compile(r"\b(iso\s?\d{4,5}|as\s?9100|iatf\s?16949|itar|nadcap)\b", re.IGNORECASE)


def classify_evidence_type(quote: str | None) -> str:
    """'proof' si la citation porte un chiffre technique mesuré ou une certification reconnue,
    sinon 'claim' (déclaration non chiffrée). Voir le commentaire ci-dessus pour la portée."""
    text = quote or ""
    if _CERTIFICATION_RE.search(text) or _PROOF_NUMBER_RE.search(text):
        return "proof"
    return "claim"


_ARCHITECTURE_STAGE_RE = re.compile(r"Architecture:\s*([^|]+)")


def _backfill_evidence_type(db: sqlite3.Connection) -> None:
    """One-time (idempotent) backfill of evidence_type for rows written before this column
    existed -- only ever touches NULL rows, so it costs nothing on a database already caught up."""
    for table in ("evidence", "offers"):
        pending = db.execute(f"SELECT id,quote FROM {table} WHERE evidence_type IS NULL").fetchall()
        for row in pending:
            db.execute(f"UPDATE {table} SET evidence_type=? WHERE id=?", (classify_evidence_type(row["quote"]), row["id"]))


def _migrate_industrial_stage_concatenation(db: sqlite3.Connection) -> None:
    """One-time (idempotent) cleanup for the audit's "industrial_stage is a denormalized
    display field" finding: it used to concatenate maturity + process/architecture/material/
    performance into one string (see the old scrapers._candidate ``stage_parts``), even though
    those dimensions already have their own columns -- except architecture, which had none
    (added alongside evidence_type above). Extracts "Architecture: X" into that new column,
    then trims industrial_stage down to just its leading maturity segment. Only rows that still
    contain "|" are touched, so this is a no-op once the whole table has been cleaned; new rows
    never concatenate in the first place (see scrapers._candidate/_ai_candidates).
    """
    pending = db.execute("SELECT id,industrial_stage FROM evidence WHERE industrial_stage LIKE '%|%'").fetchall()
    for row in pending:
        raw = row["industrial_stage"] or ""
        leading = raw.split("|", 1)[0].strip()
        match = _ARCHITECTURE_STAGE_RE.search(raw)
        if match:
            db.execute(
                "UPDATE evidence SET industrial_stage=?,architecture=COALESCE(architecture,?) WHERE id=?",
                (leading, match.group(1).strip(), row["id"]),
            )
        else:
            db.execute("UPDATE evidence SET industrial_stage=? WHERE id=?", (leading, row["id"]))
    # offers never carried an "Architecture:" segment (that dimension doesn't apply to offers),
    # so just trim to the leading maturity segment on the rare row that still looks concatenated.
    pending_offers = db.execute("SELECT id,industrial_stage FROM offers WHERE industrial_stage LIKE '%|%'").fetchall()
    for row in pending_offers:
        leading = (row["industrial_stage"] or "").split("|", 1)[0].strip()
        db.execute("UPDATE offers SET industrial_stage=? WHERE id=?", (leading, row["id"]))


def technology_signal_key(axis: str, project_name: str | None) -> str:
    """Identity for one science->industry readiness signal: the axis plus the named project it
    was observed in (not the source URL), so the same axis/project pair merges new citations
    onto one row instead of creating a duplicate every time another source confirms it."""
    return "|".join(_slug(value) for value in (axis, project_name or ""))


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
            # Must depend on source_url/quote, not just reuse row["fingerprint"] (the evidence
            # row's own fact-identity hash, constant across every source of that fact): reusing
            # it made every citation of the same fact collide on one fingerprint value, which is
            # exactly what let INSERT OR IGNORE silently duplicate rows once evidence_sources
            # gets a real uniqueness constraint (see evidence_sources_fingerprint_uq below) --
            # matches the fingerprint convention scrapers.py's _upsert_market_candidate uses.
            source_fingerprint = hashlib.sha256(f"{key}|{row['source_url']}|{row['quote']}".encode()).hexdigest()
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


def _dedupe_source_rows(db: sqlite3.Connection, table: str, fact_id_col: str) -> None:
    """Collapse source-citation rows that cite the exact same (fact, url, quote) more than once.

    A test bug (fixed alongside this) let init_databases() run its evidence migrations against
    the real production market.db on every pytest run; those migrations computed the source
    fingerprint inconsistently, so INSERT OR IGNORE never caught the resulting re-inserts and
    real duplicate rows accumulated. Kept as a standing, idempotent cleanup (not a one-off
    script) so any future fingerprint mismatch self-heals on the next startup instead of quietly
    inflating "proofs" counts across the app.
    """
    rows = db.execute(f"SELECT id,{fact_id_col},source_url,quote FROM {table} ORDER BY id").fetchall()
    seen: dict[tuple, int] = {}
    duplicate_ids: list[int] = []
    for row in rows:
        content_key = (row[fact_id_col], row["source_url"], row["quote"])
        if content_key in seen:
            duplicate_ids.append(int(row["id"]))
        else:
            seen[content_key] = int(row["id"])
    for dup_id in duplicate_ids:
        db.execute(f"DELETE FROM {table} WHERE id=?", (dup_id,))


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
    """Crée/actualise le schéma des 3 bases (appelée à chaque démarrage, voir app.py: lifespan).

    Idempotente et additive : toutes les instructions sont `CREATE TABLE IF NOT EXISTS` /
    `CREATE INDEX IF NOT EXISTS`, et les colonnes ajoutées après coup passent par
    _add_columns() qui ne fait rien si la colonne existe déjà -- donc relancer cette fonction
    sur une base qui a déjà des données ne perd jamais rien, elle ne fait que compléter le
    schéma manquant. La fonction est découpée en 3 blocs `with connect(...)`, un par fichier
    SQLite (ACTORS_DB, puis MARKET_DB, puis TECH_DB), chacun créant ses tables puis lançant
    ses migrations de données (normalisation, dédoublonnage, backfill de colonnes).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # --- Base 1/3 : ACTORS_DB (acteurs suivis, leurs pages sources, leur profil de crawl) ---
    with connect(ACTORS_DB) as db:
        db.executescript(
            """
            -- Un acteur suivi (concurrent, centre technologique, référence interne...).
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
            -- Une relation (partenaire/fournisseur/client) entre deux acteurs, pour /api/network.
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
            -- Un fait ponctuel sourcé sur un acteur : certification obtenue ou différenciateur.
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
            -- Un événement daté sourcé sur un acteur (ex: acquisition, ouverture de site).
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
            -- Socle firmographique (chantier 3/5) : ce que l'acteur EST en tant qu'entreprise
            -- (année de création, forme juridique, tranche d'effectif), par opposition à ce
            -- qu'il FAIT (marché, technologie), déjà couvert par evidence/offers. Alimenté par
            -- firmographics.collect_french_registry() -- voir ce module pour la portée exacte
            -- (France uniquement, liste d'alias SIREN vérifiés à la main, pas de matching par
            -- nom automatique) et ce qui reste volontairement NULL (revenue_eur, parent_group,
            -- sites_json, cleanroom_iso_class, laser_systems_count : aucune source branchée
            -- pour l'instant, colonnes réservées plutôt que fabriquées).
            CREATE TABLE IF NOT EXISTS actor_profile (
                actor_id INTEGER PRIMARY KEY REFERENCES actors(id) ON DELETE CASCADE,
                founded_year INTEGER,
                legal_form_code TEXT,
                headcount_bracket_code TEXT,
                revenue_eur REAL,
                parent_group TEXT,
                sites_json TEXT,
                cleanroom_iso_class TEXT,
                laser_systems_count INTEGER,
                registry_id TEXT,
                registry_name TEXT,
                registry_active INTEGER,
                source_url TEXT,
                as_of_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Enveloppe de capacités chiffrées (chantier 5), alimentée par
            -- capabilities.collect_capability_specs() : extraction déterministe (regex +
            -- lexique MATERIALS, pas d'IA) sur les pages product/equipment/capability déjà
            -- crawlées. Une ligne par acteur ; chaque champ numérique retient la MEILLEURE
            -- valeur trouvée (min pour min_feature_size_um/tolerance_um/pulse_duration_fs,
            -- max pour max_part_size_mm/throughput_units_per_h) parmi toutes ses pages --
            -- voir le docstring de capabilities.py pour le détail de chaque pattern et
            -- pourquoi batch_size_range reste la citation brute plutôt qu'une valeur reformulée.
            CREATE TABLE IF NOT EXISTS capability_spec (
                actor_id INTEGER PRIMARY KEY REFERENCES actors(id) ON DELETE CASCADE,
                min_feature_size_um REAL,
                tolerance_um REAL,
                max_part_size_mm REAL,
                throughput_units_per_h REAL,
                wavelengths_nm TEXT,
                pulse_duration_fs REAL,
                materials_qualified TEXT,
                batch_size_range TEXT,
                source_url TEXT,
                as_of_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Une page web connue pour un acteur : URL, dernier statut HTTP, hash de contenu
            -- (pour détecter les changements), type de page détecté, score de priorité de
            -- crawl... C'est la table centrale que scrapers.scrape_actors() alimente au fil du
            -- crawl (une ligne par URL découverte/visitée).
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
            -- Historique des versions d'une page (chantier 6 : "le content_hash détecte déjà
            -- le changement, il suffit de conserver l'avant"). Une ligne par changement de
            -- contenu détecté -- voir scrapers.scrape_actors, juste avant que la ligne
            -- actor_sources correspondante ne soit écrasée par la nouvelle version : c'est
            -- l'ancien (content_hash,blocks_json,title) qui est archivé ici, jamais le nouveau
            -- (déjà dans actor_sources, pas besoin d'un double). Retention bornée par
            -- scrapers.PAGE_VERSIONS_RETENTION, pour que l'historique ne grossisse pas sans
            -- limite au fil des recrawls mensuels.
            CREATE TABLE IF NOT EXISTS page_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES actor_sources(id) ON DELETE CASCADE,
                content_hash TEXT,
                blocks_json TEXT,
                title TEXT,
                captured_at TEXT,
                archived_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS page_versions_source_idx ON page_versions(source_id);
            -- Historique des lancements de collecte (une ligne par clic sur "Lancer le crawl
            -- acteurs" -- voir app.py: start_scrape / _run_job).
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
            -- État du profil de crawl "adaptatif" d'un acteur (1 ligne par acteur) : stratégie
            -- retenue (adaptive = avec IA de secours, generic = purement déterministe), état de
            -- couverture des types de page stratégiques, dernière erreur éventuelle.
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
            # content_hash au moment de la DERNIERE extraction marché (scrapers.scrape_market),
            # distinct de content_hash lui-même (qui reflète le dernier CRAWL, scrape_actors).
            # Une page dont content_hash==market_extracted_hash n'a pas changé depuis sa
            # dernière analyse marché et peut être sautée (voir _select_market_sources,
            # chantier 2 item 4 : "ne re-analyser que les pages dont le content_hash a changé").
            "market_extracted_hash": "TEXT",
            # Date de publication de la page (chantier 4), extraite au moment du crawl (voir
            # hybrid._extract_published_date) -- stockée ici pour que scrape_market() puisse la
            # lire sans re-télécharger/re-parser le HTML, qu'elle utilise ou non les blocs
            # mis en cache (_stored_blocks).
            "published_date": "TEXT",
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
            -- Un "fait marché" canonique : tel acteur adresse tel marché, avec tel composant,
            -- via telle opération laser, à tel niveau de maturité industrielle (bucket).
            -- Une ligne = un fait unique (déduplication via fact_key/application_key, voir
            -- market_fact_key/application_key plus haut) ; les citations/preuves qui le
            -- confirment sont dans evidence_sources, pas dupliquées ici.
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
            # Chantier 4 : architecture avait sa propre dimension dans _candidate() depuis le
            # début, mais jamais de colonne -- seulement du texte concaténé dans
            # industrial_stage ("Architecture: TGV"), donc invisible dès qu'on arrêterait cette
            # concaténation. evidence_type : voir classify_evidence_type ci-dessus.
            "architecture": "TEXT",
            "evidence_type": "TEXT",
        })
        db.executescript(
            """
            -- Une preuve individuelle (URL + citation exacte) à l'appui d'une ligne evidence.
            -- Plusieurs preuves peuvent citer le même fait (plusieurs langues, plusieurs pages) --
            -- c'est ce qui alimente le compteur "proofs"/"languages" affiché dans l'UI.
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

            -- Une capacité/offre d'un acteur (ex: "service de micro-usinage laser") qui n'est
            -- PAS forcément rattachée à un marché ou un composant précis -- contrairement à
            -- evidence, qui exige les trois dimensions marché/composant/opération.
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

            -- Historique : à quelle date un fait evidence a changé de bucket de maturité
            -- (ex: radar -> existing), pour pouvoir tracer sa progression dans le temps.
            CREATE TABLE IF NOT EXISTS evidence_bucket_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                from_bucket TEXT,
                to_bucket TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_bucket_transitions_evidence_idx
                ON evidence_bucket_transitions(evidence_id);

            -- File d'attente de relecture humaine : un libellé proposé par l'IA (marché/
            -- composant/opération) qui ne correspond à aucune entrée connue des lexiques de
            -- scrapers.py. Un analyste valide ou rejette via /api/vocabulary-candidates
            -- (voir accept_vocabulary_candidate/reject_vocabulary_candidate ci-dessous).
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

            -- Libellés promus depuis vocabulary_candidates : rechargés en mémoire au démarrage
            -- de chaque collecte marché (voir scrapers._load_custom_lexicon_entries) pour
            -- enrichir les lexiques MARKETS/COMPONENTS/OPERATIONS sans redéployer le code.
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
            # Chantier 4 (fiabiliser la preuve) : is_verbatim distingue une citation exacte
            # (le scraper garantit déjà `quote in block.text`, voir scrapers._candidate) d'une
            # reformulation -- jusqu'ici les deux cohabitaient dans `quote` sans distinction
            # visible. source_date reçoit la date de publication de la page (voir
            # hybrid._extract_published_date), à défaut la date d'observation.
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
            "source_date": "TEXT",
            "evidence_type": "TEXT",
        })
        _add_columns(db, "evidence_sources", {
            "relation_strength": "TEXT",
            "relation_evidence": "TEXT",
            "source_role": "TEXT",
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
        })
        _add_columns(db, "evidence", {
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
        })
        _add_columns(db, "offer_sources", {
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
            "source_date": "TEXT",
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
        # Chantier 4 : rows written by the scraper always set extraction_mode ("block-rules",
        # "anthropic:...", "ollama:..." -- see scrapers._candidate/_offer_candidates/
        # _ai_candidates) and are always genuinely verbatim (the pipeline enforces
        # `quote in block.text`); extraction_mode IS NULL is exactly the audit's own signal for
        # a manually-entered/seeded row (the 40 "pre_batch2"/"pre_deepenrich" groups etc.),
        # whose `quote` is often a paraphrase, not an exact excerpt. One-time backfill: harmless
        # to re-run since is_verbatim only ever moves 1->0 here, never back. `offers` has no
        # extraction_mode column at all (unlike evidence/*_sources) -- every offer row has
        # always come from the scraper, so its DEFAULT 1 is already correct with no backfill.
        db.execute("UPDATE evidence SET is_verbatim=0 WHERE extraction_mode IS NULL")
        db.execute("UPDATE evidence_sources SET is_verbatim=0 WHERE extraction_mode IS NULL")
        db.execute("UPDATE offer_sources SET is_verbatim=0 WHERE extraction_mode IS NULL")
        _migrate_industrial_stage_concatenation(db)
        _backfill_evidence_type(db)
        # Both migrations run on every startup, not just once: they are idempotent (a row that
        # already carries its canonical key is only re-derived, never duplicated) and this keeps
        # the evidence table self-healing if a row is ever inserted or edited outside the normal
        # upsert path (manual fix, restored backup) without a fact_key/application_key.
        _migrate_evidence_fact_model(db)
        _migrate_application_keys(db)
        # Cleanup before the uniqueness constraint below, not after: on an existing database
        # where the constraint was never actually enforced (CREATE TABLE IF NOT EXISTS does not
        # retrofit constraints onto an already-created table), duplicate content rows can already
        # exist and would make CREATE UNIQUE INDEX fail outright at every future startup.
        _dedupe_source_rows(db, "evidence_sources", "evidence_id")
        _dedupe_source_rows(db, "offer_sources", "offer_id")
        db.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_fact_key_uq ON evidence(fact_key) WHERE fact_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_application_key_uq
                ON evidence(application_key) WHERE application_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_sources_fingerprint_uq ON evidence_sources(fingerprint);
            CREATE UNIQUE INDEX IF NOT EXISTS offer_sources_fingerprint_uq ON offer_sources(fingerprint);
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
            -- Une publication scientifique collectée via Crossref (voir scrapers.scrape_technology).
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
            -- Un signal de "maturité science -> industrie" pour un axe technologique donné
            -- (ex: SLE, LIPSS...), potentiellement partagé entre plusieurs acteurs d'un même
            -- projet collaboratif -- voir db.technology_signal_key.
            CREATE TABLE IF NOT EXISTS technology_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                axis TEXT NOT NULL,
                maturity_stage TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK(bucket IN ('existing','radar')),
                project_name TEXT,
                actor_names TEXT NOT NULL DEFAULT '[]',
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                fact_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                review_status TEXT NOT NULL DEFAULT 'accepted' CHECK(review_status IN ('accepted','review','rejected')),
                field_confidence REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS technology_signals_fact_key_uq ON technology_signals(fact_key);
            CREATE TABLE IF NOT EXISTS technology_signal_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL REFERENCES technology_signals(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                language TEXT,
                field_confidence REAL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS technology_signal_sources_signal_idx ON technology_signal_sources(signal_id);
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


# Deux petits raccourcis génériques utilisés par app.py pour lire les bases sans ouvrir de
# connexion à la main à chaque endpoint : `rows` pour un SELECT qui renvoie plusieurs lignes
# (converties en dicts JSON-sérialisables), `scalar` pour une seule valeur (ex: un COUNT(*)).
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


# Même principe que accept_vocabulary_candidate/reject_vocabulary_candidate, mais pour les
# faits `evidence` en attente de relecture humaine (fact_status='review' pour un fait proposé
# par l'IA, 'partial' pour un fait à 2 dimensions sur 3 -- voir scrapers._candidate et
# scrapers._ai_candidates, chantier 2 items 2 et 3). Avant ce couple de fonctions, ces faits
# atterrissaient bien en base (jamais jetés) mais n'avaient aucun moyen d'en sortir : ni
# promotion vers la matrice marché validée, ni rejet explicite.
def accept_evidence_review(evidence_id: int) -> dict[str, Any]:
    """Promote one review/partial evidence row to 'validated' after a human check.

    Raises ValueError if the row doesn't exist or was already validated (fact_status
    'validated' facts go through the normal collection pipeline, not this manual queue).
    """
    with connect(MARKET_DB) as db:
        row = db.execute("SELECT id,fact_status FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError(f"Evidence {evidence_id} not found")
        if row["fact_status"] == "validated":
            raise ValueError(f"Evidence {evidence_id} is already validated")
        db.execute(
            "UPDATE evidence SET fact_status='validated',review_status='accepted',updated_at=? WHERE id=?",
            (utc_now(), evidence_id),
        )
    return {"id": evidence_id, "fact_status": "validated"}


def reject_evidence_review(evidence_id: int) -> None:
    """Mark a review/partial evidence row reviewed-and-declined. Raises ValueError if unknown.

    fact_status is left untouched (still 'review'/'partial', for an audit trail of what was
    proposed) -- review_status='rejected' alone is enough to drop it from both the validated
    market matrix (which filters on fact_status='validated') and the pending-review queue
    (which filters on review_status='review').
    """
    with connect(MARKET_DB) as db:
        updated = db.execute(
            "UPDATE evidence SET review_status='rejected',updated_at=? WHERE id=? AND fact_status!='validated'",
            (utc_now(), evidence_id),
        ).rowcount
    if not updated:
        raise ValueError(f"Evidence {evidence_id} not found or already validated")
