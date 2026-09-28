"""Connecteur brevets Google Patents Public Datasets (troisième source de brevets, à côté d'EPO
OPS dans patent.py et Lens.org dans lens.py) : même corpus final (documents.document_type=
'patent'), une mécanique d'accès complètement différente des deux autres -- ni API REST à quota
gratuit, ni clé porteur, mais une table BigQuery publique de ~100M brevets mondiaux, interrogée en
SQL et facturée au volume de données lues sur le PROJET GCP de l'appelant (voir ci-dessous).

Comme pour lens.py, un même brevet trouvé par plusieurs sources ne compte qu'une fois
(dédoublonné par db.upsert_document sur son fingerprint) ; `app._TECHNOLOGY_SOURCE_COUNTS`
distingue ce que CETTE source apporte par le domaine de `documents.source_url`
(patents.google.com), même mécanique que crossref/hal/arxiv.

Ce que ça change par rapport à patent.py/lens.py, et pourquoi ce module a une forme différente :

- Pas de distinction actor-scoped/topic-scoped en amont. Une requête BigQuery PAR ACTEUR suivi
  (55 acteurs) scannerait 55 fois la même table de ~1 To -- inutile et potentiellement coûteux.
  Une seule requête ramène TOUS les brevets de la classe CPC B23K26 publiés depuis
  `lookback_days`, et c'est le code Python (pas SQL) qui les répartit ensuite entre "acteur
  suivi" (attribution directe) et "candidat" (déposant récurrent inconnu), exactement comme le
  fait déjà la passe topic-scoped de patent.py/lens.py -- seulement pour la totalité des
  résultats plutôt que pour un sous-ensemble.
- Authentification par compte de service (Application Default Credentials), pas par jeton
  porteur : GOOGLE_APPLICATION_CREDENTIALS (chemin vers la clé JSON) et GOOGLE_CLOUD_PROJECT
  (projet GCP qui PAIE la requête -- BigQuery ne facture jamais le projet propriétaire du jeu de
  données public, seulement celui de l'appelant). Un projet GCP avec facturation activée est
  requis même pour ne payer que dalle : BigQuery offre 1 To de lecture gratuite par mois, largement
  suffisant pour ce volume (une requête par run, colonnes réduites au strict nécessaire), mais la
  facturation doit être ACTIVÉE sur le projet pour que l'API accepte la requête.
- Dépendance optionnelle (google-cloud-bigquery, voir requirements-google-patents.txt -- volontairement
  hors requirements.txt, voir ce fichier) importée à l'intérieur de
  collect_google_patents() plutôt qu'en tête de module : un déploiement qui n'active pas cette
  source ne doit pas échouer à charger l'application si le paquet n'est pas installé, même
  principe que EPO_OPS_KEY/LENS_API_KEY absentes -- 'not_configured', jamais un crash au démarrage.

AVERTISSEMENT DE VÉRIFICATION : le nom de la table (`patents-public-data.patents.publications`)
et les colonnes ci-dessous (publication_number, title_localized, publication_date, cpc.code,
assignee_harmonized.name) suivent le schéma publié par Google
(github.com/google/patents-public-data) tel que consulté le 22/09/2026 -- mais, comme pour
patent.py et lens.py, cette requête n'a pas encore été confrontée à un vrai projet GCP facturable
(aucun projet disponible au moment où ce module a été écrit). À exécuter une première fois en
surveillant `bytes_billed` dès qu'un projet est disponible, avant de considérer ce chantier
terminé (voir tests/test_google_patents.py, qui documente cette réserve sur ses fixtures).
"""

from __future__ import annotations

import importlib.util
import os

from actor_discovery import _known_actor_names_and_domains, _normalize_name, upsert_actor_candidate
from db import ACTORS_DB, TECH_DB, connect, upsert_document

GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()

PUBLICATIONS_TABLE = "patents-public-data.patents.publications"

# Même classe CPC que patent.py/lens.py (travail au laser -- perçage, découpe, texturation,
# soudage...), voir leur commentaire CPC_CLASS pour le détail du périmètre couvert.
CPC_CLASS = "B23K26"

