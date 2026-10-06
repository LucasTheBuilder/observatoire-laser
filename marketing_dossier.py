"""Dossier marketing (phase 1 de l'agent marketing) : ce que la veille dit du marché du
micro-usinage laser ultra-rapide -- qui revendique quoi, où, avec quelle dynamique --, assemblé
en SQL, sans aucun appel de modèle. Lecture neutre : aucun acteur n'y joue le rôle de « nous ».

Même partage des rôles que ``feedback_dossier``/``analyst`` : le code compte, le modèle (phase 2)
ne fera que formuler des recommandations à partir de ces comptes, et l'humain tranche.

- **Tout est compté, rien n'est estimé.** Chaque ligne porte des références (``offer:12``,
  ``evidence:5``, ``tech:40``, ``demand:3``, ``event:9``) que l'agent devra citer pour chaque
  affirmation. Un agent marketing laissé libre écrit des chiffres de marché plausibles et faux ;
  c'est ce que la discipline de preuve du projet interdit.
- **Confirmé ≠ à confirmer.** « Confirmé » reprend la porte d'affichage de l'app
  (``review_status='accepted'``, et pour les faits marché ``bucket`` existing/radar) ; ce qui est
  encore en revue est compté à part, jamais mélangé.
- **Ce que la base ne permet pas de dire est écrit** dans ``lacunes``, pour que l'agent le dise au
  lieu de combler le vide.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

import db as _db

PRODUCTION_STAGES = {"Production", "Industrialisation"}
UNDETERMINED_STAGE = "Maturité industrielle non déterminée"
MIN_SIGNALS_PER_AXIS = 5
REFS_PER_LINE = 3


def _refs(prefix: str, ids: list[int]) -> list[str]:
    return [f"{prefix}:{i}" for i in ids[:REFS_PER_LINE]]


def _roster() -> tuple[set[str], dict[str, Any]]:
    """Acteurs suivis : vérifiés, actifs, hors partenaires de référence (is_reference -- les
    fabricants de sources, qui équipent le marché sans y concourir)."""
    actors = [
        a for a in _db.rows(
            _db.ACTORS_DB, "SELECT name, country, actor_type, is_reference, active, review_status FROM actors")
        if not a["is_reference"] and a["active"] and a["review_status"] == "verified"
    ]
    perimeter = {
        "acteurs": len(actors),
        "par_type": Counter(a["actor_type"] or "non renseigné" for a in actors).most_common(),
        "par_pays": Counter(a["country"] or "non renseigné" for a in actors).most_common(),
    }
    return {a["name"] for a in actors}, perimeter


def _positioning(rows: list[dict], key: str, prefix: str, tracked: set[str], is_confirmed) -> list[dict[str, Any]]:
    """Par valeur de ``key`` : combien d'acteurs suivis la revendiquent (confirmé / à confirmer)."""
    by_value: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"confirmed": set(), "unconfirmed": set(), "production": set(), "refs": []}
    )
    for row in rows:
        value = row[key]
        if not value:
            continue
        entry = by_value[value]
        confirmed = is_confirmed(row)
        name = row["actor_name"]
        if name not in tracked:
            continue
        entry["confirmed" if confirmed else "unconfirmed"].add(name)
        if confirmed:
            entry["refs"].append(row["id"])
            if row.get("industrial_stage") in PRODUCTION_STAGES:
                entry["production"].add(name)
    lines = []
    for value, entry in by_value.items():
        lines.append({
            key: value,
            "acteurs_confirmes": len(entry["confirmed"]),
            "acteurs_a_confirmer": len(entry["unconfirmed"] - entry["confirmed"]),
            "acteurs_en_production": len(entry["production"]),
            "exemples": sorted(entry["confirmed"])[:5],
            "refs": _refs(prefix, sorted(entry["refs"])),
        })
    return sorted(lines, key=lambda line: (-line["acteurs_confirmes"], line[key]))


def _operations(tracked: set[str]) -> list[dict[str, Any]]:
    offers = _db.rows(
        _db.MARKET_DB,
        "SELECT id, actor_name, operation, industrial_stage, review_status FROM offers "
        "WHERE review_status IN ('accepted', 'review')",
    )
    return _positioning(offers, "operation", "offer", tracked, lambda row: row["review_status"] == "accepted")


def _markets(tracked: set[str]) -> list[dict[str, Any]]:
    evidence = _db.rows(
        _db.MARKET_DB,
        "SELECT id, actor_name, market, industrial_stage, review_status, bucket FROM evidence "
        "WHERE review_status IN ('accepted', 'review')",
    )
    return _positioning(evidence, "market", "evidence", tracked,
                        lambda row: row["review_status"] == "accepted" and row["bucket"] in ("existing", "radar"))


def _demand(today: date) -> list[dict[str, Any]]:
    signals = _db.rows(
        _db.MARKET_DB,
        "SELECT id, signal_type, source, buyer_name, title, published_at FROM demand_signals "
        "ORDER BY published_at DESC, id DESC",
    )
    since = (today - timedelta(days=730)).isoformat()
    by_type: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for signal in signals:
        by_type[(signal["signal_type"], signal["source"])].append(signal)
    return [
        {
            "type": signal_type,
            "source": source,
            "total": len(items),
            "derniers_24_mois": sum(1 for s in items if (s["published_at"] or "") >= since),
            "acheteurs": Counter(s["buyer_name"] for s in items if s["buyer_name"]).most_common(5),
            "plus_recents": [
                {"ref": f"demand:{s['id']}", "date": s["published_at"], "acheteur": s["buyer_name"], "titre": s["title"]}
                for s in items[:5]
            ],
        }
        for (signal_type, source), items in sorted(by_type.items(), key=lambda kv: -len(kv[1]))
    ]


