"""Journal append-only des décisions de revue (plan agent d'analyse, phase 0.2).

Les colonnes ``reviewed_by``/``reviewed_at``/``reject_reason`` posées sur chaque table restent
la source de vérité pour "quel est l'état ACTUEL de cet item". Ce journal répond aux deux
questions qu'elles ne peuvent structurellement pas couvrir :

1. **L'historique.** Une colonne ne garde qu'un état. Un fait rejeté, rouvert, puis accepté ne
   laisse aucune trace de son premier passage -- or un revirement est précisément ce qu'une
   analyse des rejets doit pouvoir voir.
2. **La survie à la disparition de la ligne.** ``_canonicalise_existing_evidence`` et
   ``_migrate_evidence_fact_model`` SUPPRIMENT des lignes d'evidence en fusionnant les doublons.
   La décision part avec elles, sans bruit. Le journal, lui, reste.

D'où l'instantané (actor_name/summary/source_url) figé au moment de la décision : le journal doit
rester lisible seul, même quand la ligne d'origine n'existe plus. Il n'est jamais une copie
complète de l'item -- pour tout le reste (citation, provenance, confiance), la jointure sur la
table vivante fait le travail tant que la ligne existe.

Huit files : les sept de ``review_queue.QUEUES`` plus ``candidates`` (actor_candidates), qui a
son propre chemin de décision. La liste est redéfinie ici plutôt qu'importée pour que
``review_queue`` puisse importer ce module sans cycle.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import db as _db
from db import connect, utc_now

Decision = Literal["accept", "reject"]

JOURNAL_QUEUES = (
    "evidence", "offers", "tech_signals", "events", "facts", "actors", "vocabulary", "candidates", "marketing",
)

# Par file : la base, la requête d'instantané (par id), et la requête d'amorçage (toutes les
# lignes déjà décidées). Les deux renvoient les mêmes colonnes d'identité, pour qu'un seul
# formateur (_summarize) serve aux deux usages.
_QUEUES: dict[str, dict[str, Any]] = {
    "evidence": {
        "db": "market",
        "select": "SELECT id,actor_name,market,component,operation,source_url,reviewed_by,reviewed_at,reject_reason,review_status FROM evidence",
        "summary": lambda r: f"{r['market'] or '?'} / {r['component'] or '?'} / {r['operation'] or '?'}",
    },
    "offers": {
        "db": "market",
        "select": "SELECT id,actor_name,capability,operation,source_url,reviewed_by,reviewed_at,reject_reason,review_status FROM offers",
        "summary": lambda r: r["capability"] + (f" ({r['operation']})" if r["operation"] else ""),
    },
    "tech_signals": {
        "db": "tech",
        "select": "SELECT id,actor_names,axis,maturity_stage,source_url,reviewed_by,reviewed_at,reject_reason,review_status FROM technology_signals",
        "summary": lambda r: f"{r['axis']} -- {r['maturity_stage']}",
        "actor": lambda r: _join_json_names(r["actor_names"]),
    },
    "events": {
        "db": "actors",
        "select": """SELECT e.id,a.name AS actor_name,e.event_type,e.description,e.source_url,
                            e.reviewed_by,e.reviewed_at,e.reject_reason,e.review_status
                     FROM actor_events e JOIN actors a ON a.id=e.actor_id""",
        "summary": lambda r: f"{r['event_type']} -- {(r['description'] or '')[:140]}",
    },
    "facts": {
        "db": "actors",
        "select": """SELECT f.id,a.name AS actor_name,f.dimension,f.value,f.source_url,
                            f.reviewed_by,f.reviewed_at,f.reject_reason,f.review_status
                     FROM actor_facts f JOIN actors a ON a.id=f.actor_id""",
        "summary": lambda r: f"{r['dimension']} : {r['value']}",
    },
    "actors": {
        "db": "actors",
        "select": "SELECT id,name AS actor_name,country,role,official_url AS source_url,reviewed_by,reviewed_at,reject_reason,review_status FROM actors",
        "summary": lambda r: f"{r['actor_name']} ({r['country']}) -- {r['role'] or 'rôle non renseigné'}",
    },
    "vocabulary": {
        "db": "market",
        "select": "SELECT id,actor_name,proposed_labels,source_url,reviewed_by,reviewed_at,reject_reason,review_status FROM vocabulary_candidates",
        "summary": lambda r: _summarize_proposed_labels(r["proposed_labels"]),
    },
    "candidates": {
        "db": "actors",
        "select": "SELECT id,name AS actor_name,country,suggested_official_url AS source_url,reviewed_by,reviewed_at,reject_reason,review_status FROM actor_candidates",
        "summary": lambda r: f"{r['actor_name']} ({r['country'] or 'pays inconnu'}) -- candidat acteur",
    },
    "marketing": {
        "db": "market",
        "select": """SELECT id,'HEF/IREIS' AS actor_name,kind,title,NULL AS source_url,
                            reviewed_by,reviewed_at,reject_reason,review_status
                     FROM marketing_recommendations""",
        "summary": lambda r: f"{r['kind']} -- {r['title']}",
    },
}


def _db_path(key: str):
    """Résout le chemin de base AU MOMENT DE L'APPEL, et TOUJOURS depuis le module ``db``.

    Deux pièges évités ici, tous deux constatés en écrivant ce module.

    1. _QUEUES est construit une fois au chargement : y stocker directement les chemins les
       figerait à leur valeur d'import.
    2. Surtout, ce module ne garde PAS sa propre copie de MARKET_DB/ACTORS_DB/TECH_DB. Toute la
       suite de tests redirige déjà ``db.MARKET_DB`` ; si le journal lisait des copies locales,
       il faudrait penser à patcher trois noms de plus dans chaque test touchant une décision --
       et un oubli ferait écrire les décisions de test dans les VRAIES bases, en silence dès que
       la table y existe. Une seule source de vérité pour les chemins supprime la classe entière
       de ce bug.
    """
    return {"market": _db.MARKET_DB, "actors": _db.ACTORS_DB, "tech": _db.TECH_DB}[key]


def _join_json_names(raw: str | None) -> str | None:
    try:
        names = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return None
    return ", ".join(names) or None


def _summarize_proposed_labels(raw: str | None) -> str:
    try:
        proposed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        proposed = {}
    return ", ".join(f"{dim}: {label}" for dim, label in proposed.items()) or "(aucune proposition)"


def snapshot_item(queue: str, item_id: int) -> dict[str, Any]:
    """Identité métier de l'item, lue AVANT que la décision ne la rende introuvable.

    Best-effort : une ligne déjà disparue donne un instantané vide plutôt qu'une erreur -- perdre
    le libellé est regrettable, perdre la décision serait pire.
    """
    spec = _QUEUES[queue]
    with connect(_db_path(spec["db"])) as db:
        row = db.execute(f"{spec['select']} WHERE {_id_column(queue)}=?", (item_id,)).fetchone()
    if not row:
        return {"actor_name": None, "summary": None, "source_url": None}
    actor = spec["actor"](row) if "actor" in spec else row["actor_name"]
    return {"actor_name": actor, "summary": spec["summary"](row), "source_url": row["source_url"]}


def _id_column(queue: str) -> str:
    # Les files jointes qualifient leur id (e.id, f.id) : la clause WHERE doit s'aligner.
    return {"events": "e.id", "facts": "f.id"}.get(queue, "id")


def _reviewed_at_column(queue: str) -> str:
    """Idem pour reviewed_at. `actor_events`/`actor_facts` sont jointes à `actors`, qui porte
    AUSSI une colonne reviewed_at : sans qualification, SQLite rejette la clause en "ambiguous
    column name" -- et l'amorçage échouerait précisément sur les deux files jointes."""
    return {"events": "e.reviewed_at", "facts": "f.reviewed_at"}.get(queue, "reviewed_at")


