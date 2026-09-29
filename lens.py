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

- Rate-limit mesure en direct : ce compte tient ~9-10 requetes/minute (la premiere version en
  avait conclu qu'il fallait UNE requete globale -- abandonne le 29/09/2026, voir plus bas).
- Noms de champs corriges par rapport a la premiere version (jamais confrontee a une vraie
  reponse) : la reponse porte "kind", pas "kind_symbol" ; un declarant est
  {"extracted_name": {"value": "NOM"}}, pas {"applicant_name": "NOM"} -- "extracted_name" est un
  OBJET, pas une chaine. Le filtre CPC ne matche RIEN en "term"/"match" sur
  "classifications_cpc.symbol" (verifie : 0 resultat meme sur un symbole exact connu) ; le
  champ de RECHERCHE reellement documente (docs.api.lens.org, confirme en direct) est
  "class_cpc.symbol" via une requete "query_string" avec joker ("class_cpc.symbol:B23K26*") --
  nom different du chemin de la reponse ("biblio.classifications_cpc.classifications[].symbol"),
  comme "applicant.name" (recherche) differe deja de "biblio.parties.applicants[]" (reponse).

**Refonte du 29/09/2026 : une requête par acteur suivi, et le périmètre appliqué.** La version
précédente ramenait les 100 brevets CPC B23K26 les plus récents du monde, tous procédés laser
confondus, et les écrivait tous dans le corpus. Mesuré sur les 100 écrits : 0 portait un acteur
suivi, 0 un résumé, 0 un terme femtoseconde ou ultra-rapide -- « Removing mill scale from a
tubular », « Welding flux composition », une découpeuse laser pour sacs tissés. Deux règles de
Lucas étaient enfreintes d'un coup : le corpus ne contient que ce qu'un acteur suivi signe
(13/09/2026), et seulement le laser femto et ultra-rapide (10/09/2026). Et comme rien n'était
classé, ces brevets s'affichaient sans famille ni citation -- « les brevets n'ont pas de
sources ».

Le raisonnement qui avait écarté la requête par acteur (« 55+ requêtes par passe, plus d'une
heure ») ne tient pas à la mesure : c'est UNE requête par acteur, ~9 par minute sur ce compte,
soit une dizaine de minutes pour le roster -- une collecte périodique, pas un appel interactif. Vérifié en
direct le 29/09/2026 : ALPHANOV 32 brevets ultra-rapides, Amplitude 96, TRUMPF 359, LASEA 3.

Trois gardes, dans cet ordre, et chacune a sa raison :

1. la requête exige un terme ultra-rapide dans le titre ou le résumé -- sans quoi un grand
   déposant renvoie tout son portefeuille, laser continu et machines comprises ;
2. le déposant renvoyé doit CONTENIR l'alias de l'acteur interrogé (national_projects.match_alias)
   -- la recherche plein texte de Lens sur `applicant.name` est floue, et « Amplitude » ne doit
   pas attribuer à l'acteur suivi le brevet d'une autre société qui porte ce mot ;
3. is_on_topic() sur titre + résumé, comme pour toute publication : c'est lui qui écarte le
   brevet qui décrit la SOURCE laser elle-même (oscillateur, amplificateur) plutôt qu'un usinage,
   et celui où le laser n'est qu'un instrument de mesure.

Un brevet retenu est ensuite classé par upsert_document_technology_signal, exactement comme une
publication : opération, matériau, marché, pièce, chacun avec la phrase du résumé qui le porte.

Authentification : jeton porteur (Bearer), lu depuis LENS_API_KEY -- jamais codé en dur, même
schéma que EPO_OPS_KEY dans patent.py. Un jeton Lens s'obtient gratuitement sur lens.org/lens/user
(inscription académique/non-commerciale) et se génère depuis l'onglet "API & Data" du profil.
Sans cette variable, collect_lens_patents() renvoie status='not_configured'.
"""

from __future__ import annotations

import os
import re
import time

import httpx

from cordis import _contains_whole_phrase, _normalize_org_text
from db import ACTORS_DB, TECH_DB, connect, upsert_document
from http_client import connector_client
from lexicon import is_on_topic
from national_projects import match_alias
from scrapers import upsert_document_technology_signal

LENS_API_KEY = os.getenv("LENS_API_KEY", "").strip()

LENS_SEARCH_URL = "https://api.lens.org/patent/search"


# Taille d'une page. Vérifié le 28/09/2026 : l'API refuse "size" au-delà de 100 (HTTP 400,
# "Parameter 'size' shouldn't be greater than 100"). Un gros déposant dépasse une page (TRUMPF :
# 359 brevets ultra-rapides le 29/09/2026), d'où la pagination par "from", bornée par
# MAX_PATENTS_PER_ACTOR pour qu'un seul acteur ne mange pas toute la passe.
RESULTS_LIMIT = 100
MAX_PATENTS_PER_ACTOR = 500

# Champs demandés à l'API (paramètre "include"). Le RÉSUMÉ en fait partie depuis le 29/09/2026 :
# sans lui, un brevet ne pouvait être ni filtré sur le périmètre ni classé avec une preuve -- son
# titre de brevet (« Processing method and processing system ») ne dit presque jamais rien.
_INCLUDE_FIELDS = [
    "lens_id", "jurisdiction", "doc_number", "kind", "date_published",
    "biblio.invention_title", "biblio.parties.applicants", "abstract",
]

# Ce qui fait d'un brevet laser un brevet ULTRA-RAPIDE, demandé à Lens sur le titre et le résumé.
# Aligné sur lexicon.LASER_RULES ; volontairement large, is_on_topic() reste seul juge derrière.
# « picosecond » y figure : les brevets industriels disent souvent « ps/fs » et le picoseconde est
# du laser à impulsions ultra-courtes au sens du métier.
ULTRAFAST_TERMS = (
    '(femtosecond OR "femto second" OR ultrafast OR "ultra-fast" OR ultrashort OR "ultra-short" '
    'OR picosecond OR "USP laser" OR femtoseconde OR ultracourte OR ultrakurzpuls)'
)

# Cadence : ce compte tient ~9-10 requêtes par minute (mesuré le 28/09/2026). Une pause de 7 s
# garde une marge : le roster (80 acteurs actifs le 29/09/2026) tient en une dizaine de minutes.
REQUEST_INTERVAL_SECONDS = 7.0


def _is_configured() -> bool:
    return bool(LENS_API_KEY)


def _fetch_actor_patents(client: httpx.Client, actor_name: str, offset: int = 0) -> dict:
    """Une page des brevets ultra-rapides dont `actor_name` est déposant, les plus récents d'abord.

    Le nom est passé entre guillemets et nettoyé de ceux qu'il porterait : Lens le lit en syntaxe
    query_string, où un guillemet ou une parenthèse non fermés font échouer toute la requête.
    """
    # Le sigle entre parenthèses part : « Manufacturing Technology Centre (MTC) » n'est écrit
    # ainsi sur aucune demande de brevet.
    nom = re.sub(r"\([^)]*\)", " ", actor_name)
    nom = " ".join(nom.replace('"', " ").replace("\\", " ").split())
    requete = f'applicant.name:"{nom}" AND (title:{ULTRAFAST_TERMS} OR abstract:{ULTRAFAST_TERMS})'
    response = client.post(
        LENS_SEARCH_URL,
        json={
            "query": {"query_string": {"query": requete}},
            "sort": [{"date_published": "desc"}],
            "include": _INCLUDE_FIELDS,
            "size": RESULTS_LIMIT,
            "from": offset,
        },
        headers={"Authorization": f"Bearer {LENS_API_KEY}"},
    )
    response.raise_for_status()
    return response.json()


def _abstract(doc: dict) -> str:
    """Le résumé, en anglais ou en français d'abord -- les deux langues que lit le lexique."""
    resumes = doc.get("abstract") or []
    if not isinstance(resumes, list):
        return ""
    for langue in ("en", "fr"):
        for entry in resumes:
            if isinstance(entry, dict) and entry.get("lang") == langue and entry.get("text"):
                return str(entry["text"]).strip()
    return ""


def _signed_by(applicants: list[str], actor_name: str) -> bool:
    """Vrai quand l'un des déposants renvoyés CONTIENT l'alias de l'acteur interrogé.

    La recherche de Lens sur `applicant.name` est plein texte, donc floue : interroger
    « Amplitude » peut ramener une autre société qui porte ce mot. On revérifie donc sur la
    réponse, avec l'appariement déjà utilisé pour les registres de financement
    (national_projects.match_alias) -- sigle et forme juridique retirés, phrase entière exigée.
    """
    alias = match_alias(actor_name)
    return any(_contains_whole_phrase(_normalize_org_text(nom), alias) for nom in applicants)


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
        "abstract": _abstract(doc),
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
        abstract=doc.get("abstract") or None,
    )


