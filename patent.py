"""Connecteur brevets EPO OPS (§4.C.1 audit veille, 30/08/2026, Lot 3 §3.4) : "0 brevet en base,
alors que le brevet est le signal avancé par excellence -- 18 mois d'avance sur le produit,
déposant identifié, revendications techniques exploitables."

Deux requêtes CQL complémentaires, même distinction actor-scoped / topic-scoped que cordis.py
et openalex.py (voir leurs docstrings pour le raisonnement complet) :

- _fetch_actor_patents  : par déposant (``pa=``), une requête par acteur suivi, croisée avec
  ``cpc=B23K26`` (travail au laser -- perçage, découpe, texturation...). Attribue les brevets
  trouvés à l'acteur (documents.actor_name).
- _fetch_topic_scoped_patents : par classe CPC seule, indépendante de tout acteur suivi --
  fait remonter les déposants récurrents absents de la base comme actor_candidates
  (source_type='patent'), exactement le rôle que l'audit lui donne dans son tableau de sources
  candidates (§4.D).

Authentification : OAuth2 client_credentials (clé/secret consumer gratuits sur
developers.epo.org), lus depuis EPO_OPS_KEY/EPO_OPS_SECRET -- jamais codés en dur, même schéma
que ANTHROPIC_API_KEY dans hybrid.py. Sans ces deux variables, collect_patents() renvoie
status='not_configured' plutôt que d'échouer bruyamment (mêmes raison que AiClient quand aucun
provider n'est configuré).

AVERTISSEMENT DE VÉRIFICATION (30/08/2026) : le schéma XML ci-dessous (bibliographic-data/
parties/applicants/applicant/applicant-name, publication-reference/document-id) est celui du
format d'échange EPO (DOCDB) tel que documenté publiquement -- endpoints, OAuth2 et syntaxe CQL
confirmés par la documentation OPS et plusieurs bibliothèques clientes de référence
(python-epo-ops-client, epo-cli). Faute d'un compte développeur disponible au moment où ce module
a été écrit, le PARSING XML n'a en revanche pas encore été confronté à une vraie réponse de
l'API -- à faire dès qu'une clé/secret sont disponibles, avant de considérer ce chantier terminé
(voir tests/test_patent.py, qui documente cette réserve sur ses fixtures).
"""

from __future__ import annotations

import hashlib
import os
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import httpx

from actor_discovery import _known_actor_names_and_domains, _normalize_name, upsert_actor_candidate
from db import ACTORS_DB, TECH_DB, compute_is_backfill, connect, utc_now
from http_client import connector_client

EPO_OPS_KEY = os.getenv("EPO_OPS_KEY", "").strip()
EPO_OPS_SECRET = os.getenv("EPO_OPS_SECRET", "").strip()

OPS_TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
OPS_SEARCH_BIBLIO_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"


# Format d'échange EPO (DOCDB) -- voir l'avertissement de vérification dans le docstring du module.
EXCHANGE_NS = {"ex": "http://www.epo.org/exchange"}

# Travail au laser (perçage, découpe, texturation, soudage...) -- voir §4.C.1 audit veille.
CPC_CLASS = "B23K26"

# Un nom d'acteur trop court (sigle générique) chercherait n'importe quoi dans un déposant --
# même garde-fou que cordis.MIN_ALIAS_LENGTH, dupliqué plutôt qu'importé (aucun autre lien avec
# ce module).
MIN_APPLICANT_NAME_LENGTH = 4

# OPS impose un quota "fair use" strict côté gratuit -- une pause entre deux requêtes par acteur
# reste polie sans ralentir un run mensuel de façon perceptible (55 acteurs ~= moins d'une minute).
PATENT_REQUEST_DELAY_SECONDS = 1.0

# Une page de résultats par requête suffit très largement pour un périmètre de 55 acteurs sur
# une seule classe CPC -- pas de pagination multi-page dans cette première version.
RESULTS_PER_ACTOR = 25
RESULTS_TOPIC_SCOPED = 25


