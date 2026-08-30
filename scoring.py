"""Scores séparés confiance / menace / intensité concurrentielle (audit Horizon 2 #14 : "Créer
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
- competitive_intensity_score (par marché, 0-100 -- renommé depuis "market_attractiveness" le
  30/08/2026, audit veille §10.4) : combien de concurrents se disputent déjà ce marché ? Combine
  la traction prouvée (faits existants), le pipeline (faits radar), le nombre d'acteurs actifs
  dessus, et la part de faits en stade Production/Industrialisation. L'audit a raison sur le
  fond : ce que ce score mesure est une INTENSITÉ concurrentielle (un marché encombré = score
  haut), pas une attractivité business -- rien ici ne vient de la taille du marché, de sa
  croissance ou de la demande. Le nom disait le contraire de ce qu'il mesurait ; corrigé plutôt
  que ré-interprété, en attendant qu'un vrai score d'attractivité existe (chantier §4.B,
  market_sizing + signaux de demande).

Chaque score est recalculé à la volée depuis les données déjà validées (jamais stocké, jamais
saisi à la main) -- il ne peut donc jamais dériver des faits réels, même logique que
app._completeness/_coverage_level/_freshness_factor, dont les seuils ci-dessous s'inspirent
directement (des choix éditoriaux documentés, pas une mesure physique).

Audit veille du 30/08/2026 (§10.2, §10.3) a trouvé deux défauts mesurés sur les données réelles,
corrigés ici :
- **is_verbatim** n'était pas exclu du calcul : 40 des 71 faits marché "validés" étaient en
  réalité des notes de lecture saisies à la main (`is_verbatim=0`, `SEED_EVIDENCE` -- aujourd'hui
  une liste vide dans db.py -- ne peut donc plus les reproduire), et gonflaient les trois scores
  sans qu'aucun flag ne les distingue d'un fait réellement extrait. Chaque requête ci-dessous
  filtre maintenant `is_verbatim=1` en plus de `fact_status='validated'`/`review_status='accepted'`.
- **La vélocité mesurait la date d'INSERTION, pas la date de PUBLICATION**, et le corpus entier
  tenait dans une seule fenêtre de collecte de 7 jours (23->30 août 2026) : `_is_recent` lisait
  `created_at`, alors que la latence réelle mesurée sur les offres datées va jusqu'à p90 = 1636
  jours -- une page de plusieurs années comptait comme "signal récent de vélocité concurrentielle".
  `_is_recent` lit maintenant `COALESCE(source_date, created_at)`, et le bonus de vélocité est
  neutralisé (forcé à 0) tant que `metric_snapshots` ne couvre qu'une seule période : avec un
  seul point de mesure, "récent" ne peut être distingué de "vu pour la première fois".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db import ACTORS_DB, MARKET_DB, connect

RECENT_WINDOW_DAYS = 60


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _is_recent(effective_date: str | None, now: datetime) -> bool:
    """``effective_date`` should already be COALESCE(source_date, created_at) at the call site
    -- see the module docstring for why created_at alone overstates velocity."""
    if not effective_date:
        return False
    try:
        stamp = datetime.fromisoformat(effective_date)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp.astimezone(timezone.utc)).days <= RECENT_WINDOW_DAYS


def _single_collection_window(db) -> bool:
    """True s'il n'existe encore qu'une seule période dans metric_snapshots (ou aucune) --
    voir le docstring du module : le bonus de vélocité de compute_threat_scores n'a aucun sens
    tant qu'il n'y a rien à comparer dans le temps."""
    count = db.execute("SELECT COUNT(DISTINCT period) FROM metric_snapshots").fetchone()[0]
    return int(count) <= 1


def compute_confidence_scores() -> dict[str, float | None]:
    """{actor_name: score 0-100, ou None si l'acteur n'a encore aucun fait marché/offre
    validé -- pas de confiance calculable sur une fiche vide, distinct d'une confiance nulle."""
    stats: dict[str, dict[str, int]] = {}

    def bump(actor_name: str, field: str, amount: int = 1) -> None:
        stats.setdefault(actor_name, {"validated": 0, "total": 0, "verbatim": 0, "cited": 0, "published": 0, "dated": 0})[field] += amount

    with connect(MARKET_DB) as db:
        # is_verbatim=1 required to count as "validated" (audit veille §10.2, voir le docstring
        # du module) : un fait/offre saisi à la main plutôt qu'extrait ne doit jamais gonfler la
        # confiance qu'on peut avoir dans le pipeline lui-même.
        for row in db.execute("SELECT actor_name,fact_status,date_confidence,is_verbatim FROM evidence WHERE evidence_kind='market_application'"):
            bump(row["actor_name"], "total")
            if row["fact_status"] == "validated" and row["is_verbatim"]:
                bump(row["actor_name"], "validated")
                if row["date_confidence"] is not None:
                    bump(row["actor_name"], "dated")
                    if row["date_confidence"] == "published":
                        bump(row["actor_name"], "published")
        for row in db.execute("SELECT actor_name,review_status,date_confidence,is_verbatim FROM offers"):
            bump(row["actor_name"], "total")
            if row["review_status"] == "accepted" and row["is_verbatim"]:
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
        neutralize_velocity = _single_collection_window(db)
        # is_verbatim=1 requis (audit veille §10.2, voir le docstring du module) : mêmes 40
        # faits de seed qui gonflaient confidence_score gonflent aussi celui-ci sans le filtre.
        # COALESCE(source_date,created_at) (§10.3) : la latence réelle observée va jusqu'à
        # p90=1636 jours sur les offres datées -- created_at seul dit seulement "quand nous
        # l'avons vu", pas "quand c'est devenu vrai".
        for row in db.execute(
            """SELECT actor_name,COALESCE(source_date,created_at) AS effective_date FROM evidence
               WHERE fact_status='validated' AND evidence_kind='market_application' AND is_verbatim=1"""
        ):
            bump(row["actor_name"], "demonstrated")
            if _is_recent(row["effective_date"], now):
                bump(row["actor_name"], "recent")
        for row in db.execute(
            "SELECT actor_name,COALESCE(source_date,created_at) AS effective_date FROM offers WHERE review_status='accepted' AND is_verbatim=1"
        ):
            bump(row["actor_name"], "demonstrated")
            if _is_recent(row["effective_date"], now):
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
        # Neutralisé tant qu'une seule période de collecte existe (voir _single_collection_window
        # ci-dessus) : sans deuxième fenêtre à comparer, "récent" ne peut pas être distingué de
        # "vu pour la première fois" -- le bonus vaudrait 20 pour quasi tout acteur ayant >= 5
        # faits, 0 bit d'information pour 20 points de score (audit veille §10.3.b).
        velocity_bonus = 0 if neutralize_velocity else min(20, demo["recent"] * 4)
        inactive_penalty = 15 if not actor["active"] else 0
        scores[actor_name] = round(_clamp(base + demonstrated_bonus + velocity_bonus - inactive_penalty), 1)
    return scores


def compute_competitive_intensity_scores() -> list[dict[str, Any]]:
    """[{market, intensity_score, existing, radar, actors_count, production_share}], trié du
    plus disputé au moins disputé. Un marché absent de evidence n'apparaît simplement pas (rien
    à évaluer), plutôt qu'un score fabriqué à 0.

    Renommé depuis compute_market_attractiveness_scores (audit veille §10.4, 30/08/2026) : ce
    score est composé à 100% de mesures de l'offre concurrente (faits existants/radar, nombre
    d'acteurs, part en stade Production) -- aucune composante ne vient de la demande, de la
    taille du marché ou de sa croissance. Un marché encombré y ressort comme "attractif", ce qui
    est l'inverse de l'information utile pour du business development. Le nom mesure maintenant
    ce que le calcul mesure réellement ; un vrai score d'attractivité reste à construire séparément
    une fois market_sizing et les signaux de demande disponibles (chantier §4.B)."""
    with connect(MARKET_DB) as db:
        # is_verbatim=1 (audit veille §10.2) : ce score était calculé sur une base dont 56% des
        # faits (marché "Médical" en tête) étaient de la donnée saisie à la main, sélectionnée
        # selon des intuitions de marché déjà formées -- la boucle se refermait sur elle-même.
        market_rows = db.execute(
            """SELECT market,bucket,industrial_stage,actor_name FROM evidence
               WHERE market IS NOT NULL AND market<>'' AND fact_status='validated'
               AND evidence_kind='market_application' AND is_verbatim=1"""
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
            "intensity_score": round(_clamp(score), 1),
            "existing": entry["existing"],
            "radar": entry.get("radar", 0),
            "actors_count": len(entry["actors"]),
            "production_share": round(production_share, 2),
        })
    results.sort(key=lambda item: item["intensity_score"], reverse=True)
    return results
