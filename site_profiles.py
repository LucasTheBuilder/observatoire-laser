from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urljoin, urlparse

DEFAULT_SITE_PROFILE: dict[str, Any] = {
    "profile_version": 2,
    "ollama_profile_assist": False,
    "languages": ["en", "fr", "de", "es", "it"],
    "crawl": {
        "priority_budget": 12,
        "standard_budget": 6,
        "max_depth": 2,
        "max_links_per_page": 160,
        # Coverage-first: an uncovered strategic family receives a large temporary boost.
        "coverage_boost": 500,
    },
    # Minimum number of visited pages to aim for when the family is discovered.
    # The queue may then deepen the best-scoring families with remaining budget.
    "coverage_targets": {
        "service": 1,
        "capability": 1,
        "technology": 1,
        "application": 1,
        "case_study": 1,
        "market": 1,
        "project": 1,
        "news": 1,
    },
    "coverage_order": [
        "service", "capability", "technology", "application", "case_study",
        "market", "project", "news", "product", "publication", "equipment", "other",
    ],
    # Explicit deterministic hubs are useful when menus are incomplete or JS-driven.
    # Paths are joined to the actor's official URL and are still subject to HTTP validation.
    "seed_paths": [],
    "priority_paths": [],
    "ignore_paths": [
        "/contact", "/privacy", "/cookies", "/legal", "/mentions-legales",
        "/careers", "/career", "/jobs", "/login", "/newsletter",
    ],
    "page_type_boosts": {
        "application": 12,
        "case_study": 12,
        "project": 8,
        "news": 3,
        "service": 16,
        "capability": 16,
        "technology": 14,
        "market": 12,
        "product": 8,
        "publication": 2,
        "equipment": -5,
        "about": -8,
        "other": 0,
    },
    # Market extraction must not be monopolised by one page family.
    "market_source_quotas": {
        "service_capability": 3,
        "application_market": 3,
        "technology": 2,
        "project": 2,
        "news": 1,
        "other": 1,
    },
    "preferred_content_roots": ["main", "[role=main]", "body"],
    "extra_candidate_selectors": [],
    # Conservative editorial segmentation: it activates only when the semantic
    # extractor returns very few blocks despite a page containing several headings.
    "heading_segmentation": {
        "enabled": True,
        "levels": [2, 3, 4],
        "min_chars": 55,
        "max_chars": 4500,
        "max_segments": 60,
        "trigger_max_semantic_blocks": 3,
        "trigger_min_headings": 4,
    },
    "editorial_units": {
        "min_chars": 45,
        "max_chars": 3500,
        "min_repeated_units": 3,
        "max_groups": 80,
        "parent_rescue_min_units": 3,
        "refine_large_block_chars": 900,
        "refine_min_units": 2,
        "min_coverage_ratio": 0.52,
        "coverage_rescue_min_units": 3,
    },
    "quality_extraction": {
        "oversized_block_chars": 1500,
        "oversized_split_min_units": 3,
        "linked_item_min_title_chars": 24,
        "linked_item_max_chars": 900,
        "linked_item_min_items": 3,
    },
    "priority_terms": [
        "femtosecond", "ultrafast", "ultrashort", "laser", "micromachining",
        "micro-machining", "texturing", "structuring", "manufacturing",
    ],
}


# Overrides intentionally remain compact. The generic crawler stays authoritative;
# profiles only encode stable high-value hubs and site-specific exceptions.
SITE_OVERRIDES: dict[str, dict[str, Any]] = {
    "ALPHANOV": {
        "seed_paths": [
            "/produits-et-services/procedes-laser",
            "/en/products-and-services/laser-machining-and-micro-machining",
            "/secteurs-applicatifs/lasers",
            "/en/application-sectors/lasers",
            "/produits-et-services/",
            "/en/products-and-services/",
            "/secteurs-applicatifs/",
            "/en/application-sectors/",
            "/collaborative-projects/",
            "/news/",
        ],
        "priority_paths": [
            "/produits-et-services/", "/products-and-services/", "/en/products-and-services/",
            "/secteurs-applicatifs/", "/application-sectors/", "/en/application-sectors/",
            "/collaborative-projects/", "/projets-collaboratifs/",
            "/applications/", "/news/", "/actualites/",
        ],
        "priority_terms": [
            "procédés laser", "procedes laser", "laser machining", "micro-machining",
            "surface engineering", "ingénierie de surface", "transparent materials",
            "matériaux transparents", "pilot line", "collaborative project",
        ],
        "coverage_targets": {
            "service": 2,
            "capability": 1,
            "technology": 2,
            "application": 1,
            "case_study": 1,
            "market": 1,
            "project": 1,
            "news": 1,
        },
    },
    "MANUTECH USD": {
        "seed_paths": ["/en/applications/", "/en/projects/", "/en/blog/"],
        "priority_paths": ["/applications/", "/projects/", "/blog/"],
        "extra_candidate_selectors": ["[class*=application]", "[class*=project]"],
    },
    "HEF": {
        "priority_paths": ["/project", "/projects", "/laser", "/innovation"],
    },
    "LASEA": {
        "priority_paths": ["/applications/", "/services/", "/technology/", "/news/"],
        "priority_terms": ["laser micromachining", "laser processing"],
    },
    "Pulsar Photonics": {
        "priority_paths": ["/laser-contract-manufacturing/", "/applications/", "/technology/", "/news/"],
        "priority_terms": ["contract manufacturing", "laser contract manufacturing"],
    },
}


DOMAIN_OVERRIDES: dict[str, dict[str, Any]] = {
    "alphanov.com": SITE_OVERRIDES["ALPHANOV"],
    "manutech-usd.fr": SITE_OVERRIDES["MANUTECH USD"],
    "hef.group": SITE_OVERRIDES["HEF"],
    "lasea.eu": SITE_OVERRIDES["LASEA"],
    "pulsar-photonics.de": SITE_OVERRIDES["Pulsar Photonics"],
}


def _merge_profile(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = list(dict.fromkeys([*merged[key], *value]))
        else:
            merged[key] = deepcopy(value)
    return merged


def get_site_profile(actor: dict[str, Any] | None = None, *, url: str | None = None) -> dict[str, Any]:
    actor = actor or {}
    profile = deepcopy(DEFAULT_SITE_PROFILE)
    actor_name = str(actor.get("name") or "").strip()
    if actor_name in SITE_OVERRIDES:
        profile = _merge_profile(profile, SITE_OVERRIDES[actor_name])

    source_url = url or str(actor.get("official_url") or "")
    host = urlparse(source_url).netloc.lower().removeprefix("www.")
    for domain, override in DOMAIN_OVERRIDES.items():
        if host == domain or host.endswith(f".{domain}"):
            profile = _merge_profile(profile, override)
            break

    profile["actor_name"] = actor_name or None
    profile["domain"] = host or None
    return profile


def crawl_budget(profile: dict[str, Any], priority: bool) -> int:
    crawl = profile.get("crawl", {})
    return int(crawl.get("priority_budget" if priority else "standard_budget", 12 if priority else 6))


def seed_urls(profile: dict[str, Any], official_url: str) -> list[str]:
    """Return deterministic high-value entry points, preserving the official host/language routing."""
    urls = [official_url]
    for path in profile.get("seed_paths", ()):
        urls.append(urljoin(official_url.rstrip("/") + "/", str(path).lstrip("/")))
    return list(dict.fromkeys(urls))
