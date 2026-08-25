from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import anthropic
import httpx
from anthropic.types import ToolChoiceToolParam, ToolParam
from bs4 import BeautifulSoup, NavigableString, Tag

from site_profiles import DEFAULT_SITE_PROFILE

CORE_SECTIONS = ("application", "project", "news")
SECTION_SCORES = {
    "application": 100,
    "case_study": 100,
    "project": 95,
    "news": 90,
    "service": 85,
    "capability": 82,
    "technology": 80,
    "market": 76,
    "product": 70,
    "publication": 65,
    "equipment": 40,
    "homepage": 35,
    "about": 20,
    "other": 15,
    "ignore": 0,
}
DISCOVERY_TERMS = (
    "application", "applications", "use case", "case study", "case-study", "customer case",
    "product", "products", "produit", "produits", "solution", "solutions", "service", "services", "prestation", "prestations",
    "capability", "capabilities", "capacity", "capacities", "competence", "competences", "savoir-faire", "expertise", "expertises", "technology", "technologies",
    "news", "blog", "nouveaute", "nouveauté", "actualite", "actualité", "project", "projects",
    "projet", "market", "markets", "marche", "marché", "secteur", "secteurs", "industrie", "industry", "production", "manufacturing",
    "medical", "battery", "glass", "laser", "equipment", "equipement", "publication", "paper",
)


@dataclass(frozen=True)
class ContentBlock:
    heading: str
    text: str
    path: str
    media_context: str = ""
    h1: str = ""
    h2: str = ""
    h3: str = ""

    @property
    def fingerprint(self) -> str:
        hierarchy = "|".join((self.h1, self.h2, self.h3))
        return hashlib.sha256(f"{self.path}|{hierarchy}|{self.text}".encode("utf-8", "ignore")).hexdigest()

    @property
    def section_context(self) -> str:
        return " ".join(dict.fromkeys(filter(None, (self.h1, self.h2, self.h3, self.heading))))

    @property
    def editorial_group_id(self) -> str:
        """Stable local editorial-group identifier used by the market relation engine.

        The identifier deliberately isolates repeated cards / linked items while allowing
        sibling prose blocks owned by the same section to cooperate.  It is computed from
        existing block metadata so old ``blocks_json`` can be replayed without a recrawl.
        """
        raw_path = (self.path or "").strip()
        base_path, _, marker = raw_path.partition("::")
        parts = [part.strip() for part in base_path.split(" > ") if part.strip()]
        parent_path = " > ".join(parts[:-1]) if len(parts) > 1 else base_path
        marker = marker.casefold()
        hierarchy = "|".join(value.strip().casefold() for value in (self.h2, self.h3) if value.strip())

        # Cards/list items are independent micro-contexts even when their DOM paths/classes
        # are identical.  The local title is therefore part of their identity.
        if marker in {"repeated", "linked-item"}:
            seed = f"item|{base_path}|{hierarchy}|{self.heading.strip().casefold()}"
        # Pseudo-heading segments are siblings owned by the same immediate container.
        elif marker == "pseudo":
            seed = f"pseudo|{parent_path}|{hierarchy}"
        else:
            # Semantic/heading blocks may share evidence only inside the same DOM parent and
            # H2/H3 hierarchy. H1 is page-wide and intentionally excluded.
            seed = f"section|{parent_path}|{hierarchy}"
        return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:24]


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    links: list[dict[str, Any]]
    blocks: list[ContentBlock]
    structure_hash: str
    extraction_method: str = "semantic"
    page_type: str = "other"
    page_score: int = 15
    h1: str = ""
    text_coverage: float = 0.0
    useful_coverage: float = 0.0
    quality_score: float = 0.0
    noise_ratio: float = 0.0
    oversized_blocks: int = 0



CMP_HINTS = (
    "cookie", "cookies", "consent", "cmp", "onetrust", "didomi", "tarteaucitron",
    "privacy-manager", "privacy manager", "audience measurement", "advertising network",
    "mandatory cookies", "cookies obligatoires", "specific consent", "consentement specifique",
)


def _class_list(tag: Tag) -> list[str]:
    """bs4 types a tag's `class` attribute as str | list[str] | None depending on the parser;
    this normalizes it to the list[str] it always semantically is."""
    value = tag.get("class")
    if isinstance(value, list):
        return value
    return [value] if value else []


def _attr_str(tag: Tag, name: str) -> str:
    """Single-valued attribute (id, role, aria-label, ...) as plain text, never None."""
    value = tag.get(name)
    return value if isinstance(value, str) else ""


def _noise_signature(tag: Tag) -> str:
    attrs = " ".join([_attr_str(tag, "id"), " ".join(_class_list(tag)), _attr_str(tag, "aria-label"), _attr_str(tag, "role")])
    text = _clean(tag.get_text(" ", strip=True))[:1200]
    return _plain(f"{attrs} {text}")


def _is_cmp_zone(tag: Tag) -> bool:
    """Detect a consent/CMP container without banning legitimate technical uses of words like API/privacy."""
    sig = _noise_signature(tag)
    attrs = _plain(" ".join([_attr_str(tag, "id"), " ".join(_class_list(tag)), _attr_str(tag, "aria-label")]))
    strong_attr = any(token in attrs for token in (
        "cookie", "consent", "cmp", "onetrust", "didomi", "tarteaucitron", "privacy manager", "cookiebot"
    ))
    consent_terms = sum(term in sig for term in (
        "cookie", "cookies", "consent", "mandatory cookies", "cookies obligatoires",
        "audience measurement", "advertising network", "social networks", "services google"
    ))
    interactive = bool(tag.find(["button", "input"])) or tag.get("role") in ("dialog", "alertdialog")
    return strong_attr or (interactive and consent_terms >= 2)


def _remove_noise_zones(soup: BeautifulSoup) -> int:
    """Remove navigation/chrome plus entire CMP roots before editorial extraction."""
    removed = 0
    base_selector = "script,style,noscript,svg,template,form,nav,header,footer,aside,[role=navigation]"
    for tag in list(soup.select(base_selector)):
        tag.decompose(); removed += 1
    selectors = (
        '[id*="cookie" i]', '[class*="cookie" i]', '[id*="consent" i]', '[class*="consent" i]',
        '[id*="onetrust" i]', '[class*="onetrust" i]', '[id*="didomi" i]', '[class*="didomi" i]',
        '[id*="tarteaucitron" i]', '[class*="tarteaucitron" i]', '[id*="cmp" i]', '[class*="cmp" i]',
    )
    for selector in selectors:
        for tag in list(soup.select(selector)):
            if tag.parent is not None:
                tag.decompose(); removed += 1
    for element in list(soup.find_all(["div", "section", "dialog"], attrs={"role": ["dialog", "alertdialog"]})):
        if isinstance(element, Tag) and element.parent is not None and _is_cmp_zone(element):
            element.decompose(); removed += 1
    return removed


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFKD", unquote(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{host}{path}"