def _technology(today: date, tracked: set[str]) -> list[dict[str, Any]]:
    signals = _db.rows(
        _db.TECH_DB,
        "SELECT id, axis, signal_year, actor_names FROM technology_signals_dated WHERE review_status='accepted'",
    )
    recent_from = today.year - 2
    by_axis: dict[str, list[dict]] = defaultdict(list)
    for signal in signals:
        if signal["axis"]:
            by_axis[signal["axis"]].append(signal)
    lines = []
    for axis, items in by_axis.items():
        if len(items) < MIN_SIGNALS_PER_AXIS:
            continue
        dated = [s for s in items if s["signal_year"]]
        recent = [s for s in dated if int(s["signal_year"]) >= recent_from]
        cited: set[str] = set()
        for s in items:
            try:
                cited.update(name for name in json.loads(s["actor_names"] or "[]") if name in tracked)
            except (TypeError, json.JSONDecodeError):
                pass
        lines.append({
            "axe": axis,
            "signaux": len(items),
            "dates": len(dated),
            f"depuis_{recent_from}": len(recent),
            "acteurs_cites": sorted(cited)[:5],
            "refs": _refs("tech", sorted((s["id"] for s in recent), reverse=True) or sorted(s["id"] for s in items)),
        })
    return sorted(lines, key=lambda line: (-line[f"depuis_{recent_from}"], -line["signaux"]))


def _movements(today: date, tracked: set[str]) -> dict[str, Any]:
    since = (today - timedelta(days=540)).isoformat()
    events = _db.rows(
        _db.ACTORS_DB,
        "SELECT e.id, e.event_type, e.event_date, e.description, e.review_status, a.name "
        "FROM actor_events e JOIN actors a ON a.id = e.actor_id "
        "WHERE e.event_date >= ? ORDER BY e.event_date DESC, e.id DESC",
        (since,),
    )
    events = [e for e in events if e["name"] in tracked]
    return {
        "depuis": since,
        "confirmes": [
            {"ref": f"event:{e['id']}", "type": e["event_type"], "acteur": e["name"], "date": e["event_date"],
             "description": (e["description"] or "")[:200]}
            for e in events if e["review_status"] == "verified"
        ],
        "a_confirmer_par_type": Counter(e["event_type"] for e in events if e["review_status"] != "verified").most_common(),
    }


def _share(part: int, whole: int) -> str:
    return f"{part}/{whole} ({round(100 * part / whole)} %)" if whole else "0/0"


def _gaps() -> list[dict[str, str]]:
    market, tech = _db.MARKET_DB, _db.TECH_DB
    gaps = []
    if not _db.scalar(market, "SELECT COUNT(*) FROM market_sizing"):
        gaps.append({"constat": "Aucune taille de marché enregistrée (market_sizing vide).",
                     "consequence": "Aucun chiffre de taille ou de croissance de marché ne peut être avancé."})
    if not _db.scalar(market, "SELECT COUNT(*) FROM market_reference_matrix"):
        gaps.append({"constat": "Matrice de référence marché × composant × opération vide.",
                     "consequence": "Impossible de dire quelles opérations un marché exige réellement."})
    total_evidence = _db.scalar(market, "SELECT COUNT(*) FROM evidence") or 0
    undetermined = _db.scalar(market, "SELECT COUNT(*) FROM evidence WHERE industrial_stage=?", (UNDETERMINED_STAGE,)) or 0
    if total_evidence and undetermined / total_evidence > 0.3:
        gaps.append({"constat": f"Maturité industrielle non déterminée pour {_share(undetermined, total_evidence)} des faits marché.",
                     "consequence": "Le compte « en production » sous-estime la présence industrielle réelle."})
    total_offers = _db.scalar(market, "SELECT COUNT(*) FROM offers WHERE review_status IN ('accepted','review')") or 0
    no_operation = _db.scalar(
        market, "SELECT COUNT(*) FROM offers WHERE review_status IN ('accepted','review') AND (operation IS NULL OR operation='')") or 0
    if total_offers and no_operation / total_offers > 0.1:
        gaps.append({"constat": f"{_share(no_operation, total_offers)} des offres n'ont pas d'opération identifiée.",
                     "consequence": "Ces offres n'entrent dans aucune ligne du tableau des opérations."})
    in_review = _db.scalar(market, "SELECT COUNT(*) FROM offers WHERE review_status='review'") or 0
    if total_offers and in_review / total_offers > 0.5:
        gaps.append({"constat": f"{_share(in_review, total_offers)} des offres sont encore en revue.",
                     "consequence": "Les comptes « confirmés » sont des minima ; les valider dans l'app les fiabilise."})
    total_tech = _db.scalar(tech, "SELECT COUNT(*) FROM technology_signals_dated WHERE review_status='accepted'") or 0
    undated = _db.scalar(
        tech, "SELECT COUNT(*) FROM technology_signals_dated WHERE review_status='accepted' AND signal_year IS NULL") or 0
    if total_tech and undated / total_tech > 0.3:
        gaps.append({"constat": f"{_share(undated, total_tech)} des signaux technologiques ne sont pas datés.",
                     "consequence": "Les tendances par axe ne portent que sur la part datée."})
    return gaps


def build_marketing_dossier(*, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    tracked, perimeter = _roster()
    return {
        "genere_le": _db.utc_now(),
        "perimetre": perimeter,
        "operations": _operations(tracked),
        "marches": _markets(tracked),
        "demande": _demand(today),
        "technologie": _technology(today, tracked),
        "mouvements": _movements(today, tracked),
        "lacunes": _gaps(),
    }
