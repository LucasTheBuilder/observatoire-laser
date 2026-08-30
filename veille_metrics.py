"""Tableau de bord de la veille (§10.11 audit veille, 30/08/2026, Lot 1 §1.7) : "aucun des
chiffres de ce document n'est calculé par l'application, ils viennent tous de requêtes ad hoc --
tant que ce sera le cas, aucune des dégradations décrites ici ne sera détectée en production."

Huit indicateurs de SANTÉ de la collecte/extraction elle-même -- pas des dimensions métier
(acteur/marché/technologie/maturité/signal, déjà couvertes par timeseries.metric_snapshots), mais
des mesures de qualité du pipeline : combien de pages téléchargées produisent réellement quelque
chose, combien de découvertes restent en jachère, combien de faits sont fiables. Un instantané par
(période, indicateur), même discipline que capture_metric_snapshot() : upsert sur la période
courante, jamais de point rétroactif fabriqué, jamais un flux d'événements séparé à tenir à jour.

Chaque formule est reprise verbatim du §10.11 quand la donnée est disponible dans une seule table ;
quand une ambiguïté existe, la définition retenue est documentée sur l'indicateur lui-même. Un
dénominateur nul donne value=NULL, jamais 0 -- "rien à mesurer" n'est pas la même chose que "0%".
"""

from __future__ import annotations

import statistics
from typing import Any

from db import ACTORS_DB, MARKET_DB, connect, utc_now

# Seuils d'alerte du §10.11 -- pas encore branchés sur un mécanisme d'alerte (voir Lot 1 §1.4,
# item séparé), mais documentés ici pour que la lecture d'une valeur de veille_metrics n'exige
# pas de retourner à l'audit. ("gt", x) = alerte si value > x ; ("lt", x) = alerte si value < x.
VEILLE_METRICS_THRESHOLDS: dict[str, tuple[str, float]] = {
    "collection_yield": ("lt", 0.15),
    "unvisited_discovery_rate": ("gt", 0.85),
    "crawl_error_rate": ("gt", 0.10),
    "non_verbatim_share": ("gt", 0.05),
    "unreviewed_accepted_share": ("gt", 0.20),
    "published_date_reliability": ("lt", 0.30),
    "detection_latency_days": ("gt", 180),
    "source_concentration_top10pct": ("gt", 0.50),
}


def _period(timestamp: str) -> str:
    return timestamp[:7]


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _collection_yield() -> float | None:
    """Rendement de collecte = URLs distinctes ayant produit >= 1 preuve (fait ou offre) / pages
    HTTP 200 (§10.5 : "83% des pages téléchargées ne produisent rien")."""
    with connect(ACTORS_DB) as db:
        pages_200 = int(db.execute("SELECT COUNT(*) FROM actor_sources WHERE last_http_status=200").fetchone()[0])
    with connect(MARKET_DB) as db:
        productive = int(db.execute(
            """SELECT COUNT(*) FROM (
                   SELECT source_url FROM evidence_sources
                   UNION
                   SELECT source_url FROM offer_sources
               )"""
        ).fetchone()[0])
    return _ratio(productive, pages_200)


def _unvisited_discovery_rate() -> float | None:
    """Taux de découverte non traitée = URLs jamais visitées / total (§9.1)."""
    with connect(ACTORS_DB) as db:
        total = int(db.execute("SELECT COUNT(*) FROM actor_sources WHERE active=1").fetchone()[0])
        unvisited = int(db.execute("SELECT COUNT(*) FROM actor_sources WHERE active=1 AND last_checked_at IS NULL").fetchone()[0])
    return _ratio(unvisited, total)


def _crawl_error_rate() -> float | None:
    """Taux d'erreur de crawl = erreurs / pages tentées. "Tentée" = last_checked_at renseigné ;
    "erreur" = statut hors 200-399 (inclut last_http_status=0, le sentinel pour une exception
    réseau/parsing -- voir scrapers.scrape_actors)."""
    with connect(ACTORS_DB) as db:
        attempted = int(db.execute("SELECT COUNT(*) FROM actor_sources WHERE last_checked_at IS NOT NULL").fetchone()[0])
        errors = int(db.execute(
            "SELECT COUNT(*) FROM actor_sources WHERE last_checked_at IS NOT NULL AND (last_http_status IS NULL OR last_http_status NOT BETWEEN 200 AND 399)"
        ).fetchone()[0])
    return _ratio(errors, attempted)


def _non_verbatim_share() -> float | None:
    """Part de faits non verbatim = is_verbatim=0 / total accepté (evidence, §10.2)."""
    with connect(MARKET_DB) as db:
        total = int(db.execute("SELECT COUNT(*) FROM evidence WHERE review_status='accepted'").fetchone()[0])
        non_verbatim = int(db.execute("SELECT COUNT(*) FROM evidence WHERE review_status='accepted' AND is_verbatim=0").fetchone()[0])
    return _ratio(non_verbatim, total)


