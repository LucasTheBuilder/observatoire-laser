"""arXiv (chantier /sources, 13/09/2026) : préprints physics.optics, signal plus précoce qu'une
publication indexée par Crossref/OpenAlex -- souvent plusieurs mois avant la version revue (même
logique que patent.py pour le brevet : "le signal avancé par excellence", ici appliquée à la
prépublication).

Module nommé ``arxiv_feed`` (pas ``arxiv``) pour ne jamais entrer en conflit avec le paquet PyPI
``arxiv`` si jamais installé un jour dans cet environnement.

RÉSERVE DE VÉRIFICATION (13/09/2026) : l'API Atom (export.arxiv.org/api/query) est publique,
gratuite, sans clé, documentée sans changement depuis des années (arxiv.org/help/api) -- mais
n'a PAS pu être confrontée à une vraie réponse au moment d'écrire ce module : l'IP partagée de cet
environnement recevait déjà "Rate exceeded." (HTTP 429) sur toute requête, y compris la toute
première, avant même un seul essai réussi. Même réserve que patent.py pour EPO OPS : le format
Atom ci-dessous (entry/title/summary/published/arxiv:doi) est celui documenté publiquement, pas
encore vérifié en direct -- à faire dès que le quota le permet, avant de considérer ce chantier
terminé (voir tests/test_arxiv_feed.py, qui documente cette réserve sur ses fixtures).

Topic-scoped, jamais actor-scoped : arXiv ne structure les affiliations qu'en texte libre dans
<author>, aucun identifiant d'institution fiable à matcher -- même choix que hal.py, pour la même
raison. Toute entrée rejoint ``unlinked_documents`` (règle de périmètre du 13/09/2026), promue
dans ``documents`` par openalex.resolve_unlinked_documents() si son ``arxiv:doi`` (renseigné
quand le préprint a depuis été publié en revue) référence une institution suivie.

Politesse arXiv (guideline officielle : pas plus d'une requête toutes les 3 secondes) --
ARXIV_REQUEST_DELAY_SECONDS respecte ce seuil entre deux des six requêtes TECHNOLOGY_QUERIES.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from db import TECH_DB, connect, upsert_unlinked_document
from http_client import connector_client
from lexicon import is_on_topic
from scrapers import TECHNOLOGY_QUERIES

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_RESULTS_PER_QUERY = 15
ARXIV_LOOKBACK_DAYS_DEFAULT = 60
ARXIV_REQUEST_DELAY_SECONDS = 3.0


def _entry_text(entry: ET.Element, tag: str) -> str:
    node = entry.find(f"atom:{tag}", ARXIV_NS)
    return " ".join((node.text or "").split()) if node is not None and node.text else ""


def _entry_url(entry: ET.Element) -> str:
    node = entry.find("atom:id", ARXIV_NS)
    return (node.text or "").strip() if node is not None and node.text else ""


def _entry_doi(entry: ET.Element) -> str | None:
    node = entry.find("arxiv:doi", ARXIV_NS)
    return node.text.strip() if node is not None and node.text else None


def _entry_published(entry: ET.Element) -> str | None:
    node = entry.find("atom:published", ARXIV_NS)
    if node is None or not node.text:
        return None
    return node.text[:10]  # "2026-04-30T11:21:54Z" -> "2026-04-30"


def collect_arxiv_preprints(
    lookback_days: int = ARXIV_LOOKBACK_DAYS_DEFAULT, limit_per_query: int = ARXIV_RESULTS_PER_QUERY,
) -> dict:
    """Point d'entrée (voir app.py: collectors["arxiv"])."""
    from_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    scanned = relevant = added = errors = 0
    seen_fingerprints: set[str] = set()

    try:
        with connector_client("slow") as client:
            for index, query in enumerate(TECHNOLOGY_QUERIES):
                if index > 0:
                    time.sleep(ARXIV_REQUEST_DELAY_SECONDS)
                try:
                    response = client.get(
                        ARXIV_API_URL,
                        params={
                            "search_query": f'abs:"{query}"',
                            "sortBy": "submittedDate",
                            "sortOrder": "descending",
                            "max_results": limit_per_query,
                        },
                    )
                    response.raise_for_status()
                    root = ET.fromstring(response.content)
                except Exception:
                    errors += 1
                    continue

                entries = root.findall("atom:entry", ARXIV_NS)
                scanned += len(entries)
                with connect(TECH_DB) as db:
                    for entry in entries:
                        published = _entry_published(entry)
                        if published and published < from_date:
                            continue  # arXiv trie déjà par date, mais ne borne pas le lookback lui-même
                        title = _entry_text(entry, "title")
                        summary = _entry_text(entry, "summary")
                        if not title or not is_on_topic(f"{title} {summary}"):
                            continue
                        relevant += 1
                        url = _entry_url(entry)
                        doi = _entry_doi(entry)
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
                            published_at=published,
                            abstract=summary or None,
                        )
    except Exception as exc:
        return {
            "error": str(exc)[:300], "scanned": scanned, "relevant": relevant, "added": added, "errors": errors,
        }

    return {"scanned": scanned, "relevant": relevant, "added": added, "errors": errors}
