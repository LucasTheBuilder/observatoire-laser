"""Configuration du crawler par site (profils).

Ce module ne contient AUCUNE logique de crawl : c'est un fichier de paramètres qui répond
à une seule question : "pour tel site, avec quels réglages doit-on crawler ?"

Le crawler générique (voir scrapers.py: scrape_actors) est censé fonctionner sur n'importe
quel site industriel "raisonnable" grâce à DEFAULT_SITE_PROFILE. Quand un site a une
structure particulière (menu en JS, section clé mal détectée, etc.), on peut lui ajouter une
entrée dans SITE_OVERRIDES (par nom d'acteur) ou DOMAIN_OVERRIDES (par nom de domaine) qui
vient fusionner par-dessus (surcharger) quelques clés du profil par défaut, sans jamais le
remplacer entièrement (voir _merge_profile).

Le point d'entrée public utilisé par le reste du code est get_site_profile().
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urljoin, urlparse

# Profil par défaut appliqué à TOUS les acteurs, avant toute surcharge spécifique à un site.
DEFAULT_SITE_PROFILE: dict[str, Any] = {
    "profile_version": 2,
    "ollama_profile_assist": False,
    "languages": ["en", "fr", "de", "es", "it"],
    # Paramètres qui pilotent l'exploration (crawl) du site : combien de pages visiter,
    # jusqu'à quelle profondeur de liens, et de combien "booster" un type de page pas
    # encore couvert (voir scrapers._effective_crawl_score).
    "crawl": {
        "priority_budget": 12,   # nb de pages max visitées si l'acteur est "priority"
        "standard_budget": 6,    # nb de pages max visitées sinon
        "max_depth": 2,          # profondeur max de liens suivis depuis les pages de départ
        "max_links_per_page": 160,
        # Coverage-first: an uncovered strategic family receives a large temporary boost.
        "coverage_boost": 500,
    },
    # Minimum number of visited pages to aim for when the family is discovered.
    # The queue may then deepen the best-scoring families with remaining budget.
    # Objectif minimal de pages visitées par "famille" de page (service, techno, etc.)
    # une fois qu'au moins une page de cette famille a été découverte. Le budget restant
    # sert ensuite à approfondir les familles les mieux notées (voir _effective_crawl_score).
    "coverage_targets": {
        "service": 1,
        "capability": 1,
        "technology": 1,
        # A real "Applications" section is usually a taxonomy of several distinct markets, not
        # one generic page -- a target of 1 satisfies the boost after the first hit and lets the
        # rest of a rich site (e.g. one page per vertical) starve for budget indefinitely.
        "application": 6,
        # §3.2 de l'audit (prestataires industriels) : "case_study n'a que 6 pages sur 3 000
        # dans toute la base -- la famille la plus riche en preuves d'exécution, et la moins
        # couverte". Un target de 1 suffisait à satisfaire le boost dès la première page
        # trouvée puis laissait le reste de la section (plusieurs études de cas par site,
        # comme "application") mourir de faim -- même raisonnement que ci-dessus.
        "case_study": 4,
        "market": 1,
        "project": 1,
        "news": 1,
    },
    # Ordre de priorité utilisé comme départage quand plusieurs familles sont encore
    # "sous-couvertes" en même temps (voir _effective_crawl_score).
    "coverage_order": [
        "service", "capability", "technology", "application", "case_study",
        "market", "project", "news", "product", "publication", "equipment", "other",
    ],
    # Explicit deterministic hubs are useful when menus are incomplete or JS-driven.
    # Paths are joined to the actor's official URL and are still subject to HTTP validation.
    # "seed_paths" : pages ajoutées d'office au tout début du crawl (voir seed_urls),
    # en plus de la page d'accueil de l'acteur -- utile si le menu du site ne suffit pas
    # à découvrir naturellement les pages importantes.
    "seed_paths": [],
    # "priority_paths" : fragments d'URL qui, s'ils apparaissent dans un lien découvert,
    # augmentent son score de priorité dans la file de crawl (voir hybrid.classify_source).
    "priority_paths": [],
    # "ignore_paths" : fragments d'URL à ne jamais suivre (pages sans intérêt métier :
    # contact, mentions légales, recrutement...).
    "ignore_paths": [
        "/contact", "/privacy", "/cookies", "/legal", "/mentions-legales",
        "/careers", "/career", "/jobs", "/login", "/newsletter",
    ],
    # Bonus/malus de score ajouté selon le type de page détecté (voir hybrid.classify_source) :
    # plus une page a de chances de contenir de l'info métier utile, plus son boost est élevé.
    "page_type_boosts": {
        "application": 12,
        # §3.2 : "à remonter fortement dans page_type_boosts et coverage_targets" -- passé
        # au-dessus de service/capability (16) plutôt qu'à égalité avec application (12),
        # pour que le crawler choisisse une étude de cas non encore vue avant d'approfondir
        # une famille déjà bien couverte.
        "case_study": 20,
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
    # Quand on choisit quelles pages déjà crawlées analyser pour en extraire des "market
    # facts" (voir scrapers._select_market_sources), ce quota limite combien de pages par
    # famille (service/capability, application/market, technology, ...) peuvent être prises
    # en 2ème passe, pour ne pas laisser une seule famille riche épuiser tout le budget.
    "market_source_quotas": {
        "service_capability": 3,
        "application_market": 6,
        "technology": 2,
        "project": 2,
        "news": 1,
        "other": 1,
    },
    # Sélecteurs CSS candidats pour la zone de contenu principale d'une page HTML
    # (voir hybrid.parse_document), essayés dans l'ordre jusqu'à trouver le premier existant.
    "preferred_content_roots": ["main", "[role=main]", "body"],
    "extra_candidate_selectors": [],
    # Conservative editorial segmentation: it activates only when the semantic
    # extractor returns very few blocks despite a page containing several headings.
    # Segmentation de secours : si l'extraction "sémantique" normale renvoie très peu de blocs
    # alors que la page a beaucoup de titres (H2/H3/H4), on redécoupe la page section par
    # section en se basant sur ces titres plutôt que de perdre le contenu.
    "heading_segmentation": {
        "enabled": True,
        "levels": [2, 3, 4],
        "min_chars": 55,
        "max_chars": 4500,
        "max_segments": 60,
        "trigger_max_semantic_blocks": 3,   # se déclenche seulement si <= 3 blocs trouvés...
        "trigger_min_headings": 4,          # ...alors qu'il y a >= 4 titres sur la page
    },
    # Paramètres de détection des "unités éditoriales répétées" (ex : une grille de cartes
    # produit/application identiques) afin de les traiter comme des éléments distincts plutôt
    # qu'un seul gros bloc de texte fourre-tout.
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
    # Seuils utilisés pour découper/filtrer des blocs de contenu de mauvaise qualité
    # (trop gros blocs fourre-tout, listes de liens sans assez de contexte, etc.).
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
    # Découverte des pages via le/les sitemap(s) déclarés dans robots.txt (voir
    # scrapers._discover_sitemap_urls) : borne le nombre de fichiers sitemap suivis (un
    # sitemap_index peut en référencer des dizaines) et le nombre total d'URLs de page
    # injectées comme sources -- la file de priorité "coverage-first" décide ensuite
    # lesquelles sont réellement visitées dans le budget de pages de l'acteur.
    "sitemap": {
        "enabled": True,
        "max_sitemaps": 15,
        "max_urls": 800,
    },
    # Nombre max de pages lues dans un PDF (brochure/datasheet/rapport annuel) -- voir
    # hybrid.parse_pdf_document. Les rapports d'activité peuvent faire plusieurs dizaines de
    # pages ; cette borne évite qu'un seul gros PDF n'engloutisse le temps de crawl d'un acteur.
    "pdf": {
        "max_pages": 30,
    },
}


# Overrides intentionally remain compact. The generic crawler stays authoritative;
# profiles only encode stable high-value hubs and site-specific exceptions.
# Clé = nom exact de l'acteur (voir db.ACTORS). Chaque valeur est un "override" partiel qui
# sera fusionné par-dessus DEFAULT_SITE_PROFILE via _merge_profile (voir get_site_profile).
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
            # case_study intentionally omitted: this override predates the §3.2 default bump
            # to 4 -- it was a straight copy of the (then-default) value 1, not a deliberate
            # cap, so it now inherits the higher default instead of silently overriding it back down.
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
    "FEMTOprint": {
        # The Applications hub is a full per-market taxonomy (one dedicated page per vertical) --
        # seeding them directly means the crawler doesn't have to wait for nav-link discovery to
        # reach them, and a higher application target keeps them boosted across more of the budget.
        "seed_paths": [
            "/applications.asp",
            "/applications/watchmaking-luxury.asp",
            "/applications/quantum.asp",
            "/applications/space.asp",
            "/applications/life-sciences.asp",
            "/applications/medtech.asp",
            "/applications/optics.asp",
            "/applications/photonics.asp",
            "/applications/research-development-sciences.asp",
        ],
        "priority_paths": ["/applications/"],
        "coverage_targets": {"application": 8},
        "market_source_quotas": {"application_market": 8},
    },
}


# Même mécanisme que SITE_OVERRIDES, mais indexé par nom de domaine plutôt que par nom
# d'acteur -- utile car get_site_profile() est parfois appelé seulement avec une URL, sans
# connaître l'acteur (ex : lors du crawl d'un lien découvert). Ici, chaque entrée réutilise
# directement le même dict que SITE_OVERRIDES : les deux mécanismes convergent volontairement
# vers un seul override par site.
DOMAIN_OVERRIDES: dict[str, dict[str, Any]] = {
    "alphanov.com": SITE_OVERRIDES["ALPHANOV"],
    "manutech-usd.fr": SITE_OVERRIDES["MANUTECH USD"],
    "hef.group": SITE_OVERRIDES["HEF"],
    "lasea.com": SITE_OVERRIDES["LASEA"],  # voir db.ACTORS : official_url réel, pas lasea.eu
    "pulsar-photonics.de": SITE_OVERRIDES["Pulsar Photonics"],
    "femtoprint.ch": SITE_OVERRIDES["FEMTOprint"],
}


def _merge_profile(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Fusionne récursivement un "override" par-dessus un profil de base (deep merge léger).

    - dict + dict -> mise à jour clé par clé (pas de remplacement total du sous-dictionnaire) ;
    - liste + liste -> concaténation en supprimant les doublons, en gardant l'ordre ;
    - toute autre valeur -> l'override remplace simplement la valeur de base.
    Le résultat est une copie profonde : ni `base` ni `override` ne sont modifiés en place.
    """
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
    """Point d'entrée principal du module : construit le profil effectif pour un acteur.

    Étapes :
    1. Part de DEFAULT_SITE_PROFILE (copie profonde, jamais modifié en place).
    2. Si le nom de l'acteur correspond à une entrée de SITE_OVERRIDES, la fusionne par-dessus.
    3. Si le domaine de l'URL (actor["official_url"] ou le paramètre `url`) correspond à une
       entrée de DOMAIN_OVERRIDES (match exact ou sous-domaine), la fusionne aussi.
    4. Ajoute des métadonnées pratiques (actor_name, domain) au profil final.

    Les deux mécanismes de surcharge (par nom, par domaine) pointent en général vers le même
    dict Python (voir DOMAIN_OVERRIDES ci-dessus qui référence SITE_OVERRIDES), donc dans la
    pratique un seul des deux `if` déclenche réellement un changement de profil.
    """
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
    """Nombre max de pages à visiter pour un acteur : plus élevé si l'acteur est "priority"."""
    crawl = profile.get("crawl", {})
    return int(crawl.get("priority_budget" if priority else "standard_budget", 12 if priority else 6))


def seed_urls(profile: dict[str, Any], official_url: str) -> list[str]:
    """Return deterministic high-value entry points, preserving the official host/language routing."""
    # Point de départ toujours inclus : la page d'accueil officielle de l'acteur.
    urls = [official_url]
    # Puis chaque "seed_paths" du profil est recollé (urljoin) à cette même base, pour rester
    # sur le bon domaine/la bonne langue plutôt que de coder une URL absolue en dur.
    for path in profile.get("seed_paths", ()):
        urls.append(urljoin(official_url.rstrip("/") + "/", str(path).lstrip("/")))
    # dict.fromkeys(...) est un idiome courant pour dédupliquer une liste en gardant l'ordre.
    return list(dict.fromkeys(urls))