MIN_APPLICANT_NAME_LENGTH = 4

# Combien de brevets récents la requête ramène au maximum -- garde-fou de coût et de mémoire,
# pas une limite métier : la classe B23K26 publie de l'ordre de quelques centaines à quelques
# milliers de documents par an dans le monde entier, largement sous cette limite en pratique.
RESULTS_LIMIT = 2000

# Plafond de sécurité sur le volume FACTURÉ (pas lu) par la requête, en octets. Le job échoue
# proprement (bigquery.QueryJob lève avant de facturer au-delà) plutôt que de laisser une requête
# mal filtrée consommer tout le quota gratuit mensuel en un run. 5 Go est large pour une requête
# qui ne sélectionne que 5 colonnes sur une table par ailleurs très large (title_localized,
# abstract, claims... non lues ici) -- ajustable via la variable d'environnement du même nom.
DEFAULT_MAX_BYTES_BILLED = 5_000_000_000
MAX_BYTES_BILLED = int(os.getenv("GOOGLE_PATENTS_MAX_BYTES_BILLED", str(DEFAULT_MAX_BYTES_BILLED)))


def _is_configured() -> bool:
    return bool(GOOGLE_APPLICATION_CREDENTIALS and GOOGLE_CLOUD_PROJECT)


def _bigquery_available() -> bool:
    """`find_spec` plutôt qu'un `import` direct : vérifier la présence du paquet ne doit pas
    payer le coût (temps, mémoire) de le charger en entier quand la source n'est de toute façon
    pas utilisée -- le vrai `from google.cloud import bigquery` reste dans _run_query(), la
    seule fonction qui en a réellement besoin."""
    return importlib.util.find_spec("google.cloud.bigquery") is not None


def _query_text() -> str:
    # EXISTS(...UNNEST(cpc)...) plutôt qu'un JOIN sur cpc déplié : la table n'est jamais
    # aplatie en sortie, une ligne = un brevet, ce que le code Python en aval attend déjà
    # (même forme qu'un résultat patent.py/lens.py, une entrée = un document).
    return f"""
        SELECT publication_number, title_localized, publication_date, assignee_harmonized
        FROM `{PUBLICATIONS_TABLE}`
        WHERE publication_date >= @from_date
          AND EXISTS(SELECT 1 FROM UNNEST(cpc) AS c WHERE c.code LIKE @cpc_prefix)
        LIMIT @results_limit
    """


def _title(row) -> str | None:
    for entry in row.get("title_localized") or []:
        if entry.get("language") == "en" and entry.get("text"):
            return str(entry["text"]).strip()
    for entry in row.get("title_localized") or []:
        if entry.get("text"):
            return str(entry["text"]).strip()
    return None


def _assignee_names(row) -> list[str]:
    return [
        str(entry["name"]).strip()
        for entry in (row.get("assignee_harmonized") or [])
        if isinstance(entry, dict) and entry.get("name")
    ]


def _parse_row(row) -> dict | None:
    """Une ligne BigQuery -> {patent_number,title,published_at,applicants,url}, ou None si le
    numéro de publication est absent. Tolérant par construction -- voir l'avertissement de
    vérification en tête de module."""
    publication_number = row.get("publication_number")
    if not publication_number:
        return None
    published_raw = row.get("publication_date")
    published_at = None
    if published_raw and len(str(published_raw)) == 8:
        text = str(published_raw)
        published_at = f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return {
        "patent_number": str(publication_number),
        "title": _title(row) or str(publication_number),
        "published_at": published_at,
        "applicants": _assignee_names(row),
        # Format Google Patents ("US-9876543-B2" -> "US9876543B2") -- garanti de résoudre vers
        # la fiche publique, jamais un chemin deviné.
        "url": f"https://patents.google.com/patent/{str(publication_number).replace('-', '')}",
    }


