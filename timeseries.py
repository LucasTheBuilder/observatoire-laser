"""Séries temporelles (audit Horizon 2, item 12 : "Construire séries temporelles par acteur,
marché, technologie, maturité et signal") -- et la mesure de "marché qui monte" que l'audit
note absente : "elle n'a pas encore une mesure de « marché qui monte » fondée sur séries
temporelles, nombre d'acteurs nouveaux, investissements, projets, recrutements et passage
prototype -> production" (§12, Audit du modèle marché, 27/08/2026).

Principe : un instantané (snapshot) mensuel de l'état agrégé de chaque dimension, calculé à la
volée à partir des données déjà en base -- jamais un flux d'événements séparé à tenir à jour en
parallèle -- et persisté dans metric_snapshots (une seule table, dans MARKET_DB : les 3 bases
sont des fichiers SQLite distincts, donc l'historique croisé acteur/marché/technologie/maturité/
signal est regroupé dans un seul endroit plutôt que réparti). Un instantané par
(dimension, dimension_key, période) : relancer capture_metric_snapshot() plusieurs fois dans le
même mois RAFFINE ce mois (upsert sur la période courante), ne duplique jamais et n'invente
jamais de point rétroactif pour un mois passé.

La page "Séries temporelles" qui traçait ces courbes a été supprimée le 13/09/2026, avec ses
endpoints /api/timeseries* et les lecteurs read_timeseries/list_timeseries_keys. La capture
reste : scoring._single_collection_window lit metric_snapshots pour savoir s'il existe plus
d'une période, et neutralise le bonus de vélocité de compute_threat_scores tant que ce n'est pas
le cas -- sans capture, ce bonus serait éteint pour toujours.

Cinq dimensions, au sens de l'audit :
- 'actor'      : par acteur actif -- faits marché validés par bucket, offres, sources actives.
                 Trace la trajectoire documentaire d'un acteur donné dans le temps.
- 'market'     : par marché (evidence.market) -- distribution par bucket + par stade
                 industriel, et nombre d'acteurs actifs sur ce marché.
- 'maturity'   : distribution industrial_stage (R&D -> Prototype -> Pré-industrialisation ->
                 Industrialisation -> Production), une clé '__global__' + une clé par marché --
                 c'est très précisément la mesure "passage prototype -> production" que l'audit
                 réclame.
- 'technology' : par axe technologique (technology_signals.axis) + une clé '__global__' pour
                 les documents (publications/brevets/projets), tous axes confondus.
- 'signal'     : vélocité -- combien de faits marché validés / offres / documents / signaux
                 technologiques NOUVEAUX ce mois-ci (created_at dans la période). La mesure la
                 plus proche du "marché qui monte" / "nombre d'acteurs nouveaux" de l'audit,
                 sans fabriquer de date de création pour les acteurs (voir Pending Tasks) : la
                 vélocité porte sur les FAITS, dont created_at est un vrai horodatage d'insertion,
                 jamais une date métier reconstruite.

Chaque instantané reflète l'état constaté au moment de la capture, jamais une reconstruction a
posteriori (même discipline que db.classify_source_date/compute_is_backfill) : `period` est
toujours l'horodatage système de la capture, jamais une date métier -- donc jamais de "date
inconnue" ou de fabrication possible sur cette colonne. L'historique se construit un instantané
réel par cycle de collecte (voir app._run_job, qui appelle capture_metric_snapshot() après
chaque collecte terminée -- actors/market/technology/cordis/... /monthly).
"""

from __future__ import annotations

import json
from typing import Any

from db import _INDUSTRIAL_STAGE_CANONICAL, ACTORS_DB, MARKET_DB, TECH_DB, connect, utc_now

DIMENSIONS = ("actor", "market", "technology", "maturity", "signal")
GLOBAL_KEY = "__global__"


def _period(timestamp: str) -> str:
    return timestamp[:7]


