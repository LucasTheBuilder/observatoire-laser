"""File de revue unifiée (§5.G audit veille, 30/08/2026, Lot 1 §1.1) : "sept écrans séparés ne
seront jamais tous consultés" -- avant ce module, 2 des 7 files de validation avaient un
endpoint (evidence via /api/market/review, vocabulary_candidates), les 5 autres (offers,
technology_signals, actor_events, actor_facts, actors candidats) n'en avaient aucun (§3.2).

Un seul contrat pour les 7 : GET /api/review?queue=...&status=pending|accepted|rejected renvoie
une liste d'items {id, queue, actor_name, summary, detail, confidence, priority, reviewed_by,
reviewed_at, reject_reason, created_at}, triée par priorité décroissante. POST
/api/review/{queue}/{item_id}/decide (accept/reject) écrit la décision avec sa traçabilité
(reviewed_by, reviewed_at, et pour un rejet un motif TYPÉ -- REJECT_REASONS -- "sans motif typé,
on ne peut rien apprendre des rejets").

Priorisation (classe concurrentielle de l'acteur × impact du fait × incertitude d'extraction,
§5.G item 2) : un score, pas un tri à plusieurs clés, pour que les 7 files se comparent entre
elles dans un même flux. L'incertitude est dérivée de field_confidence quand la table en a un
(evidence/offers/technology_signals) ; les files sans confiance numérique (actor_facts/
actor_events/actors/vocabulary_candidates) reçoivent un poids d'incertitude neutre plutôt qu'une
valeur inventée.

Chaque accept/reject réutilise la logique métier déjà existante quand il y en a une (evidence,
vocabulary_candidates) plutôt que de la dupliquer ; les 5 files jusqu'ici sans queue en reçoivent
une nouvelle, symétrique.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from db import (
    ACTORS_DB,
    MARKET_DB,
    TECH_DB,
    accept_evidence_review,
    accept_vocabulary_candidate,
    connect,
    reject_evidence_review,
    reject_vocabulary_candidate,
    utc_now,
)

QUEUES = ("evidence", "offers", "tech_signals", "events", "facts", "actors", "vocabulary")

# §5.G item 3 : motifs de rejet typés, pour pouvoir apprendre des rejets (item 4) au lieu de les
# perdre dans un champ libre. "hors sujet / mauvais acteur / mauvaise dimension / citation non
# probante / doublon" du §5.G, traduits en slugs stables côté API/DB.
REJECT_REASONS = ("off_topic", "wrong_actor", "wrong_dimension", "unconvincing_citation", "duplicate")

_COMPETITIVE_WEIGHT: dict[str, int] = {"C1": 3, "C2": 2, "A1": 2, "T1": 1}
_DEFAULT_COMPETITIVE_WEIGHT = 1
_NEUTRAL_UNCERTAINTY_WEIGHT = 1.5


def _competitive_weights() -> dict[str, int]:
    with connect(ACTORS_DB) as db:
        return {
            row["name"]: _COMPETITIVE_WEIGHT.get(row["competitive_class"], _DEFAULT_COMPETITIVE_WEIGHT)
            for row in db.execute("SELECT name,competitive_class FROM actors")
        }


def _priority(competitive_weight: int, impact_weight: int, confidence: float | None) -> float:
    uncertainty_weight = _NEUTRAL_UNCERTAINTY_WEIGHT if confidence is None else (1 + (1 - confidence))
    return round(competitive_weight * impact_weight * uncertainty_weight, 3)


def _item(
    *, item_id: int, queue: str, actor_name: str | None, summary: str, detail: dict[str, Any],
    confidence: float | None, priority: float, reviewed_by: str | None, reviewed_at: str | None,
    reject_reason: str | None, created_at: str | None,
) -> dict[str, Any]:
    return {
        "id": item_id, "queue": queue, "actor_name": actor_name, "summary": summary, "detail": detail,
        "confidence": confidence, "priority": priority, "reviewed_by": reviewed_by, "reviewed_at": reviewed_at,
        "reject_reason": reject_reason, "created_at": created_at,
    }


_STATUS_MAP_3WAY: dict[str, dict[str, str]] = {
    "evidence": {"pending": "review", "accepted": "accepted", "rejected": "rejected"},
    "offers": {"pending": "review", "accepted": "accepted", "rejected": "rejected"},
    "tech_signals": {"pending": "review", "accepted": "accepted", "rejected": "rejected"},
    "events": {"pending": "pending", "accepted": "verified", "rejected": "rejected"},
    "facts": {"pending": "pending", "accepted": "verified", "rejected": "rejected"},
    "actors": {"pending": "candidate", "accepted": "verified", "rejected": "rejected"},
    "vocabulary": {"pending": "pending", "accepted": "accepted", "rejected": "rejected"},
}


def _list_evidence(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["evidence"][status]
    clause = "evidence_kind='market_application' AND review_status=?"
    if status == "pending":
        # Belt-and-suspenders, same reasoning as the legacy /api/market/review endpoint: a
        # 'validated' fact must never reappear in the pending queue even if review_status
        # somehow lagged behind.
        clause += " AND fact_status!='validated'"
    with connect(MARKET_DB) as db:
        rows = db.execute(
            f"""SELECT id,actor_name,fact_status,bucket,market,component,operation,source_url,quote,
                       field_confidence,reviewed_by,reviewed_at,reject_reason,created_at
                FROM evidence WHERE {clause}""",
            (review_status,),
        ).fetchall()
    weights = _competitive_weights()
    items = []
    for row in rows:
        impact = 2 if row["bucket"] == "existing" else 1
        confidence = row["field_confidence"]
        items.append(_item(
            item_id=row["id"], queue="evidence", actor_name=row["actor_name"],
            summary=f"{row['market'] or '?'} / {row['component'] or '?'} / {row['operation'] or '?'}",
            detail={"fact_status": row["fact_status"], "bucket": row["bucket"], "quote": row["quote"], "source_url": row["source_url"]},
            confidence=confidence,
            priority=_priority(weights.get(row["actor_name"], _DEFAULT_COMPETITIVE_WEIGHT), impact, confidence),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        ))
    return items


def _list_offers(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["offers"][status]
    with connect(MARKET_DB) as db:
        rows = db.execute(
            """SELECT id,actor_name,capability,operation,laser_process,source_url,quote,
                      field_confidence,reviewed_by,reviewed_at,reject_reason,created_at
               FROM offers WHERE review_status=?""",
            (review_status,),
        ).fetchall()
    weights = _competitive_weights()
    items = []
    for row in rows:
        impact = 2 if row["operation"] else 1
        confidence = row["field_confidence"]
        items.append(_item(
            item_id=row["id"], queue="offers", actor_name=row["actor_name"],
            summary=f"{row['capability']}" + (f" ({row['operation']})" if row["operation"] else ""),
            detail={"laser_process": row["laser_process"], "quote": row["quote"], "source_url": row["source_url"]},
            confidence=confidence,
            priority=_priority(weights.get(row["actor_name"], _DEFAULT_COMPETITIVE_WEIGHT), impact, confidence),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        ))
    return items


def _list_tech_signals(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["tech_signals"][status]
    with connect(TECH_DB) as db:
        rows = db.execute(
            """SELECT id,axis,maturity_stage,bucket,project_name,actor_names,source_url,quote,
                      field_confidence,reviewed_by,reviewed_at,reject_reason,created_at
               FROM technology_signals WHERE review_status=?""",
            (review_status,),
        ).fetchall()
    weights = _competitive_weights()
    items = []
    for row in rows:
        try:
            actor_names = json.loads(row["actor_names"]) if row["actor_names"] else []
        except (TypeError, ValueError):
            actor_names = []
        actor_weight = max((weights.get(name, _DEFAULT_COMPETITIVE_WEIGHT) for name in actor_names), default=_DEFAULT_COMPETITIVE_WEIGHT)
        impact = 2 if row["bucket"] == "existing" else 1
        confidence = row["field_confidence"]
        items.append(_item(
            item_id=row["id"], queue="tech_signals", actor_name=", ".join(actor_names) or None,
            summary=f"{row['axis']} -- {row['maturity_stage']}" + (f" ({row['project_name']})" if row["project_name"] else ""),
            detail={"bucket": row["bucket"], "quote": row["quote"], "source_url": row["source_url"]},
            confidence=confidence,
            priority=_priority(actor_weight, impact, confidence),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        ))
    return items


def _list_events(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["events"][status]
    with connect(ACTORS_DB) as db:
        rows = db.execute(
            """SELECT e.id,a.name AS actor_name,a.competitive_class,e.event_type,e.description,e.source_url,
                      e.reviewed_by,e.reviewed_at,e.reject_reason,e.created_at
               FROM actor_events e JOIN actors a ON a.id=e.actor_id
               WHERE e.review_status=?""",
            (review_status,),
        ).fetchall()
    items = []
    for row in rows:
        weight = _COMPETITIVE_WEIGHT.get(row["competitive_class"], _DEFAULT_COMPETITIVE_WEIGHT)
        impact = 2 if row["event_type"] in ("investment", "acquisition", "patent") else 1
        items.append(_item(
            item_id=row["id"], queue="events", actor_name=row["actor_name"],
            summary=f"{row['event_type']} -- {row['description'][:140]}",
            detail={"source_url": row["source_url"]},
            confidence=None,
            priority=_priority(weight, impact, None),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        ))
    return items


def _list_facts(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["facts"][status]
    with connect(ACTORS_DB) as db:
        rows = db.execute(
            """SELECT f.id,a.name AS actor_name,a.competitive_class,f.dimension,f.value,f.source_url,
                      f.reviewed_by,f.reviewed_at,f.reject_reason,f.created_at
               FROM actor_facts f JOIN actors a ON a.id=f.actor_id
               WHERE f.review_status=?""",
            (review_status,),
        ).fetchall()
    items = []
    for row in rows:
        weight = _COMPETITIVE_WEIGHT.get(row["competitive_class"], _DEFAULT_COMPETITIVE_WEIGHT)
        impact = 2 if row["dimension"] == "certification" else 1
        items.append(_item(
            item_id=row["id"], queue="facts", actor_name=row["actor_name"],
            summary=f"{row['dimension']} : {row['value']}",
            detail={"source_url": row["source_url"]},
            confidence=None,
            priority=_priority(weight, impact, None),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        ))
    return items


def _list_actors(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["actors"][status]
    with connect(ACTORS_DB) as db:
        rows = db.execute(
            """SELECT id,name,country,role,official_url,reviewed_by,reviewed_at,reject_reason,updated_at
               FROM actors WHERE review_status=?""",
            (review_status,),
        ).fetchall()
    items = []
    for row in rows:
        items.append(_item(
            item_id=row["id"], queue="actors", actor_name=row["name"],
            summary=f"{row['name']} ({row['country']}) -- candidat acteur",
            detail={"role": row["role"], "official_url": row["official_url"]},
            confidence=None,
            priority=_priority(_DEFAULT_COMPETITIVE_WEIGHT, 1, None),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["updated_at"],
        ))
    return items


def _list_vocabulary(status: str) -> list[dict]:
    review_status = _STATUS_MAP_3WAY["vocabulary"][status]
    with connect(MARKET_DB) as db:
        rows = db.execute(
            """SELECT id,actor_name,proposed_labels,quote,source_url,reviewed_by,reviewed_at,reject_reason,created_at
               FROM vocabulary_candidates WHERE review_status=?""",
            (review_status,),
        ).fetchall()
    weights = _competitive_weights()
    items = []
    for row in rows:
        try:
            proposed = json.loads(row["proposed_labels"]) if row["proposed_labels"] else {}
        except (TypeError, ValueError):
            proposed = {}
        items.append(_item(
            item_id=row["id"], queue="vocabulary", actor_name=row["actor_name"],
            summary=", ".join(f"{dim}: {label}" for dim, label in proposed.items()) or "(aucune proposition)",
            detail={"quote": row["quote"], "source_url": row["source_url"], "proposed_labels": proposed},
            confidence=None,
            priority=_priority(weights.get(row["actor_name"], _DEFAULT_COMPETITIVE_WEIGHT), 1, None),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        ))
    return items


_LISTERS = {
    "evidence": _list_evidence,
    "offers": _list_offers,
    "tech_signals": _list_tech_signals,
    "events": _list_events,
    "facts": _list_facts,
    "actors": _list_actors,
    "vocabulary": _list_vocabulary,
}


def list_review_queue(queue: str, status: Literal["pending", "accepted", "rejected"] = "pending") -> list[dict]:
    """Contrat unique pour les 7 files (§5.G item 1). Triée par priorité décroissante (§5.G
    item 2) : valider trois faits sur un C1 vaut mieux que quarante sur un T1."""
    if queue not in _LISTERS:
        raise ValueError(f"Unknown queue {queue!r} -- expected one of {QUEUES}")
    items = _LISTERS[queue](status)
    items.sort(key=lambda item: item["priority"], reverse=True)
    return items


def _mark_offer_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(MARKET_DB) as db:
        return db.execute(
            "UPDATE offers SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (review_status, reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def _mark_tech_signal_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(TECH_DB) as db:
        return db.execute(
            "UPDATE technology_signals SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (review_status, reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def _mark_event_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(ACTORS_DB) as db:
        return db.execute(
            "UPDATE actor_events SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (review_status, reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def _mark_fact_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(ACTORS_DB) as db:
        return db.execute(
            "UPDATE actor_facts SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (review_status, reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def _mark_actor_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(ACTORS_DB) as db:
        return db.execute(
            "UPDATE actors SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (review_status, reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def _mark_vocabulary_decision(item_id: int, *, reviewed_by: str | None, reject_reason: str | None) -> int:
    # Acceptance still goes through accept_vocabulary_candidate (promotes the lexicon entry) --
    # this helper is only ever called for the trace columns, and for rejection's review_status.
    with connect(MARKET_DB) as db:
        return db.execute(
            "UPDATE vocabulary_candidates SET reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def decide_review_item(
    queue: str, item_id: int, decision: Literal["accept", "reject"],
    *, reviewed_by: str | None = None, reject_reason: str | None = None, dimension: str | None = None,
) -> dict[str, Any]:
    """Point d'entrée unique pour accepter/rejeter un item de n'importe laquelle des 7 files
    (§5.G item 1), avec traçabilité (§5.G item 3) : reviewed_by, reviewed_at (toujours
    horodaté ici), reject_reason (obligatoire et typé pour un rejet -- REJECT_REASONS).

    ``dimension`` n'est utilisé que pour queue='vocabulary' et decision='accept' (quelle
    dimension proposée promouvoir -- voir accept_vocabulary_candidate). Réutilise la logique
    métier déjà existante pour evidence/vocabulary plutôt que de la dupliquer.
    """
    if queue not in _LISTERS:
        raise ValueError(f"Unknown queue {queue!r} -- expected one of {QUEUES}")
    if decision == "reject" and reject_reason not in REJECT_REASONS:
        raise ValueError(f"reject_reason must be one of {REJECT_REASONS} for a rejection")

    if queue == "evidence":
        if decision == "accept":
            result = accept_evidence_review(item_id)
        else:
            reject_evidence_review(item_id)
            result = {"id": item_id, "status": "rejected"}
        with connect(MARKET_DB) as db:
            updated = db.execute(
                "UPDATE evidence SET reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
                (reviewed_by, utc_now(), reject_reason if decision == "reject" else None, item_id),
            ).rowcount
        if not updated:
            raise ValueError(f"Evidence {item_id} not found")
        return result

    if queue == "vocabulary":
        if decision == "accept":
            if not dimension:
                raise ValueError("dimension is required to accept a vocabulary candidate")
            result = accept_vocabulary_candidate(item_id, dimension)
        else:
            reject_vocabulary_candidate(item_id)
            result = {"id": item_id, "status": "rejected"}
        updated = _mark_vocabulary_decision(item_id, reviewed_by=reviewed_by, reject_reason=reject_reason if decision == "reject" else None)
        if not updated:
            raise ValueError(f"Vocabulary candidate {item_id} not found")
        return result

    review_status = _STATUS_MAP_3WAY[queue]["accepted" if decision == "accept" else "rejected"]
    mark = {
        "offers": _mark_offer_decision,
        "tech_signals": _mark_tech_signal_decision,
        "events": _mark_event_decision,
        "facts": _mark_fact_decision,
        "actors": _mark_actor_decision,
    }[queue]
    updated = mark(
        item_id, review_status=review_status, reviewed_by=reviewed_by,
        reject_reason=reject_reason if decision == "reject" else None,
    )
    if not updated:
        raise ValueError(f"{queue} item {item_id} not found")
    return {"id": item_id, "queue": queue, "status": review_status}
