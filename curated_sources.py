"""Sources entrées à la main : ce que les collecteurs ne peuvent pas déduire seuls.

Le pipeline juge la pertinence sur le texte qu'une source expose. Quand ce texte ne dit pas
ce que le projet fait réellement, le filtre a raison sur ce qu'il lit et tort sur le fond.
BILASURF en est le cas type : son objectif CORDIS emploie deux fois le mot "laser" et
JAMAIS "femtosecond", "ultrafast" ni "ultrashort", donc ``is_on_topic()`` le rejette -- alors
que ses neuf publications portent sur l'ablation laser ultra-rapide en régime burst, et que
la page Technology du projet décrit noir sur blanc un "Ultrashort pulsed laser process
development". C'est un défaut de RAPPEL du filtre, pas une erreur de précision : rien à
corriger dans le lexique, tout à apporter comme connaissance externe.

Ce module est le pendant de db.SEED_EVIDENCE, et suit exactement la même règle : il n'est
JAMAIS appliqué au démarrage. ``init_databases()`` ne l'appelle pas -- sinon toute base
fraîche (nouveau déploiement, base de test) se retrouverait avec des lignes sur un projet
qu'elle n'a jamais collecté. On l'invoque à la main, une fois.

Ce que la curation garantit en base :

- les documents portent ``curated_by`` non NULL, et ``prune_documents()`` ne les touche plus ;
- les signaux portent ``reviewed_by``/``reviewed_at``, et ``prune_technology_signals()`` ne les
  touche plus non plus -- même convention que les trois sites d'upsert de scrapers.py, où
  ``reviewed_at IS NULL`` protège déjà toute décision humaine d'un écrasement par un
  collecteur.

Sans ces deux gardes, la prochaine passe de nettoyage supprimerait tout ce que ce module
écrit : le projet parce que son objectif CORDIS échoue au filtre, et trois des neuf documents
parce que leur titre ne contient aucun terme ultra-rapide.
"""

from __future__ import annotations

import hashlib
from typing import Any

from db import (
    TECH_DB,
    connect,
    technology_signal_key,
    upsert_document,
    upsert_fact_source,
    upsert_technology_signal,
    utc_now,
)
from scrapers import upsert_document_technology_signal

# Marqueur écrit dans documents.curated_by et technology_signals.reviewed_by. Porte le nom de
# l'initiative plutôt qu'un simple "manuel" : c'est aussi ce qui permet de retrouver toutes
# les lignes d'une même curation (`WHERE curated_by LIKE 'BILASURF%'`), la table `documents`
# n'ayant aucune colonne qui rattache une publication à un projet.
BILASURF_CURATION = "BILASURF (GA 101091623) — curation manuelle"

BILASURF_CORDIS_URL = "https://cordis.europa.eu/project/id/101091623"
BILASURF_SITE_URL = "https://www.bilasurf.com/technology/"

# --- Projets européens ---------------------------------------------------------------------
#
# Un axe par entrée, comme dans technology_signals. Seule "Fonctionnalisation de surface" est
# écrite ici : c'est le seul axe que l'objectif CORDIS justifie mot pour mot. Le projet parle
# aussi de "inline monitoring capabilities", mais l'axe existant s'appelle "Monitoring IA
# procédé" et rien dans la source ne mentionne d'IA -- l'écrire serait ajouter une affirmation
# à la source au lieu de la citer. Les axes propres aux publications (Burst GHz/MHz...) sont
# détectés normalement par le lexique, sur leur propre titre.
CURATED_PROJECTS: list[dict[str, Any]] = [
    {
        "project_name": "BILASURF",
        "axis": "Fonctionnalisation de surface",
        "maturity_stage": "Maturité industrielle non déterminée",
        "bucket": "radar",
        # Les deux seuls membres du consortium déjà suivis (Ceit est coordinateur). Les huit
        # autres -- Fraunhofer-Gesellschaft, Ziehl-Abegg, Fusion Bionic, Bionic Surface,
        # Altechna R&D, SECPhO, Global Hydro Energy, Supergrid Institute -- ne sont pas des
        # acteurs de la base et ne sont pas inventés ici.
        "actor_names": ["AIMEN Technology Centre", "CEIT"],
        "source_url": BILASURF_CORDIS_URL,
        "source_title": "BILASURF — Bio-inspired laser functionalization of complex 3D industrial surfaces (CORDIS Horizon Europe, 2023-2026)",
        "sources": [
            {
                "source_url": BILASURF_CORDIS_URL,
                "source_title": "BILASURF — objectif du projet (CORDIS, GA 101091623)",
                "quote": (
                    "BILASURF aims at developing and integrating a process for high-rate laser "
                    "functionalization of complex 3D surfaces using tailored designed bio inspired "
                    "riblets to reduce friction and improve the environmental footprint of industrial "
                    "parts, assuring a high throughput with the help of inline monitoring capabilities."
                ),
                "language": "en",
            },
            {
                # La citation qui manque à CORDIS, et sans laquelle ce projet resterait
                # indéfendable dans un observatoire du laser ultra-rapide.
                "source_url": BILASURF_SITE_URL,
                "source_title": "BILASURF — Technology / Team expertise (site du projet)",
                "quote": "Ultrashort pulsed laser process development to produce micro/nanostructures",
                "language": "en",
            },
        ],
    },
]