def _contains_fragment(value: str, fragment: str) -> bool:
    plain_value = _plain(value)
    plain_fragment = _plain(fragment)
    return bool(plain_fragment and plain_fragment in plain_value)




PAGE_TYPE_ALIASES = {
    "applications": "application",
    "projects": "project",
    "products": "product",
    "services": "service",
    "capabilities": "capability",
    "technologies": "technology",
    "markets": "market",
    "publications": "publication",
    "equipments": "equipment",
}

def normalize_page_type(value: str | None) -> str:
    """Return the canonical singular page type used across crawler, DB and market selection."""
    raw = _plain(str(value or "other")).replace(" ", "_")
    return PAGE_TYPE_ALIASES.get(raw, raw or "other")


def classify_source(
    url: str,
    label: str = "",
    *,
    title: str = "",
    h1: str = "",
    profile: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Classify and score a page from deterministic URL/anchor/title/H1 evidence."""
    profile = profile or DEFAULT_SITE_PROFILE
    parsed = urlparse(url)
    path = _plain(parsed.path)
    words = _plain(f"{parsed.path} {label} {title} {h1}")

    ignore_paths = profile.get("ignore_paths", ())
    if any(_contains_fragment(parsed.path, item) for item in ignore_paths):
        return "ignore", SECTION_SCORES["ignore"]
    if re.search(r"\b(contact|mentions? legales?|privacy|confidentialite|cookies?|login|careers?|recrutement|jobs?|legal notice|newsletter)\b", words):
        return "ignore", SECTION_SCORES["ignore"]

    rules = (
        ("case_study", r"\b(case stud(?:y|ies)|case-study|customer cases?|cas clients?|realisations?|success stor(?:y|ies))\b"),
        ("application", r"\b(applications?|use cases?|applications? industrielles?)\b"),
        ("project", r"\b(projects?|projets?|collaborations?|collaborative projects?)\b"),
        ("news", r"\b(news|blogs?|actualites?|nouveautes?|press|presse|insights?)\b"),
        ("service", r"\b(services?|prestations?|sous[- ]?traitance|job shop|contract manufacturing|contract machining|manufacturing services?|lohnfertigung|auftragsfertigung|conto terzi|subcontratacion|fabricacion por contrato)\b"),
        ("capability", r"\b(capabilities|capability|capacities|capacity|competences?|savoir faire|expertises?)\b"),
        ("technology", r"\b(technology|technologies|processes?|procedes?|laser processing|micromachining)\b"),
        ("market", r"\b(markets?|industries|sectors?|secteurs?|marches?)\b"),
        ("product", r"\b(products?|produits?|solutions?)\b"),
        ("publication", r"\b(publications?|papers?|white papers?|technical papers?|scientific)\b"),
        ("equipment", r"\b(equipments?|equipements?|machines?|systems?)\b"),
        ("about", r"\b(about|company|entreprise|who we are|qui sommes nous)\b"),
    )
    page_type = "other"
    for candidate, pattern in rules:
        if re.search(pattern, words):
            page_type = candidate
            break

    if path in ("", "en", "fr", "de", "en html", "fr html", "de html"):
        page_type = "homepage" if page_type == "other" else page_type

    score = SECTION_SCORES[page_type]
    score += int(profile.get("page_type_boosts", {}).get(page_type, 0))
    path_value = parsed.path.lower()
    if any(fragment.lower() in path_value for fragment in profile.get("priority_paths", ())):
        score += 18
    combined = f"{label} {title} {h1} {parsed.path}"
    priority_hits = sum(_contains_fragment(combined, term) for term in profile.get("priority_terms", ()))
    score += min(priority_hits * 3, 12)
    return normalize_page_type(page_type), max(0, min(int(score), 140))


def _path(tag: Tag) -> str:
    parts: list[str] = []
    current: Tag | None = tag
    while current and current.name not in (None, "[document]") and len(parts) < 7:
        token = current.name
        if current.get("id"):
            token += f"#{current.get('id')}"
        else:
            classes = [item for item in _class_list(current) if len(item) < 35][:2]
            if classes:
                token += "." + ".".join(classes)
        parts.append(token)
        current = current.parent if isinstance(current.parent, Tag) else None
    return " > ".join(reversed(parts))


def _meaningful_links(soup: BeautifulSoup, base_url: str, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    profile = profile or DEFAULT_SITE_PROFILE
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    found: dict[str, dict[str, Any]] = {}
    max_links = int(profile.get("crawl", {}).get("max_links_per_page", 160))
    for link in soup.select("a[href]"):
        label = _clean(link.get_text(" ", strip=True))
        href = urljoin(base_url, _attr_str(link, "href")).split("#", 1)[0]
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower().removeprefix("www.") != base_host:
            continue
        category, score = classify_source(href, label, profile=profile)
        if category == "ignore":
            continue
        context = "navigation" if link.find_parent(["nav", "header"]) else "content"
        haystack = _plain(f"{label} {parsed.path}")
        has_discovery_term = any(_plain(term) in haystack for term in DISCOVERY_TERMS)
        has_priority_path = any(fragment.lower() in parsed.path.lower() for fragment in profile.get("priority_paths", ()))
        if context != "navigation" and not has_discovery_term and not has_priority_path:
            continue
        key = canonical_url(href)
        item = {
            "url": href,
            "label": label[:180],
            "context": context,
            "category": category,
            "score": score,
            "reason": "priority-path" if has_priority_path else ("navigation" if context == "navigation" else "discovery-term"),
        }
        previous = found.get(key)
        if not previous or score > int(previous.get("score", 0)):
            found[key] = item
    return sorted(found.values(), key=lambda item: (-int(item["score"]), item["url"]))[:max_links]


def _heading_hierarchy(tag: Tag) -> tuple[str, str, str, str]:
    """Return local editorial hierarchy without leaking headings from distant page regions.

    The old implementation used ``find_all_previous`` globally. On CMS pages this
    could attach a paragraph about femtosecond lasers to an unrelated preceding
    heading such as ``Brochures``. We now prefer headings owned by close ancestors
    and only keep the page H1 globally.
    """
    own = tag.find(["h1", "h2", "h3", "h4", "h5", "h6"], recursive=False)
    if own is None and tag.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        own = tag
    own_tag = own if isinstance(own, Tag) else None
    own_text = _clean(own_tag.get_text(" ", strip=True)) if own_tag else ""
    own_level = _heading_level(own_tag) if own_tag else None
    values: dict[int, str] = {}
    if own_level in (1, 2, 3) and own_text:
        values[own_level] = own_text

    # Walk only close ancestors. A heading qualifies when it is the ancestor's
    # leading editorial heading and therefore structurally owns the target tag.
    current = tag.parent if isinstance(tag.parent, Tag) else None
    hops = 0
    while current is not None and hops < 6:
        direct = current.find(["h1", "h2", "h3"], recursive=False)
        if isinstance(direct, Tag):
            level = _heading_level(direct)
            value = _clean(direct.get_text(" ", strip=True))
            if level in (1, 2, 3) and value and level not in values:
                values[level] = value
        if current.name in ("body", "html"):
            break
        current = current.parent if isinstance(current.parent, Tag) else None
        hops += 1

    # Page H1 is safe as global document context; deeper headings are not.
    if 1 not in values:
        page = tag.find_previous("h1")
        if page is not None:
            value = _clean(page.get_text(" ", strip=True))
            if value:
                values[1] = value

    if own_level == 1:
        values.pop(2, None); values.pop(3, None)
    elif own_level == 2:
        values.pop(3, None)

    return own_text, values.get(1, ""), values.get(2, ""), values.get(3, "")


def _content_block(tag: Tag, heading_hint: str = "") -> ContentBlock | None:
    text = _clean(tag.get_text(" ", strip=True))
    if len(text) < 55:
        return None
    own_heading, h1, h2, h3 = _heading_hierarchy(tag)
    heading = own_heading or _title_like_text(tag) or heading_hint or h3 or h2 or h1
    media_parts: list[str] = []
    for image in tag.select("img[alt]")[:4]:
        alt = _clean(_attr_str(image, "alt"))
        if alt:
            media_parts.append(alt)
    for caption in tag.select("figcaption")[:2]:
        value = _clean(caption.get_text(" ", strip=True))
        if value:
            media_parts.append(value)
    return ContentBlock(
        heading=heading[:240],
        text=text[:6000],
        path=_path(tag),
        media_context=" | ".join(media_parts)[:800],
        h1=h1[:240], h2=h2[:240], h3=h3[:240],
    )





def _title_like_text(tag: Tag) -> str:
    """Return a short editorial title carried by a non-heading element."""
    candidates: list[Tag] = []
    if tag.name in ("strong", "b"):
        candidates.append(tag)
    candidates.extend(tag.select("h1,h2,h3,h4,h5,h6,[class*=title],[class*=heading],[class*=subtitle],[class*=label]"))
    # Anchors are useful for cards, but only after stronger title cues.
    candidates.extend(tag.select("a[href]"))
    for candidate in candidates:
        value = _clean(candidate.get_text(" ", strip=True))
        words = value.split()
        if 2 <= len(value) <= 180 and 1 <= len(words) <= 18:
            return value
    return ""


def _is_pseudo_heading(tag: Tag) -> bool:
    """Conservative pseudo-heading detection for CMS pages without proper H-tags."""
    if tag.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return False
    value = _clean(tag.get_text(" ", strip=True))
    if not (2 <= len(value) <= 160 and len(value.split()) <= 16):
        return False
    classes = " ".join(_class_list(tag)).lower()
    if any(token in classes for token in ("title", "heading", "subtitle", "card-title", "item-title", "label")):
        return True
    # Typical Drupal/WordPress pattern: <p><strong>Laser turning</strong></p>.
    strong = tag.find(["strong", "b"], recursive=False)
    if strong and _clean(strong.get_text(" ", strip=True)) == value:
        return True
    if tag.name in ("strong", "b"):
        return True
    return False


def _pseudo_heading_blocks(container: Tag, profile: dict[str, Any] | None = None) -> list[ContentBlock]:
    """Split a container around short visual headings not encoded as H2/H3."""
    profile = profile or DEFAULT_SITE_PROFILE
    cfg = profile.get("editorial_units", {})
    min_chars = int(cfg.get("min_chars", 45))
    max_chars = int(cfg.get("max_chars", 3500))
    candidates: list[Tag] = []
    for tag in container.find_all(["p", "div", "span", "strong", "b"], recursive=True):
        if not isinstance(tag, Tag) or not _is_pseudo_heading(tag):
            continue
        boundary = tag
        # Promote nested strong/b/span to a paragraph/div boundary when the parent contains only the title.
        parent = tag.parent if isinstance(tag.parent, Tag) else None
        if parent and parent is not container and parent.name in ("p", "div", "li"):
            if _clean(parent.get_text(" ", strip=True)) == _clean(tag.get_text(" ", strip=True)):
                boundary = parent
        if boundary not in candidates:
            candidates.append(boundary)

    blocks: list[ContentBlock] = []
    seen: set[str] = set()
    for heading in candidates:
        heading_text = _clean(heading.get_text(" ", strip=True))
        parent = heading.parent if isinstance(heading.parent, Tag) else None
        if not parent:
            continue
        pieces: list[str] = []
        sibling = heading.next_sibling
        while sibling is not None:
            if isinstance(sibling, Tag):
                if _heading_level(sibling) is not None or _is_pseudo_heading(sibling):
                    break
                text = _clean(sibling.get_text(" ", strip=True))
                if text:
                    pieces.append(text)
            elif isinstance(sibling, NavigableString):
                text = _clean(str(sibling))
                if text:
                    pieces.append(text)
            if sum(len(part) + 1 for part in pieces) >= max_chars:
                break
            sibling = sibling.next_sibling
        body = _clean(" ".join(pieces))[:max_chars]
        text = _clean(f"{heading_text} {body}")
        if len(text) < min_chars:
            continue
        digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        _, h1, h2, h3 = _heading_hierarchy(heading)
        blocks.append(ContentBlock(
            heading=heading_text[:240], text=text, path=f"{_path(heading)}::pseudo",
            h1=h1[:240], h2=h2[:240], h3=h3[:240],
        ))
    return blocks


def _repeated_editorial_blocks(container: Tag, profile: dict[str, Any] | None = None) -> list[ContentBlock]:
    """Detect repeated sibling cards/list entries as independent editorial units."""
    profile = profile or DEFAULT_SITE_PROFILE
    cfg = profile.get("editorial_units", {})
    min_units = int(cfg.get("min_repeated_units", 3))
    min_chars = int(cfg.get("min_chars", 45))
    max_chars = int(cfg.get("max_chars", 3500))
    max_groups = int(cfg.get("max_groups", 80))
    blocks: list[ContentBlock] = []
    seen: set[str] = set()
    groups_seen = 0

    for parent in [container, *container.find_all(["div", "ul", "ol", "section"], recursive=True)]:
        if not isinstance(parent, Tag):
            continue
        children = [child for child in parent.find_all(recursive=False) if isinstance(child, Tag)]
        if not (min_units <= len(children) <= 40):
            continue
        qualified: list[tuple[Tag, str, str]] = []
        for child in children:
            text = _clean(child.get_text(" ", strip=True))
            if not (min_chars <= len(text) <= max_chars):
                continue
            title = _title_like_text(child)
            if not title:
                continue
            # Require more than the title itself, unless this is a substantial linked item.
            if len(text) < len(title) + 18 and not (child.name == "li" and child.find("a", href=True)):
                continue
            qualified.append((child, title, text))
        if len(qualified) < min_units or len(qualified) / max(1, len(children)) < 0.55:
            continue
        groups_seen += 1
        if groups_seen > max_groups:
            break
        for child, title, text in qualified:
            digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            _, h1, h2, h3 = _heading_hierarchy(child)
            blocks.append(ContentBlock(
                heading=title[:240], text=text[:max_chars], path=f"{_path(child)}::repeated",
                h1=h1[:240], h2=h2[:240], h3=h3[:240],
            ))
    return blocks


def _editorial_blocks(container: Tag, profile: dict[str, Any] | None = None) -> list[ContentBlock]:
    """Collect non-standard editorial units from cards and pseudo-headings."""
    combined = _repeated_editorial_blocks(container, profile) + _pseudo_heading_blocks(container, profile)
    result: list[ContentBlock] = []
    seen: set[str] = set()
    for block in combined:
        key = hashlib.sha256(_clean(block.text).lower().encode("utf-8", "ignore")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        result.append(block)
    return result


def _linked_editorial_blocks(container: Tag, profile: dict[str, Any] | None = None) -> list[ContentBlock]:
    """Split large link-heavy editorial lists (publications, projects, products) into local units.

    This runs only as a secondary refinement of oversized blocks, never on global navigation.
    """
    profile = profile or DEFAULT_SITE_PROFILE
    cfg = profile.get("quality_extraction", {})
    min_title_chars = int(cfg.get("linked_item_min_title_chars", 24))
    max_item_chars = int(cfg.get("linked_item_max_chars", 900))
    min_items = int(cfg.get("linked_item_min_items", 3))
    blocks: list[ContentBlock] = []
    seen: set[str] = set()
    for anchor in container.select("a[href]"):
        title = _clean(anchor.get_text(" ", strip=True))
        if len(title) < min_title_chars or len(title) > 260:
            continue
        # Prefer a compact owning item/card. Do not climb into the whole section.
        owner: Tag = anchor
        current = anchor.parent if isinstance(anchor.parent, Tag) else None
        hops = 0
        while current is not None and current is not container and hops < 4:
            text = _clean(current.get_text(" ", strip=True))
            if 0 < len(text) <= max_item_chars and current.name in ("li", "article", "div", "p"):
                owner = current
            elif len(text) > max_item_chars:
                break
            current = current.parent if isinstance(current.parent, Tag) else None
            hops += 1
        text = _clean(owner.get_text(" ", strip=True))
        if len(text) < min_title_chars:
            text = title
        if len(text) > max_item_chars:
            text = title
        digest = hashlib.sha256(_plain(text).encode("utf-8", "ignore")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        _, h1, h2, h3 = _heading_hierarchy(owner)
        blocks.append(ContentBlock(
            heading=title[:240], text=text[:max_item_chars], path=f"{_path(owner)}::linked-item",
            h1=h1[:240], h2=h2[:240], h3=h3[:240],
        ))
    return blocks if len(blocks) >= min_items else []


def _split_oversized_block(tag: Tag, block: ContentBlock, profile: dict[str, Any] | None = None) -> list[ContentBlock]:
    profile = profile or DEFAULT_SITE_PROFILE
    cfg = profile.get("quality_extraction", {})
    threshold = int(cfg.get("oversized_block_chars", 1500))
    if len(block.text) < threshold:
        return []
    pieces = _dedupe_blocks(
        _heading_segment_blocks(tag, profile)
        + _editorial_blocks(tag, profile)
        + _linked_editorial_blocks(tag, profile)
    )
    # Keep only genuinely more local units.
    pieces = [p for p in pieces if len(p.text) < min(len(block.text) * 0.85, threshold)]
    min_units = int(cfg.get("oversized_split_min_units", 3))
    return pieces if len(pieces) >= min_units else []


def _noise_ratio(blocks: list[ContentBlock]) -> float:
    if not blocks:
        return 0.0
    noise_terms = (
        "mandatory cookies", "cookies obligatoires", "audience measurement", "advertising network",
        "social networks", "specific consent", "consentement specifique", "cookie preferences",
        "manage consent", "privacy settings", "services google",
    )
    total = sum(max(1, len(_plain(b.text).split())) for b in blocks)
    noisy = 0
    for block in blocks:
        sig = _plain(f"{block.heading} {block.text}")
        if any(term in sig for term in noise_terms):
            noisy += max(1, len(_plain(block.text).split()))
    return round(noisy / total, 4) if total else 0.0


def _useful_coverage(root: Tag | BeautifulSoup, blocks: list[ContentBlock], oversized_chars: int = 1500) -> float:
    """Coverage that discounts giant catch-all blocks and therefore rewards local evidence units."""
    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9à-ÿ]{3,}", _plain(value))
    root_counter = Counter(words(_clean(root.get_text(" ", strip=True))))
    if not root_counter:
        return 1.0
    extracted: dict[str, float] = {}
    for block in blocks:
        weight = 1.0 if len(block.text) <= oversized_chars else 0.25
        for token, count in Counter(words(block.text)).items():
            extracted[token] = extracted.get(token, 0.0) + count * weight
    covered = sum(min(float(count), extracted.get(token, 0.0)) for token, count in root_counter.items())
    total = float(sum(root_counter.values()))
    return round(covered / total, 4) if total else 1.0


def _quality_metrics(root: Tag | BeautifulSoup, blocks: list[ContentBlock], profile: dict[str, Any] | None = None) -> dict[str, float | int]:
    profile = profile or DEFAULT_SITE_PROFILE
    cfg = profile.get("quality_extraction", {})
    oversized_chars = int(cfg.get("oversized_block_chars", 1500))
    text_cov = _token_coverage(root, blocks)
    useful_cov = _useful_coverage(root, blocks, oversized_chars)
    noise = _noise_ratio(blocks)
    oversized = sum(len(b.text) > oversized_chars for b in blocks)
    if blocks:
        local = sum(len(b.text) <= oversized_chars for b in blocks) / len(blocks)
        headings = [_plain(b.heading) for b in blocks if _plain(b.heading)]
        repeats = Counter(headings)
        repeated = sum(max(0, count - 2) for count in repeats.values())
        context_score = max(0.0, 1.0 - repeated / max(1, len(blocks)))
    else:
        local = 0.0; context_score = 0.0
    score = 100.0 * (0.42 * useful_cov + 0.23 * text_cov + 0.20 * local + 0.15 * context_score)
    score -= noise * 35.0
    score -= min(oversized * 3.0, 15.0)
    return {
        "text_coverage": round(text_cov, 4),
        "useful_coverage": round(useful_cov, 4),
        "noise_ratio": round(noise, 4),
        "oversized_blocks": oversized,
        "quality_score": round(max(0.0, min(100.0, score)), 1),
    }


def _token_coverage(root: Tag | BeautifulSoup, blocks: list[ContentBlock]) -> float:
    """Approximate how much of the cleaned main text survives extraction, without double-counting overlaps."""
    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9à-ÿ]{3,}", _plain(value))
    root_counter = Counter(words(_clean(root.get_text(" ", strip=True))))
    if not root_counter:
        return 1.0
    extracted: Counter[str] = Counter()
    for block in blocks:
        extracted.update(words(block.text))
    covered = sum(min(count, extracted.get(token, 0)) for token, count in root_counter.items())
    total = sum(root_counter.values())
    return round(covered / total, 4) if total else 1.0


def _dedupe_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Drop duplicates while preserving DOM order and preferring explicit editorial units."""
    def quality(block: ContentBlock) -> tuple[int, int, int]:
        editorial = int(any(token in block.path for token in ("::repeated", "::pseudo", "::segment")))
        named = int(bool(_clean(block.heading)))
        hierarchy = int(bool(block.h2 or block.h3))
        return editorial, named, hierarchy

    exact: dict[str, ContentBlock] = {}
    order: list[str] = []
    for block in blocks:
        text = _plain(block.text)
        if not text:
            continue
        if text not in exact:
            exact[text] = block
            order.append(text)
        elif quality(block) > quality(exact[text]):
            exact[text] = block

    # Exact duplicates are removed, but containment is intentionally preserved:
    # a small publication/card may legitimately be contained in a large section
    # and is precisely the more useful local evidence unit.
    return [exact[key] for key in order]


def _heading_level(tag: Tag) -> int | None:
    if tag.name and len(tag.name) == 2 and tag.name[0].lower() == "h" and tag.name[1].isdigit():
        level = int(tag.name[1])
        return level if 1 <= level <= 6 else None
    return None


def _heading_segment_blocks(root: Tag | BeautifulSoup, profile: dict[str, Any] | None = None) -> list[ContentBlock]:
    """Build local editorial sections from H2/H3/H4 boundaries.

    A segment starts at a heading and ends immediately before the next heading
    of equal or higher rank. This keeps a market/application heading attached
    to its own paragraphs without inheriting unrelated content farther down the page.
    """
    profile = profile or DEFAULT_SITE_PROFILE
    cfg = profile.get("heading_segmentation", {})
    levels = tuple(int(v) for v in cfg.get("levels", (2, 3, 4)))
    min_chars = int(cfg.get("min_chars", 55))
    max_chars = int(cfg.get("max_chars", 4500))
    max_segments = int(cfg.get("max_segments", 60))

    headings = [h for h in root.find_all([f"h{n}" for n in levels]) if isinstance(h, Tag)]
    blocks: list[ContentBlock] = []
    seen_text: set[str] = set()
    for heading in headings[:max_segments]:
        level = _heading_level(heading)
        if level is None:
            continue
        pieces: list[str] = []
        for node in heading.next_elements:
            if node is heading:
                continue
            if isinstance(node, Tag):
                next_level = _heading_level(node)
                if next_level is not None and next_level <= level:
                    break
                continue
            if not isinstance(node, NavigableString):
                continue
            parent = node.parent if isinstance(node.parent, Tag) else None
            if parent and parent.name in ("script", "style", "noscript", "template"):
                continue
            value = _clean(str(node))
            if value:
                pieces.append(value)
            if sum(len(part) + 1 for part in pieces) >= max_chars:
                break
        body = _clean(" ".join(pieces))[:max_chars]
        heading_text = _clean(heading.get_text(" ", strip=True))
        text = _clean(f"{heading_text} {body}")
        if len(text) < min_chars:
            continue
        digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if digest in seen_text:
            continue
        seen_text.add(digest)
        _, h1, h2, h3 = _heading_hierarchy(heading)
        blocks.append(ContentBlock(
            heading=heading_text[:240],
            text=text,
            path=f"{_path(heading)}::segment",
            h1=h1[:240], h2=h2[:240], h3=h3[:240],
        ))
    return blocks


def diagnose_document(html: str, base_url: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return extraction diagnostics without changing or storing any data."""
    profile = profile or DEFAULT_SITE_PROFILE
    soup = BeautifulSoup(html, "html.parser")
    raw_counts = {
        "h1": len(soup.find_all("h1")),
        "h2": len(soup.find_all("h2")),
        "h3": len(soup.find_all("h3")),
        "h4": len(soup.find_all("h4")),
        "section": len(soup.find_all("section")),
        "article": len(soup.find_all("article")),
        "anchors": len(soup.select("a[href]")),
    }
    removed_noise_zones = _remove_noise_zones(soup)
    root: Tag | BeautifulSoup = soup.body or soup
    root_selector = "body"
    for selector in profile.get("preferred_content_roots", ("main", "[role=main]", "body")):
        candidate_root = soup.select_one(selector)
        if candidate_root:
            root = candidate_root
            root_selector = selector
            break
    selectors = [
        "article", "section", "[class*=application]", "[class*=product]", "[class*=solution]",
        "[class*=project]", "[class*=news]", "[class*=card]", "[class*=item]",
        "[class*=elementor-widget]", "[class*=wp-block]", "[class*=et_pb]", "[class*=vc_row]",
    ]
    selectors.extend(profile.get("extra_candidate_selectors", ()))
    selector = ", ".join(dict.fromkeys(selectors))
    candidates = list(root.select(selector)) if hasattr(root, "select") else []
    usable = [candidate for candidate in candidates if _usable_candidate(candidate)]
    usable_ids = {id(candidate) for candidate in usable}
    selected = [candidate for candidate in usable if not any(id(desc) in usable_ids for desc in candidate.select(selector))]
    semantic_blocks = [b for c in selected if (b := _content_block(c))]
    heading_segments = _heading_segment_blocks(root, profile)
    editorial_blocks = _editorial_blocks(root if isinstance(root, Tag) else soup.body, profile) if isinstance(root if isinstance(root, Tag) else soup.body, Tag) else []
    rescued_parents = []
    for candidate in usable:
        if candidate in selected:
            continue
        sub = _editorial_blocks(candidate, profile)
        if len(sub) >= int(profile.get("editorial_units", {}).get("parent_rescue_min_units", 3)):
            rescued_parents.extend(sub)
    final_doc = parse_document(html, base_url, profile=profile)
    coverage = _token_coverage(root, final_doc.blocks)
    return {
        "url": base_url,
        "html_chars": len(html),
        "root_selector": root_selector,
        "raw_dom": raw_counts,
        "candidate_selector_matches": len(candidates),
        "candidate_usable": len(usable),
        "candidate_rejected_short_or_empty": len(candidates) - len(usable),
        "candidate_rejected_parent_has_usable_descendant": len(usable) - len(selected),
        "candidate_leaf_selected": len(selected),
        "semantic_blocks": len(semantic_blocks),
        "heading_segments": len(heading_segments),
        "editorial_units": len(editorial_blocks),
        "rescued_parent_units": len(rescued_parents),
        "coverage_ratio": coverage,
        "text_coverage": final_doc.text_coverage,
        "useful_coverage": final_doc.useful_coverage,
        "quality_score": final_doc.quality_score,
        "noise_ratio": final_doc.noise_ratio,
        "oversized_blocks": final_doc.oversized_blocks,
        "removed_noise_zones": removed_noise_zones,
        "final_blocks": len(final_doc.blocks),
        "extraction_method": final_doc.extraction_method,
        "meaningful_links": len(final_doc.links),
        "blocks": [
            {"heading": b.heading, "h1": b.h1, "h2": b.h2, "h3": b.h3, "chars": len(b.text), "path": b.path}
            for b in final_doc.blocks
        ],
    }

def _usable_candidate(tag: Tag) -> bool:
    if len(_clean(tag.get_text(" ", strip=True))) < 55:
        return False
    return tag.name in ("article", "section") or bool(tag.find(["h1", "h2", "h3", "h4", "h5", "h6", "p", "figcaption"]))


def _heading_fallback(root: Tag) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    used_paths: set[str] = set()
    for heading in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])[:80]:
        parent = heading.parent if isinstance(heading.parent, Tag) else None
        hops = 0
        while parent and parent is not root and hops < 5:
            if _usable_candidate(parent):
                other_headings = parent.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
                if len(other_headings) <= 1:
                    block = _content_block(parent, _clean(heading.get_text(" ", strip=True)))
                    if block and block.path not in used_paths:
                        blocks.append(block)
                        used_paths.add(block.path)
                    break
            parent = parent.parent if isinstance(parent.parent, Tag) else None
            hops += 1
    if blocks:
        return blocks
    for paragraph in root.find_all(["p", "li"]):
        parent = paragraph.parent if isinstance(paragraph.parent, Tag) else None
        if parent:
            block = _content_block(parent)
            if block and block.path not in used_paths:
                blocks.append(block)
                used_paths.add(block.path)
    return blocks[:60]


def parse_document(html: str, base_url: str, profile: dict[str, Any] | None = None) -> ParsedDocument:
    """Deterministically discover links and extract independent, hierarchy-aware content blocks."""
    profile = profile or DEFAULT_SITE_PROFILE
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    # Order matters: links must be classified while <nav>/<header> are still in the tree, since
    # _meaningful_links tells navigation from content links by walking up to those very tags.
    links = _meaningful_links(soup, base_url, profile=profile)

    _remove_noise_zones(soup)

    root: Tag | BeautifulSoup = soup.body or soup
    for selector in profile.get("preferred_content_roots", ("main", "[role=main]", "body")):
        candidate_root = soup.select_one(selector)
        if candidate_root:
            root = candidate_root
            break

    page_h1_tag = root.find("h1") if isinstance(root, (Tag, BeautifulSoup)) else None
    page_h1 = _clean(page_h1_tag.get_text(" ", strip=True)) if page_h1_tag else ""
    page_type, page_score = classify_source(base_url, title, title=title, h1=page_h1, profile=profile)

    selectors = [
        "article", "section", "[class*=application]", "[class*=product]", "[class*=solution]",
        "[class*=project]", "[class*=news]", "[class*=card]", "[class*=item]",
        "[class*=elementor-widget]", "[class*=wp-block]", "[class*=et_pb]", "[class*=vc_row]",
    ]
    selectors.extend(profile.get("extra_candidate_selectors", ()))
    selector = ", ".join(dict.fromkeys(selectors))
    candidates = list(root.select(selector)) if hasattr(root, "select") else []
    usable = [candidate for candidate in candidates if _usable_candidate(candidate)]
    usable_ids = {id(candidate) for candidate in usable}
    selected = [
        candidate for candidate in usable
        if not any(id(descendant) in usable_ids for descendant in candidate.select(selector))
    ]

    blocks: list[ContentBlock] = []
    seen: set[str] = set()
    selected_tags: list[Tag] = []
    for candidate in selected:
        block = _content_block(candidate)
        if not block:
            continue
        digest = hashlib.sha256(f"{block.section_context}|{block.text}".encode("utf-8", "ignore")).hexdigest()
        if digest not in seen:
            blocks.append(block)
            selected_tags.append(candidate)
            seen.add(digest)

    method = "semantic"
    cfg = profile.get("editorial_units", {})
    refine_min_chars = int(cfg.get("refine_large_block_chars", 900))
    refine_min_units = int(cfg.get("refine_min_units", 2))

    # Refine very large semantic blocks locally instead of treating an entire
    # Publications/Projects/Services container as one piece of evidence.
    refined: list[ContentBlock] = []
    refined_any = False
    for tag, block in zip(selected_tags, blocks):
        oversized_units = _split_oversized_block(tag, block, profile)
        if oversized_units:
            refined.extend(oversized_units)
            refined_any = True
            continue
        local_units = _dedupe_blocks(_heading_segment_blocks(tag, profile) + _editorial_blocks(tag, profile))
        useful_units = [u for u in local_units if len(u.text) < max(len(block.text) * 0.92, 250)]
        if len(useful_units) >= refine_min_units and (len(block.text) >= refine_min_chars or len(useful_units) >= 3):
            refined.extend(useful_units)
            refined_any = True
        else:
            refined.append(block)
    if refined_any:
        blocks = _dedupe_blocks(refined)
        if blocks and all("::segment" in block.path for block in blocks):
            method = "heading-segments"
        else:
            method = "semantic-refined"

    # Rescue rich parents that the leaf-selection rule would otherwise discard.
    # This is important for CMS card grids where a tiny 'Brochures' leaf can hide
    # a parent containing many laser-process cards.
    rescue_units: list[ContentBlock] = []
    rescue_min = int(cfg.get("parent_rescue_min_units", 3))
    for candidate in usable:
        if candidate in selected:
            continue
        local = _dedupe_blocks(_editorial_blocks(candidate, profile))
        if len(local) >= rescue_min:
            rescue_units.extend(local)
    if rescue_units:
        blocks = _dedupe_blocks(blocks + rescue_units)
        method = "semantic+editorial-rescue" if method == "semantic" else method + "+rescue"

    segmentation = profile.get("heading_segmentation", {})
    heading_count = len(root.find_all(["h2", "h3", "h4"])) if hasattr(root, "find_all") else 0
    trigger_max_blocks = int(segmentation.get("trigger_max_semantic_blocks", 3))
    trigger_min_headings = int(segmentation.get("trigger_min_headings", 4))
    if bool(segmentation.get("enabled", True)) and len(blocks) <= trigger_max_blocks and heading_count >= trigger_min_headings:
        segmented = _heading_segment_blocks(root, profile)
        if len(segmented) >= max(len(blocks) + 2, trigger_min_headings):
            blocks = segmented
            method = "heading-segments"

    # Coverage fallback: if semantic extraction retained too little of the main
    # content, add page-level editorial units rather than silently accepting it.
    coverage = _token_coverage(root, blocks)
    min_coverage = float(cfg.get("min_coverage_ratio", 0.52))
    if coverage < min_coverage and isinstance(root, Tag):
        page_units = _dedupe_blocks(_editorial_blocks(root, profile) + _heading_segment_blocks(root, profile))
        if len(page_units) >= int(cfg.get("coverage_rescue_min_units", 3)):
            candidate_blocks = _dedupe_blocks(blocks + page_units)
            new_coverage = _token_coverage(root, candidate_blocks)
            if new_coverage > coverage + 0.08:
                blocks = candidate_blocks
                coverage = new_coverage
                method = method + "+coverage-rescue"
    if not blocks:
        blocks = _heading_fallback(root)  # type: ignore[arg-type]
        method = "heading-fallback" if blocks else "empty"
    if not blocks and hasattr(root, "find_all"):
        direct_children = [child for child in root.find_all(recursive=False) if isinstance(child, Tag)]
        for child in direct_children:
            block = _content_block(child)
            if block:
                blocks.append(block)
        if blocks:
            method = "container-fallback"

    blocks = blocks[:80]
    metrics = _quality_metrics(root, blocks, profile)
    structure = "|".join(f"{block.path}:{block.h1}>{block.h2}>{block.h3}:{block.heading}" for block in blocks)
    return ParsedDocument(
        title=title,
        links=links,
        blocks=blocks,
        structure_hash=hashlib.sha256(structure.encode("utf-8", "ignore")).hexdigest(),
        extraction_method=method,
        page_type=page_type,
        page_score=page_score,
        h1=page_h1,
        text_coverage=float(metrics["text_coverage"]),
        useful_coverage=float(metrics["useful_coverage"]),
        quality_score=float(metrics["quality_score"]),
        noise_ratio=float(metrics["noise_ratio"]),
        oversized_blocks=int(metrics["oversized_blocks"]),
    )


class OllamaClient:
    # Class-level, not per-instance: /api/overview calls get_ai_client() on every request and
    # gets a brand-new OllamaClient() each time. Caching on the class lets all of those instances
    # share one 15s-TTL liveness probe instead of hitting the daemon (or timing out after 2.5s
    # against an unreachable one) on every dashboard poll.
    _availability: bool | None = None
    _checked_at: float = 0

    def __init__(self) -> None:
        self.base_url = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
        self.model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.timeout = float(os.getenv("OLLAMA_TIMEOUT", "90"))
        # Ollama has no per-request token accounting worth wiring up (it's a local daemon,
        # not billed); kept at 0 so callers can read *.total_*_tokens uniformly regardless
        # of which provider get_ai_client() returned.
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def available(self) -> bool:
        now = time.monotonic()
        if self.__class__._availability is not None and now - self.__class__._checked_at < 15:
            return bool(self.__class__._availability)
        try:
            with httpx.Client(timeout=2.5) as client:
                available = client.get(f"{self.base_url}/api/tags").status_code == 200
        except Exception:
            available = False
        self.__class__._availability = available
        self.__class__._checked_at = now
        return available

    def ask_json(self, system: str, prompt: str) -> dict[str, Any] | list[Any]:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0},
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
        content = response.json().get("message", {}).get("content", "{}")
        return json.loads(content)


class AnthropicClient:
    """Cloud fallback for market-fact extraction, selected via AI_PROVIDER=anthropic.

    Implements the same duck-typed contract as OllamaClient (available(), ask_json(),
    .model, .total_input_tokens/.total_output_tokens) so callers don't need to know which
    provider they got from get_ai_client().

    ask_json() stays schema-agnostic on purpose, like Ollama's `format: "json"` -- it is
    also used by build_profile() with a completely different expected JSON shape than the
    market-fact extraction in scrapers._ai_candidates(). Forcing a fixed tool schema here
    would silently return the wrong shape to whichever caller didn't design it. Instead,
    tool_choice forces *some* tool call (so the reply is always structured JSON, never prose
    or markdown fences) via a deliberately schema-less tool -- the caller's system prompt is
    still what defines the actual expected shape, exactly as it already does for Ollama.
    """

    _TOOL_NAME = "respond_json"
    _TOOL_SCHEMA: ToolParam = {
        "name": _TOOL_NAME,
        "description": "Renvoie la réponse structurée demandée par les instructions du message, au format JSON.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
    }

    def __init__(self) -> None:
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        # No date suffix: Anthropic model IDs are complete as-is (claude-haiku-4-5, not
        # claude-haiku-4-5-20251001) -- a dated suffix is simply an invalid model id.
        self.model = os.getenv("ANTHROPIC_EXTRACTION_MODEL", "claude-haiku-4-5")
        self._client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else None
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def available(self) -> bool:
        return self._client is not None

    def ask_json(self, system: str, prompt: str) -> dict[str, Any] | list[Any]:
        if not self._client:
            return {}
        tool_choice: ToolChoiceToolParam = {"type": "tool", "name": self._TOOL_NAME}
        try:
            # Note: this SDK's messages.create() has no temperature/top_p/top_k parameter at
            # all (verified against the installed anthropic==1.0.0 signature, not just assumed) --
            # sampling isn't controllable here, unlike Ollama's options.temperature=0 below.
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[self._TOOL_SCHEMA],
                tool_choice=tool_choice,
            )
        except anthropic.APIError:
            return {}
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        tool_block = next((block for block in response.content if block.type == "tool_use"), None)
        return tool_block.input if tool_block else {}


def get_ai_client() -> OllamaClient | AnthropicClient:
    return AnthropicClient() if os.getenv("AI_PROVIDER", "").strip().lower() == "anthropic" else OllamaClient()


# (input $/1M tokens, output $/1M tokens). This is a maintained snapshot, not a live lookup --
# check console.anthropic.com/settings/billing before trusting it for real budgeting, and update
# it if ANTHROPIC_EXTRACTION_MODEL changes to a model not listed here.
ANTHROPIC_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}


def estimate_anthropic_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Rough cost estimate from accumulated token counts; None for an unlisted model."""
    prices = ANTHROPIC_PRICING_PER_MTOK.get(model)
    if not prices:
        return None
    input_price, output_price = prices
    return round(input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price, 4)


def build_profile(
    actor: dict[str, Any],
    documents: list[tuple[str, ParsedDocument]],
    ollama: OllamaClient | AnthropicClient | None = None,
    coverage: dict[str, dict[str, Any]] | None = None,
    site_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, float]:
    host = urlparse(actor["official_url"]).netloc
    site_profile = site_profile or DEFAULT_SITE_PROFILE
    links: list[dict[str, Any]] = []
    block_paths: dict[str, int] = {}
    for _, document in documents:
        links.extend(document.links)
        for block in document.blocks:
            block_paths[block.path] = block_paths.get(block.path, 0) + 1
    links.sort(key=lambda item: -int(item.get("score", 0)))
    preliminary = {
        "profile_version": int(site_profile.get("profile_version", 1)),
        "domain": host,
        "strategy": "adaptive" if actor.get("priority") else "generic",
        "entry_points": list(dict.fromkeys(item["url"] for item in links))[:30] or [actor["official_url"]],
        "priority_sections": ["application", "case_study", "project", "news", "service", "capability", "technology"],
        "section_coverage": coverage or {},
        "content_paths": [item[0] for item in sorted(block_paths.items(), key=lambda value: value[1], reverse=True)[:12]],
        "exclude": ["nav", "header", "footer", "aside", "form", "cookie"],
        "render_mode": "html",
        "deterministic": {
            "languages": site_profile.get("languages", []),
            "crawl": site_profile.get("crawl", {}),
            "priority_paths": site_profile.get("priority_paths", []),
            "ignore_paths": site_profile.get("ignore_paths", []),
            "preferred_content_roots": site_profile.get("preferred_content_roots", []),
            "extra_candidate_selectors": site_profile.get("extra_candidate_selectors", []),
        },
    }
    allow_ollama = bool(site_profile.get("ollama_profile_assist", False))
    if not allow_ollama or not actor.get("priority") or not ollama or not ollama.available():
        preliminary["confidence"] = 0.72 if documents and any(document.blocks for _, document in documents) else 0.25
        return preliminary, "deterministic", preliminary["confidence"]

    inventory = []
    for url, document in documents[:8]:
        inventory.append({
            "url": url,
            "title": document.title,
            "category": document.page_type,
            "score": document.page_score,
            "extraction_method": document.extraction_method,
            "links": document.links[:25],
            "blocks": [
                {
                    "heading": block.heading,
                    "h1": block.h1,
                    "h2": block.h2,
                    "h3": block.h3,
                    "path": block.path,
                    "sample": block.text[:280],
                }
                for block in document.blocks[:12]
            ],
        })
    system = (
        "Tu aides à diagnostiquer un profil de site industriel déjà construit de manière déterministe. "
        "Priorise applications, projets et actualités. Réponds uniquement en JSON. "
        "N'invente aucune URL ni sélecteur absent de l'inventaire."
    )
    prompt = json.dumps({"actor": actor["name"], "domain": host, "preliminary_profile": preliminary, "inventory": inventory}, ensure_ascii=False)
    try:
        answer = ollama.ask_json(system, prompt)
        if not isinstance(answer, dict):
            raise ValueError("Profil IA non structuré")
        allowed_urls = {item["url"] for item in links} | {url for url, _ in documents} | {actor["official_url"]}
        entry_points = [url for url in answer.get("entry_points", []) if url in allowed_urls][:30]
        content_paths = [path for path in answer.get("content_paths", []) if path in block_paths][:12]
        preliminary["entry_points"] = entry_points or preliminary["entry_points"]
        preliminary["content_paths"] = content_paths or preliminary["content_paths"]
        preliminary["confidence"] = min(1.0, max(0.0, float(answer.get("confidence", 0.82))))
        return preliminary, f"deterministic+ollama:{ollama.model}", preliminary["confidence"]
    except Exception:
        preliminary["confidence"] = 0.58
        return preliminary, "deterministic-fallback", preliminary["confidence"]


def block_payload(block: ContentBlock) -> dict[str, str]:
    return {
        "heading": block.heading,
        "h1": block.h1,
        "h2": block.h2,
        "h3": block.h3,
        "text": block.text,
        "path": block.path,
        "media_context": block.media_context,
        "editorial_group_id": block.editorial_group_id,
        "fingerprint": block.fingerprint,
    }


def profile_json(profile: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()
