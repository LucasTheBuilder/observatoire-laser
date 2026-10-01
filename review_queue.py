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

import hashlib
import json
from typing import Any, Literal

import review_journal
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

QUEUES = ("evidence", "offers", "tech_signals", "events", "facts", "actors", "vocabulary", "marketing")

# §5.G item 3 : motifs de rejet typés, pour pouvoir apprendre des rejets (item 4) au lieu de les
# perdre dans un champ libre.
#
# Les cinq motifs d'origine ont été conçus pour un FAIT MARCHÉ : un triplet marché/composant/
# opération adossé à une citation. Servis tels quels aux huit files, deux d'entre eux n'ont
# aucun sens sur un candidat acteur -- "mauvais acteur" alors que l'item EST l'acteur,
# "mauvaise dimension" alors qu'un candidat n'a ni marché ni composant ni opération. Une liste
# où deux entrées sur cinq ne veulent rien dire pousse à choisir "au moins pire", et produit
# exactement les motifs bruités que l'analyse des rejets devra ensuite lire.
#
# D'où un jeu PAR FILE, avec un socle commun (off_topic, duplicate). Les files pour lesquelles
# aucune décision n'a encore été observée gardent les cinq motifs d'origine : les élargir
# aujourd'hui serait inventer des modes d'échec au lieu de les constater.
_CORE_REASONS = ("off_topic", "duplicate")
_FACT_REASONS = ("off_topic", "wrong_actor", "wrong_dimension", "unconvincing_citation", "duplicate")

REJECT_REASONS_BY_QUEUE: dict[str, tuple[str, ...]] = {
    # Fait marché : les cinq d'origine, plus deux modes d'échec que la file en attente rend
    # incontournables. `incomplete_dimension` -- 70 des 123 faits en attente sont `partial`
    # (2 dimensions sur 3), et wrong_dimension confondait "le libellé est faux" (à corriger dans
    # le lexique) avec "la dimension manque" (à corriger dans la fenêtre d'extraction), deux
    # diagnostics opposés. `capability_not_application` -- "nous POUVONS découper des électrodes"
    # n'est pas une application marché ; rejeter ça en citation non probante serait faux, la
    # citation est probante, elle prouve autre chose (voir db.classify_evidence_type).
    "evidence": (*_FACT_REASONS, "incomplete_dimension", "capability_not_application"),
    "offers": _FACT_REASONS,
    "tech_signals": _FACT_REASONS,
    "events": _FACT_REASONS,
    "facts": _FACT_REASONS,
    "vocabulary": _FACT_REASONS,
    # Acteurs (file 'actors') et candidats acteurs : l'objet est une ORGANISATION, pas un fait.
    # wrong_actor et wrong_dimension sont retirés faute d'objet. Les trois motifs ajoutés
    # viennent de ce que contient réellement la file : 397 des 441 candidats sont des partenaires
    # de consortium CORDIS (le projet était on-topic, l'organisation n'est pas un acteur laser --
    # ce n'est donc pas "hors sujet", la source était pertinente, c'est l'inférence qui ne l'est
    # pas) ; le haut de liste est saturé d'universités et de laboratoires, réels et on-topic mais
    # académiques, donc une décision de PÉRIMÈTRE et non de justesse ; et l'extraction de noms
    # remonte aussi des projets, départements ou personnes, qui ne sont pas des organisations.
    "actors": (*_CORE_REASONS, "not_an_organization", "consortium_partner_out_of_scope", "out_of_scope_academic"),
    "candidates": (*_CORE_REASONS, "not_an_organization", "consortium_partner_out_of_scope", "out_of_scope_academic"),
    # Recommandation de l'agent marketing : l'objet est un CONSEIL, pas un fait -- ni acteur ni
    # dimension à contester. Les références et les chiffres sont déjà filtrés avant écriture
    # (marketing_agent.rejection_reason) ; reste ce qu'un filtre ne voit pas : une lecture forcée
    # de références réelles, une évidence déjà connue, un conseil inapplicable ou contraire à la
    # stratégie. Seul misread_refs renvoie vers l'agent (son prompt) ; les trois autres, vers ce
    # qu'il ne peut pas savoir de nous.
    "marketing": (*_CORE_REASONS, "misread_refs", "already_known", "not_actionable", "against_strategy"),
}