# --- Publications --------------------------------------------------------------------------
#
# Les neuf productions scientifiques listées sur bilasurf.com/technology/. Titres et dates des
# cinq articles à DOI repris de Crossref (interrogé par DOI, pas recopiés depuis la page du
# projet) : trois d'entre eux portent une année de parution différente de celle annoncée par le
# site, qui cite l'année du volume.
#
# `actor_name` n'est renseigné que pour la seule publication dont Crossref porte réellement une
# affiliation (Ceit-BRTA). Les quatre autres DOI n'en déposent aucune, et deviner l'employeur
# d'un auteur d'après son nom n'est pas une preuve. openalex.py les rattachera s'il les
# retrouve par institution -- c'est déjà ce qu'il fait pour les lignes Crossref.
#
# `fingerprint_source` est explicite pour les contributions sans DOI : elles partagent la même
# page source, et l'empreinte se dérive normalement du DOI ou de l'URL -- trois d'entre elles
# se dédoublonneraient donc les unes avec les autres.
CURATED_DOCUMENTS: list[dict[str, Any]] = [
    {
        "title": "Optimization of ultrafast laser ablation of stainless steel in burst mode based on experimentally validated simulations and analytical modelling",
        "doi": "10.1038/s41598-026-37443-9",
        "published_at": "2026-01-27",
    },
    {
        "title": "Experimental study and numerical modelling on multi-pulse laser ablation of aluminium with femtosecond laser",
        "doi": "10.1016/j.optlastec.2025.112481",
        "published_at": "2025-06",
    },
    {
        "title": "Single-Step Fabrication of Highly Tunable Blazed Gratings Using Triangular-Shaped Femtosecond Laser Pulses",
        "doi": "10.3390/mi15060711",
        "published_at": "2024-05-28",
        "actor_name": "CEIT",
    },
    {
        "title": "Experimental findings and 2 dimensional two-temperature model in the multi-pulse ultrafast laser ablation on stainless steel considering the incubation factor",
        "doi": "10.1016/j.optlastec.2024.111507",
        "published_at": "2025-01",
    },
    {
        "title": "Numerical simulation and experimental validation of ultrafast laser ablation on aluminum",
        "doi": "10.1016/j.optlastec.2023.110283",
        "published_at": "2024-03",
    },
    # Contributions en conférence : pas de DOI. Sourcées sur la page qui les recense, sauf
    # celle d'euspen dont les actes sont publiquement adressables.
    {
        "title": "Environmental impact assessment of the laser micro-cladding manufacturing process for the energy sector (12th International Conference on Life Cycle Management)",
        "source_url": BILASURF_SITE_URL,
        "fingerprint_source": "bilasurf:lcm2025:micro-cladding-lca",
        "published_at": "2025",
    },
    {
        "title": "Quality improvement of laser-microstructured riblet geometries in forming tools for enhanced efficiency of injection-moulded fan impellers (25th International Conference & Exhibition of euspen)",
        "source_url": "https://www.euspen.eu/knowledge-base/ICE25224.pdf",
        "fingerprint_source": "https://www.euspen.eu/knowledge-base/ICE25224.pdf",
        "published_at": "2025",
    },
    {
        "title": "Acoustic trilateration monitoring for laser surface processing (25th International Symposium on Laser Precision Microfabrication)",
        "source_url": BILASURF_SITE_URL,
        "fingerprint_source": "bilasurf:lpm2024:acoustic-trilateration",
        "published_at": "2024",
    },
    {
        "title": "Generation of bio-based riblets to reduce drag in industrial parts using Direct Laser Writing techniques | Investigation of femtosecond laser induced multi-pulse ablation of aluminium surfaces (25th International Symposium on Laser Precision Microfabrication)",
        "source_url": BILASURF_SITE_URL,
        "fingerprint_source": "bilasurf:lpm2024:riblets-and-multipulse-ablation",
        "published_at": "2024",
    },
]


