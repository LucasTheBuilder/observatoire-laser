"""Presse spécialisée (chantier 3 de l'audit) : flux RSS de la presse photonique, pour capter
des événements datés (annonces produit, contrats, partenariats...) que CORDIS/OpenAlex/le
registre d'entreprises ne couvrent pas -- ce sont des sources structurées, la presse ne l'est
pas.

Portée volontairement modeste : sur les quatre titres cités par l'audit (Optics.org,
Photonics.com, Laser Focus World, PhotonicsViews), seuls DEUX exposent un flux RSS réel et
vérifiable (voir FEED_URLS, vérifiés manuellement avant d'écrire ce module) :
  - Laser Focus World : lien <link rel="alternate" type="application/rss+xml"> découvert sur
    sa page d'accueil.
  - Photonics.com (Photonics Spectra) : /rss.aspx, découvert par essai direct.
Optics.org n'expose aucun flux RSS découvrable (pas de <link> sur la page d'accueil, aucun
chemin conventionnel ne répond, robots.txt absent) ; PhotonicsViews n'a pas de domaine propre
identifié avec certitude. Plutôt que deviner une URL de flux qui n'existe peut-être pas, ces
deux titres sont omis -- à ajouter si une URL de flux réelle est un jour identifiée.

Différence de traitement avec CORDIS/OpenAlex : une mention d'acteur dans un article de presse
est un simple mot-clé retrouvé dans un titre/résumé, un signal bien plus faible qu'une ligne de
registre officiel ou une affiliation de publication scientifique. Chaque événement créé ici
entre donc avec ``review_status='pending'`` -- jamais publié tel quel sans relecture humaine.
Les noms d'acteurs trop courts/génériques (voir MIN_ACTOR_NAME_LENGTH) sont exclus du matching
pour éviter un faux positif sur un mot ordinaire dans un titre d'article.
"""

from __future__ import annotations

import re
import unicodedata
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from db import ACTORS_DB, connect, utc_now
from scrapers import HEADERS

FEED_URLS: dict[str, str] = {
    "Laser Focus World": "https://www.laserfocusworld.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D",
    "Photonics Spectra": "https://www.photonics.com/rss.aspx",
}
FEED_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
# Un nom d'acteur plus court que ça (une fois normalisé) est trop générique pour être cherché
# sans risque dans du texte libre (titre/résumé d'article) -- même logique que
# cordis.MIN_ALIAS_LENGTH, seuil relevé ici car la prose est plus permissive qu'un nom
# d'organisation en base de registre.
MIN_ACTOR_NAME_LENGTH = 5
MAX_ITEMS_PER_FEED = 60


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def _contains_whole_word(haystack: str, phrase: str) -> bool:
    if not phrase:
        return False
    return re.search(rf"(?<![A-Z0-9]){re.escape(phrase)}(?![A-Z0-9])", haystack) is not None


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def _parse_feed(content: bytes) -> list[dict[str, str | None]]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []
    items: list[dict[str, str | None]] = []
    for item in root.iter("item"):
        title = _strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        description = _strip_html(item.findtext("description") or "")
        if not title or not link:
            continue
        event_date = None
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        if pub_date_raw:
            try:
                event_date = parsedate_to_datetime(pub_date_raw).date().isoformat()
            except (TypeError, ValueError, OverflowError):
                event_date = None
        items.append({"title": title, "link": link, "description": description, "event_date": event_date})
    return items[:MAX_ITEMS_PER_FEED]


def _upsert_press_event(db, actor_id: int, description: str, event_date: str | None, source_url: str) -> int:
    """Dédoublonne sur (actor_id, source_url) : le lien d'un article est stable, un run répété
    ne recrée jamais le même événement -- même principe que cordis._upsert_actor_event."""
    existing = db.execute(
        "SELECT id FROM actor_events WHERE actor_id=? AND source_url=?", (actor_id, source_url),
    ).fetchone()
    if existing:
        return 0
    db.execute(
        """INSERT INTO actor_events(actor_id,event_type,description,event_date,source_url,review_status,created_at)
           VALUES(?,?,?,?,?,'pending',?)""",
        (actor_id, "press_mention", description[:500], event_date, source_url, utc_now()),
    )
    return 1


def collect_press_mentions() -> dict:
    """Point d'entrée (voir app.py: collectors["press"])."""
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT id,name FROM actors WHERE active=1").fetchall()]
    aliases = {actor["name"]: _normalize(actor["name"]) for actor in actors}
    aliases = {name: alias for name, alias in aliases.items() if len(alias) >= MIN_ACTOR_NAME_LENGTH}
    actor_ids = {actor["name"]: actor["id"] for actor in actors}

    feeds_ok = events_added = errors = 0
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=FEED_TIMEOUT) as client:
        for feed_name, feed_url in FEED_URLS.items():
            try:
                response = client.get(feed_url)
                response.raise_for_status()
                items = _parse_feed(response.content)
            except Exception:
                errors += 1
                continue
            feeds_ok += 1
            with connect(ACTORS_DB) as db:
                for item in items:
                    haystack = _normalize(f"{item['title']} {item['description']}")
                    for actor_name, alias in aliases.items():
                        if not _contains_whole_word(haystack, alias):
                            continue
                        description = f"Mention presse ({feed_name}) : {item['title']}"
                        events_added += _upsert_press_event(
                            db, actor_ids[actor_name], description, item["event_date"], str(item["link"]),
                        )

    return {
        "feeds_configured": len(FEED_URLS),
        "feeds_ok": feeds_ok,
        "events_added": events_added,
        "errors": errors,
    }
