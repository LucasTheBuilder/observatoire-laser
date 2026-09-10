"""Import des projets européens recensés par l'audit du 10/09/2026.

L'audit a fait un travail que le pipeline ne sait pas faire : passer les 84 625 projets FP7 +
H2020 + Horizon Europe au crible d'une double porte (vocabulaire laser, puis terme de procédé
avec contexte industriel), puis RELIRE À LA MAIN les 92 survivants pour les classer. Son
résultat est une connaissance externe, comme BILASURF -- voir curated_sources.py pour le même
raisonnement.

Ce module en importe la partie reproductible et rien d'autre :

- de l'audit vient UNIQUEMENT la classification humaine, ``AUDIT_EU_PROJECTS`` : pour chaque
  numéro de convention, l'acronyme, le tier (T1 cœur procédé / T2 briques amont) et la
  catégorie thématique ;
- des dumps CORDIS viennent TOUTES les métadonnées : titre, objectif, dates, coordinateur,
  pays, nombre de participants, contribution européenne.

Cette séparation est le point important. Un chiffre affiché par l'app doit être vérifiable à sa
source, pas recopié d'un document intermédiaire -- fût-il excellent. Elle a d'ailleurs déjà
servi : l'audit annonce « 67 projets » au §2 alors que ses propres sections en totalisent 66,
et « 52 hors Horizon sur 67 » là où le compte donne 51 sur 66.

Le tier T3 (« adjacent » : dépôt laser pulsé, synthèse de nanoparticules, irradiation) est
délibérément exclu : l'audit le classe lui-même hors micro-usinage USP, et la veille suit les
développements du laser ultra-rapide et de ses procédés.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Any, Iterator

from cordis import available_caches
from db import TECH_DB, connect, technology_signal_key, upsert_eu_project, upsert_fact_source, upsert_technology_signal, utc_now
from lexicon import DOCUMENT_LEXICONS, best_quote, detect_maturity, is_on_topic, match_all_labels

AUDIT_CURATION = "Audit projets UE laser ultra-rapide (10/09/2026)"

# Numéro de convention -> (acronyme, tier, catégorie). Extrait du markdown de l'audit ; c'est la
# SEULE chose qui en vienne.
from eu_project_audit_map import AUDIT_EU_PROJECTS  # noqa: E402  (table de données, isolée)


def _csv_rows(zip_path: Path, member: str) -> Iterator[dict[str, str]]:
    with zipfile.ZipFile(zip_path) as archive, archive.open(member) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter=";")


def _programme_label(zip_path: Path) -> str:
    return {"horizon_projects.zip": "HE", "h2020_projects.zip": "H2020", "fp7_projects.zip": "FP7"}.get(zip_path.name, "?")


def _money(value: str | None) -> float | None:
    """`ecMaxContribution` est écrit à la française dans les dumps CORDIS ("5601668,75")."""
    text = (value or "").strip().replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def collect_project_metadata(caches: list[Path] | None = None) -> dict[str, dict[str, Any]]:
    """Relit les projets de l'audit dans les dumps, puis leurs consortiums.

    Deux passes par cache, en streaming : project.csv d'abord (une fiche par projet), puis
    organization.csv pour le coordinateur et le décompte des participants -- ce dernier fait
    60 Mo pour Horizon Europe seul, il n'est jamais chargé en mémoire.
    """
    wanted = set(AUDIT_EU_PROJECTS)
    found: dict[str, dict[str, Any]] = {}
    for zip_path in caches if caches is not None else available_caches():
        remaining = wanted - set(found)
        if not remaining:
            break
        for row in _csv_rows(zip_path, "project.csv"):
            project_id = (row.get("id") or "").strip()
            if project_id not in remaining:
                continue
            found[project_id] = {
                "title": (row.get("title") or "").strip(),
                "objective": (row.get("objective") or "").strip(),
                "started_at": (row.get("startDate") or "")[:10] or None,
                "ended_at": (row.get("endDate") or "")[:10] or None,
                "ec_contribution_eur": _money(row.get("ecMaxContribution")),
                "programme": _programme_label(zip_path),
                "_cache": zip_path,
            }
        # Le consortium se lit dans le MÊME dump que le projet : une organisation n'est
        # rattachée qu'au programme où le projet vit.
        in_this_cache = {pid for pid, meta in found.items() if meta.get("_cache") == zip_path}
        if not in_this_cache:
            continue
        for row in _csv_rows(zip_path, "organization.csv"):
            project_id = (row.get("projectID") or "").strip()
            if project_id not in in_this_cache:
                continue
            meta = found[project_id]
            meta["participants"] = meta.get("participants", 0) + 1
            if (row.get("role") or "").strip() == "coordinator":
                meta["coordinator"] = (row.get("name") or "").strip()
                meta["coordinator_country"] = (row.get("country") or "").strip()
    for meta in found.values():
        meta.pop("_cache", None)
    return found


def import_audit_projects(caches: list[Path] | None = None) -> dict[str, int]:
    """Écrit les projets et leurs axes. Idempotent.

    Chaque projet reçoit AUSSI ses signaux techniques, dérivés de son objectif par le même
    lexique que le reste de l'app -- jamais déclarés à la main. Un projet dont l'objectif ne
    porte aucun terme ultra-rapide (10 des 79, l'audit les retient sur lecture humaine) est
    marqué ``reviewed_by`` : c'est ce qui le met hors d'atteinte de prune_technology_signals,
    comme BILASURF.
    """
    metadata = collect_project_metadata(caches)
    stamp = utc_now()
    report = {"projects_added": 0, "projects_missing": 0, "signals_added": 0, "sources_added": 0, "curated": 0}

    with connect(TECH_DB) as db:
        for project_id, (acronym, tier, category) in AUDIT_EU_PROJECTS.items():
            meta = metadata.get(project_id)
            if not meta or not meta.get("title"):
                report["projects_missing"] += 1
                continue
            source_url = f"https://cordis.europa.eu/project/id/{project_id}"
            text = f"{meta['title']} {meta['objective']}"
            # Un projet que NOTRE filtre rejetterait est conservé, mais marqué : l'audit l'a
            # relu à la main, et l'écarter reviendrait à préférer notre lexique à une lecture
            # humaine documentée.
            curated = not is_on_topic(text)
            report["curated"] += int(curated)

            report["projects_added"] += upsert_eu_project(
                db, project_id=project_id, acronym=acronym, title=meta["title"],
                programme=meta.get("programme"), started_at=meta.get("started_at"),
                ended_at=meta.get("ended_at"), coordinator=meta.get("coordinator"),
                coordinator_country=meta.get("coordinator_country"),
                participants=meta.get("participants"),
                ec_contribution_eur=meta.get("ec_contribution_eur"),
                tier=tier, category=category, source_url=source_url,
                curated_by=AUDIT_CURATION if curated else None,
            )

            bucket, stage = detect_maturity(meta["objective"])
            if bucket == "unknown":
                bucket = "radar"
            for dimension, lexicon in DOCUMENT_LEXICONS.items():
                for label, hits in match_all_labels(text, lexicon):
                    fact_key = technology_signal_key(label, acronym)
                    quote = best_quote(meta["objective"] or meta["title"], hits)[:700]
                    if not quote:
                        continue
                    created, signal_id = upsert_technology_signal(
                        db, fact_key=fact_key, axis=label, maturity_stage=stage, bucket=bucket,
                        actor_names=[], source_url=source_url, quote=quote,
                        field_confidence=0.9, project_name=acronym,
                        source_title=f"{acronym} — {meta['title']}", dimension=dimension,
                    )
                    report["signals_added"] += created
                    report["sources_added"] += upsert_fact_source(
                        db, "technology_signal_sources", signal_id,
                        source_url=source_url, source_title=f"{acronym} — {meta['title']}",
                        quote=quote, language="en", field_confidence=0.9,
                        fingerprint=f"{fact_key}|{source_url}|{quote}"[:400],
                    )
                    if curated:
                        db.execute(
                            "UPDATE technology_signals SET reviewed_by=?,reviewed_at=COALESCE(reviewed_at,?) WHERE id=?",
                            (AUDIT_CURATION, stamp, signal_id),
                        )
    return report


if __name__ == "__main__":
    for key, value in import_audit_projects().items():
        print(f"{key:20} {value}")
