"""Rétro-datation via Wayback Machine CDX (§5.E.2 audit veille, 30/08/2026, Lot 4 §18) :
"comparer le plus ancien snapshot contenant un terme au premier ne le contenant pas date
l'apparition d'une offre à quelques mois près, rétroactivement -- le seul moyen d'avoir deux ans
de profondeur historique sans attendre deux ans."

L'API CDX (web.archive.org/cdx/search/cdx) est publique, gratuite, sans clé -- vérifiée en direct
avant d'écrire ce module, y compris le format de récupération "brut" d'un snapshot
(``https://web.archive.org/web/{timestamp}id_/{url}``, suffixe ``id_`` : contenu original, sans
la barre d'outils/réécriture de liens que Wayback ajoute par défaut).

Cible : les citations déjà en base (``evidence_sources.quote``, jamais reformulées) marquées
``is_verbatim=1`` -- rétro-dater une citation paraphrasée ou générée par l'IA n'aurait aucun sens,
il n'y a rien à chercher mot pour mot dans un ancien snapshot. Recherche par BISECTION sur la
liste de snapshots (dédupliquée par ``digest``, donc une seule requête par version de contenu
réellement distincte) plutôt que de tout télécharger : O(log n) requêtes au lieu de O(n).

``first_appeared_at`` reste NULL tant qu'aucune correspondance n'est trouvée -- même dans le
snapshot le plus récent archivé (la page a pu changer depuis, ou Wayback n'a jamais archivé cette
page) : jamais deviné, jamais une date fabriquée à partir de ``created_at``.
"""

from __future__ import annotations

import re

import httpx

from db import MARKET_DB, connect

CDX_API = "http://web.archive.org/cdx/search/cdx"
WAYBACK_RAW_URL = "https://web.archive.org/web/{timestamp}id_/{url}"
WAYBACK_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRODATING_BATCH_SIZE_DEFAULT = 20
# Une citation complète (jusqu'à ~900 caractères ailleurs dans ce projet) risque de tomber sur
# une balise HTML/un retour à la ligne qui la coupe en plein milieu dans un vieux snapshot --
# un préfixe plus court, mais encore spécifique, résiste mieux à une mise en page différente.
SEARCH_TERM_MAX_CHARS = 60

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_html_text(html: str) -> str:
    """Retire les balises et aplatit les espaces -- pour comparer du texte de citation à du HTML
    d'archive sans que la mise en page (retours à la ligne, balises inline) ne fasse échouer une
    correspondance par ailleurs réelle."""
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", html)).strip().upper()


def _search_term(quote: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", quote).strip()
    return normalized[:SEARCH_TERM_MAX_CHARS].upper()


def _fetch_snapshots(client: httpx.Client, url: str) -> list[tuple[str, str]]:
    """Liste des snapshots HTTP 200 de `url`, un par version de contenu distincte
    (``collapse=digest``), du plus ancien au plus récent -- ordre natif de l'API CDX."""
    response = client.get(
        CDX_API,
        params={"url": url, "output": "json", "collapse": "digest", "filter": "statuscode:200", "limit": 300},
    )
    response.raise_for_status()
    rows = response.json()
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    ts_idx, orig_idx = header.index("timestamp"), header.index("original")
    return [(row[ts_idx], row[orig_idx]) for row in rows[1:]]


def _snapshot_contains_term(client: httpx.Client, timestamp: str, original_url: str, term_upper: str) -> bool | None:
    """None sur échec de récupération (jamais interprété comme "absent")."""
    try:
        response = client.get(WAYBACK_RAW_URL.format(timestamp=timestamp, url=original_url), follow_redirects=True)
        response.raise_for_status()
    except Exception:
        return None
    return term_upper in _normalize_html_text(response.text)


def _find_first_appearance(client: httpx.Client, snapshots: list[tuple[str, str]], term_upper: str) -> str | None:
    """Bisection : `snapshots` est trié du plus ancien au plus récent. Renvoie le timestamp du
    plus ancien snapshot contenant `term_upper`, ou None si le terme n'apparaît dans aucun
    snapshot récupérable (y compris si chaque requête a échoué)."""
    lo, hi = 0, len(snapshots) - 1
    newest_has_term = _snapshot_contains_term(client, *snapshots[hi], term_upper)
    if not newest_has_term:
        return None  # absent même du plus récent snapshot -- rien à dater
    oldest_has_term = _snapshot_contains_term(client, *snapshots[lo], term_upper)
    if oldest_has_term:
        return snapshots[lo][0]  # déjà présent dès le premier snapshot archivé
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if _snapshot_contains_term(client, *snapshots[mid], term_upper):
            hi = mid
        else:
            lo = mid
    return snapshots[hi][0]


def retrodate_evidence_sources(*, limit: int = RETRODATING_BATCH_SIZE_DEFAULT) -> dict:
    """Point d'entrée (voir app.py: collectors["wayback_retrodating"]).

    Traite au plus `limit` citations verbatim pas encore rétro-datées par run -- une bisection
    par citation fait plusieurs requêtes à web.archive.org, mieux vaut un run modeste et régulier
    qu'un seul run massif qui se ferait limiter (rate limit) en cours de route.
    """
    with connect(MARKET_DB) as db:
        candidates = db.execute(
            """SELECT id,source_url,quote FROM evidence_sources
               WHERE is_verbatim=1 AND first_appeared_at IS NULL
               ORDER BY id LIMIT ?""",
            (limit,),
        ).fetchall()

    scanned = dated = no_snapshots = not_found = errors = 0
    with httpx.Client(timeout=WAYBACK_TIMEOUT) as client, connect(MARKET_DB) as db:
        for row in candidates:
            scanned += 1
            term_upper = _search_term(row["quote"])
            if not term_upper:
                continue
            try:
                snapshots = _fetch_snapshots(client, row["source_url"])
            except Exception:
                errors += 1
                continue
            if not snapshots:
                no_snapshots += 1
                continue
            try:
                timestamp = _find_first_appearance(client, snapshots, term_upper)
            except Exception:
                errors += 1
                continue
            if not timestamp:
                not_found += 1
                continue
            snapshot_url = WAYBACK_RAW_URL.format(timestamp=timestamp, url=row["source_url"])
            first_appeared_at = f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            db.execute(
                "UPDATE evidence_sources SET first_appeared_at=?,first_appeared_snapshot_url=? WHERE id=?",
                (first_appeared_at, snapshot_url, row["id"]),
            )
            dated += 1

    return {
        "scanned": scanned, "dated": dated, "no_snapshots": no_snapshots,
        "not_found": not_found, "errors": errors,
    }
