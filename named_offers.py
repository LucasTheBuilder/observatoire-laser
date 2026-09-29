"""Offres nommées : ce qu'un acteur VEND, sous le nom qu'il lui donne (« USP laser
processing », « Laser fine cutting », « LightFab 3D Printer », « MM Series Compact Laser
Micromachining Platform »...), par opposition à `offers`, qui réduit chaque capacité à une
opération d'un lexique fermé d'une quinzaine de termes (voir scrapers._offer_candidates).

Audit de la page Offres & capacités (29/09/2026) : sur 4 acteurs comparés à leur site, la page
montrait « Ablation, Polissage » pour KMLT qui annonce 6 lignes de service, et rien du tout pour
LightFab ni OpTek. Le lexique d'opérations ne peut pas représenter un produit, une ligne de
prestation ou un parc machine, quel que soit le volume crawlé.

Aucune collecte web, aucune IA -- même principe que capabilities.py : relit les blocs déjà en
cache (actor_sources.blocks_json) des pages service/product/capability/equipment du PROPRE
domaine de l'acteur. Deux sources de noms, toutes deux verbatim :

1. Le H1 de la page (à défaut son titre, sans le suffixe « | Société ») : une page service ou
   produit porte le nom de ce qu'elle vend.
2. Un intertitre de bloc, seulement s'il nomme lui-même une opération/un procédé du lexique
   (« Laser Drilling », « Scribing & Dicing ») ou un modèle (« MM-500 »). Mesuré sur les blocs
   en cache : les autres intertitres sont des questions de FAQ, des arguments (« Advantages »,
   « Our strengths »), des secteurs (« Life Sciences », « Medtech ») ou des appels à l'action.

La description est la première phrase du bloc, citée telle quelle. `review_status` vaut
'accepted' seulement si (a) la page mentionne le périmètre ultra-rapide (lexicon._laser_match)
et (b) le nom lui-même est reconnaissable comme une offre (terme laser, nom de produit, opération
ou procédé du lexique, code de modèle) ; sinon 'review' -- un H1 comme « Customized solutions »,
ou la page « Laser Fine Cutting » d'un acteur qui n'y parle pas d'ultra-court, reste en base pour
relecture, jamais affiché d'office.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from db import ACTORS_DB, MARKET_DB, connect, utc_now
from lexicon import OPERATIONS, TECHNOLOGY_AXES, _laser_match, _normalize_text, match_all_labels

NAMED_OFFER_PAGE_TYPES = ("service", "product", "capability", "equipment")

# Intertitres/H1 qui ne nomment jamais une offre -- relevés sur les blocs en cache le 29/09/2026.
GENERIC_NAMES = frozenset({
    "advantages", "benefits", "our strengths", "strengths", "features", "key features", "overview",
    "introduction", "download", "downloads", "contact", "contact us", "request a quote", "get a quote",
    "speak to our experts", "read more", "learn more", "news", "about", "about us", "impressions",
    "how it works", "result", "results", "industry solutions", "industries", "applications",
    "application", "product diversity", "services", "our services", "service", "products",
    "our products", "product", "solutions", "our solutions", "capabilities", "our capabilities",
    "equipment", "technology", "technologies", "technical data", "specifications", "faq",
    "references", "quality", "quality management", "home", "welcome", "vorteile", "leistungen",
    "produkte", "kontakt", "anwendungen", "avantages", "prestations", "produits", "nos services",
    "article", "articles", "company", "unternehmen", "options", "facilities", "ausstattung",
    "systems", "complete systems", "our systems", "machines", "our machines",
})
QUESTION_OPENERS = ("what ", "how ", "why ", "which ", "where ", "when ", "who ", "can ", "do ", "does ", "is ", "are ")
# Code de modèle (« MM-500 »...) -- sauf les normes : « ISO 13485 » n'est pas un produit.
MODEL_CODE = re.compile(r"\b(?!(?:ISO|EN|DIN|IEC|ASTM|AS)\b)[A-Z]{1,5}[- ]?\d{2,5}[A-Z]?\b")
# Un produit se reconnaît aussi à son nom commun ou à sa marque déposée (« GL.evo laser
# machine », « LightFab 3D Printer », « Laser 4.0® »), relevés en relecture le 29/09/2026.
PRODUCT_NOUN = re.compile(r"\b(?:lasers?|machines?|systems?|printer|platform|workstation|series|station)\b|[®™]", re.I)
# Un intertitre plus long est un slogan (« Perfect markings thanks to laser marking and laser
# engraving »), pas le nom d'une prestation -- relevé sur les blocs KMLT.
MAX_SECTION_WORDS = 7
LISTICLE = re.compile(r"^\d+\s+(?:benefits|reasons|tips|ways|things|facts)\b", re.I)
# Documents, contenus éditoriaux et accroches, relevés dans les 149 premières offres acceptées
# (« BROCHURE LASER », « Fine laser cutting: OUR STATISTICS », « Glass laser cutting examples »,
# « Lasertec : histoire d'une entreprise », « Unlock Innovation with... », « Centre technologique »).
NOT_AN_OFFER = re.compile(
    r"\b(?:brochure|flyer|datasheet|statistics|examples?|histoire|history|more about|unlock|"
    r"centre technologique|technology cent(?:er|re))\b",
    re.I,
)
TITLE_SEPARATORS = re.compile(r"\s+[|–—-]\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s")
MAX_NAME_CHARS = 80
MAX_NAME_WORDS = 10
MAX_DESCRIPTION_CHARS = 320


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _first_party(url: str, official_url: str) -> bool:
    official, cited = _host(official_url), _host(url)
    return bool(official) and (cited == official or cited.endswith("." + official))


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t-–—|:")


def _page_name(title: str | None, h1: str | None, actor_name: str) -> str:
    """H1 d'abord ; sinon le titre, dont on retire le segment qui n'est que le nom de la société
    (« Laser fine cutting | KMLT » -> « Laser fine cutting »)."""
    if _clean(h1):
        return _clean(h1)
    actor_key = _normalize_text(actor_name)
    segments = [_clean(part) for part in TITLE_SEPARATORS.split(title or "") if _clean(part)]
    kept = [part for part in segments if _normalize_text(part) not in actor_key and actor_key not in _normalize_text(part)]
    return (kept or segments or [""])[0]


def _is_nameable(name: str, actor_name: str, max_words: int = MAX_NAME_WORDS) -> bool:
    key = _normalize_text(name)
    if not key or len(name) > MAX_NAME_CHARS or len(name.split()) > max_words:
        return False
    # Question de FAQ, titre d'article tronqué (« Throughput versus hole quality in... ») ou
    # article « liste » (« 7 Benefits of Femtosecond Fiber Lasers »).
    if name.rstrip().endswith(("?", "...", "…")) or key.startswith(QUESTION_OPENERS) or LISTICLE.match(name):
        return False
    # Une phrase (« More about our ultra-small drilling processes. ») n'est pas un nom.
    if name.rstrip().endswith(".") or NOT_AN_OFFER.search(name):
        return False
    # Un nom commence par une majuscule ; « athermal, ultrafast laser drilling » est un fragment
    # de phrase mis en gras, pas un intertitre.
    if name[0].isalpha() and name[0].islower():
        return False
    # « Oxford Lasers Article » : le nom de la société suivi d'un mot générique.
    residue = key.replace(_normalize_text(actor_name), " ").strip()
    return key not in GENERIC_NAMES and residue not in GENERIC_NAMES


def _names_an_offer(name: str) -> bool:
    """Le nom SEUL dit-il qu'il s'agit d'une offre laser ? (terme laser, opération ou procédé du
    lexique, code de modèle)."""
    return bool(
        _laser_match(name)
        or PRODUCT_NOUN.search(name)
        or match_all_labels(name, OPERATIONS)
        or match_all_labels(name, TECHNOLOGY_AXES)
        or MODEL_CODE.search(name)
    )


def _description(text: str, name: str) -> str:
    """Première phrase du bloc, verbatim, sans la répétition du nom en tête (les blocs en cache
    commencent souvent par leur propre intertitre)."""
    body = _clean(text)
    while name and body.lower().startswith(name.lower()):
        body = _clean(body[len(name):])
    first = SENTENCE_END.split(body, maxsplit=1)[0] if body else ""
    return first[:MAX_DESCRIPTION_CHARS]


def extract_named_offers(actor_name: str, official_url: str, url: str, page_type: str,
                         title: str | None, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Offres nommées d'UNE page (fonction pure, testable sans base)."""
    if page_type not in NAMED_OFFER_PAGE_TYPES or not _first_party(url, official_url) or not blocks:
        return []
    page_text = " ".join(_clean(block.get("text")) for block in blocks)
    if not re.search(r"\blasers?\b", f"{title or ''} {page_text}", re.I):
        return []
    # Périmètre ultra-rapide : une page qui ne le mentionne nulle part (OpTek, laser
    # nanoseconde...) peut nommer une vraie offre, mais pas forcément du périmètre -- relecture.
    in_scope = _laser_match(f"{title or ''} {page_text}")

    found: list[dict[str, Any]] = []
    h1 = next((block.get("h1") for block in blocks if _clean(block.get("h1"))), None)
    page_name = _page_name(title, h1, actor_name)
    if _is_nameable(page_name, actor_name):
        # Le premier bloc de la page est souvent un bandeau générique (« We deliver a full-service
        # solution » sur la page FemtoMPP) : on décrit l'offre par le premier bloc qui la nomme,
        # à défaut par le premier qui parle d'ultra-court, à défaut par le premier bloc.
        texts = [block.get("text") or "" for block in blocks if _clean(block.get("text"))]
        name_key = _normalize_text(page_name)
        described = (
            next((text for text in texts if name_key and name_key in _normalize_text(text)), None)
            or next((text for text in texts if _laser_match(text)), None)
            or (texts[0] if texts else "")
        )
        found.append({"name": page_name, "kind": "page", "description": _description(described, page_name)})

    for block in blocks:
        heading = _clean(block.get("heading"))
        if not _is_nameable(heading, actor_name, MAX_SECTION_WORDS):
            continue
        # Un intertitre ne compte que s'il nomme LUI-MÊME une opération/un procédé/un modèle.
        if not (match_all_labels(heading, OPERATIONS) or match_all_labels(heading, TECHNOLOGY_AXES) or MODEL_CODE.search(heading)):
            continue
        found.append({"name": heading, "kind": "section", "description": _description(block.get("text") or "", heading)})

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in found:
        key = _normalize_text(item["name"])
        if key in seen:
            continue
        seen.add(key)
        operations = sorted({label for label, _ in match_all_labels(f"{item['name']} {item['description']}", OPERATIONS)})
        results.append({
            **item,
            "actor_name": actor_name,
            "name_key": key,
            "source_url": url,
            "page_type": page_type,
            "operations": operations,
            "review_status": "accepted" if in_scope and _names_an_offer(item["name"]) else "review",
        })
    return results