def record_decision(
    queue: str, item_id: int, decision: Decision,
    *, reject_reason: str | None = None, decided_by: str | None = None,
    decided_at: str | None = None, snapshot: dict[str, Any] | None = None,
) -> int:
    """Ajoute une ligne au journal. Jamais d'UPDATE : deux décisions successives sur le même item
    donnent deux lignes, c'est tout l'intérêt.

    ``snapshot`` permet à l'appelant de fournir une identité déjà lue (utile quand la décision
    vient de la supprimer) ; à défaut elle est relue ici.
    """
    if queue not in _QUEUES:
        raise ValueError(f"Unknown queue {queue!r} -- expected one of {JOURNAL_QUEUES}")
    if decision not in ("accept", "reject"):
        raise ValueError(f"decision must be 'accept' or 'reject', got {decision!r}")
    identity = snapshot if snapshot is not None else snapshot_item(queue, item_id)
    with connect(_db.MARKET_DB) as db:
        row_id = db.execute(
            """INSERT INTO review_decisions(queue,item_id,decision,reject_reason,decided_by,decided_at,
                                            actor_name,summary,source_url)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                queue, item_id, decision, reject_reason, decided_by, decided_at or utc_now(),
                identity.get("actor_name"), identity.get("summary"), identity.get("source_url"),
            ),
        ).lastrowid
    assert row_id is not None
    return row_id


def list_decisions(
    *, queue: str | None = None, decision: Decision | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if queue:
        clauses.append("queue=?")
        params.append(queue)
    if decision:
        clauses.append("decision=?")
        params.append(decision)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(_db.MARKET_DB) as db:
        return [
            dict(row) for row in db.execute(
                f"SELECT * FROM review_decisions{where} ORDER BY decided_at DESC, id DESC LIMIT ?",
                (*params, limit),
            )
        ]


def seed_from_columns() -> dict[str, int]:
    """Amorce le journal à partir des décisions déjà inscrites dans les colonnes de trace.

    Le journal est arrivé après les colonnes : sans cet amorçage, toute décision prise avant sa
    mise en service serait invisible. Idempotent -- une (queue,item_id) déjà journalisée est
    ignorée, donc relancer le script ne duplique rien. Ne peut évidemment pas reconstituer un
    historique : une seule ligne par item, celle de son état actuel, horodatée à son reviewed_at
    réel et non à maintenant.
    """
    with connect(_db.MARKET_DB) as db:
        already = {
            (row["queue"], int(row["item_id"]))
            for row in db.execute("SELECT queue,item_id FROM review_decisions")
        }

    seeded: dict[str, int] = {}
    for queue, spec in _QUEUES.items():
        with connect(_db_path(spec["db"])) as db:
            rows = db.execute(f"{spec['select']} WHERE {_reviewed_at_column(queue)} IS NOT NULL").fetchall()
        count = 0
        for row in rows:
            if (queue, int(row["id"])) in already:
                continue
            actor = spec["actor"](row) if "actor" in spec else row["actor_name"]
            record_decision(
                queue, int(row["id"]),
                "reject" if row["review_status"] == "rejected" else "accept",
                reject_reason=row["reject_reason"], decided_by=row["reviewed_by"],
                decided_at=row["reviewed_at"],
                snapshot={"actor_name": actor, "summary": spec["summary"](row), "source_url": row["source_url"]},
            )
            count += 1
        if count:
            seeded[queue] = count
    return seeded


if __name__ == "__main__":
    report = seed_from_columns()
    print("Amorçage du journal depuis les colonnes de trace :")
    for queue, count in sorted(report.items()):
        print(f"  {queue:14} {count}")
    if not report:
        print("  (rien à amorcer -- aucune décision hors journal)")
