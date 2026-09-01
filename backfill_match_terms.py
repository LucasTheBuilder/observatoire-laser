"""Reconstitue `evidence.match_terms` sur les lignes collectées AVANT que la colonne n'existe.

Script de maintenance ponctuel, même famille que prune_off_topic_sources.py : à lancer une fois,
à la main, jamais appelé automatiquement. Sauvegarde les 3 bases avant toute écriture.

POURQUOI. `match_terms` (les termes du lexique qui ont déclenché chaque dimension) est le seul
lien entre une décision humaine et la règle à corriger : sans lui, un rejet dit "ce fait est
faux" mais jamais "à cause de quelle règle". Les lignes déjà en base l'ont à NULL, or ce sont
précisément celles que l'utilisateur s'apprête à valider -- sans ce backfill, la toute première
session de revue produirait des motifs rattachés à rien.

CE QUI EST REJOUABLE, ET CE QUI NE L'EST PAS. `_candidate()` résout les 3 dimensions de cœur sur
``relation_text`` -- stocké dans ``evidence.relation_evidence`` -- donc elles se re-dérivent à
l'identique. Les dimensions complémentaires (process/architecture/material/performance) sont,
elles, résolues sur ``section``, une fenêtre plus large qui n'est stockée nulle part : elles ne
sont PAS reconstituées ici, et rien n'est inventé pour les remplacer. Les lignes traitées portent
donc ``match_terms_backfilled=1``, pour qu'aucune analyse ultérieure ne prenne cette absence pour
un constat ("cette règle matériau ne se déclenche jamais sur les faits anciens").

LEXIQUE UTILISÉ. Celui d'aujourd'hui, entrées personnalisées comprises (comme le fait une
collecte). La question à laquelle répond ce backfill est donc "quels termes des règles ACTUELLES
justifient ce fait", pas "quelles règles avaient tiré à l'époque" -- indécidable, l'état du
lexique au moment de chaque extraction n'étant pas historisé.

Ne touche jamais une ligne dont ``match_terms`` est déjà renseigné, et ne modifie aucune autre
colonne : ni statut de revue, ni décision, ni date.
"""

from __future__ import annotations

import argparse
import json

from db import MARKET_DB, backup_all_databases, connect
from lexicon import COMPONENTS, MARKETS, OPERATIONS, _match_label_details
from scrapers import _load_custom_lexicon_entries

CORE_LEXICONS = (("market", MARKETS), ("component", COMPONENTS), ("operation", OPERATIONS))


def _core_match_terms(relation_evidence: str) -> dict[str, list[str]]:
    """Mêmes appels que _candidate() sur la même fenêtre, dans le même ordre."""
    found = {
        dimension: _match_label_details(relation_evidence, lexicon)[1]
        for dimension, lexicon in CORE_LEXICONS
    }
    return {dimension: terms for dimension, terms in found.items() if terms}


def backfill_evidence_match_terms(*, apply: bool = False) -> dict[str, int]:
    """Renvoie le détail de ce qui a été (ou serait) écrit. ``apply=False`` ne touche à rien."""
    _load_custom_lexicon_entries()
    with connect(MARKET_DB) as db:
        rows = db.execute(
            """SELECT id, relation_evidence FROM evidence
               WHERE match_terms IS NULL AND relation_evidence IS NOT NULL AND relation_evidence != ''"""
        ).fetchall()
        total_null = int(db.execute("SELECT COUNT(*) FROM evidence WHERE match_terms IS NULL").fetchone()[0])

        resolved: list[tuple[str, int]] = []
        unresolved = 0
        for row in rows:
            terms = _core_match_terms(row["relation_evidence"])
            if terms:
                resolved.append((json.dumps(terms, ensure_ascii=False, sort_keys=True), int(row["id"])))
            else:
                # La fenêtre existe mais aucune règle actuelle n'y matche : on laisse NULL plutôt
                # que d'écrire un objet vide, qui se lirait comme "aucun terme n'a déclenché ce
                # fait" alors que la vraie information est "non reconstituable aujourd'hui".
                unresolved += 1

        if apply and resolved:
            db.executemany(
                "UPDATE evidence SET match_terms=?, match_terms_backfilled=1 WHERE id=? AND match_terms IS NULL",
                resolved,
            )

    return {
        "evidence_without_match_terms": total_null,
        "with_relation_evidence": len(rows),
        "without_relation_evidence": total_null - len(rows),
        "resolved": len(resolved),
        "unresolved_no_current_rule_matches": unresolved,
        "written": len(resolved) if apply else 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="écrire réellement (sinon simulation)")
    args = parser.parse_args()

    if args.apply:
        backups = backup_all_databases()
        print(f"Sauvegarde préalable : {len(backups)} fichier(s)")
        for path in backups:
            print(f"  - {path}")
        print()

    report = backfill_evidence_match_terms(apply=args.apply)
    print("SIMULATION (aucune écriture) -- relancer avec --apply" if not args.apply else "ÉCRITURE APPLIQUÉE")
    print(f"  evidence sans match_terms          : {report['evidence_without_match_terms']}")
    print(f"    dont avec relation_evidence      : {report['with_relation_evidence']}")
    print(f"    dont sans (non reconstituables)  : {report['without_relation_evidence']}")
    print(f"  reconstituées (cœur uniquement)    : {report['resolved']}")
    print(f"  aucune règle actuelle ne matche    : {report['unresolved_no_current_rule_matches']}")
    print(f"  lignes écrites                     : {report['written']}")