# Libellés en français, tenus ICI et exposés par l'API (voir app.py: GET /api/reject-reasons)
# plutôt que recopiés dans static/app.js : deux listes à maintenir en parallèle finissent
# toujours par diverger, et c'est le slug stocké en base qui compte.
REJECT_REASON_LABELS: dict[str, str] = {
    "off_topic": "Hors sujet",
    "wrong_actor": "Mauvais acteur",
    "wrong_dimension": "Mauvaise dimension",
    "unconvincing_citation": "Citation non probante",
    "duplicate": "Doublon",
    "incomplete_dimension": "Dimension manquante",
    "capability_not_application": "Capacité, pas une application",
    "not_an_organization": "Pas une organisation",
    "consortium_partner_out_of_scope": "Partenaire de consortium hors périmètre",
    "out_of_scope_academic": "Hors périmètre (académique)",
    "misread_refs": "Références mal lues",
    "already_known": "Déjà connu / déjà en cours",
    "not_actionable": "Pas actionnable",
    "against_strategy": "Contraire à notre stratégie",
}

# Union de tous les motifs : sert aux contrôles génériques et à la documentation d'API. La
# validation d'une décision, elle, passe TOUJOURS par reasons_for_queue() -- accepter ici un
# motif valide pour une autre file reviendrait à ne rien avoir typé du tout.
REJECT_REASONS = tuple(dict.fromkeys(
    reason for reasons in REJECT_REASONS_BY_QUEUE.values() for reason in reasons
))


def reasons_for_queue(queue: str) -> tuple[str, ...]:
    """Motifs valides pour une file. Une file inconnue n'en a aucun -- jamais un repli permissif."""
    return REJECT_REASONS_BY_QUEUE.get(queue, ())

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
    "marketing": {"pending": "review", "accepted": "accepted", "rejected": "rejected"},
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
            # market/component/operation exposés séparément en plus de `summary` : l'échantillon
            # d'audit propose « garder comme référence » sur les faits marché, et
            # POST /api/golden-facts attend les trois dimensions distinctes. Les relire en
            # reparsant `summary` côté JS ferait dépendre le golden set d'un format d'affichage.
            detail={
                "fact_status": row["fact_status"], "bucket": row["bucket"], "quote": row["quote"],
                "source_url": row["source_url"], "market": row["market"],
                "component": row["component"], "operation": row["operation"],
            },
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


