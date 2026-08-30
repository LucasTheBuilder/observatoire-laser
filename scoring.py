"""Scores séparés confiance / menace / attractivité marché (audit Horizon 2 #14 : "Créer
scores séparés : confiance, menace, attractivité marché").

Distinct du score de complétude déjà en place (app._completeness) : celui-ci mesure la
PRÉSENCE de données (7 dimensions renseignées ou non), pas leur qualité ni leur portée
concurrentielle -- c'est très précisément ce que l'audit reproche : "il mesure la présence de
données, pas leur qualité décisionnelle. [...] Renommer explicitement l'indicateur
« complétude documentaire » et créer séparément un score de confiance" (§10). Les trois scores
ci-dessous répondent chacun à une question différente :

- confidence_score (par acteur, 0-100) : peut-on FAIRE CONFIANCE aux données qu'on a sur cet
  acteur ? Combine la part de faits validés (vs en attente), la part de citations verbatim (vs
  reformulées, voir evidence.is_verbatim/offers.is_verbatim -- chantier 4), la part de dates de
  publication confirmées (vs observées seulement/inconnues, voir date_confidence -- révision
  chronologie du 30/08/2026), et le statut de validation humaine de l'acteur lui-même
  (review_status). Un acteur sans aucun fait collecté n'a pas de score de confiance : il n'y a
  rien à évaluer (None, pas 0 -- 0 dirait à tort "confiance nulle" plutôt que "pas encore de
  données").
- threat_score (par acteur, 0-100) : quel niveau de menace CONCURRENTIELLE cet acteur
  représente-t-il ? Combine la classe (C1 > C2 > T1), l'ampleur des faits/offres démontrés, et
  la vélocité récente (nouveaux faits/offres sur les 60 derniers jours -- signal d'accélération,
  cohérent avec BACKFILL_THRESHOLD_DAYS de db.py). Un acteur de référence interne
  (is_reference=1) n'est structurellement jamais une menace : forcé à 0.
- market_attractiveness_score (par marché, 0-100) : ce marché mérite-t-il de l'attention
  business ? Combine la traction prouvée (faits existants), le pipeline (faits radar), le
  nombre d'acteurs actifs dessus, et la part de faits en stade Production/Industrialisation
  (proximité du "passage prototype -> production" que l'audit citait comme mesure manquante).

Chaque score est recalculé à la volée depuis les données déjà validées (jamais stocké, jamais
saisi à la main) -- il ne peut donc jamais dériver des faits réels, même logique que
app._completeness/_coverage_level/_freshness_factor, dont les seuils ci-dessous s'inspirent
directement (des choix éditoriaux documentés, pas une mesure physique).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db import ACTORS_DB, MARKET_DB, connect

RECENT_WINDOW_DAYS = 60


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _is_recent(created_at: str | None, now: datetime) -> bool:
    if not created_at:
        return False
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp.astimezone(timezone.utc)).days <= RECENT_WINDOW_DAYS


def compute_confidence_scores() -> dict[str, float | None]:
    """{actor_name: score 0-100, ou None si l'acteur n'a encore aucun fait marché/offre
    validé -- pas de confiance calculable sur une fiche vide, distinct d'une confiance nulle."""
    stats: dict[str, dict[str, int]] = {}

    def bump(actor_name: str, field: str, amount: int = 1) -> None:
        stats.setdefault(actor_name, {"validated": 0, "total": 0, "verbatim": 0, "cited": 0, "published": 0, "dated": 0})[field] += amount

    with connect(MARKET_DB) as db:
        for row in db.execute("SELECT actor_name,fact_status,date_confidence FROM evidence WHERE evidence_kind='market_application'"):
            bump(row["actor_name"], "total")
            if row["fact_status"] == "validated":
                bump(row["actor_name"], "validated")
                if row["date_confidence"] is not None:
                    bump(row["actor_name"], "dated")
                    if row["date_confidence"] == "published":
                        bump(row["actor_name"], "published")
        for row in db.execute("SELECT actor_name,review_status,date_confidence FROM offers"):
            bump(row["actor_name"], "total")
            if row["review_status"] == "accepted":
                bump(row["actor_name"], "validated")
                if row["date_confidence"] is not None:
                    bump(row["actor_name"], "dated")
                    if row["date_confidence"] == "published":
                        bump(row["actor_name"], "published")
        for row in db.execute(
            """SELECT e.actor_name,es.is_verbatim FROM evidence_sources es JOIN evidence e ON e.id=es.evidence_id
               WHERE e.fact_status='validated' AND e.evidence_kind='market_application'"""
        ):
            bump(row["actor_name"], "cited")
            if row["is_verbatim"]:
                bump(row["actor_name"], "verbatim")
        for row in db.execute(
            """SELECT o.actor_name,os.is_verbatim FROM offer_sources os JOIN offers o ON o.id=os.offer_id
               WHERE o.review_status='accepted'"""
        ):
            bump(row["actor_name"], "cited")
            if row["is_verbatim"]:
                bump(row["actor_name"], "verbatim")

    with connect(ACTORS_DB) as db:
        review_status_by_name = {row["name"]: row["review_status"] for row in db.execute("SELECT name,review_status FROM actors")}

    scores: dict[str, float | None] = {}
    for actor_name, s in stats.items():
        if s["validated"] == 0:
            scores[actor_name] = None
            continue
        validated_ratio = s["validated"] / s["total"] if s["total"] else 0.0
        verbatim_ratio = s["verbatim"] / s["cited"] if s["cited"] else 0.0
        published_ratio = s["published"] / s["dated"] if s["dated"] else 0.0
        verified_bonus = 1.0 if review_status_by_name.get(actor_name) == "verified" else 0.0
        score = 100 * (0.35 * validated_ratio + 0.25 * verbatim_ratio + 0.25 * published_ratio + 0.15 * verified_bonus)
        scores[actor_name] = round(_clamp(score), 1)
    return scores


# Classe -> base de menace (choix éditorial : C1 = concurrence directe, la menace la plus
# immédiate ; C2 = partielle ; T1 = centre technologique, rarement un concurrent commercial
# direct mais jamais nul -- il peut transférer une techno à un concurrent).
_THREAT_BASE_BY_CLASS = {"C1": 55, "C2": 30, "T1": 10}
_THREAT_BASE_DEFAULT = 15


def compute_threat_scores() -> dict[str, float]:
    """{actor_name: score 0-100}. Toujours défini (contrairement à confidence_score) : même un
    acteur sans preuve encore collectée a une classe et représente potentiellement une menace
    de par sa seule existence connue -- l'absence de preuve limite le score, elle ne l'annule
    pas (sauf pour un acteur de référence interne, forcé à 0)."""
    now = datetime.now(timezone.utc)
    with connect(ACTORS_DB) as db:
        actor_rows = {
            row["name"]: dict(row)
            for row in db.execute("SELECT name,competitive_class,is_reference,active FROM actors")
        }

    evidence_by_actor: dict[str, dict[str, int]] = {}

    def bump(actor_name: str, field: str) -> None:
        evidence_by_actor.setdefault(actor_name, {"demonstrated": 0, "recent": 0})[field] += 1

    with connect(MARKET_DB) as db:
        for row in db.execute(
            "SELECT actor_name,created_at FROM evidence WHERE fact_status='validated' AND evidence_kind='market_application'"
        ):
            bump(row["actor_name"], "demonstrated")
            if _is_recent(row["created_at"], now):
                bump(row["actor_name"], "recent")
        for row in db.execute("SELECT actor_name,created_at FROM offers WHERE review_status='accepted'"):
            bump(row["actor_name"], "demonstrated")
            if _is_recent(row["created_at"], now):
                bump(row["actor_name"], "recent")

    scores: dict[str, float] = {}
    for actor_name, actor in actor_rows.items():
        if actor["is_reference"]:
            scores[actor_name] = 0.0
            continue
        base = _THREAT_BASE_BY_CLASS.get(actor["competitive_class"], _THREAT_BASE_DEFAULT)
        demo = evidence_by_actor.get(actor_name, {"demonstrated": 0, "recent": 0})
        # Capped, pas linéaire à l'infini : au-delà de 15 faits démontrés / 5 faits récents,
        # un fait de plus ne rend pas l'acteur structurellement plus menaçant.
        demonstrated_bonus = min(25, demo["demonstrated"] * 1.5)
        velocity_bonus = min(20, demo["recent"] * 4)
        inactive_penalty = 15 if not actor["active"] else 0
        scores[actor_name] = round(_clamp(base + demonstrated_bonus + velocity_bonus - inactive_penalty), 1)
    return scores


def compute_market_attractiveness_scores() -> list[dict[str, Any]]:
    """[{market, attractiveness_score, existing, radar, actors_count, production_share}], trié
    du plus attractif au moins attractif. Un marché absent de evidence n'apparaît simplement
    pas (rien à évaluer), plutôt qu'un score fabriqué à 0."""
    with connect(MARKET_DB) as db:
        market_rows = db.execute(
            """SELECT market,bucket,industrial_stage,actor_name FROM evidence
               WHERE market IS NOT NULL AND market<>'' AND fact_status='validated'
               AND evidence_kind='market_application'"""
        ).fetchall()

    by_market: dict[str, dict[str, Any]] = {}
    for row in market_rows:
        entry = by_market.setdefault(row["market"], {"existing": 0, "radar": 0, "actors": set(), "production_like": 0, "total": 0})
        entry["total"] += 1
        entry[row["bucket"]] = entry.get(row["bucket"], 0) + 1
        entry["actors"].add(row["actor_name"])
        stage = (row["industrial_stage"] or "").split("|", 1)[0].strip()
        if stage in ("Production", "Industrialisation"):
            entry["production_like"] += 1

    results = []
    for market, entry in by_market.items():
        # Plafonds calibrés sur l'échelle observée du jeu de données actuel (quelques dizaines
        # de faits max par marché, quelques acteurs actifs max) -- comme _coverage_level, un
        # choix éditorial à recalibrer si le volume de données change d'ordre de grandeur.
        existing_component = min(1.0, entry["existing"] / 8)
        radar_component = min(1.0, entry["radar"] / 8)
        actors_component = min(1.0, len(entry["actors"]) / 5)
        production_share = entry["production_like"] / entry["total"] if entry["total"] else 0.0
        score = 100 * (0.35 * existing_component + 0.15 * radar_component + 0.25 * actors_component + 0.25 * production_share)
        results.append({
            "market": market,
            "attractiveness_score": round(_clamp(score), 1),
            "existing": entry["existing"],
            "radar": entry.get("radar", 0),
            "actors_count": len(entry["actors"]),
            "production_share": round(production_share, 2),
        })
    results.sort(key=lambda item: item["attractiveness_score"], reverse=True)
    return results
