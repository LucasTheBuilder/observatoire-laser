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
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import (
    ACTORS_DB,
    MARKET_DB,
    TECH_DB,
    accept_vocabulary_candidate,
    add_actor_relation,
    backup_all_databases,
    connect,
    create_actor,
    find_actor_duplicate_candidates,
    init_databases,
    reject_vocabulary_candidate,
    rows,
    set_actor_active,
    update_actor_classification,
)
from hybrid import AnthropicClient, estimate_anthropic_cost_usd, get_ai_client
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

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="observatoire")
job_lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {
    "actors": {"status": "idle", "result": None, "error": None},
    "market": {"status": "idle", "result": None, "error": None},
    "technology": {"status": "idle", "result": None, "error": None},
    "monthly": {"status": "idle", "result": None, "error": None},
}


def _collect_monthly() -> dict:
    """Run the three collectors in dependency order for a one-click monthly refresh."""
    return {
        "actors": scrape_actors(),
        "market": scrape_market(),
        "technology": scrape_technology(),
    }


collectors: dict[str, Callable[[], dict]] = {
    "actors": scrape_actors,
    "market": scrape_market,
    "technology": scrape_technology,
    "monthly": _collect_monthly,
}


def _jobs_snapshot() -> dict[str, dict]:
    with job_lock:
        return {kind: dict(job) for kind, job in jobs.items()}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_databases()
    try:
        yield
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Observatoire Laser", version="3.4.1-optimized", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def _last_run(path: Path) -> dict | None:
    result = rows(path, "SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1")
    return result[0] if result else None


