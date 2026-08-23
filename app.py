from __future__ import annotations

import json
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import ACTORS_DB, MARKET_DB, TECH_DB, backup_all_databases, connect, init_databases, rows
from hybrid import OllamaClient
from scrapers import scrape_actors, scrape_market, scrape_technology

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="observatoire")
job_lock = threading.Lock()
jobs = {
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


collectors = {
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
    ollama = OllamaClient()

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
            "ollama_available": ollama.available(),
            "model": ollama.model,
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
    return rows(ACTORS_DB, """SELECT a.id,a.name,a.country,a.role,a.priority,a.official_url,a.active,a.last_scraped_at,a.last_status,
                               p.strategy,p.status AS profile_status,p.confidence,p.generated_by,p.needs_reprofile,p.health_score,
                               p.coverage_ready,p.coverage_discovered
                               FROM actors a LEFT JOIN site_profiles p ON p.actor_id=a.id
                               WHERE a.active=1 ORDER BY a.priority DESC,a.name""")


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
