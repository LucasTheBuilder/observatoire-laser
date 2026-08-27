"""API web de l'Observatoire Laser (FastAPI) : sert le front (static/) et expose les données
collectées par db.py/scrapers.py sous forme d'endpoints JSON.

Ce fichier ne contient pas de logique de crawl ni d'extraction : il lit/écrit les 3 bases
SQLite via db.py, et déclenche les collectes (scrapers.py) en tâche de fond via un petit
système de "jobs" maison (voir `jobs`, `executor`, `_run_job`) plutôt que d'utiliser Celery/
RQ -- une seule collecte à la fois suffit pour ce volume de données.

Organisation des endpoints (tous préfixés /api/, sauf `/` qui sert index.html) :
- /api/overview, /api/monthly, /api/pipeline-funnel : tableaux de bord / synthèses calculées
  à la volée à partir des données déjà en base (aucun appel réseau).
- /api/actors* : CRUD sur les acteurs suivis, leurs relations, leurs faits/événements sourcés.
- /api/network : graphe acteurs <-> marchés <-> technologies pour la carte réseau du front.
- /api/profiles* : état des profils de crawl adaptatifs (site_profiles).
- /api/market, /api/market/proofs : faits marché validés et leurs preuves sourcées.
- /api/market/review* : file de relecture humaine des faits partiels/proposés par l'IA
  (fact_status='partial'/'review') avant qu'ils ne rejoignent /api/market.
- /api/vocabulary-candidates* : file de relecture humaine des libellés proposés par l'IA.
- /api/offers*, /api/technology-signals* : offres concurrentes et signaux technologiques.
- /api/scrape/{kind} : démarre/consulte une collecte (actors/market/technology/cordis/
  firmographics/openalex/press/monthly).
"""

from __future__ import annotations

from dotenv import load_dotenv

# Must run before importing db/hybrid/scrapers: those modules read several settings
# (BACKUP_RETENTION_COUNT, CRAWL_DELAY_SECONDS, CRAWLER_CONTACT, ...) from the environment
# at import time, so .env has to be loaded first for a local (non-Docker) run to pick them
# up. Docker Compose already injects its own environment/env_file, so this is a no-op there.
load_dotenv()

import json
import threading
import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cordis import collect_cordis
from db import (
    ACTORS_DB,
    MARKET_DB,
    TECH_DB,
    accept_evidence_review,
    accept_vocabulary_candidate,
    add_actor_relation,
    backup_all_databases,
    connect,
    create_actor,
    delete_actor,
    find_actor_duplicate_candidates,
    init_databases,
    reject_evidence_review,
    reject_vocabulary_candidate,
    rows,
    set_actor_active,
    update_actor_classification,
)
from firmographics import collect_french_registry
from hybrid import AnthropicClient, estimate_anthropic_cost_usd, get_ai_client
from openalex import collect_openalex_publications
from press import collect_press_mentions
from scrapers import MATURITY_RULES, scrape_actors, scrape_market, scrape_technology

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Low-to-high maturity order for display, derived from the same rules the crawler uses to
# detect a fact's stage (MATURITY_RULES is declared highest-first, for match priority).
VALUE_CHAIN_STAGES = [name for name, _bucket, _terms in reversed(MATURITY_RULES)]


def _leading_value_chain_stage(raw: str | None) -> str | None:
    """offers/evidence.industrial_stage can be a composite like "Prototype | Matériau: Verre"
    (see scrapers._candidate's stage_parts) -- only the leading maturity name is a stage.
    """
    if not raw:
        return None
    name = raw.split("|", 1)[0].strip()
    return name if name in VALUE_CHAIN_STAGES else None


def _coverage_level(source_count: int) -> str:
    """How much of market.db's own evidence backs an actor's fiche -- computed live from
    distinct documentary sources (offers + evidence) rather than stored, so it can never
    drift from the facts actually collected. Thresholds calibrated against the fiches-cibles
    audit's own "Couverture documentaire" ratings (bonne/partielle/faible).
    """
    if source_count >= 10:
        return "good"
    if source_count >= 1:
        return "partial"
    return "weak"

# Un seul worker : les collectes (scrape_actors/scrape_market/scrape_technology) sont
# longues et intensives en réseau/IA, donc on les sérialise plutôt que de les paralléliser --
# voir start_scrape() qui refuse de lancer un job si un autre tourne déjà (HTTP 409).
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="observatoire")
job_lock = threading.Lock()
# État en mémoire (pas en base) de chaque type de collecte : idle -> running -> completed/failed.
# Remis à zéro à chaque redémarrage de l'appli ; l'historique persistant, lui, vit dans la
# table collection_runs de chaque base (voir db.py).
jobs: dict[str, dict[str, Any]] = {
    "actors": {"status": "idle", "result": None, "error": None},
    "market": {"status": "idle", "result": None, "error": None},
    "technology": {"status": "idle", "result": None, "error": None},
    "cordis": {"status": "idle", "result": None, "error": None},
    "firmographics": {"status": "idle", "result": None, "error": None},
    "openalex": {"status": "idle", "result": None, "error": None},
    "press": {"status": "idle", "result": None, "error": None},
    "monthly": {"status": "idle", "result": None, "error": None},
}