def _upsert(db, offer: dict[str, Any], stamp: str) -> int:
    fingerprint = hashlib.sha256(f"{offer['actor_name']}|{offer['name_key']}".encode()).hexdigest()
    existing = db.execute("SELECT id,reviewed_at FROM named_offers WHERE fingerprint=?", (fingerprint,)).fetchone()
    if existing:
        # Jamais par-dessus une décision humaine (même garde que _upsert_offer_candidate).
        db.execute(
            """UPDATE named_offers SET last_seen_at=?,
                   review_status=CASE WHEN reviewed_at IS NULL THEN ? ELSE review_status END,
                   description=CASE WHEN description='' THEN ? ELSE description END
               WHERE id=?""",
            (stamp, offer["review_status"], offer["description"], existing["id"]),
        )
        return 0
    db.execute(
        """INSERT INTO named_offers(actor_name,name,kind,description,source_url,page_type,operations,
                                    review_status,fingerprint,first_seen_at,last_seen_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            offer["actor_name"], offer["name"], offer["kind"], offer["description"], offer["source_url"],
            offer["page_type"], json.dumps(offer["operations"], ensure_ascii=False), offer["review_status"],
            fingerprint, stamp, stamp,
        ),
    )
    return 1


def collect_named_offers() -> dict[str, Any]:
    """Point d'entrée (voir app.py: collectors["named_offers"])."""
    placeholders = ",".join("?" * len(NAMED_OFFER_PAGE_TYPES))
    with connect(ACTORS_DB) as db:
        pages = [dict(row) for row in db.execute(
            f"""SELECT a.name,a.official_url,s.url,s.page_type,s.last_title,s.blocks_json
                FROM actor_sources s JOIN actors a ON a.id=s.actor_id
                WHERE a.active=1 AND s.active=1 AND s.page_type IN ({placeholders})
                  AND s.blocks_json IS NOT NULL AND s.blocks_json NOT IN ('','[]')
                  AND (s.last_http_status IS NULL OR s.last_http_status BETWEEN 200 AND 399)
                ORDER BY a.name,s.id""",
            NAMED_OFFER_PAGE_TYPES,
        ).fetchall()]

    stamp = utc_now()
    added = 0
    by_status: dict[str, int] = defaultdict(int)
    actors: set[str] = set()
    with connect(MARKET_DB) as db:
        for page in pages:
            try:
                blocks = json.loads(page["blocks_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            for offer in extract_named_offers(
                page["name"], page["official_url"], page["url"], page["page_type"], page["last_title"], blocks,
            ):
                added += _upsert(db, offer, stamp)
                by_status[offer["review_status"]] += 1
                actors.add(offer["actor_name"])
        # Chaque passage relit TOUTES les pages en cache : une offre acceptée qui n'en sort plus
        # (règle resserrée, page retirée du site) repasse en relecture -- jamais supprimée, et
        # jamais par-dessus une décision humaine.
        retired = db.execute(
            """UPDATE named_offers SET review_status='review'
               WHERE last_seen_at<? AND review_status='accepted' AND reviewed_at IS NULL""",
            (stamp,),
        ).rowcount
    return {"pages_read": len(pages), "named_offers_seen": sum(by_status.values()), "added": added,
            "retired_to_review": retired, "by_status": dict(by_status), "actors": len(actors)}
