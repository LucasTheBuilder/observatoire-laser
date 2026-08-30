"""Nettoyage rétroactif : retire les lignes CORDIS/OpenAlex collectées AVANT que le filtre
thématique n'existe (voir audit v8 §2.1 -- 73/83 projets CORDIS et 123/203 publications
OpenAlex hors sujet). Réutilise exactement cordis._project_is_on_topic() et
openalex._work_is_on_topic(), donc un projet/une publication est retiré ici si et seulement si
il/elle aurait été rejeté(e) par une collecte lancée aujourd'hui -- jamais une règle réinventée
pour l'occasion.

Script de maintenance ponctuel (même famille que reset_market_db.py), pas un collecteur : à
relancer manuellement si le lexique LASER_RULES/PROCESS_TECHNOLOGIES évolue et que d'anciennes
lignes doivent être réauditées.

Sauvegarde les 3 bases avant toute suppression (voir db.backup_all_databases()).
"""

from __future__ import annotations

import re
from pathlib import Path

from cordis import CORDIS_CACHE_PATH, _project_details, _project_is_on_topic
from db import ACTORS_DB, TECH_DB, backup_all_databases, connect
from openalex import _work_is_on_topic

_PROJECT_ID_RE = re.compile(r"^https://cordis\.europa\.eu/project/id/(\S+)$")


def _cordis_project_id(source_url: str | None) -> str | None:
    match = _PROJECT_ID_RE.match(source_url or "")
    return match.group(1) if match else None


def prune_cordis(*, cache_path: Path = CORDIS_CACHE_PATH) -> dict:
    """Retire les actor_events/actor_relations d'origine CORDIS dont le projet (identifié via
    source_url) ne passe plus (ou n'a jamais dû passer) _project_is_on_topic().

    Un project_id référencé en base mais absent du cache local n'est jamais supprimé sur
    absence de preuve -- seuls les projets qu'on peut positivement relire et classer hors
    sujet sont retirés.
    """
    with connect(ACTORS_DB) as db:
        events = db.execute("SELECT id,source_url FROM actor_events WHERE event_type='cordis_project'").fetchall()
        relations = db.execute(
            "SELECT id,source_url FROM actor_relations WHERE source_url LIKE 'https://cordis.europa.eu/project/id/%'"
        ).fetchall()

    project_ids = {pid for row in (*events, *relations) if (pid := _cordis_project_id(row["source_url"]))}
    if not project_ids:
        return {"projects_checked": 0, "projects_off_topic": 0, "events_removed": 0, "relations_removed": 0}
    if not cache_path.exists():
        raise SystemExit(
            f"Cache CORDIS introuvable ({cache_path}) : impossible de relire titre/objectif des "
            "projets déjà en base. Relancer cordis.collect_cordis() une fois pour le reconstituer."
        )

    projects = _project_details(cache_path, project_ids)
    off_topic_ids = {pid for pid, project in projects.items() if not _project_is_on_topic(project)}

    events_removed = relations_removed = 0
    with connect(ACTORS_DB) as db:
        for row in events:
            if _cordis_project_id(row["source_url"]) in off_topic_ids:
                db.execute("DELETE FROM actor_events WHERE id=?", (row["id"],))
                events_removed += 1
        for row in relations:
            if _cordis_project_id(row["source_url"]) in off_topic_ids:
                db.execute("DELETE FROM actor_relations WHERE id=?", (row["id"],))
                relations_removed += 1

    return {
        "projects_checked": len(projects),
        "projects_off_topic": len(off_topic_ids),
        "events_removed": events_removed,
        "relations_removed": relations_removed,
    }


def prune_openalex_documents() -> dict:
    """Retire les publications attribuées à un acteur (actor_name non NULL -- la marque des
    lignes issues d'openalex.py, voir openalex._upsert_document) dont le titre ne passe plus
    _work_is_on_topic(). Les publications Crossref jamais attribuées (actor_name NULL) sont
    hors du champ de ce nettoyage : scrapers.scrape_technology() les filtre déjà à la collecte.
    """
    with connect(TECH_DB) as db:
        rows = db.execute(
            "SELECT id,title FROM documents WHERE document_type='publication' AND actor_name IS NOT NULL"
        ).fetchall()

    off_topic_ids = [row["id"] for row in rows if not _work_is_on_topic(row["title"] or "")]
    with connect(TECH_DB) as db:
        for doc_id in off_topic_ids:
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    return {"documents_checked": len(rows), "documents_removed": len(off_topic_ids)}


if __name__ == "__main__":
    backups = backup_all_databases()
    print(f"Sauvegarde préalable : {len(backups)} fichier(s)")
    for path in backups:
        print(f"  - {path}")

    cordis_report = prune_cordis()
    print(
        f"CORDIS : {cordis_report['projects_checked']} projets réexaminés, "
        f"{cordis_report['projects_off_topic']} hors sujet -> "
        f"{cordis_report['events_removed']} actor_events et "
        f"{cordis_report['relations_removed']} actor_relations supprimés."
    )

    openalex_report = prune_openalex_documents()
    print(
        f"OpenAlex : {openalex_report['documents_checked']} publications réexaminées, "
        f"{openalex_report['documents_removed']} hors sujet supprimées."
    )