def _upsert_google_patent(db, actor_name: str | None, doc: dict) -> tuple[int, int]:
    """Adaptateur : traduit un `doc` Google Patents en colonnes `documents`, delegue à
    db.upsert_document (partagée avec openalex.py/patent.py/lens.py). `doc["url"]` reste sur le
    domaine patents.google.com -- ce qui permet à app.py de compter les brevets apportés par
    CETTE source (voir _TECHNOLOGY_SOURCE_COUNTS, même mécanique que crossref/hal/arxiv : hôtes
    disjoints, jamais de colonne dédiée)."""
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


def _run_query(lookback_days: int):
    """Isolé dans sa propre fonction : c'est la seule partie qui importe google-cloud-bigquery
    et parle réellement au réseau, donc la seule que les tests ont besoin de patcher."""
    from datetime import date, timedelta

    from google.cloud import bigquery  # import paresseux -- voir docstring du module

    client = bigquery.Client(project=GOOGLE_CLOUD_PROJECT)
    from_date = int((date.today() - timedelta(days=max(1, lookback_days))).strftime("%Y%m%d"))
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("from_date", "INT64", from_date),
            bigquery.ScalarQueryParameter("cpc_prefix", "STRING", f"{CPC_CLASS}%"),
            bigquery.ScalarQueryParameter("results_limit", "INT64", RESULTS_LIMIT),
        ],
        maximum_bytes_billed=MAX_BYTES_BILLED,
    )
    return list(client.query(_query_text(), job_config=job_config).result())


def collect_google_patents(*, lookback_days: int = 365) -> dict:
    """Point d'entrée (voir app.py: collectors["google_patents"])."""
    if not _is_configured():
        return {
            "status": "not_configured",
            "message": "GOOGLE_APPLICATION_CREDENTIALS/GOOGLE_CLOUD_PROJECT absents -- projet "
                       "GCP avec facturation activée requis (1 To de lecture BigQuery gratuit/mois)",
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 0,
        }
    if not _bigquery_available():
        return {
            "status": "not_configured",
            "message": "Paquet google-cloud-bigquery non installé -- pip install -r requirements-google-patents.txt",
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 0,
        }

    with connect(ACTORS_DB) as db:
        known_names, _known_domains = _known_actor_names_and_domains(db)
        # Nom d'affichage par nom normalisé, pour attribuer documents.actor_name avec la
        # graphie suivie plutôt qu'avec le déposant tel qu'écrit par l'office de brevets.
        display_name = {
            _normalize_name(row["name"]): row["name"]
            for row in db.execute("SELECT name FROM actors WHERE active=1").fetchall()
        }

    actors_matched_names: set[str] = set()
    patents_added = patents_attributed = candidates_added = errors = 0

    try:
        rows = _run_query(lookback_days)
        documents = [doc for doc in (_parse_row(dict(row)) for row in rows) if doc]
    except Exception as exc:
        return {
            "status": "error", "message": str(exc)[:300],
            "actors_matched": 0, "patents_added": 0, "patents_attributed": 0,
            "topic_scoped_candidates_added": 0, "errors": 1,
        }

    with connect(TECH_DB) as tech_db, connect(ACTORS_DB) as actors_db:
        for doc in documents:
            # Un brevet peut avoir plusieurs déposants (co-dépôt) : le premier déposant SUIVI
            # trouvé porte l'attribution (documents.actor_name est une seule colonne, même
            # limite que patent.py/lens.py), les autres restent lisibles dans le déposant brut
            # si besoin d'y revenir plus tard.
            tracked = next(
                (name for name in doc["applicants"] if _normalize_name(name) in display_name),
                None,
            )
            actor_name = display_name.get(_normalize_name(tracked)) if tracked else None
            if actor_name:
                actors_matched_names.add(actor_name)

            added, attributed = _upsert_google_patent(tech_db, actor_name, doc)
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
                    context=f"Brevet CPC {CPC_CLASS} {doc['patent_number']} (Google Patents)",
                    source_url=doc["url"],
                )

    return {
        "status": "ok",
        "actors_matched": len(actors_matched_names),
        "patents_added": patents_added,
        "patents_attributed": patents_attributed,
        "topic_scoped_candidates_added": candidates_added,
        "errors": errors,
    }
