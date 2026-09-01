"""Dossier de retour d'expérience (phase 1 du plan agent d'analyse) : ce que les décisions
humaines disent du scraping, assemblé en SQL déterministe, sans aucun appel de modèle.

C'est ce que l'agent d'analyse lira -- mais le dossier vaut par lui-même : il répond déjà à
« qu'est-ce que j'ai rejeté, et pourquoi », et il est testable sans dépenser un centime d'API.

Trois apports, et le troisième est le moins évident.

1. **Les rejets, avec leur provenance.** Motif typé + citation + termes du lexique qui ont
   déclenché chaque dimension (``match_terms``) + mode d'extraction + force de relation. Sans
   les termes, un rejet dit « ce fait est faux » sans jamais dire « à cause de quelle règle ».
2. **Des contre-exemples acceptés.** Un dossier fait uniquement d'échecs conduit à
   sur-généraliser : il faut voir ce qui marche sur les mêmes règles.
3. **Les faits de référence NON retrouvés.** Les rejets ne contiennent que des faux positifs ;
   un lecteur qui n'aurait que ça ne pourrait jamais proposer que de RESSERRER des règles.
   ``golden_facts`` est la seule source de faux négatifs de l'application. Ceux que le pipeline
   ne retrouve pas sont classés en trois causes distinctes -- voir ``classify_golden_misses`` --
   parce que « jamais collecté », « trouvé mais mal étiqueté » et « trouvé puis écarté » n'ont
   ni la même origine ni le même correctif, alors que ``compute_recall`` les confond tous en un
   seul chiffre.

Aucune de ces sorties n'est une opinion : tout est compté, rien n'est estimé.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from db import MARKET_DB, connect, utc_now

# Les seules files dont les items portent une provenance exploitable (citation, termes, mode
# d'extraction). Les six autres sont comptées dans les totaux mais n'ont rien à enrichir.
_ENRICHABLE = {
    "evidence": """SELECT id, quote, match_terms, match_terms_backfilled, extraction_mode,
                          relation_strength, field_confidence, fact_status, source_url
                   FROM evidence WHERE id IN ({placeholders})""",
    "offers": """SELECT id, quote, match_terms, NULL AS match_terms_backfilled, NULL AS extraction_mode,
                        NULL AS relation_strength, field_confidence, NULL AS fact_status, source_url
                 FROM offers WHERE id IN ({placeholders})""",
}

GOLDEN_MISS_KINDS = ("aucune_trace", "mal_etiquete", "trouve_puis_jete")


def _load_provenance(queue: str, item_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not item_ids or queue not in _ENRICHABLE:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    with connect(MARKET_DB) as db:
        rows = db.execute(_ENRICHABLE[queue].format(placeholders=placeholders), item_ids).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _parse_terms(raw: str | None) -> dict[str, list[str]]:
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decisions_with_provenance(decision: str, limit: int) -> list[dict[str, Any]]:
    with connect(MARKET_DB) as db:
        rows = [
            dict(row) for row in db.execute(
                """SELECT queue,item_id,decision,reject_reason,decided_by,decided_at,
                          actor_name,summary,source_url
                   FROM review_decisions WHERE decision=? ORDER BY decided_at DESC, id DESC LIMIT ?""",
                (decision, limit),
            )
        ]
    by_queue: dict[str, list[int]] = {}
    for row in rows:
        by_queue.setdefault(row["queue"], []).append(int(row["item_id"]))
    provenance = {queue: _load_provenance(queue, ids) for queue, ids in by_queue.items()}

    enriched = []
    for row in rows:
        extra = provenance.get(row["queue"], {}).get(int(row["item_id"]), {})
        enriched.append({
            **row,
            "quote": extra.get("quote"),
            "match_terms": _parse_terms(extra.get("match_terms")),
            "match_terms_backfilled": bool(extra.get("match_terms_backfilled")),
            "extraction_mode": extra.get("extraction_mode"),
            "relation_strength": extra.get("relation_strength"),
            "field_confidence": extra.get("field_confidence"),
            # La ligne d'origine a pu disparaître (fusion de doublons) : l'instantané du journal
            # reste, la provenance non. Le signaler plutôt que de laisser croire à un fait sans
            # citation.
            "row_still_exists": bool(extra),
        })
    return enriched


def classify_golden_misses() -> list[dict[str, Any]]:
    """Range chaque fait de référence non retrouvé dans l'une des trois causes possibles.

    ``compute_recall`` compare exactement sur (actor_name, market, component, operation) et
    renvoie un seul taux : un fait trouvé mais mal étiqueté y compte comme une absence totale.
    Or les correctifs n'ont rien à voir --

    - ``trouve_puis_jete`` : les bonnes dimensions existent, mais fact_status != 'validated'.
      Le problème est le filtrage en aval, pas le matching.
    - ``mal_etiquete`` : une preuve existe sur la MÊME URL pour ce même acteur, avec d'autres
      dimensions. Une règle s'est déclenchée avec le mauvais libellé -- directement actionnable
      sur le lexique.
    - ``aucune_trace`` : rien pour cet acteur sur cette URL. Le problème est en amont du
      lexique : page jamais crawlée, blocs filtrés en bruit, portail laser trop strict.

    L'ordre du test est celui-ci, du plus précis au plus large.
    """
    with connect(MARKET_DB) as db:
        golden = db.execute(
            "SELECT id,actor_name,market,component,operation,source_url,expected_quote FROM golden_facts"
        ).fetchall()
        misses = []
        for fact in golden:
            exact = db.execute(
                """SELECT fact_status FROM evidence
                   WHERE actor_name=? AND market=? AND component=? AND operation=? LIMIT 1""",
                (fact["actor_name"], fact["market"], fact["component"], fact["operation"]),
            ).fetchone()
            if exact and exact["fact_status"] == "validated":
                continue  # retrouvé : ce n'est pas un manque
            if exact:
                kind, detail = "trouve_puis_jete", f"fact_status={exact['fact_status']}"
            else:
                same_url = db.execute(
                    """SELECT market,component,operation FROM evidence
                       WHERE actor_name=? AND source_url=? LIMIT 1""",
                    (fact["actor_name"], fact["source_url"]),
                ).fetchone()
                if same_url:
                    kind = "mal_etiquete"
                    detail = f"extrait comme {same_url['market']} / {same_url['component']} / {same_url['operation']}"
                else:
                    kind, detail = "aucune_trace", "aucune preuve pour cet acteur sur cette URL"
            misses.append({
                "golden_fact_id": int(fact["id"]), "kind": kind, "detail": detail,
                "actor_name": fact["actor_name"],
                "expected": f"{fact['market']} / {fact['component']} / {fact['operation']}",
                "source_url": fact["source_url"], "expected_quote": fact["expected_quote"],
            })
    return misses


def _term_scoreboard(rejections: list[dict], accepted: list[dict]) -> list[dict[str, Any]]:
    """Pour chaque (dimension, terme) : combien de décisions l'ont retenu, combien l'ont rejeté.

    C'est le tableau qui rend un rejet imputable à une règle. Il ne conclut rien tout seul --
    aux volumes actuels les effectifs se comptent sur les doigts d'une main -- mais il donne au
    lecteur les exemples à examiner plutôt qu'un taux à croire.
    """
    counts: dict[tuple[str, str], Counter[str]] = {}
    for bucket, label in ((rejections, "rejected"), (accepted, "accepted")):
        for row in bucket:
            for dimension, terms in row["match_terms"].items():
                for term in terms:
                    counts.setdefault((dimension, term), Counter())[label] += 1
    scoreboard: list[dict[str, Any]] = [
        {
            "dimension": dimension, "term": term,
            "accepted": tally["accepted"], "rejected": tally["rejected"],
            "total": tally["accepted"] + tally["rejected"],
        }
        for (dimension, term), tally in counts.items()
    ]
    # Les termes les plus rejetés d'abord : c'est l'ordre dans lequel un lecteur veut les examiner.
    scoreboard.sort(key=lambda row: (-int(row["rejected"]), -int(row["total"]), str(row["term"])))
    return scoreboard


def _tally(rows: list[dict], key: str) -> list[dict[str, Any]]:
    counter = Counter(str(row.get(key) or "(non renseigné)") for row in rows)
    return [{key: value, "count": count} for value, count in counter.most_common()]


def build_feedback_dossier(*, limit: int = 400) -> dict[str, Any]:
    """Point d'entrée (voir app.py: GET /api/feedback-dossier). Purement calculé, ne modifie rien."""
    rejections = _decisions_with_provenance("reject", limit)
    accepted = _decisions_with_provenance("accept", limit)
    misses = classify_golden_misses()

    with connect(MARKET_DB) as db:
        by_queue = [
            {"queue": row["queue"], "decision": row["decision"], "count": int(row["n"])}
            for row in db.execute(
                "SELECT queue,decision,COUNT(*) AS n FROM review_decisions GROUP BY queue,decision"
            )
        ]
        golden_total = int(db.execute("SELECT COUNT(*) FROM golden_facts").fetchone()[0])

    return {
        "generated_at": utc_now(),
        "decisions": {
            "rejected": len(rejections),
            "accepted": len(accepted),
            "by_queue": by_queue,
        },
        "rejections": rejections,
        "accepted_examples": accepted,
        "misses": {
            "golden_facts_total": golden_total,
            "missed": len(misses),
            # Un dénominateur nul donne None, jamais 0 : "rien à mesurer" n'est pas "0 %"
            # (même règle que veille_metrics).
            "recall": round((golden_total - len(misses)) / golden_total, 4) if golden_total else None,
            "by_kind": {kind: sum(1 for m in misses if m["kind"] == kind) for kind in GOLDEN_MISS_KINDS},
            "items": misses,
        },
        "aggregates": {
            "by_reason": _tally(rejections, "reject_reason"),
            "by_extraction_mode": _tally(rejections, "extraction_mode"),
            "by_relation_strength": _tally(rejections, "relation_strength"),
            "by_term": _term_scoreboard(rejections, accepted),
        },
    }