_MARKETING_CONFIDENCE_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def _list_marketing(status: str) -> list[dict]:
    with connect(MARKET_DB) as db:
        rows = db.execute(
            "SELECT * FROM marketing_recommendations WHERE review_status=?",
            (_STATUS_MAP_3WAY["marketing"][status],),
        ).fetchall()
    return [
        _item(
            item_id=row["id"], queue="marketing", actor_name="HEF/IREIS", summary=row["title"],
            detail={"kind": row["kind"], "rationale": row["rationale"], "refs": json.loads(row["refs"]),
                    "confidence": row["confidence"], "run_id": row["run_id"], "model": row["model"]},
            confidence=None, priority=float(_MARKETING_CONFIDENCE_WEIGHT.get(row["confidence"], 1)),
            reviewed_by=row["reviewed_by"], reviewed_at=row["reviewed_at"], reject_reason=row["reject_reason"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


_LISTERS = {
    "evidence": _list_evidence,
    "offers": _list_offers,
    "tech_signals": _list_tech_signals,
    "events": _list_events,
    "facts": _list_facts,
    "actors": _list_actors,
    "vocabulary": _list_vocabulary,
    "marketing": _list_marketing,
}


def list_review_queue(queue: str, status: Literal["pending", "accepted", "rejected"] = "pending") -> list[dict]:
    """Contrat unique pour les 7 files (§5.G item 1). Triée par priorité décroissante (§5.G
    item 2) : valider trois faits sur un C1 vaut mieux que quarante sur un T1."""
    if queue not in _LISTERS:
        raise ValueError(f"Unknown queue {queue!r} -- expected one of {QUEUES}")
    items = _LISTERS[queue](status)
    items.sort(key=lambda item: item["priority"], reverse=True)
    return items


AUDIT_REVIEWED_BY = "audit"
DEFAULT_AUDIT_SEED = "audit-1"


def sample_review_queue(
    queue: str, status: Literal["pending", "accepted", "rejected"] = "accepted",
    *, size: int = 30, seed: str = DEFAULT_AUDIT_SEED,
) -> dict[str, Any]:
    """Échantillon ALÉATOIRE d'une file, pour auditer ce que le pipeline n'a jamais soumis.

    La file de revue ne montre que ce dont le pipeline a DOUTÉ. Les lignes qu'il a acceptées
    seul -- 363 offres, 31 faits marché, 14 signaux, 117 faits acteurs au 01/09/2026, aucune
    avec ``reviewed_at`` -- ne passent jamais devant personne. Une erreur systématique dans une
    règle confiante est donc invisible par construction : c'est le pipeline qui choisit
    l'échantillon relu, avec la logique même qui pourrait être fausse. Tirer au hasard dans la
    population acceptée est le seul moyen d'estimer sa précision.

    Le tirage est REPRODUCTIBLE : classement par sha256(seed:queue:id), donc le même (seed,
    size) redonne les mêmes items d'une session à l'autre -- un audit interrompu se reprend là
    où il s'est arrêté au lieu de repartir sur un échantillon différent, ce qui invaliderait la
    mesure. Changer ``seed`` tire un échantillon franchement neuf.

    Les items déjà audités (``reviewed_at`` renseigné) sortent de la population : l'échantillon
    se recharge donc en items encore jamais vus au fur et à mesure des décisions. ``population``
    renvoie le nombre restant, ``audited``/``rejected`` le cumul déjà traité, de quoi afficher
    une précision courante sans requête supplémentaire.
    """
    if queue not in _LISTERS:
        raise ValueError(f"Unknown queue {queue!r} -- expected one of {QUEUES}")
    if size < 1:
        raise ValueError("size must be >= 1")
    everything = _LISTERS[queue](status)
    unaudited = [item for item in everything if not item["reviewed_at"]]
    ranked = sorted(
        unaudited,
        key=lambda item: hashlib.sha256(f"{seed}:{queue}:{item['id']}".encode()).hexdigest(),
    )
    items = ranked[:size]
    items.sort(key=lambda item: item["priority"], reverse=True)
    # Les rejets quittent `status='accepted'`, donc ils ne sont pas dans `everything` : le cumul
    # audité se lit sur les deux statuts, sans quoi la précision affichée n'aurait pas de
    # dénominateur (et vaudrait toujours 100 %).
    rejected_audited = [item for item in _LISTERS[queue]("rejected") if item["reviewed_at"]]
    accepted_audited = [item for item in everything if item["reviewed_at"]]
    return {
        "queue": queue, "status": status, "seed": seed, "items": items,
        "population": len(unaudited),
        "audited": len(accepted_audited) + len(rejected_audited),
        "rejected": len(rejected_audited),
    }


def _mark_offer_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(MARKET_DB) as db:
        return db.execute(
            "UPDATE offers SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
            (review_status, reviewed_by, utc_now(), reject_reason, item_id),
        ).rowcount


def _mark_marketing_decision(item_id: int, *, review_status: str, reviewed_by: str | None, reject_reason: str | None) -> int:
    with connect(MARKET_DB) as db:
        return db.execute(
            "UPDATE marketing_recommendations SET review_status=?,reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=?",
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
    if decision == "reject" and reject_reason not in reasons_for_queue(queue):
        raise ValueError(
            f"reject_reason must be one of {reasons_for_queue(queue)} for a rejection on queue {queue!r}"
        )

    # Instantané pris AVANT la décision : accepter un candidat vocabulaire le promeut, accepter
    # un fait change son statut, et une fusion de doublons peut faire disparaître la ligne plus
    # tard. Lire l'identité maintenant garantit que le journal reste lisible dans tous ces cas.
    snapshot = review_journal.snapshot_item(queue, item_id)

    def _journal() -> None:
        # Appelé seulement après une écriture réussie -- jamais sur le chemin d'erreur, sinon le
        # journal enregistrerait des décisions qui n'ont pas eu lieu.
        review_journal.record_decision(
            queue, item_id, decision, reject_reason=reject_reason if decision == "reject" else None,
            decided_by=reviewed_by, snapshot=snapshot,
        )

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
        _journal()
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
        _journal()
        return result

    review_status = _STATUS_MAP_3WAY[queue]["accepted" if decision == "accept" else "rejected"]
    mark = {
        "offers": _mark_offer_decision,
        "tech_signals": _mark_tech_signal_decision,
        "events": _mark_event_decision,
        "facts": _mark_fact_decision,
        "actors": _mark_actor_decision,
        "marketing": _mark_marketing_decision,
    }[queue]
    updated = mark(
        item_id, review_status=review_status, reviewed_by=reviewed_by,
        reject_reason=reject_reason if decision == "reject" else None,
    )
    if not updated:
        raise ValueError(f"{queue} item {item_id} not found")
    _journal()
    return {"id": item_id, "queue": queue, "status": review_status}
