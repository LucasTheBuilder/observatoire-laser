"""Qualité du dispositif de veille, pas seulement des fiches (§5.H audit veille, 30/08/2026,
Lot 4 §17) : "`_completeness` et `compute_confidence_scores` évaluent les ACTEURS. Rien n'évalue
la VEILLE." Quatre indicateurs :

1. **Rappel** -- sur un golden set de faits vérifiés à la main, combien le pipeline en
   retrouve-t-il actuellement ? Nécessite ``golden_facts`` peuplée par un humain (voir
   ``add_golden_fact``) -- vide par défaut, jamais copiée depuis ``evidence`` (ça ferait du
   golden set une redite de ce que le pipeline croit déjà, pas un test de rappel indépendant).
   Limite assumée : compare contre l'état ACTUEL d'``evidence``, pas une ré-extraction en direct
   de la page source -- un vrai test de non-régression au sens strict ré-exécuterait le pipeline
   d'extraction sur la page, ce que ce module ne fait pas.
2. **Précision** -- part de faits REJETÉS parmi ceux réellement passés par une revue humaine
   (``reviewed_at IS NOT NULL``), ventilée par ``extraction_mode``. Calculée sur les données déjà
   en base, pas besoin de saisie : la grande majorité des faits n'ont jamais été soumis à revue
   (``fact_status='validated'`` directement par le pipeline déterministe) -- les inclure
   gonflerait ce chiffre artificiellement, ils n'ont jamais eu l'occasion d'être rejetés.
3. **Latence de détection** -- médiane (``created_at`` - ``source_date``) en jours, sur les faits
   dont la date de publication réelle est connue. Calculée sur les données déjà en base.
4. **Santé de couverture** -- acteurs sans source HTTP 200 depuis N jours, et total des
   catégories de pages stratégiques marquées `missing` dans ``site_profiles.coverage_json``
   (déjà calculé à chaque crawl, jamais agrégé nulle part avant ce module -- voir aussi
   `/api/collection-health`, qui expose le détail par acteur plutôt que cet agrégat global).
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone

from db import ACTORS_DB, MARKET_DB, connect, utc_now

STALE_CRAWL_DAYS_DEFAULT = 60
# Mêmes catégories "stratégiques" que static/app.js (sectionStates) -- about/careers/datasheet/
# homepage/ignore/other/product/publication existent dans coverage_json mais ne comptent pas
# comme un manque stratégique au même sens.
STRATEGIC_PAGE_TYPES = ("service", "capability", "technology", "application", "market", "project", "news")


def compute_recall() -> dict:
    with connect(MARKET_DB) as db:
        golden = db.execute("SELECT actor_name,market,component,operation FROM golden_facts").fetchall()
        if not golden:
            return {"recall": None, "golden_facts": 0, "matched": 0}
        matched = 0
        for fact in golden:
            found = db.execute(
                """SELECT 1 FROM evidence WHERE actor_name=? AND market=? AND component=? AND operation=?
                   AND fact_status='validated' LIMIT 1""",
                (fact["actor_name"], fact["market"], fact["component"], fact["operation"]),
            ).fetchone()
            matched += int(found is not None)
    return {"recall": round(matched / len(golden), 4), "golden_facts": len(golden), "matched": matched}


def compute_precision() -> dict:
    with connect(MARKET_DB) as db:
        # La correspondance evidence -> extraction_mode passe par une sous-requête corrélée sur
        # evidence_sources -- GROUP BY directement sur son alias mé-évalue sous SQLite (constaté
        # en écrivant ce module : deux lignes de review_status/extraction_mode distincts se
        # retrouvaient regroupées comme une seule). La sous-requête intermédiaire (`ranked`)
        # matérialise la valeur par ligne AVANT le GROUP BY, qui ne porte plus alors que sur des
        # colonnes déjà résolues.
        rows = db.execute(
            """SELECT ranked.review_status, ranked.extraction_mode, COUNT(*) AS n FROM (
                   SELECT e.id, e.review_status,
                          COALESCE((SELECT es.extraction_mode FROM evidence_sources es
                                    WHERE es.evidence_id=e.id ORDER BY es.id LIMIT 1), 'block-rules') AS extraction_mode
                   FROM evidence e
                   WHERE e.reviewed_at IS NOT NULL
               ) AS ranked
               GROUP BY ranked.extraction_mode, ranked.review_status"""
        ).fetchall()
    by_mode: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_mode.setdefault(row["extraction_mode"], {"total": 0, "rejected": 0})
        bucket["total"] += row["n"]
        if row["review_status"] == "rejected":
            bucket["rejected"] += row["n"]
    return {
        "by_extraction_mode": [
            {
                "extraction_mode": mode, "total": stats["total"], "rejected": stats["rejected"],
                "rejection_rate": round(stats["rejected"] / stats["total"], 4) if stats["total"] else None,
            }
            for mode, stats in sorted(by_mode.items())
        ],
        "reviewed_total": sum(s["total"] for s in by_mode.values()),
    }


def compute_detection_latency() -> dict:
    with connect(MARKET_DB) as db:
        rows = db.execute("SELECT source_date,created_at FROM evidence WHERE source_date IS NOT NULL").fetchall()
    deltas = []
    for row in rows:
        try:
            published = date.fromisoformat(str(row["source_date"])[:10])
            created = date.fromisoformat(str(row["created_at"])[:10])
        except ValueError:
            continue
        delta = (created - published).days
        if delta >= 0:
            deltas.append(delta)
    if not deltas:
        return {"median_days": None, "sample_size": 0}
    return {"median_days": statistics.median(deltas), "sample_size": len(deltas)}


def compute_coverage_health(*, stale_days: int = STALE_CRAWL_DAYS_DEFAULT) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
    with connect(ACTORS_DB) as db:
        active_total = db.execute("SELECT COUNT(*) FROM actors WHERE active=1").fetchone()[0]
        stale_actors = db.execute(
            """SELECT COUNT(*) FROM actors a
               LEFT JOIN (
                   SELECT actor_id, MAX(last_checked_at) AS last_ok FROM actor_sources
                   WHERE last_http_status=200 GROUP BY actor_id
               ) s ON s.actor_id=a.id
               WHERE a.active=1 AND (s.last_ok IS NULL OR s.last_ok<?)""",
            (cutoff,),
        ).fetchone()[0]
        profiles = db.execute(
            """SELECT p.coverage_json FROM site_profiles p JOIN actors a ON a.id=p.actor_id WHERE a.active=1"""
        ).fetchall()
    missing_strategic_pages = 0
    for row in profiles:
        try:
            coverage = json.loads(row["coverage_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for page_type in STRATEGIC_PAGE_TYPES:
            if (coverage.get(page_type) or {}).get("status") == "missing":
                missing_strategic_pages += 1
    return {
        "active_actors": active_total,
        "stale_actors": stale_actors,
        "stale_days_threshold": stale_days,
        "missing_strategic_pages_total": missing_strategic_pages,
    }


def data_quality_report() -> dict:
    """Point d'entrée (voir app.py: GET /api/data-quality) : les quatre indicateurs du §5.H
    ensemble. Ne modifie rien -- purement calculé à la demande sur les données déjà en base."""
    return {
        "recall": compute_recall(),
        "precision": compute_precision(),
        "detection_latency": compute_detection_latency(),
        "coverage_health": compute_coverage_health(),
    }


def list_golden_facts() -> list[dict]:
    with connect(MARKET_DB) as db:
        return [dict(row) for row in db.execute("SELECT * FROM golden_facts ORDER BY actor_name,market").fetchall()]


def add_golden_fact(
    *, actor_name: str, market: str, component: str, operation: str,
    source_url: str, expected_quote: str, added_by: str | None = None,
) -> int:
    """Un fait golden est saisi par un humain qui a vérifié la page lui-même -- jamais copié
    depuis evidence (voir docstring du module)."""
    actor_name, market, component, operation = actor_name.strip(), market.strip(), component.strip(), operation.strip()
    source_url, expected_quote = source_url.strip(), expected_quote.strip()
    if not all([actor_name, market, component, operation, expected_quote]):
        raise ValueError("actor_name, market, component, operation and expected_quote are all required")
    if not source_url.startswith(("http://", "https://")):
        raise ValueError("source_url must be an absolute http(s) URL")
    with connect(MARKET_DB) as db:
        row_id = db.execute(
            """INSERT INTO golden_facts(actor_name,market,component,operation,source_url,expected_quote,added_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (actor_name, market, component, operation, source_url, expected_quote, added_by, utc_now()),
        ).lastrowid
    assert row_id is not None
    return row_id


def delete_golden_fact(fact_id: int) -> None:
    with connect(MARKET_DB) as db:
        deleted = db.execute("DELETE FROM golden_facts WHERE id=?", (fact_id,)).rowcount
    if not deleted:
        raise ValueError(f"Golden fact {fact_id} not found")