def _unreviewed_accepted_share() -> float | None:
    """Part de faits non validés = 'accepted' sans passage en revue (§10.7/§10.11). Mesuré sur
    offers (c'est là que le problème a été mesuré et corrigé -- voir _offer_review_reasons,
    Lot 1 §1.2) : parmi les offres review_status='accepted', la part qui ne repose encore que
    sur une SEULE source (donc jamais réellement confirmée, même si le statut dit "accepted") --
    devrait tendre vers 0 dans le temps si le routage structurel tient ses promesses."""
    with connect(MARKET_DB) as db:
        accepted_total = int(db.execute("SELECT COUNT(*) FROM offers WHERE review_status='accepted'").fetchone()[0])
        single_sourced = int(db.execute(
            """SELECT COUNT(*) FROM offers o WHERE o.review_status='accepted' AND (
                   SELECT COUNT(DISTINCT source_url) FROM offer_sources WHERE offer_id=o.id
               ) <= 1"""
        ).fetchone()[0])
    return _ratio(single_sourced, accepted_total)


def _published_date_reliability() -> float | None:
    """Fiabilité de datation = date_confidence='published' / total, sur evidence+offers
    combinés (§8.3/§10.9)."""
    with connect(MARKET_DB) as db:
        evidence_total, evidence_published = db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN date_confidence='published' THEN 1 ELSE 0 END) FROM evidence"
        ).fetchone()
        offers_total, offers_published = db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN date_confidence='published' THEN 1 ELSE 0 END) FROM offers"
        ).fetchone()
    total = int(evidence_total or 0) + int(offers_total or 0)
    published = int(evidence_published or 0) + int(offers_published or 0)
    return _ratio(published, total)


def _detection_latency_days() -> float | None:
    """Latence de détection = médiane(source_date -> created_at) en jours, sur les lignes qui
    ont une source_date réelle (§10.9)."""
    with connect(MARKET_DB) as db:
        deltas = [
            row[0] for row in db.execute(
                """SELECT julianday(created_at) - julianday(source_date) FROM evidence
                   WHERE source_date IS NOT NULL
                   UNION ALL
                   SELECT julianday(created_at) - julianday(source_date) FROM offers
                   WHERE source_date IS NOT NULL"""
            ).fetchall()
        ]
    return round(statistics.median(deltas), 1) if deltas else None


def _source_concentration_top10pct() -> float | None:
    """Concentration des sources = part des faits (preuves) issus du top 10% d'URLs distinctes
    par nombre de preuves produites (§10.5 : une seule URL en produit 11)."""
    with connect(MARKET_DB) as db:
        counts = [
            int(row[0]) for row in db.execute(
                """SELECT n FROM (
                       SELECT source_url, COUNT(*) AS n FROM evidence_sources GROUP BY source_url
                       UNION ALL
                       SELECT source_url, COUNT(*) AS n FROM offer_sources GROUP BY source_url
                   )"""
            ).fetchall()
        ]
    if not counts:
        return None
    counts.sort(reverse=True)
    total = sum(counts)
    top_n = max(1, round(len(counts) * 0.10))
    return _ratio(sum(counts[:top_n]), total)


_INDICATORS: dict[str, Any] = {
    "collection_yield": _collection_yield,
    "unvisited_discovery_rate": _unvisited_discovery_rate,
    "crawl_error_rate": _crawl_error_rate,
    "non_verbatim_share": _non_verbatim_share,
    "unreviewed_accepted_share": _unreviewed_accepted_share,
    "published_date_reliability": _published_date_reliability,
    "detection_latency_days": _detection_latency_days,
    "source_concentration_top10pct": _source_concentration_top10pct,
}


def capture_veille_metrics(period: str | None = None) -> dict[str, float | None]:
    """Calcule et persiste les 8 indicateurs pour la période courante. Idempotent dans le mois
    courant (upsert), comme capture_metric_snapshot. Retourne {indicateur: valeur} pour le
    journal d'appel (voir app._run_job)."""
    captured_at = utc_now()
    period = period or _period(captured_at)
    values: dict[str, float | None] = {name: fn() for name, fn in _INDICATORS.items()}
    with connect(MARKET_DB) as db:
        for indicator, value in values.items():
            db.execute(
                """INSERT INTO veille_metrics(period,indicator,value,captured_at) VALUES(?,?,?,?)
                   ON CONFLICT(period,indicator) DO UPDATE SET value=excluded.value,captured_at=excluded.captured_at""",
                (period, indicator, value, captured_at),
            )
    return values