@app.get("/api/overview")
def overview():
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
                """SELECT id,actor_name,offer_type,capability,operation,laser_process,material,
                          performance,industrial_stage,created_at,last_seen_at
                   FROM offers
                   WHERE review_status='accepted' AND created_at>=?
                   ORDER BY created_at DESC
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
    # Includes paused (active=0) actors too, with the flag exposed, so the UI can offer a
    # "reactivate" action -- filtering them out here would make pausing one-way.
    actors = rows(ACTORS_DB, """SELECT a.id,a.name,a.country,a.role,a.priority,a.official_url,a.active,a.last_scraped_at,a.last_status,
                               a.competitive_class,a.is_reference,a.parent_actor,a.entity_note,a.review_status,
                               a.actor_type,a.business_models,
                               p.strategy,p.status AS profile_status,p.confidence,p.generated_by,p.needs_reprofile,p.health_score,
                               p.coverage_ready,p.coverage_discovered
                               FROM actors a LEFT JOIN site_profiles p ON p.actor_id=a.id
                               ORDER BY a.active DESC,a.priority DESC,a.name""")
    # Evidence gate (P0): a C1/C2 label is only an analyst's classification until our own
    # crawl has actually produced a capability/service claim for that actor -- otherwise it
    # is indistinguishable from a guess. This never downgrades the class, it just flags it.
    confirmed_actors = {row["actor_name"] for row in rows(MARKET_DB, "SELECT DISTINCT actor_name FROM offers")}
    # Value-chain stages (P1): derived live from offers.industrial_stage/evidence.industrial_stage
    # instead of a separately-maintained field, so it can never drift from the actual facts.
    stages_by_actor: dict[str, set[str]] = {}
    for row in (
        rows(MARKET_DB, "SELECT actor_name,industrial_stage FROM offers")
        + rows(MARKET_DB, "SELECT actor_name,industrial_stage FROM evidence")
    ):
        stage = _leading_value_chain_stage(row["industrial_stage"])
        if stage:
            stages_by_actor.setdefault(row["actor_name"], set()).add(stage)
    for actor in actors:
        actor["business_models"] = json.loads(actor["business_models"]) if actor["business_models"] else []
        actor["evidence_confirmed"] = (
            actor["competitive_class"] not in ("C1", "C2") or actor["name"] in confirmed_actors
        )
        demonstrated = stages_by_actor.get(actor["name"], set())
        actor["value_chain_stages"] = [stage for stage in VALUE_CHAIN_STAGES if stage in demonstrated]
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
    active: bool | None = None
    competitive_class: str | None = None
    is_reference: bool | None = None
    parent_actor: str | None = None
    entity_note: str | None = None
    review_status: str | None = None
    actor_type: str | None = None
    business_models: list[str] | None = None


@app.patch("/api/actors/{actor_id}")
def update_actor(actor_id: int, payload: ActorUpdateRequest):
    """Partial update: pause/resume, and/or set the analytical classification fields
    (competitive class, actor type, business model(s), internal-reference flag, M&A
    parent/note, review status). History (sources, evidence) is always kept -- only these
    columns change.
    """
    try:
        if payload.active is not None:
            set_actor_active(actor_id, payload.active)
        update_actor_classification(
            actor_id,
            competitive_class=payload.competitive_class,
            is_reference=payload.is_reference,
            parent_actor=payload.parent_actor,
            entity_note=payload.entity_note,
            review_status=payload.review_status,
            actor_type=payload.actor_type,
            business_models=payload.business_models,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": actor_id, **payload.model_dump(exclude_none=True)}


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
    tech_links = rows(MARKET_DB, "SELECT DISTINCT actor_name,operation FROM offers WHERE operation IS NOT NULL AND operation<>''")

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
    grouped = rows(
        MARKET_DB,
        """SELECT e.id,e.bucket,e.market,e.component,e.operation,
                  COUNT(es.id) AS proofs,
                  COUNT(DISTINCT COALESCE(es.language,'unknown')) AS languages
           FROM evidence e
           LEFT JOIN evidence_sources es ON es.evidence_id=e.id
           WHERE e.fact_status='validated' AND e.evidence_kind='market_application'
             AND e.bucket IN ('existing','radar')
             AND e.market IS NOT NULL AND e.component IS NOT NULL AND e.operation IS NOT NULL
             AND TRIM(e.market)!='' AND e.market!='Non identifié'
             AND TRIM(e.component)!='' AND TRIM(e.operation)!=''
           GROUP BY e.id,e.bucket,e.market,e.component,e.operation
           ORDER BY e.market,e.component,e.operation""",
    )
    return {
        "existing": [item for item in grouped if item["bucket"] == "existing"],
        "radar": [item for item in grouped if item["bucket"] == "radar"],
    }


@app.get("/api/market/proofs")
def proofs(bucket: str, market: str, component: str, operation: str):
    return rows(
        MARKET_DB,
        """SELECT e.actor_name,e.industrial_stage,es.source_url,es.source_title,es.source_date,es.quote,
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
    try:
        return accept_vocabulary_candidate(candidate_id, payload.dimension)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/vocabulary-candidates/{candidate_id}/reject")
def reject_vocabulary(candidate_id: int):
    try:
        reject_vocabulary_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": candidate_id, "status": "rejected"}


@app.get("/api/offers")
def offers():
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
                  os.source_url,os.source_title,os.quote,os.language,os.block_heading,os.extraction_mode,os.field_confidence
           FROM offers o JOIN offer_sources os ON os.offer_id=o.id
           WHERE o.id=? AND o.review_status='accepted'
           ORDER BY os.created_at DESC""",
        (offer_id,),
    )


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
def start_scrape(kind: Literal["actors", "market", "technology", "monthly"]):
    with job_lock:
        if any(job["status"] == "running" for job in jobs.values()):
            raise HTTPException(status_code=409, detail="Une collecte est déjà en cours.")
        jobs[kind] = {"status": "running", "result": None, "error": None}
    executor.submit(_run_job, kind)
    return _jobs_snapshot()[kind]


@app.get("/api/scrape/{kind}")
def scrape_status(kind: Literal["actors", "market", "technology", "monthly"]):
    return _jobs_snapshot()[kind]


if __name__ == "__main__":
    init_databases()
    threading.Timer(1.4, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run(app, host="127.0.0.1", port=8765)
