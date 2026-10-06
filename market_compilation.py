"""Compilation marché & produit : ce que les pages Offres & capacités et Technologie laser disent
d'un marché ou d'un produit nommé, rattaché à ce marché ou à ce produit.

Demandé par Lucas le 28/09/2026 : la page Marché ne lisait que la table `evidence` (31 faits où
marché, pièce et opération sont reliés dans une même phrase). Les 363 capacités et les ~300
publications/projets n'y arrivaient jamais, faute de colonne marché dans `offers` et de lien
quelconque vers `technology.db`.

Règle unique, celle de Lucas : « uniquement les informations que tu peux classer dans un nom de
marché ou un produit ». Rien n'est donc INFÉRÉ ici -- pas de marché déduit d'une pièce, pas de
pièce déduite d'un matériau. Une offre ou une publication rejoint un marché quand une phrase de
sa source nomme ce marché, et cette phrase est renvoyée avec le rattachement : c'est la preuve
que le panneau affiche.

Pourquoi ne pas simplement lancer MARKETS/COMPONENTS sur le texte entier -- mesuré sur les 363
offres le 28/09/2026, puis relu à la main ligne par ligne :

  - une citation d'offre est souvent une PAGE : liste de publications, menu de navigation,
    liste de vidéos. « waveguides in lithium niobate for quantum technology » y rattachait le
    polissage de Fraunhofer ILT au Quantum. D'où la phrase comme unité, bornée en longueur, et
    le refus des références bibliographiques ;
  - « optics » sur un site d'acteur désigne presque toujours l'optique DE LA MACHINE (« helical
    drilling optics », « fixed-optics systems », « f-theta lens ») : 24 des 24 publications
    classées Optique par le lexique documentaire l'étaient par ce biais ou par la lentille de
    focalisation. Le marché Optique ne s'ouvre donc ici qu'à des termes qui nomment un produit
    optique ;
  - « photonic » seul ramène « digital photonic process chain » (le vocabulaire de Fraunhofer
    pour la chaîne de procédé laser) et « photonic crystal fiber » (la fibre qui livre le
    faisceau) ; « quantum » ramène « quantum efficiency » et « quantum-dot-doped glass » ;
    « electrode » ramène des électrodes de photodétecteur, d'OLED et d'électrolyse sous le
    libellé « Électrodes de batteries » ; « diffractive optical element » est, six fois sur
    sept, l'outil qui divise le faisceau et non la pièce fabriquée.

Les lexiques resserrés (DOCUMENT_MARKETS/DOCUMENT_COMPONENTS) et le classement phrase par
phrase vivent dans lexicon.py depuis le 06/10/2026 : les tags marché et produit de la page
Technologie y passent aussi. MARKETS/COMPONENTS restent intacts pour l'extraction de faits.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lexicon import (
    DOCUMENT_COMPONENTS,
    DOCUMENT_MARKETS,
    DOCUMENT_SENTENCE_MAX,
    OFFER_SENTENCE_MAX,
    OPERATIONS,
    LexiconRule,
    labels_named_in_sentences,
)
from sources import SOURCES


def classify(
    texts: list[str],
    *,
    names: list[str] | None = None,
    operation_rule: LexiconRule | None = None,
    max_len: int | None = DOCUMENT_SENTENCE_MAX,
    reject_bibliography: bool = False,
    reject_negation: bool = False,
) -> dict[str, dict[str, str]]:
    """Les marchés et produits que `texts` nomme, phrase par phrase :
    ``{"market": {libellé: phrase}, "product": {libellé: phrase}}``. Mêmes lexiques et mêmes
    gardes que les tags de la page Technologie (lexicon.labels_named_in_sentences)."""
    return {
        dimension: labels_named_in_sentences(
            texts, lexicon, names=names, operation_rule=operation_rule,
            max_len=max_len, reject_bibliography=reject_bibliography, reject_negation=reject_negation,
        )
        for dimension, lexicon in (("market", DOCUMENT_MARKETS), ("product", DOCUMENT_COMPONENTS))
    }


def _proofs(found: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"dimension": dimension, "label": label, "sentence": sentence}
        for dimension in ("market", "product")
        for label, sentence in found[dimension].items()
    ]


def compile_offers(market_db: Path) -> list[dict[str, Any]]:
    """Chaque capacité acceptée qui nomme au moins un marché ou un produit."""
    with closing(sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        offers = conn.execute(
            """SELECT id,actor_name,capability,operation,offer_type,industrial_stage,quote,source_url
                 FROM offers WHERE review_status='accepted' ORDER BY actor_name,capability"""
        ).fetchall()
        quotes: dict[int, list[str]] = {}
        for row in conn.execute("SELECT offer_id,quote FROM offer_sources WHERE quote IS NOT NULL"):
            quotes.setdefault(int(row["offer_id"]), []).append(row["quote"])

    items: list[dict[str, Any]] = []
    for offer in offers:
        texts = list(dict.fromkeys([*quotes.get(int(offer["id"]), []), offer["quote"] or ""]))
        found = classify(
            texts,
            names=[offer["actor_name"]],
            operation_rule=OPERATIONS.get(offer["operation"] or ""),
            max_len=OFFER_SENTENCE_MAX,
            reject_bibliography=True,
            reject_negation=True,
        )
        if not (found["market"] or found["product"]):
            continue
        items.append({
            "uid": f"offer:{offer['id']}",
            "offer_id": int(offer["id"]),
            "actor": offer["actor_name"],
            "capability": offer["capability"],
            "operation": offer["operation"],
            "stage": offer["industrial_stage"],
            "source_url": offer["source_url"],
            "markets": list(found["market"]),
            "products": list(found["product"]),
            "proofs": _proofs(found),
            "source_id": "actor_websites",
        })
    return items


# Qui a apporté une entrée du corpus technique, lu sur son URL comme le fait
# app._TECHNOLOGY_SOURCE_COUNTS (aucune colonne ne nomme le collecteur). Toute publication de
# `documents` vient d'OpenAlex : Crossref, HAL et arXiv remplissent la file hors roster, jamais
# le corpus.
_PROJECT_HOSTS = (("cordis.europa.eu", "cordis"), ("anr.fr", "anr"), ("gtr.ukri.org", "ukri_gtr"))
_PATENT_HOSTS = (("espacenet.com", "epo_ops"), ("lens.org", "lens"), ("patents.google.com", "google_patents"))


def _source_of(kind: str, url: str) -> str:
    host = urlparse(url or "").netloc
    table = _PATENT_HOSTS if kind == "brevet" else _PROJECT_HOSTS if kind == "projet" else ()
    for pattern, source_id in table:
        if pattern in host:
            return source_id
    if kind == "projet":
        # Femtocell et les projets documentés sur le site d'un acteur : c'est lui la source.
        return "actor_websites"
    return "openalex"


def compile_technology(tech_db: Path) -> list[dict[str, Any]]:
    """Chaque publication, projet ou brevet du corpus qui nomme un marché ou un produit.

    Lu dans le texte de la source (titre + résumé ; pour un projet, titre + citations de ses
    signaux), pas dans les signaux `market`/`component` déjà posés sur la page Technologie : ce
    sont eux que la relecture du 28/09/2026 a trouvés trop larges (voir l'en-tête du module).
    """
    with closing(sqlite3.connect(f"file:{tech_db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        documents = conn.execute(
            """SELECT id,actor_name,document_type,title,abstract,source_url,published_at,doi
                 FROM documents"""
        ).fetchall()
        signals = conn.execute(
            """SELECT s.id,s.project_name,s.actor_names,s.source_url,s.project_start,s.funding_scope,
                      ts.quote
                 FROM technology_signals s
                 LEFT JOIN technology_signal_sources ts ON ts.signal_id=s.id
                WHERE s.review_status='accepted' AND TRIM(COALESCE(s.project_name,''))!=''"""
        ).fetchall()

    items: list[dict[str, Any]] = []
    for document in documents:
        kind = "brevet" if document["document_type"] == "patent" else "pub"
        actors = [document["actor_name"]] if document["actor_name"] else []
        found = classify([document["title"] or "", document["abstract"] or ""], names=actors)
        if not (found["market"] or found["product"]):
            continue
        items.append({
            "uid": f"doc:{document['id']}",
            "kind": kind,
            "title": document["title"],
            "actors": actors,
            "date": document["published_at"],
            "source_url": document["source_url"],
            "markets": list(found["market"]),
            "products": list(found["product"]),
            "proofs": _proofs(found),
            "source_id": _source_of(kind, document["source_url"] or ""),
        })

    projects: dict[str, dict[str, Any]] = {}
    for signal in signals:
        url = signal["source_url"] or ""
        project = projects.setdefault(url, {
            "title": signal["project_name"], "actors": [], "date": signal["project_start"],
            "texts": [signal["project_name"]], "first_id": int(signal["id"]),
        })
        for actor in _actor_names(signal["actor_names"]):
            if actor not in project["actors"]:
                project["actors"].append(actor)
        if signal["quote"] and signal["quote"] not in project["texts"]:
            project["texts"].append(signal["quote"])
    for url, project in projects.items():
        found = classify(project["texts"], names=project["actors"])
        if not (found["market"] or found["product"]):
            continue
        items.append({
            "uid": f"proj:{project['first_id']}",
            "kind": "projet",
            "title": project["title"],
            "actors": project["actors"],
            "date": project["date"],
            "source_url": url,
            "markets": list(found["market"]),
            "products": list(found["product"]),
            "proofs": _proofs(found),
            "source_id": _source_of("projet", url),
        })
    items.sort(key=lambda item: item["date"] or "", reverse=True)
    return items


def _actor_names(raw: str | None) -> list[str]:
    try:
        return [str(name) for name in json.loads(raw)] if raw else []
    except (json.JSONDecodeError, TypeError):
        return []


# Le glossaire en bas de page : les sources qui nourrissent CETTE page, groupées par ce qu'elles
# y apportent, avec le compte de ce qui y est réellement arrivé. Même contrat que la page
# Technologie (registre pour le nom et l'état de la clé, base pour le compte), mais le compte
# est celui de la compilation : une publication qui ne nomme aucun marché ne compte pas ici.
_GLOSSARY: tuple[tuple[str, str, str], ...] = (
    ("actor_websites", "Pages des acteurs suivis", "rattachés"),
    ("openalex", "Publications scientifiques", "publications rattachées"),
    ("cordis", "Projets financés", "projets rattachés"),
    ("anr", "Projets financés", "projets rattachés"),
    ("ukri_gtr", "Projets financés", "projets rattachés"),
    ("epo_ops", "Brevets", "brevets rattachés"),
    ("lens", "Brevets", "brevets rattachés"),
    ("google_patents", "Brevets", "brevets rattachés"),
    ("boamp", "Signaux de demande", "appels d'offres"),
    ("ted", "Signaux de demande", "appels d'offres"),
    ("anr", "Signaux de demande", "cofinancements"),
)

# Le pluriel est écrit, jamais fabriqué : « 1 projets rattachés » ne se lit pas.
_SINGULAR_UNITS = {
    "publications rattachées": "publication rattachée",
    "projets rattachés": "projet rattaché",
    "brevets rattachés": "brevet rattaché",
    "appels d'offres": "appel d'offres",
    "cofinancements": "cofinancement",
}
# demand_signals.source porte le nom du guichet tel que demand_signals.py l'écrit.
_DEMAND_SOURCE_NAMES = {"boamp": "BOAMP", "ted": "TED", "anr": "ANR"}


def source_glossary(
    *, evidence_count: int, offers: list[dict[str, Any]], technology: list[dict[str, Any]],
    demand_counts: dict[str, int],
) -> list[dict[str, Any]]:
    registry = {source.id: source for source in SOURCES}
    tech_counts: dict[str, int] = {}
    for item in technology:
        tech_counts[item["source_id"]] = tech_counts.get(item["source_id"], 0) + 1
    glossary: list[dict[str, Any]] = []
    for source_id, role, unit in _GLOSSARY:
        source = registry[source_id]
        if role == "Pages des acteurs suivis":
            projects = tech_counts.get(source_id, 0)
            count = evidence_count + len(offers) + projects
            unit = f"rattachés ({evidence_count} faits, {len(offers)} offres{f', {projects} projets' if projects else ''})"
        elif role == "Signaux de demande":
            count = demand_counts.get(_DEMAND_SOURCE_NAMES[source_id], 0)
        else:
            count = tech_counts.get(source_id, 0)
        if count == 1:
            unit = _SINGULAR_UNITS.get(unit, unit)
        glossary.append({
            "id": source_id,
            "name": source.name,
            "domain": source.domain,
            "role": role,
            "count": count,
            "count_unit": unit,
            "status": source.status,
            "env_vars": list(source.env_vars),
        })
    return glossary


# Le corpus technique se relit en ~2,5 s (300 résumés × 60 règles) : trop pour chaque
# affichage de la page, et inutile tant que ni market.db ni technology.db n'a bougé. La clé est
# l'état des fichiers, WAL compris -- une écriture de collecte change forcément l'un des deux.
_cache: dict[str, Any] = {"key": None, "value": None}


def _files_state(*paths: Path) -> tuple[tuple[int, int], ...]:
    state = []
    for path in paths:
        for candidate in (path, path.with_name(path.name + "-wal")):
            try:
                stat = candidate.stat()
                state.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                state.append((0, 0))
    return tuple(state)


def compile_market_page(market_db: Path, tech_db: Path) -> dict[str, list[dict[str, Any]]]:
    key = _files_state(market_db, tech_db)
    if _cache["key"] != key:
        _cache["value"] = {"offers": compile_offers(market_db), "technology": compile_technology(tech_db)}
        _cache["key"] = key
    return _cache["value"]
