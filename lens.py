"""Connecteur brevets Lens.org (deuxième source de brevets, à côté d'EPO OPS dans patent.py) :
même besoin ("0 brevet en base tant qu'aucune clé n'est configurée"), une source différente --
Lens.org agrège plusieurs offices (USPTO, EPO, WIPO, JPO...) sous un schéma unique, là où EPO OPS
ne couvre que ce que l'EPO indexe. Les deux tournent indépendamment ; un même brevet trouvé par
les deux ne compte qu'une fois (dédoublonné par db.upsert_document sur son fingerprint -- le
numéro de brevet, indépendant de la source qui l'a rapporté). `app._TECHNOLOGY_SOURCE_COUNTS`
distingue ensuite ce que chaque source a apporté par le domaine de `documents.source_url`
(lens.org ici, espacenet.com pour patent.py) -- même mécanique que crossref/hal/arxiv, voir son
commentaire dans app.py.

Même distinction actor-scoped / topic-scoped que patent.py (voir son docstring) :
- _fetch_actor_patents  : par déposant (``applicant.name``), une requête par acteur suivi,
  croisée avec la classe CPC B23K26 (travail au laser). Attribue les brevets trouvés à l'acteur.
- _fetch_topic_scoped_patents : par classe CPC seule, fait remonter les déposants récurrents
  absents de la base comme actor_candidates (source_type='patent'), même rôle que le pendant EPO.

Authentification : jeton porteur (Bearer), lu depuis LENS_API_KEY -- jamais codé en dur, même
schéma que EPO_OPS_KEY dans patent.py. Un jeton Lens s'obtient gratuitement sur lens.org/lens/user
(inscription académique/non-commerciale) et se génère depuis l'onglet "API & Data" du profil.
Sans cette variable, collect_lens_patents() renvoie status='not_configured'.

AVERTISSEMENT DE VÉRIFICATION : la forme de requête (POST /patent/search, DSL {"query":
{"bool": {"must": [...]}}}, jeu de champs "include") et les noms de champs de réponse ci-dessous
suivent la documentation publique de l'API (docs.api.lens.org, github.com/cambialens/lens-api-doc)
telle que consultée le 22/09/2026 -- mais, comme pour patent.py, le PARSING n'a pas encore été
confronté à une vraie réponse (aucun jeton disponible au moment où ce module a été écrit). Le
parsing des champs de réponse est donc délibérément tolérant (un champ absent ou renommé donne un
document ignoré ou partiel, jamais une exception) -- à confronter à une vraie réponse et à
resserrer dès qu'un jeton est disponible, avant de considérer ce chantier terminé (voir
tests/test_lens.py, qui documente cette réserve sur ses fixtures).
"""

from __future__ import annotations

import os
import time

import httpx

from actor_discovery import _known_actor_names_and_domains, _normalize_name, upsert_actor_candidate
from db import ACTORS_DB, TECH_DB, connect, upsert_document
from http_client import connector_client

LENS_API_KEY = os.getenv("LENS_API_KEY", "").strip()

LENS_SEARCH_URL = "https://api.lens.org/patent/search"

# Travail au laser (perçage, découpe, texturation, soudage...) -- même classe que patent.py,
# voir son commentaire CPC_CLASS pour le détail du périmètre couvert.
CPC_CLASS = "B23K26"

# Même garde-fou que patent.MIN_APPLICANT_NAME_LENGTH, dupliqué plutôt qu'importé (aucun autre
# lien entre les deux modules -- ils ne partagent que db.py/actor_discovery.py).
MIN_APPLICANT_NAME_LENGTH = 4

# Lens applique aussi un quota "fair use" sur son offre gratuite -- même politesse qu'EPO OPS.
LENS_REQUEST_DELAY_SECONDS = 1.0

RESULTS_PER_ACTOR = 25
RESULTS_TOPIC_SCOPED = 25

# Champs demandés à l'API (paramètre "include") : de quoi peupler documents + actor_candidates
# sans redemander la fiche complète du brevet, qu'on ne consomme pas ici.
_INCLUDE_FIELDS = [
    "lens_id", "jurisdiction", "doc_number", "kind_symbol", "date_published",
    "biblio.invention_title", "biblio.parties.applicants",
]


def _search_biblio(client: httpx.Client, query: dict, size: int) -> dict:
    response = client.post(
        LENS_SEARCH_URL,
        json={"query": query, "include": _INCLUDE_FIELDS, "size": size, "from": 0},
        headers={"Authorization": f"Bearer {LENS_API_KEY}"},
    )
    response.raise_for_status()
    return response.json()


def _applicant_query(actor_name: str) -> dict:
    return {"bool": {"must": [
        {"match": {"applicant.name": actor_name}},
        {"term": {"classifications_cpc.symbol": CPC_CLASS}},
    ]}}


def _topic_query() -> dict:
    return {"term": {"classifications_cpc.symbol": CPC_CLASS}}


def _titles(doc: dict) -> str | None:
    # biblio.invention_title est une liste multilingue ([{lang, text}, ...]) -- l'anglais
    # d'abord, la première langue disponible sinon (même logique que patent._parse_
    # exchange_document, qui fait face au même format multilingue côté EPO OPS).
    titles = (doc.get("biblio") or {}).get("invention_title") or []
    if not isinstance(titles, list):
        return None
    for entry in titles:
        if isinstance(entry, dict) and entry.get("lang") == "en" and entry.get("text"):
            return str(entry["text"]).strip()
    for entry in titles:
        if isinstance(entry, dict) and entry.get("text"):
            return str(entry["text"]).strip()
    return None


