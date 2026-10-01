"""Dossier marketing (phase 1 de l'agent marketing) : ce que la veille dit de la position de
HEF/IREIS face aux concurrents suivis, assemblé en SQL, sans aucun appel de modèle.

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

OUR_COMPETITIVE_CLASS = "A1"
PRODUCTION_STAGES = {"Production", "Industrialisation"}
UNDETERMINED_STAGE = "Maturité industrielle non déterminée"
MIN_SIGNALS_PER_AXIS = 5
REFS_PER_LINE = 3


def _refs(prefix: str, ids: list[int]) -> list[str]:
    return [f"{prefix}:{i}" for i in ids[:REFS_PER_LINE]]


def _our_status(names: set[str], confirmed: set[str], unconfirmed: set[str]) -> str:
    if names & confirmed:
        return "confirmé"
    if names & unconfirmed:
        return "à confirmer"
    return "absent"


def _roster() -> tuple[set[str], set[str], dict[str, Any]]:
    actors = _db.rows(
        _db.ACTORS_DB,
        "SELECT name, country, actor_type, competitive_class, is_reference, active, review_status FROM actors",
    )
    us = {a["name"] for a in actors if a["is_reference"] and a["competitive_class"] == OUR_COMPETITIVE_CLASS}
    competitors = [
        a for a in actors if not a["is_reference"] and a["active"] and a["review_status"] == "verified"
    ]
    perimeter = {
        "nous": sorted(us),
        "concurrents": len(competitors),
        "par_type": Counter(a["actor_type"] or "non renseigné" for a in competitors).most_common(),
        "par_pays": Counter(a["country"] or "non renseigné" for a in competitors).most_common(),
    }
    return us, {a["name"] for a in competitors}, perimeter


def _positioning(rows: list[dict], key: str, prefix: str, us: set[str], competitors: set[str],
                 is_confirmed) -> list[dict[str, Any]]:
    """Par valeur de ``key`` : qui la revendique (confirmé / à confirmer), et nous ?"""
    by_value: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"confirmed": set(), "unconfirmed": set(), "us_confirmed": set(), "us_unconfirmed": set(),
                 "production": set(), "refs": []}
    )
    for row in rows:
        value = row[key]
        if not value:
            continue
        entry = by_value[value]
        confirmed = is_confirmed(row)
        name = row["actor_name"]
        if name in us:
            entry["us_confirmed" if confirmed else "us_unconfirmed"].add(name)
        elif name in competitors:
            entry["confirmed" if confirmed else "unconfirmed"].add(name)
            if confirmed:
                entry["refs"].append(row["id"])
                if row.get("industrial_stage") in PRODUCTION_STAGES:
                    entry["production"].add(name)
    lines = []
    for value, entry in by_value.items():
        lines.append({
            key: value,
            "concurrents_confirmes": len(entry["confirmed"]),
            "concurrents_a_confirmer": len(entry["unconfirmed"] - entry["confirmed"]),
            "concurrents_en_production": len(entry["production"]),
            "exemples": sorted(entry["confirmed"])[:5],
            "nous": _our_status(us, entry["us_confirmed"], entry["us_unconfirmed"]),
            "refs": _refs(prefix, sorted(entry["refs"])),
        })
    return sorted(lines, key=lambda line: (-line["concurrents_confirmes"], line[key]))


def _operations(us: set[str], competitors: set[str]) -> list[dict[str, Any]]:
    offers = _db.rows(
        _db.MARKET_DB,
        "SELECT id, actor_name, operation, industrial_stage, review_status FROM offers "
        "WHERE review_status IN ('accepted', 'review')",
    )
    return _positioning(offers, "operation", "offer", us, competitors,
                        lambda row: row["review_status"] == "accepted")


def _markets(us: set[str], competitors: set[str]) -> list[dict[str, Any]]:
    evidence = _db.rows(
        _db.MARKET_DB,
        "SELECT id, actor_name, market, industrial_stage, review_status, bucket FROM evidence "
        "WHERE review_status IN ('accepted', 'review')",
    )
    return _positioning(evidence, "market", "evidence", us, competitors,
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


def _technology(today: date, competitors: set[str]) -> list[dict[str, Any]]:
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
                cited.update(name for name in json.loads(s["actor_names"] or "[]") if name in competitors)
            except (TypeError, json.JSONDecodeError):
                pass
        lines.append({
            "axe": axis,
            "signaux": len(items),
            "dates": len(dated),
            f"depuis_{recent_from}": len(recent),
            "concurrents_cites": sorted(cited)[:5],
            "refs": _refs("tech", sorted((s["id"] for s in recent), reverse=True) or sorted(s["id"] for s in items)),
        })
    return sorted(lines, key=lambda line: (-line[f"depuis_{recent_from}"], -line["signaux"]))


def _movements(today: date, competitors: set[str]) -> dict[str, Any]:
    since = (today - timedelta(days=540)).isoformat()
    events = _db.rows(
        _db.ACTORS_DB,
        "SELECT e.id, e.event_type, e.event_date, e.description, e.review_status, a.name "
        "FROM actor_events e JOIN actors a ON a.id = e.actor_id "
        "WHERE e.event_date >= ? ORDER BY e.event_date DESC, e.id DESC",
        (since,),
    )
    events = [e for e in events if e["name"] in competitors]
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


def _gaps(us: set[str]) -> list[dict[str, str]]:
    market, tech = _db.MARKET_DB, _db.TECH_DB
    gaps = []
    placeholders = ",".join("?" * len(us)) or "''"
    ours = dict(Counter(
        r["review_status"] for r in _db.rows(
            market, f"SELECT review_status FROM offers WHERE actor_name IN ({placeholders})", tuple(us))
    ))
    if ours.get("accepted", 0) < 5:
        gaps.append({
            "constat": f"Notre propre offre ({', '.join(sorted(us)) or 'aucun acteur de référence'}) est à peine "
                       f"décrite : {ours.get('accepted', 0)} offre(s) confirmée(s), {ours.get('review', 0)} en revue.",
            "consequence": "Toute comparaison « nous vs concurrents » repose sur une offre incomplète : "
                           "un « absent » peut signifier « non collecté », pas « non proposé ».",
        })
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
    us, competitors, perimeter = _roster()
    return {
        "genere_le": _db.utc_now(),
        "perimetre": perimeter,
        "operations": _operations(us, competitors),
        "marches": _markets(us, competitors),
        "demande": _demand(today),
        "technologie": _technology(today, competitors),
        "mouvements": _movements(today, competitors),
        "lacunes": _gaps(us),
    }
