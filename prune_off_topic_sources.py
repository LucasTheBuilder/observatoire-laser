"""Nettoyage rétroactif : retire les lignes CORDIS/OpenAlex collectées AVANT que le filtre
thématique n'existe (voir audit v8 §2.1 -- 73/83 projets CORDIS et 123/203 publications
OpenAlex hors sujet). Réutilise exactement cordis._project_is_on_topic() et
openalex._work_is_on_topic(), donc un projet/une publication est retiré ici si et seulement si
il/elle aurait été rejeté(e) par une collecte lancée aujourd'hui -- jamais une règle réinventée
pour l'occasion.

Trois familles de lignes, dans cet ordre : actors.db (actor_events/actor_relations),
`documents`, puis `technology_signals`. Cette dernière manquait jusqu'à l'audit du 08/09/2026,
ce qui laissait des projets sans aucun rapport avec le laser s'afficher sur la page
Technologie laser alors que le filtre qui les aurait rejetés existait déjà.

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


def prune_technology_signals(*, cache_path: Path = CORDIS_CACHE_PATH) -> dict:
    """Retire les technology_signals qu'une collecte lancée aujourd'hui n'aurait pas créés.

    Cette table manquait au nettoyage (audit du 08/09/2026) : prune_cordis() ne touche que
    actors.db, prune_documents() que `documents`. Les signaux techniques issus de
    projets hors sujet survivaient donc aux deux passes, et restaient affichés comme "projets
    européens" sur la page Technologie laser -- quatre en production (RE4DY, iDriving, EEETHOS,
    INTELLASE), dont trois sans une seule occurrence du mot "laser" dans leur objectif CORDIS.

    Deux familles de signaux, deux critères, aucun réinventé pour l'occasion :

    - signal de projet CORDIS -> _project_is_on_topic(), le même que prune_cordis() ;
    - signal rattaché à un document (project_name NULL) -> le document doit toujours exister
      dans `documents`. Un signal dont le document vient d'être retiré comme hors sujet est
      orphelin par construction, sans avoir besoin de rejuger son texte.

    Un projet absent du cache local n'est JAMAIS supprimé sur absence de preuve (même règle que
    prune_cordis) : c'est ce qui protège les signaux dont la source n'est pas CORDIS du tout,
    comme Femtocell, documenté sur le site d'ALPHANOV.

    Les lignes technology_signal_sources partent avec leur signal (ON DELETE CASCADE, et
    db.connect() active PRAGMA foreign_keys).
    """
    with connect(TECH_DB) as db:
        signals = db.execute("SELECT id,source_url,project_name FROM technology_signals").fetchall()
        known_documents = {row["source_url"] for row in db.execute("SELECT source_url FROM documents")}

    project_signals = {row["id"]: pid for row in signals if (pid := _cordis_project_id(row["source_url"]))}
    orphan_ids = [
        row["id"] for row in signals
        if not (row["project_name"] or "").strip() and row["source_url"] not in known_documents
    ]

    off_topic_ids: list[int] = []
    projects: dict[str, dict[str, str]] = {}
    if project_signals:
        if not cache_path.exists():
            raise SystemExit(
                f"Cache CORDIS introuvable ({cache_path}) : impossible de relire titre/objectif des "
                "projets déjà en base. Relancer cordis.collect_cordis() une fois pour le reconstituer."
            )
        projects = _project_details(cache_path, set(project_signals.values()))
        off_topic_ids = [
            signal_id for signal_id, project_id in project_signals.items()
            if (project := projects.get(project_id)) is not None and not _project_is_on_topic(project)
        ]

    removed = sorted({*off_topic_ids, *orphan_ids})
    with connect(TECH_DB) as db:
        for signal_id in removed:
            db.execute("DELETE FROM technology_signals WHERE id=?", (signal_id,))

    return {
        "signals_checked": len(signals),
        "projects_reread": len(projects),
        "signals_off_topic": len(off_topic_ids),
        "signals_orphaned": len(orphan_ids),
        "signals_removed": len(removed),
    }


def prune_documents() -> dict:
    """Retire les publications dont le titre ne passe plus _work_is_on_topic().

    Couvre TOUTES les publications depuis l'audit du 09/09/2026, alors que la version d'origine
    se limitait à celles attribuées à un acteur (actor_name non NULL, la marque d'openalex.py).
    L'argument qui excluait les lignes Crossref -- "scrape_technology() les filtre déjà à la
    collecte" -- ne vaut que pour les collectes À VENIR : une ligne écrite avant un durcissement
    du filtre reste en base pour toujours. C'est exactement ce qui s'est produit avec un article
    de spectroscopie d'absorption transitoire femtoseconde, collecté par Crossref et affiché
    comme document de l'observatoire, et c'est le même angle mort que celui qui avait laissé
    quatre projets hors sujet dans technology_signals.
    """
    with connect(TECH_DB) as db:
        rows = db.execute(
            "SELECT id,title FROM documents WHERE document_type='publication'"
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

    documents_report = prune_documents()
    print(
        f"Publications : {documents_report['documents_checked']} réexaminées, "
        f"{documents_report['documents_removed']} hors sujet supprimées."
    )

    # Après prune_documents(), jamais avant : la détection des signaux orphelins lit
    # `documents`, et doit donc la voir déjà nettoyée.
    signals_report = prune_technology_signals()
    print(
        f"Signaux techno : {signals_report['signals_checked']} réexaminés "
        f"({signals_report['projects_reread']} projets relus dans le cache), "
        f"{signals_report['signals_off_topic']} hors sujet et "
        f"{signals_report['signals_orphaned']} orphelins -> "
        f"{signals_report['signals_removed']} supprimés."
    )