def _collect_monthly() -> dict:
    """Run the seven collectors in dependency order for a one-click monthly refresh."""
    # Ordre important : market/technology s'appuient sur les pages découvertes par actors.
    # cordis/firmographics/openalex/press n'ont aucune dépendance sur le crawl web (sources
    # indépendantes, chantiers 3 et 5).
    return {
        "actors": scrape_actors(),
        "market": scrape_market(),
        "technology": scrape_technology(),
        "cordis": collect_cordis(),
        "firmographics": collect_french_registry(),
        "openalex": collect_openalex_publications(),
        "press": collect_press_mentions(),
    }


collectors: dict[str, Callable[[], dict]] = {
    "actors": scrape_actors,
    "market": scrape_market,
    "technology": scrape_technology,
    "cordis": collect_cordis,
    "firmographics": collect_french_registry,
    "openalex": collect_openalex_publications,
    "press": collect_press_mentions,
    "monthly": _collect_monthly,
}


def _jobs_snapshot() -> dict[str, dict]:
    with job_lock:
        return {kind: dict(job) for kind, job in jobs.items()}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # S'exécute une fois au démarrage de l'appli (avant le premier appel API) : crée/met à
    # jour le schéma des 3 bases. `yield` cède la main pendant toute la durée de vie du
    # serveur ; le code après `finally` s'exécute à l'arrêt (Ctrl+C, redéploiement...).
    init_databases()
    try:
        yield
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Observatoire Laser", version="3.4.1-optimized", lifespan=lifespan)


# StaticFiles ships an ETag/Last-Modified but no Cache-Control, which leaves browsers free to use
# heuristic caching -- serving a stale app.js/CSS after a redeploy without even asking the server
# first (this bit a real fix: the dialog-close CSS change wasn't visible until a hard refresh).
# no-cache (not no-store) still lets the browser cache locally, it just forces a conditional
# revalidation on every load, so a change is picked up on the very next request instead of never.
# Middleware FastAPI : s'exécute pour CHAQUE requête HTTP (pas seulement /static/), d'où le
# `call_next(request)` qui laisse d'abord la requête suivre son chemin normal, puis le filtre
# `if request.url.path.startswith("/static/")` pour ne modifier l'en-tête que sur les assets.
@app.middleware("http")
async def no_cache_static_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def _last_run(path: Path) -> dict | None:
    result = rows(path, "SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1")
    return result[0] if result else None


@app.get("/api/overview")
def overview():
    """Chiffres-clés affichés en haut du tableau de bord : nb d'acteurs, faits marché existants/
    radar, documents techno, état des profils de crawl adaptatifs, jobs en cours... Tout est
    recalculé à la volée depuis les 3 bases à chaque appel (aucun cache), donc toujours à jour."""
    ai_client = get_ai_client()

    # One connection per database instead of opening a new SQLite connection for every scalar.
    with connect(ACTORS_DB) as db:
        actor_stats = db.execute(
            """SELECT
                   SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS count,
                   SUM(CASE WHEN active=1 AND priority=1 THEN 1 ELSE 0 END) AS priority
               FROM actors"""
        ).fetchone()
        adaptive_stats = db.execute(
            """SELECT
                   SUM(CASE WHEN strategy='adaptive' AND status='ready' THEN 1 ELSE 0 END) AS ready,
                   SUM(CASE WHEN strategy='adaptive' AND status='partial' THEN 1 ELSE 0 END) AS partial,
                   SUM(CASE WHEN strategy='adaptive' AND status='degraded' THEN 1 ELSE 0 END) AS degraded,
                   SUM(CASE WHEN needs_reprofile=1 THEN 1 ELSE 0 END) AS needs_reprofile
               FROM site_profiles"""
        ).fetchone()
        actors_last = db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()

    with connect(MARKET_DB) as db:
        market_stats = db.execute(
            """SELECT
                   SUM(CASE WHEN bucket='existing' AND fact_status='validated' AND evidence_kind='market_application' THEN 1 ELSE 0 END) AS existing_count,
                   SUM(CASE WHEN bucket='radar' AND fact_status='validated' AND evidence_kind='market_application' THEN 1 ELSE 0 END) AS radar_count,
                   SUM(CASE WHEN fact_status='validated' AND evidence_kind='market_application' THEN 1 ELSE 0 END) AS evidence_count
               FROM evidence"""
        ).fetchone()
        offers_count = db.execute("SELECT COUNT(*) FROM offers WHERE review_status='accepted'").fetchone()[0]
        proof_sources = (
            db.execute("SELECT COUNT(*) FROM evidence_sources").fetchone()[0]
            + db.execute("SELECT COUNT(*) FROM offer_sources").fetchone()[0]
        )
        market_last = db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
        month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
        ai_usage_month = db.execute(
            "SELECT COALESCE(SUM(ai_input_tokens),0) AS input_tokens, COALESCE(SUM(ai_output_tokens),0) AS output_tokens "
            "FROM collection_runs WHERE started_at>=?",
            (month_start,),
        ).fetchone()
        vocabulary_pending = db.execute(
            "SELECT COUNT(*) FROM vocabulary_candidates WHERE review_status='pending'"
        ).fetchone()[0]

    with connect(TECH_DB) as db:
        technology_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        technology_last = db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()

    return {
        "actors": {
            "count": int(actor_stats["count"] or 0),
            "priority": int(actor_stats["priority"] or 0),
            "last_run": dict(actors_last) if actors_last else None,
        },
        "market": {
            "existing": int(market_stats["existing_count"] or 0),
            "radar": int(market_stats["radar_count"] or 0),
            "evidence": int(market_stats["evidence_count"] or 0),
            "offers": int(offers_count or 0),
            "proof_sources": int(proof_sources or 0),
            "last_run": dict(market_last) if market_last else None,
            "vocabulary_pending": int(vocabulary_pending or 0),
        },
        "technology": {
            "documents": int(technology_count or 0),
            "last_run": dict(technology_last) if technology_last else None,
            "scheduled": False,
        },
        "adaptive": {
            "ready": int(adaptive_stats["ready"] or 0),
            "partial": int(adaptive_stats["partial"] or 0),
            "degraded": int(adaptive_stats["degraded"] or 0),
            "needs_reprofile": int(adaptive_stats["needs_reprofile"] or 0),
            "ollama_available": ai_client.available(),
            "model": ai_client.model,
            "ai_provider": "anthropic" if isinstance(ai_client, AnthropicClient) else "ollama",
            "ai_cost_month_usd": (
                estimate_anthropic_cost_usd(
                    ai_client.model, int(ai_usage_month["input_tokens"]), int(ai_usage_month["output_tokens"])
                )
                if isinstance(ai_client, AnthropicClient)
                else None
            ),
        },
        "jobs": _jobs_snapshot(),
    }


