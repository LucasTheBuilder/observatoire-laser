"""Sort de la validation humaine ce que les règles automatiques ACTUELLES laissent passer.

Audit du 07/10/2026, demandé par Lucas : « avec nos récents ajouts il est possible que
certaines infos passent les filtres maintenant et n'aient plus besoin d'une validation
humaine ». Une ligne mise en file sous une règle plus stricte n'est jamais rejugée -- les
collecteurs ne recalculent le statut que d'une ligne qu'ils revoient.

Quatre règles, et pas une de plus. Chacune rejoue un mécanisme qui existe déjà ; aucune ne
décide à la place d'un humain ce que le code envoie à un humain par principe (presse,
mentions de financement, candidats acteurs nouveaux, vrais trous de vocabulaire) :

  - offres : `scrapers._offer_review_reasons`, la règle de collecte d'aujourd'hui, ne trouve
    plus aucun motif de revue ;
  - faits marché proposés par l'IA (`fact_status='review'`) : le moteur DÉTERMINISTE retrouve
    dans une seule phrase de la citation exactement le même triplet marché/pièce/opération,
    non ambigu, non nié, avec un verbe qui l'affirme -- et le marché passe aussi le lexique
    resserré (DOCUMENT_MARKETS), pour ne pas valider « optical glass » comme marché Optique.
    Les faits `partial` restent en file : les compléter changerait leur clé, c'est un autre
    fait ;
  - candidats acteurs déjà au roster (même nom, ou même domaine que le site officiel) :
    rejetés comme doublons, la décision sur l'acteur a déjà été prise ;
  - candidats vocabulaire dont chaque libellé proposé existe déjà dans le lexique : le modèle
    a répondu par une liste (« ['Gravure', 'Microdécoupe'] ») ; ce n'est pas un trou de
    vocabulaire (voir scrapers.is_known_label_list).

Toutes les décisions passent par les chemins normaux (review_queue.decide_review_item,
actor_discovery.reject_candidate) : journalisées, signées AUTO_REVIEWER, réversibles comme une
décision humaine.

À lancer DANS le conteneur, jamais depuis Windows (voir CLAUDE.md) :

    docker compose exec -T observatoire python release_pending_review.py --dry-run
    docker compose exec -T observatoire python release_pending_review.py
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.parse import urlparse

from actor_discovery import reject_candidate
from db import ACTORS_DB, MARKET_DB, backup_all_databases, connect
from lexicon import DOCUMENT_MARKETS, _match_all_labels
from review_queue import decide_review_item
from scrapers import (
    _has_predicate,
    _is_negated,
    _offer_review_reasons,
    _relation_window_is_ambiguous,
    _resolved_core_labels,
    _sentences,
    is_known_label_list,
)

AUTO_REVIEWER = "audit automatique (règles du 07/10/2026)"


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _offers_to_accept() -> list[dict[str, Any]]:
    with connect(ACTORS_DB) as db:
        official = {row["name"]: row["official_url"] for row in db.execute("SELECT name,official_url FROM actors")}
    out = []
    with connect(MARKET_DB) as db:
        for offer in db.execute(
            "SELECT * FROM offers WHERE review_status='review' AND reviewed_at IS NULL"
        ).fetchall():
            sources = db.execute(
                "SELECT COUNT(DISTINCT source_url) FROM offer_sources WHERE offer_id=?", (offer["id"],)
            ).fetchone()[0]
            candidate = {
                "quote": offer["quote"] or "", "page_type": offer["page_type"], "url": offer["source_url"] or "",
                "operation": offer["operation"], "process": offer["laser_process"],
                "official_url": official.get(offer["actor_name"]),
            }
            if not _offer_review_reasons(candidate, max(sources, 1)):
                out.append({"id": offer["id"], "actor": offer["actor_name"], "label": offer["capability"]})
    return out


def _confirming_sentence(quote: str, stored: tuple[str, str, str]) -> str | None:
    for sentence in _sentences(quote):
        if _resolved_core_labels(sentence) != stored:
            continue
        if _relation_window_is_ambiguous(sentence) or _is_negated(sentence) or not _has_predicate(sentence):
            continue
        if stored[0] not in {label for label, _ in _match_all_labels(sentence, DOCUMENT_MARKETS)}:
            continue
        return sentence
    return None


def _facts_to_accept() -> list[dict[str, Any]]:
    out = []
    with connect(MARKET_DB) as db:
        for fact in db.execute(
            """SELECT * FROM evidence WHERE evidence_kind='market_application' AND review_status='review'
                 AND fact_status='review' AND reviewed_at IS NULL
                 AND market IS NOT NULL AND component IS NOT NULL AND operation IS NOT NULL"""
        ).fetchall():
            sentence = _confirming_sentence(fact["quote"] or "", (fact["market"], fact["component"], fact["operation"]))
            if sentence:
                out.append({
                    "id": fact["id"], "actor": fact["actor_name"],
                    "label": f"{fact['market']} / {fact['component']} / {fact['operation']}", "sentence": sentence,
                })
    return out


def _duplicate_candidates() -> list[dict[str, Any]]:
    out = []
    with connect(ACTORS_DB) as db:
        # Les acteurs vérifiés en dernier : à domaine égal, c'est eux que le rapport nomme
        # (le MTC partage the-mtc.org avec une ligne « Coventry » rejetée).
        actors = db.execute(
            "SELECT name,official_url FROM actors ORDER BY review_status='verified', id"
        ).fetchall()
        by_name = {row["name"].casefold(): row["name"] for row in actors}
        by_host = {_host(row["official_url"]): row["name"] for row in actors if row["official_url"]}
        for candidate in db.execute(
            "SELECT id,name,suggested_official_url FROM actor_candidates WHERE review_status='pending'"
        ).fetchall():
            match = by_name.get(candidate["name"].casefold())
            if not match and candidate["suggested_official_url"]:
                match = by_host.get(_host(candidate["suggested_official_url"]))
            if match:
                out.append({"id": candidate["id"], "label": candidate["name"], "actor": match})
    return out


def _known_vocabulary() -> list[dict[str, Any]]:
    out = []
    with connect(MARKET_DB) as db:
        for row in db.execute(
            "SELECT id,proposed_labels FROM vocabulary_candidates WHERE review_status='pending'"
        ).fetchall():
            proposed = json.loads(row["proposed_labels"] or "{}")
            if proposed and all(is_known_label_list(dimension, str(value)) for dimension, value in proposed.items()):
                out.append({"id": row["id"], "label": proposed})
    return out


def release_pending_review(*, dry_run: bool = False) -> dict[str, list[dict[str, Any]]]:
    plan = {
        "offers": _offers_to_accept(),
        "evidence": _facts_to_accept(),
        "candidates": _duplicate_candidates(),
        "vocabulary": _known_vocabulary(),
    }
    if dry_run:
        return plan
    backup_all_databases()
    for item in plan["offers"]:
        decide_review_item("offers", item["id"], "accept", reviewed_by=AUTO_REVIEWER)
    for item in plan["evidence"]:
        decide_review_item("evidence", item["id"], "accept", reviewed_by=AUTO_REVIEWER)
    for item in plan["vocabulary"]:
        decide_review_item("vocabulary", item["id"], "reject", reviewed_by=AUTO_REVIEWER, reject_reason="duplicate")
    for item in plan["candidates"]:
        reject_candidate(item["id"], reviewed_by=AUTO_REVIEWER, reject_reason="duplicate")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Liste les décisions sans rien écrire.")
    args = parser.parse_args()
    result = release_pending_review(dry_run=args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, **{k: len(v) for k, v in result.items()}, "detail": result},
                     ensure_ascii=False, indent=1))