def _applicant_names(doc: dict) -> list[str]:
    applicants = ((doc.get("biblio") or {}).get("parties") or {}).get("applicants") or []
    if not isinstance(applicants, list):
        return []
    names = []
    for entry in applicants:
        if not isinstance(entry, dict):
            continue
        name = entry.get("applicant_name") or entry.get("extracted_name") or entry.get("name")
        if name:
            names.append(str(name))
    return names


def _parse_result(doc: dict) -> dict | None:
    """Un résultat de recherche Lens -> {patent_number,title,published_at,applicants,url}, ou
    None si l'identifiant du document est absent. Tolérant par construction -- voir
    l'avertissement de vérification en tête de module."""
    if not isinstance(doc, dict):
        return None
    doc_number = doc.get("doc_number")
    if not doc_number:
        return None
    jurisdiction = doc.get("jurisdiction") or ""
    kind = doc.get("kind_symbol") or ""
    patent_number = f"{jurisdiction}{doc_number}{kind}"

    published_raw = doc.get("date_published")
    published_at = None
    if published_raw and isinstance(published_raw, str) and len(published_raw) >= 10:
        # Format ISO documenté ("YYYY-MM-DD") -- contrairement au AAAAMMJJ compact d'EPO OPS.
        published_at = published_raw[:10]

    lens_id = doc.get("lens_id") or ""
    return {
        "patent_number": patent_number,
        "title": _titles(doc) or patent_number,
        "published_at": published_at,
        "applicants": _applicant_names(doc),
        # Lien de fiche Lens par lens_id quand il est connu (résout toujours vers le bon
        # document) ; à défaut, recherche Lens par numéro de publication.
        "url": f"https://www.lens.org/lens/patent/{lens_id}" if lens_id
        else f"https://www.lens.org/lens/search/patent/list?q=doc_number:{doc_number}",
    }


def _extract_documents(payload: dict) -> list[dict]:
    results = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    parsed = [_parse_result(doc) for doc in results]
    return [doc for doc in parsed if doc]


def _fetch_actor_patents(client: httpx.Client, actor_name: str) -> list[dict]:
    payload = _search_biblio(client, _applicant_query(actor_name), RESULTS_PER_ACTOR)
    return _extract_documents(payload)


def _fetch_topic_scoped_patents(client: httpx.Client) -> list[dict]:
    payload = _search_biblio(client, _topic_query(), RESULTS_TOPIC_SCOPED)
    return _extract_documents(payload)


def _upsert_lens_patent(db, actor_name: str | None, doc: dict) -> tuple[int, int]:
    """Adaptateur : traduit un `doc` Lens en colonnes `documents`, delegue à db.upsert_document
    (partagée avec openalex.py/patent.py). `doc["url"]` reste sur le domaine lens.org -- c'est
    ce qui permet à app.py de compter les brevets apportés par CETTE source (voir
    _TECHNOLOGY_SOURCE_COUNTS, même mécanique que crossref/hal/arxiv : hôtes disjoints, jamais
    de colonne dédiée)."""
    return upsert_document(
        db,
        document_type="patent",
        title=doc["title"],
        source_url=doc["url"],
        fingerprint_source=doc["patent_number"],
        actor_name=actor_name,
        published_at=doc["published_at"],
        patent_number=doc["patent_number"],
    )


def collect_lens_patents() -> dict:
    """Point d'entrée (voir app.py: collectors["lens_patents"])."""
    if not LENS_API_KEY:
        return {
            "status": "not_configured",
            "message": "LENS_API_KEY absente -- jeton gratuit sur lens.org/lens/user (onglet API & Data)",
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 0,
        }

    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT name FROM actors WHERE active=1").fetchall()]
        known_names, _known_domains = _known_actor_names_and_domains(db)

    actors_matched = patents_added = patents_attributed = candidates_added = errors = 0

    try:
        with connector_client("slow") as client:
            with connect(TECH_DB) as tech_db:
                eligible = [a for a in actors if len(_normalize_name(a["name"])) >= MIN_APPLICANT_NAME_LENGTH]
                for index, actor in enumerate(eligible):
                    try:
                        documents = _fetch_actor_patents(client, actor["name"])
                    except Exception:
                        errors += 1
                        documents = []
                    if documents:
                        actors_matched += 1
                    for doc in documents:
                        added, attributed = _upsert_lens_patent(tech_db, actor["name"], doc)
                        patents_added += added
                        patents_attributed += attributed
                    if index < len(eligible) - 1:
                        time.sleep(LENS_REQUEST_DELAY_SECONDS)

            try:
                topic_documents = _fetch_topic_scoped_patents(client)
            except Exception:
                topic_documents = []
                errors += 1

            with connect(TECH_DB) as tech_db, connect(ACTORS_DB) as actors_db:
                for doc in topic_documents:
                    added, attributed = _upsert_lens_patent(tech_db, None, doc)
                    patents_added += added
                    patents_attributed += attributed
                    for applicant in doc["applicants"]:
                        if _normalize_name(applicant) in known_names:
                            continue
                        candidates_added += upsert_actor_candidate(
                            actors_db, applicant, "patent",
                            context=f"Brevet CPC {CPC_CLASS} {doc['patent_number']} (Lens.org)",
                            source_url=doc["url"],
                        )
    except Exception as exc:
        return {
            "status": "error", "message": str(exc)[:300],
            "actors_matched": actors_matched, "patents_added": patents_added,
            "patents_attributed": patents_attributed,
            "topic_scoped_candidates_added": candidates_added, "errors": errors + 1,
        }

    return {
        "status": "ok",
        "actors_matched": actors_matched,
        "patents_added": patents_added,
        "patents_attributed": patents_attributed,
        "topic_scoped_candidates_added": candidates_added,
        "errors": errors,
    }