@app.get("/api/monthly")
def monthly(days: int = Query(default=30, ge=1, le=365)):
    """Return a compact delta view for the analyst's recurring review."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    with connect(ACTORS_DB) as db:
        changed_sources_count = db.execute(
            """SELECT COUNT(*)
               FROM actor_sources
               WHERE active=1 AND last_changed_at IS NOT NULL AND last_changed_at>=?""",
            (cutoff,),
        ).fetchone()[0]
        changed_sources = [
            dict(row) for row in db.execute(
                """SELECT a.name AS actor_name,s.url,s.page_type,s.last_title,s.last_changed_at
                   FROM actor_sources s
                   JOIN actors a ON a.id=s.actor_id
                   WHERE s.active=1 AND s.last_changed_at IS NOT NULL AND s.last_changed_at>=?
                   ORDER BY s.last_changed_at DESC
                   LIMIT 40""",
                (cutoff,),
            )
        ]

    with connect(MARKET_DB) as db:
        recent_counts = db.execute(
            """SELECT
                   SUM(CASE WHEN evidence_kind='market_application' AND fact_status='validated' AND created_at>=? THEN 1 ELSE 0 END) AS new_market,
                   SUM(CASE WHEN evidence_kind='market_application' AND fact_status='validated' AND created_at<? AND last_seen_at>=? THEN 1 ELSE 0 END) AS resurfaced_market
               FROM evidence""",
            (cutoff, cutoff, cutoff),
        ).fetchone()
        new_offers_count = db.execute(
            "SELECT COUNT(*) FROM offers WHERE review_status='accepted' AND created_at>=?",
            (cutoff,),
        ).fetchone()[0]
        new_market = [
            dict(row) for row in db.execute(
                """SELECT id,actor_name,bucket,market,component,operation,industrial_stage,
                          field_confidence,created_at,last_seen_at
                   FROM evidence
                   WHERE evidence_kind='market_application'
                     AND fact_status='validated'
                     AND created_at>=?
                   ORDER BY created_at DESC
                   LIMIT 40""",
                (cutoff,),
            )
        ]
        new_offers = [
            dict(row) for row in db.execute(
                """SELECT o.id,o.actor_name,o.offer_type,o.capability,o.operation,o.laser_process,o.material,
                          o.performance,o.industrial_stage,o.created_at,o.last_seen_at,
                          (SELECT COUNT(*) FROM offer_sources os WHERE os.offer_id=o.id) AS proofs,
                          (SELECT COUNT(DISTINCT COALESCE(os.language,'unknown')) FROM offer_sources os WHERE os.offer_id=o.id) AS languages
                   FROM offers o
                   WHERE o.review_status='accepted' AND o.created_at>=?
                   ORDER BY o.created_at DESC
                   LIMIT 40""",
                (cutoff,),
            )
        ]
        resurfaced = [
            dict(row) for row in db.execute(
                """SELECT id,actor_name,bucket,market,component,operation,last_seen_at
                   FROM evidence
                   WHERE evidence_kind='market_application'
                     AND fact_status='validated'
                     AND created_at<?
                     AND last_seen_at>=?
                   ORDER BY last_seen_at DESC
                   LIMIT 40""",
                (cutoff, cutoff),
            )
        ]

    with connect(TECH_DB) as db:
        technology_count = db.execute(
            "SELECT COUNT(*) FROM documents WHERE created_at>=?",
            (cutoff,),
        ).fetchone()[0]
        technology = [
            dict(row) for row in db.execute(
                """SELECT id,actor_name,document_type,title,source_url,published_at,doi,patent_number,created_at
                   FROM documents
                   WHERE created_at>=?
                   ORDER BY created_at DESC
                   LIMIT 40""",
                (cutoff,),
            )
        ]

    return {
        "days": days,
        "cutoff": cutoff,
        "counts": {
            "changed_sources": int(changed_sources_count or 0),
            "new_market": int(recent_counts["new_market"] or 0),
            "new_offers": int(new_offers_count or 0),
            "resurfaced_market": int(recent_counts["resurfaced_market"] or 0),
            "technology": int(technology_count or 0),
        },
        "changed_sources": changed_sources,
        "new_market": new_market,
        "new_offers": new_offers,
        "resurfaced_market": resurfaced,
        "technology": technology,
    }


@app.get("/api/actors")
def list_actors():
    """Liste complète des acteurs avec, pour chacun, des champs calculés (jamais stockés
    directement) à partir de market.db : `evidence_confirmed` (un C1/C2 a-t-il vraiment une
    preuve en base ?), `value_chain_stages` (à quels stades de maturité il a été observé),
    `coverage_level` (bonne/partielle/faible, selon le nb de sources distinctes)."""
    # Includes paused (active=0) actors too, with the flag exposed, so the UI can offer a
    # "reactivate" action -- filtering them out here would make pausing one-way.
    actors = rows(ACTORS_DB, """SELECT a.id,a.name,a.country,a.role,a.priority,a.official_url,a.active,a.last_scraped_at,a.last_status,
                               a.competitive_class,a.is_reference,a.parent_actor,a.entity_note,a.review_status,
                               a.actor_type,a.business_models,a.strategic_summary,a.last_verified_at,
                               p.strategy,p.status AS profile_status,p.confidence,p.generated_by,p.needs_reprofile,p.health_score,
                               p.coverage_ready,p.coverage_discovered,
                               f.founded_year,f.legal_form_code,f.headcount_bracket_code,f.registry_name,f.source_url AS registry_source_url
                               FROM actors a LEFT JOIN site_profiles p ON p.actor_id=a.id
                               LEFT JOIN actor_profile f ON f.actor_id=a.id
                               ORDER BY a.active DESC,a.priority DESC,a.name""")
    # Evidence gate (P0): a C1/C2 label is only an analyst's classification until our own
    # crawl has actually produced a capability/service claim for that actor -- otherwise it
    # is indistinguishable from a guess. This never downgrades the class, it just flags it.
    # Same acceptance gate the rest of this file uses per table (offers.review_status,
    # evidence.fact_status -- see /api/offers and /api/market): an actor/stage/source count that
    # skips it would count facts still pending review, in practice always true today since
    # nothing currently leaves offers unaccepted, but worth keeping explicit and consistent.
    confirmed_actors = {
        row["actor_name"]
        for row in rows(MARKET_DB, "SELECT DISTINCT actor_name FROM offers WHERE review_status='accepted'")
    }
    # Value-chain stages (P1): derived live from offers.industrial_stage/evidence.industrial_stage
    # instead of a separately-maintained field, so it can never drift from the actual facts.
    stages_by_actor: dict[str, set[str]] = {}
    for row in (
        rows(MARKET_DB, "SELECT actor_name,industrial_stage FROM offers WHERE review_status='accepted'")
        + rows(MARKET_DB, "SELECT actor_name,industrial_stage FROM evidence WHERE fact_status='validated'")
    ):
        stage = _leading_value_chain_stage(row["industrial_stage"])
        if stage:
            stages_by_actor.setdefault(row["actor_name"], set()).add(stage)
    # Coverage level (fiches-cibles audit): computed from the same distinct-source count the
    # pipeline funnel uses, never hand-set, so a fiche can't claim more than market.db proves.
    sources_by_actor: dict[str, set[str]] = {}
    for row in (
        rows(MARKET_DB, "SELECT o.actor_name,os.source_url FROM offer_sources os JOIN offers o ON o.id=os.offer_id WHERE o.review_status='accepted'")
        + rows(MARKET_DB, "SELECT e.actor_name,es.source_url FROM evidence_sources es JOIN evidence e ON e.id=es.evidence_id WHERE e.fact_status='validated'")
    ):
        sources_by_actor.setdefault(row["actor_name"], set()).add(row["source_url"])
    facts_by_actor: dict[int, list[dict[str, Any]]] = {}
    for row in rows(ACTORS_DB, "SELECT actor_id,dimension,value,source_url FROM actor_facts ORDER BY dimension,id"):
        facts_by_actor.setdefault(row["actor_id"], []).append(row)
    events_by_actor: dict[int, list[dict[str, Any]]] = {}
    for row in rows(ACTORS_DB, "SELECT actor_id,event_type,description,event_date,source_url FROM actor_events ORDER BY event_date DESC,id"):
        events_by_actor.setdefault(row["actor_id"], []).append(row)
    for actor in actors:
        actor["business_models"] = json.loads(actor["business_models"]) if actor["business_models"] else []
        actor["evidence_confirmed"] = (
            actor["competitive_class"] not in ("C1", "C2") or actor["name"] in confirmed_actors
        )
        demonstrated = stages_by_actor.get(actor["name"], set())
        actor["value_chain_stages"] = [stage for stage in VALUE_CHAIN_STAGES if stage in demonstrated]
        actor["coverage_level"] = _coverage_level(len(sources_by_actor.get(actor["name"], set())))
        actor["facts"] = facts_by_actor.get(actor["id"], [])
        actor["events"] = events_by_actor.get(actor["id"], [])
    return actors


@app.get("/api/actors/duplicates")
def actor_duplicates():
    """Name/domain/parent-company similarity report for human review -- never merges or
    deletes anything by itself.
    """
    return find_actor_duplicate_candidates()


@app.get("/api/pipeline-funnel")
def pipeline_funnel():
    """Discovered -> fetched -> parsed -> evidence -> validated funnel (P0 audit item): a
    row in actor_sources is not proof of anything by itself -- this reports how many
    actually became a stored, human-reviewable claim, instead of just counting pages.
    """
    with connect(ACTORS_DB) as db:
        discovered = db.execute("SELECT COUNT(*) FROM actor_sources").fetchone()[0]
        fetched = db.execute("SELECT COUNT(*) FROM actor_sources WHERE last_http_status IS NOT NULL").fetchone()[0]
        parsed = db.execute("SELECT COUNT(*) FROM actor_sources WHERE extraction_mode IS NOT NULL").fetchone()[0]
    evidence_urls: set[str] = set()
    validated_urls: set[str] = set()
    with connect(MARKET_DB) as db:
        for row in db.execute("SELECT os.source_url,o.review_status FROM offer_sources os JOIN offers o ON o.id=os.offer_id"):
            evidence_urls.add(row["source_url"])
            if row["review_status"] == "accepted":
                validated_urls.add(row["source_url"])
        for row in db.execute("SELECT es.source_url,e.review_status FROM evidence_sources es JOIN evidence e ON e.id=es.evidence_id"):
            evidence_urls.add(row["source_url"])
            if row["review_status"] == "accepted":
                validated_urls.add(row["source_url"])
    return {
        "discovered": discovered, "fetched": fetched, "parsed": parsed,
        "evidence": len(evidence_urls), "validated": len(validated_urls),
    }


# --- CRUD acteurs : les modèles Pydantic ci-dessous valident/documentent automatiquement le
# corps JSON attendu par FastAPI pour chaque endpoint POST/PATCH. ---
class ActorCreateRequest(BaseModel):
    name: str
    country: str
    role: str
    official_url: str
    priority: bool = False


@app.post("/api/actors")
def add_actor(payload: ActorCreateRequest):
    """Add an actor outside the static seed list -- grows coverage without a code deploy."""
    try:
        actor_id = create_actor(payload.name, payload.country, payload.role, payload.official_url, payload.priority)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": actor_id, "name": payload.name}


class ActorUpdateRequest(BaseModel):
    name: str | None = None
    country: str | None = None
    role: str | None = None
    official_url: str | None = None
    priority: bool | None = None
    active: bool | None = None
    competitive_class: str | None = None
    is_reference: bool | None = None
    parent_actor: str | None = None
    entity_note: str | None = None
    review_status: str | None = None
    actor_type: str | None = None
    business_models: list[str] | None = None
    strategic_summary: str | None = None


@app.patch("/api/actors/{actor_id}")
def update_actor(actor_id: int, payload: ActorUpdateRequest):
    """Partial update: edit the descriptive fields (name, country, role, official_url,
    priority), pause/resume, and/or set the analytical classification fields (competitive
    class, actor type, business model(s), internal-reference flag, M&A parent/note, review
    status). History (sources, evidence) is always kept -- only these columns change.
    """
    try:
        if payload.active is not None:
            set_actor_active(actor_id, payload.active)
        update_actor_classification(
            actor_id,
            name=payload.name,
            country=payload.country,
            role=payload.role,
            official_url=payload.official_url,
            priority=payload.priority,
            competitive_class=payload.competitive_class,
            is_reference=payload.is_reference,
            parent_actor=payload.parent_actor,
            entity_note=payload.entity_note,
            review_status=payload.review_status,
            actor_type=payload.actor_type,
            business_models=payload.business_models,
            strategic_summary=payload.strategic_summary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": actor_id, **payload.model_dump(exclude_none=True)}


@app.delete("/api/actors/{actor_id}")
def remove_actor(actor_id: int):
    """Permanently delete an actor and everything scraped for it. Irreversible -- the UI
    must confirm with the user before calling this.
    """
    try:
        delete_actor(actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": actor_id, "deleted": True}


class ActorRelationRequest(BaseModel):
    relation_type: str
    related_name: str
    related_actor_id: int | None = None
    note: str | None = None
    source_url: str | None = None


@app.post("/api/actors/{actor_id}/relations")
def add_relation(actor_id: int, payload: ActorRelationRequest):
    """Record a partner/supplier/client edge for the network map (see /api/network)."""
    try:
        relation_id = add_actor_relation(
            actor_id, payload.relation_type, payload.related_name,
            related_actor_id=payload.related_actor_id, note=payload.note, source_url=payload.source_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": relation_id}


@app.get("/api/network")
def network():
    """Actor <-> market <-> technology <-> partner graph for the network map. Built from
    already-collected evidence/offers (no new scraping): an actor only appears once it has
    at least one demonstrated market, technology, or recorded relation, so an actor with no
    data yet doesn't clutter the map as an isolated dot.
    """
    actors = {
        row["name"]: row
        for row in rows(ACTORS_DB, "SELECT id,name,competitive_class FROM actors WHERE active=1 AND review_status='verified' AND is_reference=0")
    }
    relations = rows(
        ACTORS_DB,
        """SELECT a.name AS actor_name,r.related_name,r.relation_type
           FROM actor_relations r JOIN actors a ON a.id=r.actor_id""",
    )
    parents = rows(ACTORS_DB, "SELECT name,parent_actor FROM actors WHERE parent_actor IS NOT NULL AND parent_actor<>''")
    market_links = rows(
        MARKET_DB,
        """SELECT DISTINCT actor_name,market FROM evidence
           WHERE market IS NOT NULL AND market<>'' AND review_status='accepted' AND bucket IN ('existing','radar')""",
    )
    # Bug fix: every other offers query in this file (list_actors, pipeline_funnel, /api/offers,
    # /api/offers/{id}/proofs) gates on review_status='accepted'; this one didn't, so an offer
    # stuck in 'review'/'rejected' would still show up as a technology edge on the network map.
    tech_links = rows(
        MARKET_DB,
        "SELECT DISTINCT actor_name,operation FROM offers WHERE operation IS NOT NULL AND operation<>'' AND review_status='accepted'",
    )

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    def actor_node(name: str) -> str:
        node_id = f"actor:{name}"
        if node_id not in nodes:
            info = actors.get(name)
            nodes[node_id] = {"id": node_id, "type": "actor", "label": name, "class": info["competitive_class"] if info else None}
        return node_id

    for link in market_links:
        if link["actor_name"] not in actors:
            continue
        market_id = f"market:{link['market']}"
        nodes.setdefault(market_id, {"id": market_id, "type": "market", "label": link["market"]})
        edges.append({"source": actor_node(link["actor_name"]), "target": market_id})

    for link in tech_links:
        if link["actor_name"] not in actors:
            continue
        tech_id = f"tech:{link['operation']}"
        nodes.setdefault(tech_id, {"id": tech_id, "type": "technology", "label": link["operation"]})
        edges.append({"source": actor_node(link["actor_name"]), "target": tech_id})

    for row in parents:
        if row["name"] not in actors:
            continue
        parent_id = f"actor:{row['parent_actor']}"
        nodes.setdefault(parent_id, {"id": parent_id, "type": "actor", "label": row["parent_actor"], "class": None})
        edges.append({"source": actor_node(row["name"]), "target": parent_id, "relation": "parent"})

    for row in relations:
        if row["actor_name"] not in actors:
            continue
        related_id = f"actor:{row['related_name']}"
        nodes.setdefault(related_id, {"id": related_id, "type": "actor", "label": row["related_name"], "class": None})
        edges.append({"source": actor_node(row["actor_name"]), "target": related_id, "relation": row["relation_type"]})

    return {"nodes": list(nodes.values()), "edges": edges}


@app.get("/api/profiles")
def profiles():
    """État de crawl de chaque acteur actif : stratégie retenue, confiance, dernière erreur,
    et couverture par type de page stratégique (décodée depuis coverage_json)."""
    profiles = rows(ACTORS_DB, """SELECT a.id,a.name,a.priority,a.official_url,p.strategy,p.status,p.confidence,
                               p.generated_by,p.version,p.last_profiled_at,p.needs_reprofile,p.failure_count,
                               p.health_score,p.last_error,p.coverage_json,p.coverage_ready,p.coverage_discovered
                               FROM actors a JOIN site_profiles p ON p.actor_id=a.id
                               WHERE a.active=1 ORDER BY a.priority DESC,a.name""")
    for profile in profiles:
        try:
            profile["coverage"] = json.loads(profile.pop("coverage_json") or "{}")
        except (TypeError, ValueError):
            profile["coverage"] = {}
    return profiles


@app.get("/api/profiles/{actor_id}/sources")
def profile_sources(actor_id: int):
    return rows(ACTORS_DB, """SELECT url,page_type,source_score,last_http_status,last_checked_at,last_title,
                               ambiguous,last_error,extraction_mode,discovery_depth
                               FROM actor_sources WHERE actor_id=? AND active=1
                               ORDER BY source_score DESC,id""", (actor_id,))


@app.get("/api/market")
def market():
    """Vue "matrice marché" du front : chaque ligne evidence validée devient un item, classé
    en `existing` (production industrielle) ou `radar` (R&D/pilote/prototype) selon son bucket.
    Le détail de chaque preuve (citation, URL...) est chargé séparément via /api/market/proofs
    quand l'utilisateur clique sur une cellule -- pour ne pas tout renvoyer d'un coup."""
    grouped = rows(
        MARKET_DB,
        """SELECT e.id,e.bucket,e.market,e.component,e.operation,e.actor_name,
                  COUNT(es.id) AS proofs,
                  COUNT(DISTINCT COALESCE(es.language,'unknown')) AS languages
           FROM evidence e
           LEFT JOIN evidence_sources es ON es.evidence_id=e.id
           WHERE e.fact_status='validated' AND e.evidence_kind='market_application'
             AND e.bucket IN ('existing','radar')
             AND e.market IS NOT NULL AND e.component IS NOT NULL AND e.operation IS NOT NULL
             AND TRIM(e.market)!='' AND e.market!='Non identifié'
             AND TRIM(e.component)!='' AND TRIM(e.operation)!=''
           GROUP BY e.id,e.bucket,e.market,e.component,e.operation,e.actor_name
           ORDER BY e.market,e.component,e.operation""",
    )
    return {
        "existing": [item for item in grouped if item["bucket"] == "existing"],
        "radar": [item for item in grouped if item["bucket"] == "radar"],
    }


@app.get("/api/market/proofs")
def proofs(bucket: str, market: str, component: str, operation: str):
    """Toutes les preuves sourcées (citations + URL) derrière une cellule de la matrice marché,
    identifiée par sa combinaison bucket/marché/composant/opération (voir /api/market)."""
    return rows(
        MARKET_DB,
        """SELECT e.actor_name,e.industrial_stage,es.source_url,es.source_title,es.source_date,es.quote,es.is_verbatim,
                  es.language,es.block_heading,es.extraction_mode,es.field_confidence,
                  es.relation_strength,es.relation_evidence,es.source_role,
                  e.laser_process,e.material,e.performance,e.maturity_level
           FROM evidence e
           JOIN evidence_sources es ON es.evidence_id=e.id
           WHERE e.bucket=? AND e.market=? AND e.component=? AND e.operation=?
             AND e.fact_status='validated'
           ORDER BY COALESCE(es.source_date,es.created_at) DESC""",
        (bucket, market, component, operation),
    )


@app.get("/api/vocabulary-candidates")
def vocabulary_candidates(status: Literal["pending", "accepted", "rejected"] = "pending"):
    """AI-proposed market/component/operation labels that matched no known lexicon entry.

    Triage queue: accept promotes one dimension's proposal into the live custom lexicon
    (usable on the next collection, no code deploy); reject just marks it reviewed.
    """
    candidates = rows(
        MARKET_DB,
        """SELECT id,actor_name,source_url,source_title,quote,block_heading,
                  proposed_labels,resolved_labels,review_status,created_at,last_seen_at
           FROM vocabulary_candidates
           WHERE review_status=?
           ORDER BY last_seen_at DESC""",
        (status,),
    )
    for candidate in candidates:
        for key in ("proposed_labels", "resolved_labels"):
            try:
                candidate[key] = json.loads(candidate[key])
            except (TypeError, ValueError):
                candidate[key] = {}
    return candidates


class VocabularyDecisionRequest(BaseModel):
    dimension: Literal["market", "component", "operation"]


@app.post("/api/vocabulary-candidates/{candidate_id}/accept")
def accept_vocabulary(candidate_id: int, payload: VocabularyDecisionRequest):
    """Valide le libellé proposé par l'IA pour une dimension (market/component/operation) :
    il devient utilisable dès la prochaine collecte, sans toucher au code (voir
    scrapers._load_custom_lexicon_entries)."""
    try:
        return accept_vocabulary_candidate(candidate_id, payload.dimension)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/vocabulary-candidates/{candidate_id}/reject")
def reject_vocabulary(candidate_id: int):
    """Marque le candidat comme relu-et-refusé, sans créer d'entrée de lexique."""
    try:
        reject_vocabulary_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": candidate_id, "status": "rejected"}