def _get_access_token(client: httpx.Client) -> str:
    response = client.post(
        OPS_TOKEN_URL,
        auth=httpx.BasicAuth(EPO_OPS_KEY, EPO_OPS_SECRET),
        data={"grant_type": "client_credentials"},
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("EPO OPS: réponse d'authentification sans access_token")
    return str(token)


def _cql_escape(value: str) -> str:
    """CQL délimite une phrase par des guillemets doubles -- un nom d'acteur n'en contient
    normalement jamais, mais on les retire par sécurité plutôt que d'envoyer une requête cassée."""
    return value.replace('"', "")


def _search_biblio(client: httpx.Client, token: str, cql: str, range_end: int) -> ET.Element:
    response = client.get(
        OPS_SEARCH_BIBLIO_URL,
        params={"q": cql},
        headers={"Authorization": f"Bearer {token}", "X-OPS-Range": f"1-{range_end}"},
    )
    response.raise_for_status()
    return ET.fromstring(response.content)


def _element_text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    node = element.find(path, EXCHANGE_NS)
    if node is None or not node.text:
        return None
    return node.text.strip()


def _parse_exchange_document(doc_el: ET.Element) -> dict | None:
    """Un <ex:exchange-document> -> {patent_number,title,published_at,applicants,url}, ou None
    si les champs indispensables (numéro de publication) sont absents."""
    biblio = doc_el.find("ex:bibliographic-data", EXCHANGE_NS)
    if biblio is None:
        return None
    pub_ref = biblio.find(
        "ex:publication-reference/ex:document-id[@document-id-type='docdb']", EXCHANGE_NS
    )
    if pub_ref is None:
        return None
    country = _element_text(pub_ref, "ex:country") or ""
    doc_number = _element_text(pub_ref, "ex:doc-number") or ""
    kind = _element_text(pub_ref, "ex:kind") or ""
    patent_number = f"{country}{doc_number}{kind}"
    if not doc_number:
        return None

    published_raw = _element_text(pub_ref, "ex:date")
    published_at = None
    if published_raw and len(published_raw) == 8:
        published_at = f"{published_raw[0:4]}-{published_raw[4:6]}-{published_raw[6:8]}"

    title = None
    for title_el in biblio.findall("ex:invention-title", EXCHANGE_NS):
        if title_el.get("lang") == "en" and title_el.text:
            title = title_el.text.strip()
            break
    if not title:
        title = _element_text(biblio, "ex:invention-title") or patent_number

    applicants = []
    for applicant_el in biblio.findall("ex:parties/ex:applicants/ex:applicant", EXCHANGE_NS):
        data_format = applicant_el.get("data-format")
        if data_format and data_format != "docdb":
            continue
        name = _element_text(applicant_el, "ex:applicant-name/ex:name")
        if name:
            applicants.append(name)

    return {
        "patent_number": patent_number,
        "title": title,
        "published_at": published_at,
        "applicants": applicants,
        # Lien de recherche Espacenet par numéro de publication -- jamais un chemin d'enregistrement
        # direct deviné, une recherche par pn= est garantie de résoudre vers le bon document.
        "url": f"https://worldwide.espacenet.com/patent/search?q=pn%3D{patent_number}",
    }


def _fetch_actor_patents(client: httpx.Client, token: str, actor_name: str) -> list[dict]:
    cql = f'pa="{_cql_escape(actor_name)}" and cpc={CPC_CLASS}'
    root = _search_biblio(client, token, cql, RESULTS_PER_ACTOR)
    documents = []
    for doc_el in root.findall(".//ex:exchange-document", EXCHANGE_NS):
        parsed = _parse_exchange_document(doc_el)
        if parsed:
            documents.append(parsed)
    return documents


def _fetch_topic_scoped_patents(client: httpx.Client, token: str, from_date_yyyymmdd: str) -> list[dict]:
    cql = f"cpc={CPC_CLASS} and pd>={from_date_yyyymmdd}"
    root = _search_biblio(client, token, cql, RESULTS_TOPIC_SCOPED)
    documents = []
    for doc_el in root.findall(".//ex:exchange-document", EXCHANGE_NS):
        parsed = _parse_exchange_document(doc_el)
        if parsed:
            documents.append(parsed)
    return documents


def _upsert_patent_document(db, actor_name: str | None, doc: dict) -> tuple[int, int]:
    """Même logique que openalex._upsert_document : (1 si nouvelle ligne, 1 si actor_name vient
    d'être renseigné sur une ligne déjà vue sans attribution -- ex: trouvé d'abord par la passe
    topic-scoped, puis attribué par la passe par-déposant, ou l'inverse)."""
    fingerprint = hashlib.sha256(doc["patent_number"].casefold().encode()).hexdigest()
    stamp = utc_now()
    date_confidence = "published" if doc["published_at"] else "observed_only"
    is_backfill = compute_is_backfill(stamp, doc["published_at"], date_confidence)
    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO documents(
               actor_name,document_type,title,source_url,published_at,patent_number,date_confidence,is_backfill,fingerprint,created_at,last_seen_at
           ) VALUES(?,'patent',?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, doc["title"], doc["url"], doc["published_at"], doc["patent_number"],
            date_confidence, is_backfill, fingerprint, stamp, stamp,
        ),
    )
    inserted = int(db.total_changes > before)
    if inserted:
        return 1, 0
    db.execute("UPDATE documents SET last_seen_at=? WHERE fingerprint=?", (stamp, fingerprint))
    attributed = 0
    if actor_name:
        row = db.execute("SELECT actor_name FROM documents WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row and not row["actor_name"]:
            db.execute("UPDATE documents SET actor_name=? WHERE fingerprint=?", (actor_name, fingerprint))
            attributed = 1
    return 0, attributed


def collect_patents(*, lookback_days: int = 365) -> dict:
    """Point d'entrée (voir app.py: collectors["patents"])."""
    if not EPO_OPS_KEY or not EPO_OPS_SECRET:
        return {
            "status": "not_configured",
            "message": "EPO_OPS_KEY/EPO_OPS_SECRET absents -- inscription gratuite sur developers.epo.org",
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 0,
        }

    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT name FROM actors WHERE active=1").fetchall()]
        known_names, _known_domains = _known_actor_names_and_domains(db)

    actors_matched = patents_added = patents_attributed = candidates_added = errors = 0

    try:
        with connector_client("slow", follow_redirects=False) as client:
            token = _get_access_token(client)

            with connect(TECH_DB) as tech_db:
                eligible = [a for a in actors if len(_normalize_name(a["name"])) >= MIN_APPLICANT_NAME_LENGTH]
                for index, actor in enumerate(eligible):
                    try:
                        documents = _fetch_actor_patents(client, token, actor["name"])
                    except Exception:
                        errors += 1
                        documents = []
                    if documents:
                        actors_matched += 1
                    for doc in documents:
                        added, attributed = _upsert_patent_document(tech_db, actor["name"], doc)
                        patents_added += added
                        patents_attributed += attributed
                    if index < len(eligible) - 1:
                        time.sleep(PATENT_REQUEST_DELAY_SECONDS)

            from_date = (date.today() - timedelta(days=max(1, lookback_days))).strftime("%Y%m%d")
            try:
                topic_documents = _fetch_topic_scoped_patents(client, token, from_date)
            except Exception:
                topic_documents = []
                errors += 1

            with connect(TECH_DB) as tech_db, connect(ACTORS_DB) as actors_db:
                for doc in topic_documents:
                    added, attributed = _upsert_patent_document(tech_db, None, doc)
                    patents_added += added
                    patents_attributed += attributed
                    for applicant in doc["applicants"]:
                        if _normalize_name(applicant) in known_names:
                            continue
                        candidates_added += upsert_actor_candidate(
                            actors_db, applicant, "patent",
                            context=f"Brevet CPC {CPC_CLASS} {doc['patent_number']}",
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
