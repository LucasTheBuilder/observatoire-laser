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

Near-duplicate (revue web-scraping, priorité #5) : le dédoublonnage sur (actor_id, source_url)
suffit pour les pages acteurs (une URL = une page), mais pas ici -- un même communiqué repris
par Laser Focus World ET Photonics Spectra a deux source_url distinctes pour le même événement
réel. _is_near_duplicate() compare le nouveau titre aux titres déjà en base pour cet acteur par
similarité de Jaccard sur des trigrammes de mots (résistant à une reformulation légère entre
deux médias, contrairement à une comparaison de chaînes exactes) ; au-dessus du seuil, l'item
est considéré comme déjà couvert et n'est pas réinséré.
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


# Chantier Horizon 2 #13 (audit) : "Ajouter brevets, recrutements et investissements comme
# signaux structurés" -- jusqu'ici chaque mention de presse entrait sous le même event_type
# générique 'press_mention', indifférenciée qu'il s'agisse d'un brevet déposé, d'une levée de
# fonds ou d'un simple article. Classification déterministe par mots-clés (même logique que
# scrapers.MATURITY_RULES) plutôt qu'un appel IA : ni le titre ni le résumé RSS ne sont assez
# longs pour justifier le coût/latence d'un LLM, et un faux négatif retombe simplement en
# 'press_mention' générique (un signal moins précisément typé, pas un signal perdu). Ordre de
# priorité explicite : brevet et investissement sont testés avant recrutement, le faux-positif
# le plus fréquent (une ligne "nous recrutons" mentionnée en passant dans un article sur autre
# chose ne doit pas éclipser un signal de dépôt de brevet ou de levée de fonds plus rare et plus
# significatif dans le même article).
SIGNAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "patent": ("brevet", "patent", "intellectual property filing"),
    "investment": (
        "levee de fonds", "leve des fonds", "tour de table", "investissement", "financement serie",
        "series a", "series b", "series c", "funding round", "raises usd", "raises eur", "acquiert",
        "acquisition", "rachat de", "rachete", "capital risque", "venture capital",
    ),
    "recruitment": (
        "recrute", "recrutement", "embauche", "poste a pourvoir", "offre d emploi", "is hiring",
        "we are hiring", "job opening", "career opportunit",
    ),
}
SIGNAL_EVENT_LABELS = {"patent": "Brevet", "investment": "Investissement", "recruitment": "Recrutement", "press_mention": "Mention presse"}


# Near-duplicate (priorité #5) : trigrammes de mots + Jaccard, pas d'égalité de chaîne exacte
# (voir le docstring du module). Le seuil est un choix éditorial documenté, pas une mesure.
# 0.45 est calibré sur un exemple réaliste : "ALPHANOV unveils new femtosecond laser platform"
# vs "ALPHANOV unveils new femtosecond laser production platform" (un second média insère un
# seul mot) donne un Jaccard trigramme de 0.5 -- juste au-dessus. Deux articles réellement
# distincts sur le même acteur (ex: une annonce produit vs une nomination) partagent 0 trigramme
# en pratique, donc une marge confortable reste sous ce seuil sans avoir besoin de le pousser plus haut.
NEAR_DUPLICATE_SHINGLE_SIZE = 3
NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.45


def _word_shingles(text: str, size: int = NEAR_DUPLICATE_SHINGLE_SIZE) -> set[tuple[str, ...]]:
    words = _normalize(text).split()
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _title_from_description(description: str) -> str:
    """Isole le titre d'origine dans une description déjà stockée -- toujours de la forme
    "Label (Feed) : titre" (voir collect_press_mentions), donc le premier " : " sépare
    fiablement le préfixe du titre, quel que soit le contenu du titre lui-même."""
    _, _, title = description.partition(" : ")
    return title or description


def _is_near_duplicate(title: str, existing_descriptions: list[str]) -> bool:
    """True si `title` recoupe suffisamment un événement déjà stocké pour ce même acteur --
    même communiqué repris par un second média, que le dédoublonnage exact sur source_url ne
    peut pas voir puisque le lien diffère."""
    shingles = _word_shingles(title)
    if not shingles:
        return False
    return any(
        _jaccard(shingles, _word_shingles(_title_from_description(existing))) >= NEAR_DUPLICATE_JACCARD_THRESHOLD
        for existing in existing_descriptions
    )


def classify_press_event(title: str, description: str) -> str:
    """'patent'/'investment'/'recruitment' si un mot-clé structurant est détecté dans le titre
    + résumé (dans cet ordre de priorité), sinon 'press_mention' (comportement identique à
    avant ce chantier)."""
    haystack = _normalize(f"{title} {description}")
    for category, keywords in SIGNAL_KEYWORDS.items():
        if any(_normalize(keyword) in haystack for keyword in keywords):
            return category
    return "press_mention"


def _upsert_press_event(db, actor_id: int, event_type: str, description: str, event_date: str | None, source_url: str) -> int:
    """Dédoublonne sur (actor_id, source_url) : le lien d'un article est stable, un run répété
    ne recrée jamais le même événement -- même principe que cordis._upsert_actor_event. Une
    reclassification (event_type a changé depuis la dernière collecte, ex: le mot-clé ajouté
    plus tard) met à jour la ligne existante plutôt que de la laisser figée sur son ancien
    type -- le lien reste la clé de dédoublonnage, le type peut s'affiner."""
    existing = db.execute(
        "SELECT id,event_type FROM actor_events WHERE actor_id=? AND source_url=?", (actor_id, source_url),
    ).fetchone()
    if existing:
        if existing["event_type"] != event_type:
            db.execute("UPDATE actor_events SET event_type=? WHERE id=?", (event_type, existing["id"]))
        return 0
    db.execute(
        """INSERT INTO actor_events(actor_id,event_type,description,event_date,source_url,review_status,created_at)
           VALUES(?,?,?,?,?,'pending',?)""",
        (actor_id, event_type, description[:500], event_date, source_url, utc_now()),
    )
    return 1


def collect_press_mentions() -> dict:
    """Point d'entrée (voir app.py: collectors["press"])."""
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT id,name FROM actors WHERE active=1").fetchall()]
    aliases = {actor["name"]: _normalize(actor["name"]) for actor in actors}
    aliases = {name: alias for name, alias in aliases.items() if len(alias) >= MIN_ACTOR_NAME_LENGTH}
    actor_ids = {actor["name"]: actor["id"] for actor in actors}

    feeds_ok = events_added = near_duplicates_skipped = errors = 0
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
                    title = item["title"] or ""
                    description_text = item["description"] or ""
                    haystack = _normalize(f"{title} {description_text}")
                    signal_type = classify_press_event(title, description_text)
                    for actor_name, alias in aliases.items():
                        if not _contains_whole_word(haystack, alias):
                            continue
                        actor_id = actor_ids[actor_name]
                        existing_descriptions = [
                            row["description"] for row in db.execute(
                                "SELECT description FROM actor_events WHERE actor_id=? AND source_url!=?",
                                (actor_id, str(item["link"])),
                            ).fetchall()
                        ]
                        if _is_near_duplicate(title, existing_descriptions):
                            near_duplicates_skipped += 1
                            continue
                        label = SIGNAL_EVENT_LABELS[signal_type]
                        description = f"{label} ({feed_name}) : {title}"
                        events_added += _upsert_press_event(
                            db, actor_id, signal_type, description, item["event_date"], str(item["link"]),
                        )

    return {
        "feeds_configured": len(FEED_URLS),
        "feeds_ok": feeds_ok,
        "events_added": events_added,
        "near_duplicates_skipped": near_duplicates_skipped,
        "errors": errors,
    }