@app.get("/api/market/review")
def market_review(status: Literal["pending", "accepted", "rejected"] = "pending"):
    """File de revue humaine pour les faits marché non encore validés (chantier 2 items 2 et
    3) : ``fact_status='partial'`` (2 dimensions sur 3, la troisième non trouvée) ou
    ``fact_status='review'`` (proposé par l'IA sur un bloc que le lexique déterministe avait
    rejeté). Contrairement à la matrice /api/market, ces faits n'apparaissent nulle part
    ailleurs dans l'app tant qu'ils ne sont pas explicitement acceptés ou rejetés ici.

    ``status="pending"`` also excludes fact_status='validated' as a belt-and-suspenders check;
    it must NOT be applied to "accepted", since accept_evidence_review() is exactly what flips
    fact_status to 'validated' -- filtering it out there would make every accepted row
    permanently invisible to this endpoint, including through its own "accepted" filter.
    """
    review_status = "accepted" if status == "accepted" else ("rejected" if status == "rejected" else "review")
    clause = "evidence_kind='market_application' AND review_status=?"
    if status == "pending":
        clause += " AND fact_status!='validated'"
    return rows(
        MARKET_DB,
        f"""SELECT id,actor_name,fact_status,bucket,market,component,operation,industrial_stage,
                  source_url,source_title,source_date,quote,is_verbatim,relation_strength,relation_evidence,field_confidence,
                  extraction_mode,created_at,last_seen_at
           FROM evidence
           WHERE {clause}
           ORDER BY last_seen_at DESC""",
        (review_status,),
    )


