"""Signaux de demande via appels d'offres publics (§4.B.3 audit veille, 30/08/2026, Lot 4 §15) :
"un cahier des charges mentionnant du micro-usinage femtoseconde est un signal d'achat, pas un
signal de discours." Sort la dimension marché du miroir de l'offre : jusqu'ici `market.db` ne
documentait que ce que les acteurs suivis DISENT faire (offers/evidence), jamais ce que le marché
ACHÈTE réellement.

Deux sources, toutes deux vérifiées en direct (curl) avant d'écrire ce module -- jamais assumées
depuis la seule documentation :

- **TED** (Tenders Electronic Daily, UE) : POST api.ted.europa.eu/v3/notices/search, requête en
  syntaxe Lucene-like (``FT~"phrase" AND PD>=YYYYMMDD``), accès anonyme, sans clé.
- **BOAMP** (France) : GET boamp-datadila.opendatasoft.com/api/**explore/v2.0** -- PAS
  ``/api/records/1.0/search/``, un ancien mirroir OpenDataSoft v1 dont les données se sont
  révélées figées vers 2015/2022 en vérifiant (triées "plus récent d'abord", la date la plus
  récente restait 2015) : piège trouvé puis écarté avant d'écrire ce module. Requête ODSQL
  (``where=search(objet,"phrase")``), accès anonyme, sans clé.

Réutilise ``scrapers.TECHNOLOGY_QUERIES`` (déjà vérifiées pour Crossref/OpenAlex) plutôt que
d'inventer de nouveaux termes de recherche, et ``scrapers.is_on_topic()`` en filtre final -- une
recherche plein texte sur un intitulé d'avis peut matcher des tokens sans rapport.

Hors scope, volontairement : la seconde moitié du §4.B.3, "offres d'emploi des donneurs d'ordre".
Aucune API publique fiable identifiée, et "donneur d'ordre" (client final, pas concurrent) n'est
même pas un type d'entité qui existe dans ce schéma aujourd'hui -- l'ajouter correctement
demanderait de définir cette notion d'abord, pas juste brancher une source de plus.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import httpx

from db import MARKET_DB, connect, utc_now
from scrapers import HEADERS, TECHNOLOGY_QUERIES, is_on_topic

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
BOAMP_SEARCH_URL = "https://boamp-datadila.opendatasoft.com/api/explore/v2.0/catalog/datasets/boamp/records"
DEMAND_SIGNALS_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEMAND_SIGNALS_LOOKBACK_DAYS_DEFAULT = 180
RESULTS_PER_QUERY = 10


def _ted_notice_title(notice: dict) -> str:
    titles = notice.get("notice-title") or {}
    return titles.get("eng") or next(iter(titles.values()), "") or ""


def _ted_buyer_name(notice: dict) -> str | None:
    buyers = notice.get("buyer-name") or {}
    for names in buyers.values():
        if names:
            return str(names[0])
    return None


def _ted_notice_url(notice: dict, number: str) -> str:
    html_links = ((notice.get("links") or {}).get("html")) or {}
    return html_links.get("ENG") or next(iter(html_links.values()), None) or f"https://ted.europa.eu/en/notice/-/detail/{number}"


def _parse_ted_notice(notice: dict) -> dict[str, Any] | None:
    number = notice.get("publication-number")
    if not number:
        return None
    published_at = (notice.get("publication-date") or "")[:10] or None
    return {
        "source": "TED", "external_id": number, "title": _ted_notice_title(notice),
        "buyer_name": _ted_buyer_name(notice), "published_at": published_at,
        "url": _ted_notice_url(notice, number),
    }


def _fetch_ted_notices(client: httpx.Client, query: str, from_date_yyyymmdd: str) -> list[dict]:
    payload = {
        "query": f'FT~"{query}" AND PD>={from_date_yyyymmdd}',
        "fields": ["publication-number", "notice-title", "publication-date", "buyer-name", "links"],
        "limit": RESULTS_PER_QUERY,
        "scope": "ALL",
    }
    response = client.post(TED_SEARCH_URL, json=payload)
    response.raise_for_status()
    return response.json().get("notices") or []


def _parse_boamp_record(fields: dict) -> dict[str, Any] | None:
    idweb = fields.get("idweb")
    if not idweb:
        return None
    return {
        "source": "BOAMP", "external_id": idweb, "title": fields.get("objet") or "",
        "buyer_name": fields.get("nomacheteur"), "published_at": fields.get("dateparution"),
        "url": fields.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:{idweb}",
    }


def _fetch_boamp_records(client: httpx.Client, query: str, from_date_iso: str) -> list[dict]:
    escaped_query = query.replace('"', "")
    params = {
        "where": f'search(objet, "{escaped_query}") and dateparution >= date\'{from_date_iso}\'',
        "order_by": "dateparution desc",
        "limit": str(RESULTS_PER_QUERY),
    }
    response = client.get(BOAMP_SEARCH_URL, params=params)
    response.raise_for_status()
    return [row["record"]["fields"] for row in response.json().get("records") or []]


def _upsert_demand_signal(db, signal_type: str, item: dict) -> int:
    fingerprint = hashlib.sha256(f"{item['source']}|{item['external_id']}".encode()).hexdigest()
    stamp = utc_now()
    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO demand_signals(
               signal_type,source,buyer_name,title,published_at,source_url,fingerprint,created_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            signal_type, item["source"], item["buyer_name"], item["title"][:500],
            item["published_at"], item["url"], fingerprint, stamp, stamp,
        ),
    )
    inserted = int(db.total_changes > before)
    if not inserted:
        db.execute("UPDATE demand_signals SET last_seen_at=? WHERE fingerprint=?", (stamp, fingerprint))
    return inserted


def collect_demand_signals(*, lookback_days: int = DEMAND_SIGNALS_LOOKBACK_DAYS_DEFAULT) -> dict:
    """Point d'entrée (voir app.py: collectors["demand_signals"])."""
    from_date = date.today() - timedelta(days=max(1, lookback_days))
    from_date_yyyymmdd = from_date.strftime("%Y%m%d")
    from_date_iso = from_date.isoformat()

    ted_scanned = ted_added = boamp_scanned = boamp_added = errors = 0
    with httpx.Client(headers=HEADERS, timeout=DEMAND_SIGNALS_TIMEOUT) as client, connect(MARKET_DB) as db:
        for query in TECHNOLOGY_QUERIES:
            try:
                notices = _fetch_ted_notices(client, query, from_date_yyyymmdd)
            except Exception:
                errors += 1
            else:
                for notice in notices:
                    item = _parse_ted_notice(notice)
                    if not item or not is_on_topic(item["title"]):
                        continue
                    ted_scanned += 1
                    ted_added += _upsert_demand_signal(db, "tender", item)

            try:
                records = _fetch_boamp_records(client, query, from_date_iso)
            except Exception:
                errors += 1
            else:
                for fields in records:
                    item = _parse_boamp_record(fields)
                    if not item or not is_on_topic(item["title"]):
                        continue
                    boamp_scanned += 1
                    boamp_added += _upsert_demand_signal(db, "tender", item)

    return {
        "ted_scanned": ted_scanned, "ted_added": ted_added,
        "boamp_scanned": boamp_scanned, "boamp_added": boamp_added,
        "errors": errors,
    }
