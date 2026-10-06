"""Reclasse les tags marché et pièce déjà posés sur les documents de la page Technologie laser,
avec la règle du 06/10/2026 : une phrase du titre ou du résumé doit les nommer (voir
scrapers.document_sentence_tags et lexicon.labels_named_in_sentences).

Pourquoi un script plutôt qu'attendre la collecte : une collecte AJOUTE des signaux, elle n'en
retire jamais -- les tags posés par l'ancienne lecture du texte entier (« f-theta lens » classé
Optique, « photonic process chain » classé Photonique...) resteraient affichés indéfiniment.

Pour chaque document : un tag que la nouvelle règle ne retrouve plus est supprimé ; un tag
qu'elle retrouve garde sa ligne mais sa citation devient la phrase qui le nomme ; un tag
qu'elle trouve en plus est créé par le chemin normal (upsert_document_technology_signal).
Les lignes relues par un humain (reviewed_at non NULL) et les signaux de PROJET ne sont jamais
touchés -- même garde que db._purge_unknown_document_axes.

À lancer DANS le conteneur, jamais depuis Windows (voir CLAUDE.md, « Données de production ») :

    docker compose exec -T observatoire python reclassify_document_tags.py --dry-run
    docker compose exec -T observatoire python reclassify_document_tags.py

Sauvegarde les 3 bases avant toute écriture.
"""

from __future__ import annotations

import argparse
import json

from db import TECH_DB, backup_all_databases, connect
from scrapers import SENTENCE_DIMENSIONS, document_sentence_tags, upsert_document_technology_signal


def reclassify_document_tags(*, dry_run: bool = False) -> dict:
    if not dry_run:
        backup_all_databases()
    removed: list[dict] = []
    requoted = 0
    added: list[dict] = []
    placeholders = ",".join("?" * len(SENTENCE_DIMENSIONS))
    with connect(TECH_DB) as db:
        documents = db.execute(
            "SELECT id,actor_name,title,abstract,source_url FROM documents"
        ).fetchall()
        for document in documents:
            url = document["source_url"]
            wanted = document_sentence_tags(document["title"] or "", document["abstract"], document["actor_name"])
            existing = db.execute(
                f"""SELECT id,axis,dimension FROM technology_signals
                     WHERE source_url=? AND project_name IS NULL AND reviewed_at IS NULL
                       AND dimension IN ({placeholders})""",
                (url, *SENTENCE_DIMENSIONS),
            ).fetchall()
            known = {(row["dimension"], row["axis"]) for row in existing}
            for row in existing:
                key = (row["dimension"], row["axis"])
                if key not in wanted:
                    old_quote = db.execute(
                        "SELECT quote FROM technology_signal_sources WHERE signal_id=? LIMIT 1", (row["id"],)
                    ).fetchone()
                    removed.append({
                        "dimension": row["dimension"], "label": row["axis"], "title": document["title"],
                        "old_quote": old_quote["quote"] if old_quote else None,
                    })
                    db.execute("DELETE FROM technology_signal_sources WHERE signal_id=?", (row["id"],))
                    db.execute("DELETE FROM technology_signals WHERE id=?", (row["id"],))
                    continue
                # Gardé : seule la phrase qui le nomme reste en preuve. La nouvelle ligne de
                # source est écrite juste après par upsert_document_technology_signal.
                stale_sources = db.execute(
                    "DELETE FROM technology_signal_sources WHERE signal_id=? AND quote!=?",
                    (row["id"], wanted[key]),
                ).rowcount
                requoted += bool(stale_sources)
            for key, sentence in wanted.items():
                if key not in known:
                    added.append({"dimension": key[0], "label": key[1], "title": document["title"], "sentence": sentence})
            if wanted:
                upsert_document_technology_signal(
                    db, document["title"] or "", document["abstract"] or "", url, document["actor_name"],
                )
        if dry_run:
            db.rollback()
    return {
        "dry_run": dry_run,
        "documents": len(documents),
        "removed": removed,
        "added": added,
        "requoted": requoted,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Calcule le rapport sans rien écrire.")
    args = parser.parse_args()
    print(json.dumps(reclassify_document_tags(dry_run=args.dry_run), ensure_ascii=False, indent=1))
