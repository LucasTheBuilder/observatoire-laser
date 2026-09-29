"""Connecteur brevets Lens.org (deuxième source de brevets, à côté d'EPO OPS dans patent.py) :
même besoin ("0 brevet en base tant qu'aucune clé n'est configurée"), une source différente --
Lens.org agrège plusieurs offices (USPTO, EPO, WIPO, JPO...) sous un schéma unique, là où EPO OPS
ne couvre que ce que l'EPO indexe. Les deux tournent indépendamment ; un même brevet trouvé par
les deux ne compte qu'une fois (dédoublonné par db.upsert_document sur son fingerprint -- le
numéro de brevet, indépendant de la source qui l'a rapporté). `app._TECHNOLOGY_SOURCE_COUNTS`
distingue ensuite ce que chaque source a apporté par le domaine de `documents.source_url`
(lens.org ici, espacenet.com pour patent.py) -- même mécanique que crossref/hal/arxiv, voir son
commentaire dans app.py.

Verifie le 28/09/2026 contre un vrai compte (jeton d'essai fourni par l'utilisateur), ce qui a
change la forme de ce module par rapport a patent.py :

- PAS de distinction actor-scoped/topic-scoped par une requete par acteur (comme patent.py).
  Mesure en direct : ce compte tient ~9-10 requetes/minute, tres en dessous des 55+ requetes
  qu'une boucle par acteur suivi aurait demandees en une passe (voir google_patents.py, qui
  applique deja ce raisonnement pour une toute autre raison -- le cout BigQuery). Une seule
  requete ramene les brevets CPC B23K26 les plus RECENTS (triee par date_published desc), et
  c'est le code Python qui les repartit ensuite entre acteur suivi et candidat -- meme logique
  que la passe topic-scoped de patent.py, seulement pour la totalite des resultats.
- Noms de champs corriges par rapport a la premiere version (jamais confrontee a une vraie
  reponse) : la reponse porte "kind", pas "kind_symbol" ; un declarant est
  {"extracted_name": {"value": "NOM"}}, pas {"applicant_name": "NOM"} -- "extracted_name" est un
  OBJET, pas une chaine. Le filtre CPC ne matche RIEN en "term"/"match" sur
  "classifications_cpc.symbol" (verifie : 0 resultat meme sur un symbole exact connu) ; le
  champ de RECHERCHE reellement documente (docs.api.lens.org, confirme en direct) est
  "class_cpc.symbol" via une requete "query_string" avec joker ("class_cpc.symbol:B23K26*") --
  nom different du chemin de la reponse ("biblio.classifications_cpc.classifications[].symbol"),
  comme "applicant.name" (recherche) differe deja de "biblio.parties.applicants[]" (reponse).

Authentification : jeton porteur (Bearer), lu depuis LENS_API_KEY -- jamais codé en dur, même
schéma que EPO_OPS_KEY dans patent.py. Un jeton Lens s'obtient gratuitement sur lens.org/lens/user
(inscription académique/non-commerciale) et se génère depuis l'onglet "API & Data" du profil.
Sans cette variable, collect_lens_patents() renvoie status='not_configured'.
"""

from __future__ import annotations

import os

import httpx

from actor_discovery import _known_actor_names_and_domains, _normalize_name, upsert_actor_candidate
from db import ACTORS_DB, TECH_DB, connect, upsert_document
from http_client import connector_client

LENS_API_KEY = os.getenv("LENS_API_KEY", "").strip()

LENS_SEARCH_URL = "https://api.lens.org/patent/search"

# Travail au laser (perçage, découpe, texturation, soudage...) -- même classe que patent.py,
# voir son commentaire CPC_CLASS pour le détail du périmètre couvert.
CPC_CLASS = "B23K26"

MIN_APPLICANT_NAME_LENGTH = 4

# Combien de brevets récents une seule requête ramène. Vérifié le 28/09/2026 : l'API refuse
# "size" au-delà de 100 (HTTP 400, "Parameter 'size' shouldn't be greater than 100") -- pas de
# pagination/scroll ici, une seule page suffit très largement pour un flux de veille récente.
RESULTS_LIMIT = 100

# Champs demandés à l'API (paramètre "include") : de quoi peupler documents + actor_candidates
# sans redemander la fiche complète du brevet, qu'on ne consomme pas ici.
_INCLUDE_FIELDS = [
    "lens_id", "jurisdiction", "doc_number", "kind", "date_published",
    "biblio.invention_title", "biblio.parties.applicants",
]


def _is_configured() -> bool:
    return bool(LENS_API_KEY)