def _canonical_stage(raw: str | None) -> str:
    """Ramène industrial_stage à son stade canonique de tête (voir
    db._migrate_industrial_stage_placeholder_values pour la même règle de repli) : un composite
    ("Prototype | Matériau: Verre") ne garde que le stade, et toute valeur non reconnue retombe
    sur le stade "non déterminée" plutôt que d'être comptée comme une catégorie fantôme."""
    stage = (raw or "").split("|", 1)[0].strip()
    return stage if stage in _INDUSTRIAL_STAGE_CANONICAL else "Maturité industrielle non déterminée"


def _upsert(db, dimension: str, dimension_key: str, period: str, captured_at: str, metrics: dict[str, Any]) -> None:
    db.execute(
        """INSERT INTO metric_snapshots(dimension,dimension_key,period,captured_at,metrics_json)
           VALUES(?,?,?,?,?)
           ON CONFLICT(dimension,dimension_key,period) DO UPDATE SET
               captured_at=excluded.captured_at, metrics_json=excluded.metrics_json""",
        (dimension, dimension_key, period, captured_at, json.dumps(metrics, ensure_ascii=False)),
    )


def capture_metric_snapshot(period: str | None = None) -> dict[str, int]:
    """Calcule et persiste un instantané pour les 5 dimensions ci-dessus. Idempotent dans le
    mois courant. Retourne {dimension: nb de clés capturées}, pour le journal d'appel (voir
    app._run_job)."""
    captured_at = utc_now()
    period = period or _period(captured_at)

    with connect(ACTORS_DB) as adb:
        active_actors = {
            row["name"]: row["competitive_class"]
            for row in adb.execute("SELECT name,competitive_class FROM actors WHERE active=1")
        }
        sources_active = {
            row["name"]: row["n"]
            for row in adb.execute(
                """SELECT a.name AS name, COUNT(*) AS n FROM actor_sources s
                   JOIN actors a ON a.id=s.actor_id WHERE s.active=1 GROUP BY a.name"""
            )
        }

    with connect(TECH_DB) as tdb:
        axis_signals: dict[str, dict[str, int]] = {}
        for row in tdb.execute(
            "SELECT axis,bucket,COUNT(*) AS n FROM technology_signals WHERE review_status='accepted' GROUP BY axis,bucket"
        ):
            axis_signals.setdefault(row["axis"], {})[row["bucket"]] = row["n"]
        documents_by_type = {
            row["document_type"]: row["n"]
            for row in tdb.execute("SELECT document_type,COUNT(*) AS n FROM documents GROUP BY document_type")
        }
        new_documents = tdb.execute(
            "SELECT COUNT(*) FROM documents WHERE substr(created_at,1,7)=?", (period,)
        ).fetchone()[0]
        new_signals = tdb.execute(
            "SELECT COUNT(*) FROM technology_signals WHERE substr(created_at,1,7)=? AND review_status='accepted'",
            (period,),
        ).fetchone()[0]

    counts: dict[str, int] = {}
    with connect(MARKET_DB) as mdb:
        # --- acteur : faits marché validés + offres par acteur, sources depuis ACTORS_DB ---
        evidence_by_actor: dict[str, dict[str, int]] = {}
        for row in mdb.execute(
            """SELECT actor_name,bucket,COUNT(*) AS n FROM evidence
               WHERE fact_status='validated' AND evidence_kind='market_application'
               GROUP BY actor_name,bucket"""
        ):
            evidence_by_actor.setdefault(row["actor_name"], {})[row["bucket"]] = row["n"]
        offers_by_actor = {
            row["actor_name"]: row["n"]
            for row in mdb.execute("SELECT actor_name,COUNT(*) AS n FROM offers WHERE review_status='accepted' GROUP BY actor_name")
        }
        for name, competitive_class in active_actors.items():
            bucket_counts = evidence_by_actor.get(name, {})
            _upsert(mdb, "actor", name, period, captured_at, {
                "competitive_class": competitive_class,
                "evidence_existing": bucket_counts.get("existing", 0),
                "evidence_radar": bucket_counts.get("radar", 0),
                "evidence_pending": bucket_counts.get("pending", 0),
                "offers_count": offers_by_actor.get(name, 0),
                "sources_active": sources_active.get(name, 0),
            })
        counts["actor"] = len(active_actors)

        # --- marché : distribution bucket + stade industriel par marché ---
        market_rows = mdb.execute(
            """SELECT market,bucket,industrial_stage,actor_name FROM evidence
               WHERE market IS NOT NULL AND market<>'' AND fact_status='validated'
               AND evidence_kind='market_application'"""
        ).fetchall()
        by_market: dict[str, dict[str, Any]] = {}
        for row in market_rows:
            entry = by_market.setdefault(row["market"], {"bucket": {}, "stage": {}, "actors": set()})
            entry["bucket"][row["bucket"]] = entry["bucket"].get(row["bucket"], 0) + 1
            stage = _canonical_stage(row["industrial_stage"])
            entry["stage"][stage] = entry["stage"].get(stage, 0) + 1
            entry["actors"].add(row["actor_name"])
        for market, entry in by_market.items():
            _upsert(mdb, "market", market, period, captured_at, {
                "total": sum(entry["bucket"].values()),
                "existing": entry["bucket"].get("existing", 0),
                "radar": entry["bucket"].get("radar", 0),
                "actors_count": len(entry["actors"]),
                "stage_distribution": entry["stage"],
            })
        counts["market"] = len(by_market)

        # --- maturité : distribution industrial_stage globale + par marché ---
        global_stage: dict[str, int] = {}
        global_bucket: dict[str, int] = {}
        for row in market_rows:
            stage = _canonical_stage(row["industrial_stage"])
            global_stage[stage] = global_stage.get(stage, 0) + 1
            global_bucket[row["bucket"]] = global_bucket.get(row["bucket"], 0) + 1
        _upsert(mdb, "maturity", GLOBAL_KEY, period, captured_at, {
            "stage_distribution": global_stage,
            "bucket_distribution": global_bucket,
            "total": len(market_rows),
        })
        for market, entry in by_market.items():
            _upsert(mdb, "maturity", market, period, captured_at, {
                "stage_distribution": entry["stage"],
                "bucket_distribution": entry["bucket"],
                "total": sum(entry["bucket"].values()),
            })
        counts["maturity"] = 1 + len(by_market)

        # --- technologie : par axe (lu depuis TECH_DB plus haut), + clé globale documents ---
        for axis, bucket_counts in axis_signals.items():
            _upsert(mdb, "technology", axis, period, captured_at, {
                "signals_existing": bucket_counts.get("existing", 0),
                "signals_radar": bucket_counts.get("radar", 0),
                "signals_total": sum(bucket_counts.values()),
            })
        _upsert(mdb, "technology", GLOBAL_KEY, period, captured_at, {
            "documents_total": sum(documents_by_type.values()),
            "documents_by_type": documents_by_type,
        })
        counts["technology"] = len(axis_signals) + 1

        # --- signal : vélocité, nouveautés créées CE MOIS-CI (created_at = horodatage d'insertion réel) ---
        new_evidence = mdb.execute(
            """SELECT COUNT(*) FROM evidence WHERE substr(created_at,1,7)=? AND fact_status='validated'
               AND evidence_kind='market_application'""",
            (period,),
        ).fetchone()[0]
        new_offers = mdb.execute(
            "SELECT COUNT(*) FROM offers WHERE substr(created_at,1,7)=? AND review_status='accepted'", (period,)
        ).fetchone()[0]
        _upsert(mdb, "signal", GLOBAL_KEY, period, captured_at, {
            "new_evidence": new_evidence,
            "new_offers": new_offers,
            "new_documents": new_documents,
            "new_technology_signals": new_signals,
        })
        counts["signal"] = 1

    return counts