@app.post("/api/market/review/{evidence_id}/accept")
def accept_market_review(evidence_id: int):
    """Valide un fait partiel/proposé par l'IA : il rejoint la matrice marché (/api/market)
    dès cet appel, avec fact_status='validated'."""
    try:
        return accept_evidence_review(evidence_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/market/review/{evidence_id}/reject")
def reject_market_review(evidence_id: int):
    """Marque le fait comme relu-et-refusé ; il garde son fact_status d'origine (traçabilité)
    mais ne réapparaît plus dans la file de revue ni dans la matrice marché."""
    try:
        reject_evidence_review(evidence_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": evidence_id, "status": "rejected"}


@app.get("/api/offers")
def offers():
    """Offres/capacités concurrentes validées (voir db.py: table `offers`), avec le nombre de
    preuves et de langues distinctes qui les confirment."""
    return rows(
        MARKET_DB,
        """SELECT o.id,o.actor_name,o.offer_type,o.capability,o.operation,o.laser_process,o.material,o.performance,
                  o.industrial_stage,o.page_type,COUNT(os.id) AS proofs,
                  COUNT(DISTINCT COALESCE(os.language,'unknown')) AS languages
           FROM offers o
           LEFT JOIN offer_sources os ON os.offer_id=o.id
           WHERE o.review_status='accepted'
           GROUP BY o.id,o.actor_name,o.offer_type,o.capability,o.operation,o.laser_process,o.material,o.performance,o.industrial_stage,o.page_type
           ORDER BY o.actor_name,o.offer_type,o.capability""",
    )


@app.get("/api/offers/{offer_id}/proofs")
def offer_proofs(offer_id: int):
    return rows(
        MARKET_DB,
        """SELECT o.actor_name,o.offer_type,o.capability,o.operation,o.laser_process,o.material,o.performance,o.industrial_stage,
                  os.source_url,os.source_title,os.source_date,os.quote,os.is_verbatim,os.language,os.block_heading,os.extraction_mode,os.field_confidence
           FROM offers o JOIN offer_sources os ON os.offer_id=o.id
           WHERE o.id=? AND o.review_status='accepted'
           ORDER BY os.created_at DESC""",
        (offer_id,),
    )


# Intelligence techno (P0 audit item): science->industry readiness signals are transverse --
# a project like OPeraTIC can span several actors at once -- so they get their own table
# instead of being force-fit onto one actor's facts. See db.technology_signal_key.
@app.get("/api/technology-signals")
def technology_signals():
    signals = rows(
        TECH_DB,
        """SELECT t.id,t.axis,t.maturity_stage,t.bucket,t.project_name,t.actor_names,
                  COUNT(ts.id) AS proofs,
                  COUNT(DISTINCT COALESCE(ts.language,'unknown')) AS languages
           FROM technology_signals t
           LEFT JOIN technology_signal_sources ts ON ts.signal_id=t.id
           WHERE t.review_status='accepted'
           GROUP BY t.id,t.axis,t.maturity_stage,t.bucket,t.project_name,t.actor_names
           ORDER BY t.bucket DESC,t.axis""",
    )
    for signal in signals:
        signal["actor_names"] = json.loads(signal["actor_names"]) if signal["actor_names"] else []
    return signals


@app.get("/api/technology-signals/{signal_id}/proofs")
def technology_signal_proofs(signal_id: int):
    proofs = rows(
        TECH_DB,
        """SELECT t.axis,t.maturity_stage,t.project_name,t.actor_names,
                  ts.source_url,ts.source_title,ts.quote,ts.language
           FROM technology_signals t JOIN technology_signal_sources ts ON ts.signal_id=t.id
           WHERE t.id=? AND t.review_status='accepted'
           ORDER BY ts.created_at DESC""",
        (signal_id,),
    )
    for proof in proofs:
        proof["actor_names"] = json.loads(proof["actor_names"]) if proof["actor_names"] else []
    return proofs


# Cette fonction tourne dans le thread de fond de `executor` (pas dans le thread FastAPI qui
# répond aux requêtes) : c'est elle qui appelle réellement scrape_actors/scrape_market/
# scrape_technology, potentiellement pendant plusieurs minutes, sans bloquer l'API.
def _run_job(kind: str) -> None:
    try:
        try:
            backup_all_databases()
        except Exception:
            pass  # a backup failure (e.g. disk full) must never block the collection itself
        result = collectors[kind]()
        with job_lock:
            jobs[kind] = {"status": "completed", "result": result, "error": None}
    except Exception as exc:
        with job_lock:
            jobs[kind] = {"status": "failed", "result": None, "error": str(exc)[:500]}


@app.post("/api/scrape/{kind}")
def start_scrape(kind: Literal["actors", "market", "technology", "cordis", "firmographics", "openalex", "press", "monthly"]):
    """Démarre une collecte en tâche de fond (voir _run_job) et rend la main immédiatement.

    Le front est censé ensuite sonder GET /api/scrape/{kind} régulièrement pour connaître
    l'avancement, puisque le job continue de tourner après la réponse de ce endpoint.
    """
    with job_lock:
        if any(job["status"] == "running" for job in jobs.values()):
            raise HTTPException(status_code=409, detail="Une collecte est déjà en cours.")
        jobs[kind] = {"status": "running", "result": None, "error": None}
    executor.submit(_run_job, kind)
    return _jobs_snapshot()[kind]


@app.get("/api/scrape/{kind}")
def scrape_status(kind: Literal["actors", "market", "technology", "cordis", "firmographics", "openalex", "press", "monthly"]):
    """Consulte l'état (idle/running/completed/failed) du dernier job de ce type."""
    return _jobs_snapshot()[kind]


if __name__ == "__main__":
    # Lancement en local (hors Docker) : `python app.py`. init_databases() est appelé deux
    # fois au total dans ce cas précis (ici, puis à nouveau par `lifespan` au démarrage
    # d'uvicorn) -- sans conséquence puisque la fonction est idempotente.
    init_databases()
    threading.Timer(1.4, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run(app, host="127.0.0.1", port=8765)