def _fetch_recent_patents(client: httpx.Client) -> dict:
    response = client.post(
        LENS_SEARCH_URL,
        json={
            # query_string + joker : seule forme vérifiée qui matche réellement des brevets
            # CPC B23K26 sur ce compte -- voir docstring du module.
            "query": {"query_string": {"query": f"class_cpc.symbol:{CPC_CLASS}*"}},
            "sort": [{"date_published": "desc"}],
            "include": _INCLUDE_FIELDS,
            "size": RESULTS_LIMIT,
            "from": 0,
        },
        headers={"Authorization": f"Bearer {LENS_API_KEY}"},
    )
    response.raise_for_status()
    return response.json()


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
        # "extracted_name" est un OBJET ({"value": "NOM"}), vérifié le 28/09/2026 -- une chaîne
        # nue reste acceptée en repli (couvre une forme de réponse plus ancienne/différente,
        # jamais observée mais pas à exclure sans preuve).
        extracted = entry.get("extracted_name")
        name = extracted.get("value") if isinstance(extracted, dict) else extracted
        name = name or entry.get("applicant_name") or entry.get("name")
        if name:
            names.append(str(name))
    return names


def _parse_result(doc: dict) -> dict | None:
    """Un résultat de recherche Lens -> {patent_number,title,published_at,applicants,url}, ou
    None si l'identifiant du document est absent. Tolérant par construction : un champ encore
    renommé demain donne un document partiel, jamais une exception."""
    if not isinstance(doc, dict):
        return None
    doc_number = doc.get("doc_number")
    if not doc_number:
        return None
    jurisdiction = doc.get("jurisdiction") or ""
    kind = doc.get("kind") or ""
    patent_number = f"{jurisdiction}{doc_number}{kind}"

    published_raw = doc.get("date_published")
    published_at = None
    if published_raw and isinstance(published_raw, str) and len(published_raw) >= 10:
        # Format ISO ("YYYY-MM-DD"), vérifié le 28/09/2026 -- contrairement au AAAAMMJJ
        # compact d'EPO OPS.
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
    if not _is_configured():
        return {
            "status": "not_configured",
            "message": "LENS_API_KEY absente -- jeton gratuit sur lens.org/lens/user (onglet API & Data)",
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 0,
        }

    with connect(ACTORS_DB) as db:
        known_names, _known_domains = _known_actor_names_and_domains(db)
        # Nom d'affichage par nom normalisé, même principe que google_patents.py : attribuer
        # documents.actor_name avec la graphie suivie plutôt qu'avec le déposant tel qu'écrit
        # par l'office de brevets.
        display_name = {
            _normalize_name(row["name"]): row["name"]
            for row in db.execute("SELECT name FROM actors WHERE active=1").fetchall()
        }

    actors_matched_names: set[str] = set()
    patents_added = patents_attributed = candidates_added = 0

    try:
        with connector_client("slow") as client:
            payload = _fetch_recent_patents(client)
    except Exception as exc:
        return {
            "status": "error", "message": str(exc)[:300],
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 1,
        }

    results = payload.get("data") if isinstance(payload, dict) else None
    documents = [doc for doc in (_parse_result(row) for row in (results or [])) if doc]

    with connect(TECH_DB) as tech_db, connect(ACTORS_DB) as actors_db:
        for doc in documents:
            # Un brevet peut avoir plusieurs déposants (co-dépôt) : le premier déposant SUIVI
            # trouvé porte l'attribution (documents.actor_name est une seule colonne, même
            # limite que patent.py), les autres restent lisibles dans le déposant brut.
            tracked = next(
                (name for name in doc["applicants"] if _normalize_name(name) in display_name),
                None,
            )
            actor_name = display_name.get(_normalize_name(tracked)) if tracked else None
            if actor_name:
                actors_matched_names.add(actor_name)

            added, attributed = _upsert_lens_patent(tech_db, actor_name, doc)
            patents_added += added
            patents_attributed += attributed

            if actor_name:
                continue
            for applicant in doc["applicants"]:
                if len(_normalize_name(applicant)) < MIN_APPLICANT_NAME_LENGTH:
                    continue
                if _normalize_name(applicant) in known_names:
                    continue
                candidates_added += upsert_actor_candidate(
                    actors_db, applicant, "patent",
                    context=f"Brevet CPC {CPC_CLASS} {doc['patent_number']} (Lens.org)",
                    source_url=doc["url"],
                )

    return {
        "status": "ok",
        "actors_matched": len(actors_matched_names),
        "patents_added": patents_added,
        "patents_attributed": patents_attributed,
        "topic_scoped_candidates_added": candidates_added,
        "errors": 0,
    }
