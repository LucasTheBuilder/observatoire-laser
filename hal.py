"""HAL (chantier /sources, 13/09/2026) : archive ouverte française, complément topique à
scrapers.scrape_technology() (Crossref) -- pertinent ici car le roster suivi est très
franco-allemand (ALPHANOV, IREPA LASER, FEMTO Engineering, Manutech-USD...), et beaucoup de
laboratoires français y déposent avant/à côté d'une revue indexée par Crossref/OpenAlex.

Vérifié en direct contre api.archives-ouvertes.fr avant d'écrire ce module (13/09/2026) : API
Solr/JSON publique, sans clé, sans authentification. Requête réelle (``q="femtosecond laser
micromachining"``, ``fq=submittedDate_s:[...]``, ``sort=submittedDate_s desc``) confirmée en
production -- champs ``title_s``/``en_abstract_s``/``abstract_s``/``doiId_s``/``uri_s``/
``submittedDate_s`` tous présents sur de vraies réponses.

Topic-scoped, jamais actor-scoped : HAL ne renvoie qu'un sigle de laboratoire
(``structAcronym_s``/``labStructAcronym_s``), le même risque de faux positif par acronyme déjà
documenté par gleif.py/firmographics.py pour un matching par nom -- donc aucune tentative
d'attribution directe à un acteur suivi ici, même discipline. Toute ligne rejoint
``unlinked_documents`` (règle de périmètre du 13/09/2026, voir scrapers.scrape_technology : "le
corpus ne contient que ce qu'un acteur suivi signe"), et se voit promue dans ``documents`` par
openalex.resolve_unlinked_documents() si son DOI référence une institution suivie -- exactement
le chemin déjà emprunté par la collecte thématique Crossref.
"""

from __future__ import annotations

from datetime import date, timedelta

from db import TECH_DB, connect, upsert_unlinked_document
from http_client import connector_client
from lexicon import is_on_topic
from scrapers import TECHNOLOGY_QUERIES

HAL_SEARCH_URL = "https://api.archives-ouvertes.fr/search/"
HAL_FIELDS = "title_s,en_abstract_s,abstract_s,doiId_s,uri_s,submittedDate_s"
HAL_ROWS_PER_QUERY = 20
HAL_LOOKBACK_DAYS_DEFAULT = 60


def _hal_title(doc: dict) -> str:
    titles = doc.get("title_s") or []
    return titles[0].strip() if titles else ""


def _hal_abstract(doc: dict) -> str:
    for key in ("en_abstract_s", "abstract_s"):
        values = doc.get(key) or []
        if values and values[0]:
            return values[0].strip()
    return ""


def collect_hal_publications(
    lookback_days: int = HAL_LOOKBACK_DAYS_DEFAULT, limit_per_query: int = HAL_ROWS_PER_QUERY,
) -> dict:
    """Point d'entrée (voir app.py: collectors["hal"])."""
    from_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    scanned = relevant = added = errors = 0
    seen_fingerprints: set[str] = set()

    try:
        with connector_client("api") as client:
            for query in TECHNOLOGY_QUERIES:
                try:
                    response = client.get(
                        HAL_SEARCH_URL,
                        params={
                            "q": f'"{query}"',
                            "fq": f"submittedDate_s:[{from_date}T00:00:00Z TO NOW]",
                            "sort": "submittedDate_s desc",
                            "rows": limit_per_query,
                            "wt": "json",
                            "fl": HAL_FIELDS,
                        },
                    )
                    response.raise_for_status()
                    docs = ((response.json() or {}).get("response") or {}).get("docs") or []
                except Exception:
                    errors += 1
                    continue

                scanned += len(docs)
                with connect(TECH_DB) as db:
                    for doc in docs:
                        title = _hal_title(doc)
                        abstract = _hal_abstract(doc)
                        if not title or not is_on_topic(f"{title} {abstract}"):
                            continue
                        relevant += 1
                        doi = doc.get("doiId_s") or None
                        url = doc.get("uri_s") or ""
                        fingerprint_source = doi or url
                        if not fingerprint_source or fingerprint_source in seen_fingerprints:
                            continue
                        seen_fingerprints.add(fingerprint_source)
                        added += upsert_unlinked_document(
                            db,
                            title=title,
                            source_url=url,
                            fingerprint_source=fingerprint_source,
                            doi=doi,
                            published_at=(doc.get("submittedDate_s") or "")[:10] or None,
                            abstract=abstract or None,
                        )
    except Exception as exc:
        return {
            "error": str(exc)[:300], "scanned": scanned, "relevant": relevant, "added": added, "errors": errors,
        }

    return {"scanned": scanned, "relevant": relevant, "added": added, "errors": errors}