def collect_lens_patents() -> dict:
    """Point d'entrée (voir app.py: collectors["lens_patents"]).

    Une requête par acteur suivi (paginée), puis les gardes du docstring du module. Chaque brevet
    écrit porte donc un acteur du roster, relève du laser ultra-rapide, et sort classé avec ses
    preuves -- jamais un brevet orphelin dans le corpus.

    Plus d'inscription de candidats acteurs depuis Lens : la version précédente en créait à partir
    de n'importe quel déposant de brevet laser du monde (83 d'un coup le 28/09/2026), et les
    co-déposants d'un brevet américain sont le plus souvent les inventeurs, des personnes.
    """
    vide = {
        "actors_queried": 0, "actors_matched": 0, "patents_seen": 0, "patents_added": 0,
        "patents_attributed": 0, "off_topic": 0, "not_signed": 0, "technology_signals_added": 0,
        "errors": 0,
    }
    if not _is_configured():
        return {
            **vide, "status": "not_configured",
            "message": "LENS_API_KEY absente -- jeton gratuit sur lens.org/lens/user (onglet API & Data)",
        }

    with connect(ACTORS_DB) as db:
        actors = [row["name"] for row in db.execute(
            "SELECT name FROM actors WHERE active=1 ORDER BY name"
        ).fetchall()]

    report = dict(vide)
    actors_matched: set[str] = set()
    trouves: list[tuple[str, dict]] = []

    # Tout le réseau d'abord, l'écriture ensuite : une transaction SQLite ouverte pendant les
    # minutes de requêtes bloquerait les autres écrivains (même raison que cordis.py).
    premiere_requete = True
    derniere_erreur = ""
    with connector_client("slow") as client:
        for actor_name in actors:
            report["actors_queried"] += 1
            offset = 0
            while offset < MAX_PATENTS_PER_ACTOR:
                if not premiere_requete:
                    time.sleep(REQUEST_INTERVAL_SECONDS)
                premiere_requete = False
                try:
                    payload = _fetch_actor_patents(client, actor_name, offset)
                except Exception as exc:
                    report["errors"] += 1
                    derniere_erreur = str(exc)[:300]
                    break
                resultats = (payload.get("data") if isinstance(payload, dict) else None) or []
                for doc in (_parse_result(row) for row in resultats):
                    if doc:
                        trouves.append((actor_name, doc))
                offset += RESULTS_LIMIT
                total = payload.get("total") if isinstance(payload, dict) else None
                if len(resultats) < RESULTS_LIMIT or not isinstance(total, int) or offset >= total:
                    break

    with connect(TECH_DB) as tech_db:
        for actor_name, doc in trouves:
            report["patents_seen"] += 1
            if not _signed_by(doc["applicants"], actor_name):
                report["not_signed"] += 1
                continue
            if not is_on_topic(f"{doc['title']} {doc.get('abstract') or ''}"):
                report["off_topic"] += 1
                continue
            actors_matched.add(actor_name)
            added, attributed = _upsert_lens_patent(tech_db, actor_name, doc)
            report["patents_added"] += added
            report["patents_attributed"] += attributed
            report["technology_signals_added"] += upsert_document_technology_signal(
                tech_db, doc["title"], doc.get("abstract") or "", doc["url"], actor_name,
            )

    report["actors_matched"] = len(actors_matched)
    # Toutes les requêtes en échec, c'est la source qui est en panne (jeton révoqué, quota
    # épuisé), pas un acteur au nom malcommode : le dire, plutôt qu'un « ok » à zéro brevet.
    if report["actors_queried"] and report["errors"] == report["actors_queried"]:
        return {**report, "status": "error", "message": derniere_erreur}
    return {**report, "status": "ok"}