def _restore_project(db, project: dict[str, Any], stamp: str) -> tuple[int, int]:
    fact_key = technology_signal_key(project["axis"], project["project_name"])
    created, signal_id = upsert_technology_signal(
        db,
        fact_key=fact_key,
        axis=project["axis"],
        maturity_stage=project["maturity_stage"],
        bucket=project["bucket"],
        actor_names=project["actor_names"],
        source_url=project["source_url"],
        quote=project["sources"][0]["quote"],
        # 1.0 : la ligne ne repose pas sur une heuristique mais sur une lecture humaine des
        # deux sources citées ci-dessous.
        field_confidence=1.0,
        project_name=project["project_name"],
        source_title=project["source_title"],
    )
    # C'est cette marque qui met la ligne hors de portée de prune_technology_signals().
    db.execute(
        "UPDATE technology_signals SET reviewed_by=?,reviewed_at=COALESCE(reviewed_at,?) WHERE id=?",
        (BILASURF_CURATION, stamp, signal_id),
    )
    sources_added = 0
    for source in project["sources"]:
        sources_added += upsert_fact_source(
            db, "technology_signal_sources", signal_id,
            source_url=source["source_url"], source_title=source["source_title"],
            quote=source["quote"], language=source.get("language"), field_confidence=1.0,
            fingerprint=hashlib.sha256(
                f"{fact_key}|{source['source_url']}|{source['quote']}".encode()
            ).hexdigest(),
        )
    return created, sources_added


def restore_curated_sources() -> dict[str, int]:
    """Écrit les sources curées en base. Idempotent, à lancer à la main.

    Renvoie le détail de ce qui a été créé -- des zéros partout sur un second passage.
    """
    stamp = utc_now()
    report = {"projects_added": 0, "project_sources_added": 0, "documents_added": 0, "axes_added": 0}
    with connect(TECH_DB) as db:
        for project in CURATED_PROJECTS:
            created, sources_added = _restore_project(db, project, stamp)
            report["projects_added"] += created
            report["project_sources_added"] += sources_added

        for document in CURATED_DOCUMENTS:
            doi = document.get("doi")
            source_url = document.get("source_url") or (f"https://doi.org/{doi}" if doi else "")
            if not source_url:
                raise ValueError(f"Document curé sans source adressable : {document['title'][:60]!r}")
            inserted, _ = upsert_document(
                db,
                document_type="publication",
                title=document["title"],
                source_url=source_url,
                fingerprint_source=document.get("fingerprint_source") or doi or source_url,
                actor_name=document.get("actor_name"),
                published_at=document.get("published_at"),
                doi=doi,
            )
            report["documents_added"] += inserted
            db.execute(
                "UPDATE documents SET curated_by=? WHERE source_url=? AND title=?",
                (BILASURF_CURATION, source_url, document["title"]),
            )
            # Même classification que pour un document collecté : l'axe vient du lexique sur le
            # titre, il n'est pas décidé ici. Les contributions dont le titre ne porte aucun
            # terme ultra-rapide en ressortent simplement sans axe.
            report["axes_added"] += upsert_document_technology_signal(
                db, document["title"], "", source_url, document.get("actor_name"),
            )
    return report


if __name__ == "__main__":
    for key, value in restore_curated_sources().items():
        print(f"{key:24} {value}")
