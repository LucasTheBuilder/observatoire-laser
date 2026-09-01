"""Moteur de collecte : crawl des sites acteurs, extraction de "faits marché", et collecte
de publications scientifiques. C'est le plus gros fichier du projet ; il se lit en 6 blocs :

1. HTTP poli (robots.txt, throttle par host, retries) : _fetch() et ses helpers.
2. Lexiques métier (MARKETS, COMPONENTS, OPERATIONS, PROCESS_TECHNOLOGIES, MATERIALS,
   PERFORMANCE_TERMS, LASER_RULES, MATURITY_RULES...) : des dictionnaires de règles de
   correspondance texte -> libellé canonique, qui remplacent un vrai NLP par une approche
   déterministe et auditable (chaque libellé retenu est traçable à un terme précis du texte).
3. Moteur de correspondance générique sur ces lexiques (_match_label, _rule_match_terms,
   _laser_match, _detect_maturity...) et de validation de "relation" entre marché/composant/
   opération dans un même passage de texte (_relation_evidence) pour éviter de recombiner à
   tort des informations qui viennent de deux endroits différents de la page.
4. Extraction des candidats à partir des blocs de contenu d'une page déjà parsée par
   hybrid.parse_document : _candidate() (fait marché complet), _offer_candidates() (capacité/
   offre concurrente), _ai_candidates() (repli IA conservateur avec file de relecture pour le
   vocabulaire inconnu).
5. Le crawler lui-même : scrape_actors() explore chaque site acteur avec une file de priorité
   (heap) qui favorise dynamiquement les types de page pas encore couverts (voir
   site_profiles.coverage_targets), et enregistre chaque page visitée dans actor_sources.
6. Les deux autres collectes : scrape_market() (relit les pages déjà crawlées et en extrait
   des faits, écrits en base via _upsert_market_candidate/_upsert_offer_candidate) et
   scrape_technology() (interroge l'API Crossref pour des publications récentes).
"""

from __future__ import annotations

import gzip
import hashlib
import heapq
import itertools
import json
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

import httpx

from db import (
    ACTORS_DB,
    BUCKET_RANK,
    DATE_CONFIDENCE_RANK,
    MARKET_DB,
    TECH_DB,
    application_key,
    classify_evidence_type,
    classify_source_date,
    compute_is_backfill,
    connect,
    language_from_url,
    market_fact_key,
    numeric_spec_tokens,
    offer_fact_key,
    purge_stale_backlog,
    technology_signal_key,
    upsert_actor_event,
    upsert_document,
    upsert_fact_source,
    upsert_technology_signal,
    utc_now,
)
from http_client import HEADERS, TIMEOUTS
from hybrid import (
    AnthropicClient,
    ContentBlock,
    OllamaClient,
    ParsedDocument,
    ai_cost_cap_reached,
    block_payload,
    build_profile,
    canonical_url,
    classify_source,
    get_ai_client,
    is_pdf_response,
    normalize_page_type,
    parse_document,
    parse_pdf_document,
    profile_json,
)

# Le lexique metier vit dans lexicon.py (voir son docstring) : il decrit le vocabulaire du
# domaine, pas le crawl. Reimporte ici sous ses noms d'origine -- underscore compris -- pour que
# le reste de ce module, et les tests qui importent depuis `scrapers`, restent inchanges.
from lexicon import (  # noqa: F401  (reexports pour les importateurs historiques)
    _GENERIC_PROCESS_AXES,
    APPLICATION_ARCHITECTURES,
    COMPONENTS,
    CONTRAST_CUES,
    LASER_RULES,
    MARKET_INFERENCE,
    MARKET_SYNONYM_CLUSTERS,
    MARKETS,
    MATERIALS,
    MATURITY_RULES,
    MORPHOLOGY_VARIANTS,
    NEGATION_CUES,
    OPERATIONS,
    PERFORMANCE_TERMS,
    PROCESS_TECHNOLOGIES,
    Lexicon,
    LexiconRule,
    _contains_term,
    _contains_term_normalized,
    _detect_maturity,
    _laser_match,
    _match_all_labels,
    _match_label_details,
    _normalize_text,
    _quote,
    _rule_match_terms,
    _rule_matches,
    _specificity_score,
    _term_pattern,
    _term_variants,
    is_on_topic,
)
from site_profiles import SITE_OVERRIDES, crawl_budget, get_site_profile, seed_urls

AiClient = OllamaClient | AnthropicClient

# L'identité HTTP sortante vit dans http_client.py, partagée avec tous les connecteurs (voir le
# docstring de ce module). Ces noms restent exposés ici parce que plusieurs modules les importent
# depuis scrapers de longue date ; les nouveaux appelants doivent importer http_client.
TIMEOUT = TIMEOUTS["crawl"]

# Politeness: a fixed per-host delay plus bounded retries on transient failures. Both are
# deliberately conservative defaults for small industrial/institutional sites that are not
# built to absorb bursty traffic; override via env vars if a faster/slower pace is needed.
CRAWL_DELAY_SECONDS = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
CRAWL_MAX_RETRIES = int(os.getenv("CRAWL_MAX_RETRIES", "2"))

# Chantier 6 : nb de versions archivées (db.page_versions) conservées par page -- borne la
# croissance de l'historique au fil des recrawls mensuels répétés, plutôt que de tout garder.
PAGE_VERSIONS_RETENTION = 5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
ROBOTS_CACHE_TTL_SECONDS = 6 * 3600

# === Bloc 1/6 : HTTP poli (robots.txt, throttle par host, retries) ===
_robots_cache: dict[str, tuple[float, "robotparser.RobotFileParser | None"]] = {}
_last_request_at: dict[str, float] = {}
# Plan d'action web-scraping, ce qu'on garde de "ce que je ne reprendrais pas" (Scrapy) :
# "Prenez plutôt les deux idées isolées : crawl_delay lu depuis robots.txt (robotparser
# l'expose déjà, vous l'ignorez) et un délai adaptatif à la latence." _last_latency_at n'est
# qu'un signal de congestion grossier (dernière latence observée par host), volontairement
# borné (ADAPTIVE_DELAY_CAP_SECONDS) pour qu'une seule requête lente ne fige jamais tout le
# crawl sur ce host.
_last_latency_seconds: dict[str, float] = {}
ADAPTIVE_DELAY_FACTOR = 0.5
ADAPTIVE_DELAY_CAP_SECONDS = 5.0


def _robots_parser(client: httpx.Client, origin: str) -> "robotparser.RobotFileParser | None":
    """Fetch and cache robots.txt for one origin. Any failure means 'no rules' (allow)."""
    now = time.monotonic()
    cached = _robots_cache.get(origin)
    if cached is not None and now - cached[0] < ROBOTS_CACHE_TTL_SECONDS:
        return cached[1]
    parser: robotparser.RobotFileParser | None
    try:
        response = client.get(f"{origin}/robots.txt", timeout=8.0)
        if response.status_code == 200:
            parser = robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
        else:
            parser = None
    except Exception:
        parser = None
    _robots_cache[origin] = (now, parser)
    return parser


def _robots_allowed(client: httpx.Client, url: str) -> bool:
    """True si robots.txt (mis en cache par _robots_parser) autorise notre user-agent à visiter cette URL."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return True
    origin = f"{parsed.scheme}://{parsed.netloc}"
    parser = _robots_parser(client, origin)
    if parser is None:
        return True
    try:
        return parser.can_fetch(HEADERS["User-Agent"], url)
    except Exception:
        return True


def _robots_crawl_delay(origin: str) -> float | None:
    """Reads the Crawl-delay directive robots.txt may declare for our user-agent. Reuses the
    parser _robots_allowed already cached via _robots_parser (always called before _throttle
    inside _fetch) -- no extra network round-trip just to read this."""
    cached = _robots_cache.get(origin)
    if cached is None or cached[1] is None:
        return None
    try:
        delay = cached[1].crawl_delay(HEADERS["User-Agent"])
    except Exception:
        return None
    return float(delay) if delay is not None else None


def _throttle(url: str) -> None:
    """Enforce a minimum delay between two requests to the same host -- the largest of our own
    default (CRAWL_DELAY_SECONDS), whatever Crawl-delay robots.txt asks our user-agent for, and
    an adaptive component that backs off further right after a slow response from that host."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_delay = _robots_crawl_delay(origin) or 0.0
    adaptive_delay = min(_last_latency_seconds.get(host, 0.0) * ADAPTIVE_DELAY_FACTOR, ADAPTIVE_DELAY_CAP_SECONDS)
    delay = max(CRAWL_DELAY_SECONDS, robots_delay, adaptive_delay)
    if delay <= 0:
        return
    now = time.monotonic()
    last = _last_request_at.get(host)
    if last is not None:
        remaining = delay - (now - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_request_at[host] = time.monotonic()


def _fetch(client: httpx.Client, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """GET one URL while respecting robots.txt, per-host throttling and transient-error retries.

    ``headers`` carries the conditional-GET pair (If-None-Match/If-Modified-Since, see
    scrape_actors) for this one request only, merged on top of the client's defaults -- a 304
    Not Modified is returned as-is (httpx only raises on 4xx/5xx), the caller decides what to
    do with it.
    """
    if not _robots_allowed(client, url):
        raise PermissionError(f"robots.txt interdit: {url}")
    attempt = 0
    while True:
        _throttle(url)
        host = urlparse(url).netloc.lower()
        started = time.monotonic()
        try:
            response = client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt >= CRAWL_MAX_RETRIES:
                raise
            time.sleep(2 ** attempt)
            attempt += 1
            continue
        _last_latency_seconds[host] = time.monotonic() - started
        if response.status_code in RETRYABLE_STATUS_CODES and attempt < CRAWL_MAX_RETRIES:
            time.sleep(2 ** attempt)
            attempt += 1
            continue
        # httpx.raise_for_status() treats ANY non-2xx as an error, 304 included ("Redirect
        # response" per its own error_types) -- confirmed against real sites (ALPHANOV, KMLT)
        # after this module first shipped conditional GET, where every 304 was being counted
        # as a crawl error instead of the valid "nothing changed" outcome it is.
        if response.status_code != 304:
            response.raise_for_status()
        return response


_SITEMAP_TAG = "loc"


def _sitemap_locations(client: httpx.Client, origin: str) -> list[str]:
    """URLs de sitemap à essayer pour ce site : celles déclarées via `Sitemap:` dans
    robots.txt (déjà téléchargé/mis en cache par _robots_parser -- RobotFileParser expose ces
    directives via .site_maps()), sinon l'emplacement conventionnel /sitemap.xml."""
    parser = _robots_parser(client, origin)
    locations: list[str] = []
    if parser is not None:
        try:
            locations = [str(item) for item in (parser.site_maps() or [])]
        except Exception:
            locations = []
    return locations or [f"{origin}/sitemap.xml"]


def _parse_sitemap_xml(content: bytes) -> tuple[list[str], list[str]]:
    """Parse un document sitemap (urlset ou sitemapindex, transparently gzippé ou non) et
    renvoie (urls de page, urls de sous-sitemaps) -- l'appelant décide s'il faut redescendre
    dans les sous-sitemaps (voir _discover_sitemap_urls)."""
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except Exception:
            return [], []
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return [], []
    locs = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == _SITEMAP_TAG and (element.text or "").strip()
    ]
    is_index = root.tag.rsplit("}", 1)[-1] == "sitemapindex"
    return ([], locs) if is_index else (locs, [])


def _discover_sitemap_urls(client: httpx.Client, official_url: str, profile: dict) -> list[str]:
    """Explore robots.txt puis le(s) sitemap(s) du site (en suivant les sitemap_index
    imbriqués, dans une limite de fichiers et d'URLs -- voir profile["sitemap"]) et renvoie la
    liste des URLs de page découvertes sur le même domaine que l'acteur.

    C'est l'amélioration décrite dans l'audit : au lieu de compter uniquement sur les liens
    trouvés en explorant les pages une à une (profondeur 2-3, budget de quelques pages),
    on lit la liste exhaustive que le site publie lui-même pour les moteurs de recherche.
    La priorisation reste inchangée : ces URLs ne font qu'entrer dans la même file
    "coverage-first" que les liens découverts normalement (voir scrape_actors).
    """
    cfg = profile.get("sitemap", {})
    if not cfg.get("enabled", True):
        return []
    parsed = urlparse(official_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_host = parsed.netloc.lower().removeprefix("www.")
    max_sitemaps = int(cfg.get("max_sitemaps", 15))
    max_urls = int(cfg.get("max_urls", 800))

    queue = list(dict.fromkeys(_sitemap_locations(client, origin)))
    seen: set[str] = set()
    page_urls: list[str] = []
    fetched = 0
    while queue and fetched < max_sitemaps and len(page_urls) < max_urls:
        sitemap_url = queue.pop(0)
        key = canonical_url(sitemap_url)
        if key in seen:
            continue
        seen.add(key)
        if urlparse(sitemap_url).netloc.lower().removeprefix("www.") != base_host:
            continue
        try:
            response = _fetch(client, sitemap_url)
            locs, children = _parse_sitemap_xml(response.content)
        except Exception:
            continue
        fetched += 1
        for loc in locs:
            if urlparse(loc).netloc.lower().removeprefix("www.") == base_host:
                page_urls.append(loc)
        queue.extend(child for child in children if canonical_url(child) not in seen)
    return page_urls[:max_urls]


# Requêtes bibliographiques envoyées à l'API Crossref par scrape_technology(), une par ligne.
TECHNOLOGY_QUERIES = (
    "femtosecond laser micromachining",
    "ultrafast laser processing manufacturing",
    "femtosecond laser surface texturing",
    "selective laser etching femtosecond",
    "femtosecond laser glass microfabrication",
    "femtosecond laser semiconductor processing",
)











def _load_custom_lexicon_entries(target: dict[str, Lexicon] | None = None) -> None:
    """Merge operator-accepted vocabulary_candidates (db.accept_vocabulary_candidate) into
    the live lexicons, so a label promoted through the triage queue is usable immediately --
    no code change or redeploy needed to grow recall.

    ``target`` defaults to the real module-level MARKETS/COMPONENTS/OPERATIONS dicts, mutated
    in place (this is process-lifetime state, matching a long-running server); pass explicit
    dicts to check the merge logic in isolation without touching that shared state.
    """
    target = target if target is not None else {"market": MARKETS, "component": COMPONENTS, "operation": OPERATIONS}
    try:
        with connect(MARKET_DB) as db:
            entries = db.execute("SELECT dimension,label,match_terms FROM custom_lexicon_entries").fetchall()
    except Exception:
        return
    for row in entries:
        lexicon = target.get(row["dimension"])
        if lexicon is None:
            continue
        try:
            terms = tuple(json.loads(row["match_terms"]))
        except (TypeError, ValueError):
            continue
        if terms:
            lexicon[row["label"]] = {"any_of": terms}

# Terms that very often belong to menus/legal/navigation rather than technical evidence.
NAVIGATION_NOISE = (
    "cookie policy", "privacy policy", "terms of use", "all rights reserved",
    "skip to content", "sign in", "log in", "newsletter", "contact us",
)

# Bibliography citation entries (a reference list item, not a company's own claim) are
# reliably recognizable by their DOI link, present verbatim regardless of language or page
# formatting -- e.g. "https://doi.org/10.2961/jlmn.2015.02.0022 Fornaroli, C., Holtkamp, J....".
# Audit v8 §2.3: two Fraunhofer ILT facts were extracted straight from a DOI reference list.
_DOI_PATTERN = re.compile(r"\bdoi\.org/10\.\d{4,9}/", re.IGNORECASE)

# "Read more"/breadcrumb-style fragments observed leaking into extracted blocks as menu debris
# rather than editorial content (audit v8 §2.3: Kirana "Navigation R&D Femtosecond
# Micromachining", Workshop of Photonics "Read more..."). Kept separate from NAVIGATION_NOISE:
# these terms are only trusted as a noise signal in combination with the structural check in
# _is_noise_block (short, no sentence punctuation) -- "read more" appearing inside an actual
# sentence is legitimate prose, not menu debris.
_MENU_FRAGMENT_TERMS = (
    "read more", "learn more", "find out more", "voir plus", "en savoir plus",
    "back to top", "retour en haut", "share this", "partager", "navigation", "breadcrumb",
)



# Marqueurs assertifs requis pour qu'une fenêtre de relation compte comme une AFFIRMATION plutôt
# qu'un fragment de menu/titre/liste (§10.6 audit veille) : sur les 71 faits acceptés analysés à
# la main, 54 (76%) ne contenaient aucun de ces marqueurs -- ce sont des titres de page ("Laser
# Micro Drilling") ou des fragments de liste ("Applications/Markets: medical technology,
# microelectronics..."), pas des phrases affirmant un lien composant->opération. Comme pour
# NEGATION_CUES, c'est un proxy syntaxique lexical (présence d'un marqueur), pas une analyse
# grammaticale réelle du prédicat -- volontairement conservateur : listé au lieu d'inféré.
PREDICATE_CUES = (
    "is", "are", "was", "were", "provides", "provide", "offers", "offer", "enables", "enable",
    "uses", "use", "using", "used", "performs", "perform", "delivers", "deliver",
    "produces", "produce", "manufactures", "manufacture", "specializes", "specialises",
    "enters", "enter", "reaches", "reach", "achieves", "achieve", "features", "feature",
    "includes", "include", "combines", "combine", "supports", "support",
    "we", "our", "develops", "develop",
    "propose", "proposons", "offre", "offrons", "fournit", "fournissons",
    "utilise", "utilisons", "permet", "permettent", "réalise", "realise", "réalisons", "realisons",
    "assure", "assurons", "produisons", "fabrique", "fabriquons", "développe", "developpe",
    "bietet", "ermöglicht", "ermoglicht", "verwendet", "liefert", "nutzt",
)




# Adaptive depth (P1 audit item): a page that already reads as service/capability/application/
# technology is exactly where a deeper job-shop/contract-manufacturing/Lohnfertigung page is
# likely to sit one click further in -- static max_depth would stop just short of it.
HIGH_VALUE_PAGE_TYPES = frozenset({"service", "capability", "application", "technology"})

# Compatibilité avec d'éventuels imports externes : inclut aussi les règles regex.
def _compat_terms(rules: Lexicon) -> tuple[str, ...]:
    terms: list[str] = []
    for label, rule in rules.items():
        terms.extend(rule.get("all_of", ()))
        terms.extend(rule.get("any_of", ()))
        # Pour les règles uniquement regex, le label reste un terme de compatibilité utile.
        if rule.get("regex") and not rule.get("all_of") and not rule.get("any_of"):
            terms.append(label)
    return tuple(dict.fromkeys(terms))

LASER_TERMS = _compat_terms(LASER_RULES)
INDUSTRIAL_TERMS = MATURITY_RULES[0][2]
RADAR_TERMS = tuple(term for _, bucket, terms in MATURITY_RULES[1:] if bucket == "radar" for term in terms)





















def _match_label(text: str, lexicon: Lexicon) -> str | None:
    """Comme _match_label_details, mais ne renvoie que le libellé (sans les termes trouvés)."""
    return _match_label_details(text, lexicon)[0]




def _matching_terms(text: str, lexicon: Lexicon) -> list[str]:
    hits: list[str] = []
    for rule in lexicon.values():
        hits.extend(_rule_match_terms(text, rule))
    return list(dict.fromkeys(hits))




# §4.A audit veille (30/08/2026, Lot 2 §2.3) : "un acteur qui recrute trois process engineer --
# ultrafast laser annonce sa direction technique 12 mois avant sa page produit." Un intitulé de
# poste n'entre en base que s'il combine un terme laser ultra-rapide (LASER_RULES, via
# _laser_match) ET un rôle technique de cette liste dans le MÊME bloc -- une offre "assistant
# commercial" ou "comptable" sur la même page carrières ne doit jamais créer de signal.
CAREER_ROLE_TERMS = (
    "process engineer", "r&d engineer", "research engineer", "applications engineer",
    "optical engineer", "photonics engineer", "laser engineer", "systems engineer",
    "manufacturing engineer", "product engineer", "development engineer", "test engineer",
    "ingenieur procede", "ingenieur r&d", "ingenieur recherche", "ingenieur photonique",
    "ingenieur laser", "ingenieur applications", "ingenieur developpement",
    "entwicklungsingenieur", "anwendungsingenieur", "verfahrensingenieur",
)


def _extract_career_signal(block: ContentBlock) -> str | None:
    """Renvoie l'intitulé (tronqué) si ce bloc d'une page carrières combine un terme laser
    ultra-rapide et un rôle technique connu, sinon None. Voir CAREER_ROLE_TERMS."""
    text = " ".join(filter(None, (block.heading, block.text)))
    if not text or not _laser_match(text) or not any(_contains_term(text, term) for term in CAREER_ROLE_TERMS):
        return None
    return (block.heading or text)[:200]










# === Bloc 4/6 : extraction des candidats à partir des blocs de contenu d'une page ===
# Un ContentBlock (défini dans hybrid.py) est un fragment de contenu déjà extrait/nettoyé
# d'une page HTML (un paragraphe, une carte produit, une section...), avec sa hiérarchie de
# titres (h1/h2/h3/heading) et un identifiant de "groupe éditorial" (editorial_group_id) qui
# relie les blocs issus d'un même élément répété (ex: les blocs d'une même carte produit).
def _context_for_block(title: str, blocks: list[ContentBlock], index: int) -> tuple[str, str]:
    """Return direct block text and strict local section context.

    Page title is deliberately excluded from market/component/operation classification.  It may
    only be used by `_laser_context` to establish that the page belongs to the ultrafast-laser
    topic.  This prevents a generic page title or introductory market label from contaminating
    unrelated Publications / News / Related projects blocks.
    """
    block = blocks[index]
    direct = " ".join(dict.fromkeys(filter(None, (block.heading, block.text, block.media_context))))
    hierarchy = (getattr(block, "h1", ""), getattr(block, "h2", ""), getattr(block, "h3", ""))
    section = " ".join(dict.fromkeys(filter(None, (*hierarchy, block.heading, block.text, block.media_context))))
    return direct, section


def _sentences(text: str) -> list[str]:
    """Small deterministic sentence/window splitter used for relation validation."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    # Semicolons and bullets are meaningful separators on product/project/publication cards.
    parts = re.split(r"(?<=[.!?])\s+|\s*[•·▪◦]\s*|\s*;\s*", clean)
    return [part.strip() for part in parts if len(part.strip()) >= 12]


def _is_negated(text: str) -> bool:
    """Conservative negation/contrast guard for one relation window (sentence, heading+sentence
    pair, or combined structured unit). Kept lexicon-based, like the rest of this module, rather
    than attempting real negation-scope parsing."""
    return any(_contains_term(text, cue) for cue in (*NEGATION_CUES, *CONTRAST_CUES))


def _has_predicate(text: str) -> bool:
    """§10.6 audit veille: require an assertive marker (verb or first-person possessive) in the
    accepted window, so a relation isn't built from a bare menu fragment or page title that
    happens to contain market+component+operation terms with nothing actually asserting the
    link between them."""
    return any(_contains_term(text, cue) for cue in PREDICATE_CUES)


def _section_role(block: ContentBlock) -> str:
    """Devine à quelle zone éditoriale appartient un bloc (publication/news/project/
    application/service/content) d'après ses titres H2/H3, pour ajuster les règles de
    propagation de contexte plus loin (une zone "publication"/"news"/"project" ne propage
    jamais son marché/composant/opération aux blocs voisins -- voir _structured_neighbors).
    Describe the local editorial zone without excluding it from market evidence."""
    heading = _normalize_text(" ".join(filter(None, (block.h2, block.h3, block.heading))))
    roles = (
        ("publication", ("publication", "publications", "paper", "papers", "reference", "references", "bibliography", "bibliographie")),
        ("news", ("news", "latest news", "actualite", "actualites", "nouveaute", "nouveautes", "press")),
        ("project", ("related project", "related projects", "collaborative project", "collaborative projects", "projet", "projets")),
        ("application", ("application", "applications", "use case", "case study")),
        ("service", ("service", "services", "capability", "capabilities", "prestation", "prestations")),
    )
    for role, terms in roles:
        if any(_contains_term(heading, term) for term in terms):
            return role
    return "content"


def _core_labels(text: str) -> tuple[str | None, str | None, str | None]:
    """Les 3 dimensions "core" (marché, composant, opération) détectées dans `text`, chacune
    au sens du MEILLEUR match seulement (voir _match_label_details) -- contrairement à
    _core_label_sets qui renvoie TOUS les matches possibles."""
    return (
        _match_label_details(text, MARKETS)[0],
        _match_label_details(text, COMPONENTS)[0],
        _match_label_details(text, OPERATIONS)[0],
    )


def _editorial_group_key(block: ContentBlock) -> str:
    """Return the strict micro-context identifier for relation sharing."""
    return block.editorial_group_id


def _is_isolated_editorial_item(block: ContentBlock) -> bool:
    """Vrai si ce bloc fait partie d'une carte répétée ou d'un item de liste (voir hybrid.py) :
    ces blocs ne partagent jamais leur contexte avec leurs voisins, pour éviter qu'une info
    d'une carte "fuite" vers la carte suivante (ex : deux applications différentes côte à côte)."""
    marker = (block.path or "").partition("::")[2].casefold()
    return marker in {"repeated", "linked-item"}


def _structured_neighbors(blocks: list[ContentBlock], index: int, radius: int = 2) -> list[ContentBlock]:
    """Return only adjacent blocks owned by the exact same editorial micro-context.

    Repeated cards and linked list items are deliberately isolated.  This prevents a market
    from one application card being combined with a component/operation from a sibling card.
    """
    current = blocks[index]
    if _section_role(current) in {"publication", "news", "project"} or _is_isolated_editorial_item(current):
        return []
    key = _editorial_group_key(current)
    if not key:
        return []
    result: list[ContentBlock] = []
    lo, hi = max(0, index - radius), min(len(blocks), index + radius + 1)
    for pos in range(lo, hi):
        if pos == index:
            continue
        candidate = blocks[pos]
        if _section_role(candidate) in {"publication", "news", "project"} or _is_isolated_editorial_item(candidate):
            continue
        if _editorial_group_key(candidate) == key:
            result.append(candidate)
    return result


def _core_label_sets(text: str) -> tuple[set[str], set[str], set[str]]:
    """Return every matching canonical market/component/operation label."""
    return (
        {label for label, _ in _match_all_labels(text, MARKETS)},
        {label for label, _ in _match_all_labels(text, COMPONENTS)},
        {label for label, _ in _match_all_labels(text, OPERATIONS)},
    )


def _relation_window_is_ambiguous(text: str) -> bool:
    """Reject Cartesian-product windows containing several independent applications.

    Multiple operations can legitimately describe one component. Multiple market labels are
    tolerated only when a *single* detected component canonically resolves one of them (e.g.
    IOL -> Médical while the word "lens" also triggers Optique).
    """
    markets, components, _ = _core_label_sets(text)
    if len(components) > 1:
        inferred_markets = {_infer_market(component, None) for component in components}
        inferred_markets.discard(None)
        # Several component labels may describe the same semiconductor/battery object hierarchy
        # (e.g. wafer + advanced packaging). Accept only when they converge on one explicit market.
        if not (len(inferred_markets) == 1 and next(iter(inferred_markets)) in markets):
            return True
    if len(markets) <= 1:
        return False
    if components:
        inferred_markets = {_infer_market(component, None) for component in components}
        inferred_markets.discard(None)
        if len(inferred_markets) == 1 and next(iter(inferred_markets)) in markets:
            return False
    # No component to anchor on: tolerate markets that are near-synonyms in this industry's
    # vocabulary (see MARKET_SYNONYM_CLUSTERS) instead of flagging them as two applications.
    if any(markets <= cluster for cluster in MARKET_SYNONYM_CLUSTERS):
        return False
    return True


def _resolved_core_labels(text: str) -> tuple[str | None, str | None, str | None]:
    """Resolve one coherent core triple, preferring explicit component-linked market semantics."""
    market, component, operation = _core_labels(text)
    if component:
        inferred = _infer_market(component, None)
        explicit_markets = {label for label, _ in _match_all_labels(text, MARKETS)}
        if inferred and inferred in explicit_markets:
            market = inferred
    return market, component, operation


def _is_multi_context_block(block: ContentBlock) -> bool:
    """Detect broad containers that enumerate several applications/components.

    Such blocks may still yield a *direct* sentence-level fact, but must not use adjacent-window
    or structured propagation because that would recreate cross-card contamination.
    """
    markets, components, _ = _core_label_sets(block.text)
    return len(markets) > 1 or len(components) > 1

def _inc_diagnostic(diagnostics: dict[str, int] | None, key: str, amount: int = 1) -> None:
    """Incrémente un compteur de télémétrie (ex: "relation_too_weak", "no_laser_context") si un
    dict `diagnostics` est fourni -- sert à comprendre après coup pourquoi tel bloc a/n'a pas
    produit de fait (voir le résumé stocké dans collection_runs.diagnostics_json)."""
    if diagnostics is not None:
        diagnostics[key] = int(diagnostics.get(key, 0)) + amount


def _relation_evidence(
    block: ContentBlock,
    structured_blocks: list[ContentBlock] | None = None,
    diagnostics: dict[str, int] | None = None,
    page_market: str | None = None,
) -> tuple[str | None, str]:
    """Validate Market ↔ Component ↔ Operation without cross-context recombination.

    ``direct`` is sentence-local; ``contextual`` uses only the block's immediate heading or a
    two-sentence window; ``structured`` may cross blocks only when they share the exact
    ``editorial_group_id``. Repeated cards/list items are isolated micro-contexts. A window
    carrying an explicit negation/contrast cue (see NEGATION_CUES/CONTRAST_CUES) is skipped even
    when it would otherwise satisfy the lexical relation, since the sentence is denying or
    contrasting the claim rather than making it (e.g. "unlike laser cutting, we use..."). A
    window with no assertive marker at all (see PREDICATE_CUES, §10.6 audit veille) is skipped
    too: it is far more likely a menu fragment or page title than an actual claim.

    ``page_market`` (chantier 2 item 1, see _url_market_hint) is tried last, only once steps
    1-4 have all failed to find an in-block market: it lets an unambiguous page-level market
    (from the URL/breadcrumb, e.g. "/applications/medical/") stand in for a market that would
    otherwise never appear in the block's own text.
    """
    sentences = _sentences(block.text)
    if not sentences:
        return None, ""

    # 1) Direct statement.  A sentence containing several different markets/components is an
    # application list, not a safe relation window.
    for sentence in sentences:
        market, component, operation = _resolved_core_labels(sentence)
        if market and component and operation and not _relation_window_is_ambiguous(sentence):
            if _is_negated(sentence):
                _inc_diagnostic(diagnostics, "relation_negated_rejected")
                continue
            if not _has_predicate(sentence):
                _inc_diagnostic(diagnostics, "relation_no_predicate_rejected")
                continue
            return "direct", sentence[:900]

    multi_context = _is_multi_context_block(block)

    # 2) Immediate H2/H3/heading can supply one unambiguous market to a sentence binding the
    # component and operation. H1 remains excluded because it is page-wide.
    local_heading = " ".join(dict.fromkeys(filter(None, (block.h2, block.h3, block.heading))))
    heading_markets, _, _ = _core_label_sets(local_heading)
    if len(heading_markets) == 1:
        for sentence in sentences:
            _, component, operation = _core_labels(sentence)
            if component and operation and not _relation_window_is_ambiguous(sentence):
                evidence = f"{local_heading} | {sentence}"
                if not _relation_window_is_ambiguous(evidence):
                    if _is_negated(sentence) or _is_negated(local_heading):
                        _inc_diagnostic(diagnostics, "relation_negated_rejected")
                        continue
                    if not _has_predicate(evidence):
                        _inc_diagnostic(diagnostics, "relation_no_predicate_rejected")
                        continue
                    return "contextual", evidence[:900]

    # 3) Adjacent sentence windows are allowed only inside a single-purpose block.
    if not multi_context:
        for i in range(len(sentences) - 1):
            window = f"{sentences[i]} {sentences[i + 1]}"
            market, component, operation = _resolved_core_labels(window)
            if market and component and operation and not _relation_window_is_ambiguous(window):
                density = max(
                    sum(bool(x) for x in _core_labels(sentences[i])),
                    sum(bool(x) for x in _core_labels(sentences[i + 1])),
                )
                if density >= 2:
                    if _is_negated(window):
                        _inc_diagnostic(diagnostics, "relation_negated_rejected")
                        continue
                    if not _has_predicate(window):
                        _inc_diagnostic(diagnostics, "relation_no_predicate_rejected")
                        continue
                    return "contextual", window[:900]

    # 4) Structured relation: every contributing block must belong to exactly the same local
    # editorial group. Broad multi-application parents and card/list items cannot propagate.
    neighbors = structured_blocks or []
    if (
        neighbors
        and not multi_context
        and _section_role(block) not in {"publication", "news", "project"}
        and not _is_isolated_editorial_item(block)
    ):
        group_id = _editorial_group_key(block)
        units = [block, *[u for u in neighbors if _editorial_group_key(u) == group_id]]
        unit_texts = [" ".join(filter(None, (u.h2, u.h3, u.heading, u.text))) for u in units]
        combined = " ".join(dict.fromkeys(unit_texts))
        market, component, operation = _resolved_core_labels(combined)
        densities = [sum(bool(value) for value in _core_labels(text)) for text in unit_texts]
        if (
            market and component and operation
            and max(densities, default=0) >= 2
            and not _relation_window_is_ambiguous(combined)
        ):
            if _is_negated(combined):
                _inc_diagnostic(diagnostics, "relation_negated_rejected")
            elif not _has_predicate(combined):
                _inc_diagnostic(diagnostics, "relation_no_predicate_rejected")
            else:
                return "structured", combined[:1400]

    # 5) Page-level market: last resort, only once nothing above supplied a market at all.
    # Mirrors step 2 (component+operation bound in one sentence) but the market comes from
    # the page itself rather than a heading, so it is deliberately the weakest signal here.
    if page_market:
        for sentence in sentences:
            _, component, operation = _core_labels(sentence)
            if component and operation and not _relation_window_is_ambiguous(sentence):
                if _is_negated(sentence):
                    _inc_diagnostic(diagnostics, "relation_negated_rejected")
                    continue
                if not _has_predicate(sentence):
                    _inc_diagnostic(diagnostics, "relation_no_predicate_rejected")
                    continue
                return "page_context", sentence[:900]

    return None, ""

def _laser_context(title: str, block: ContentBlock, section_context: str | None = None) -> str:
    section = section_context or " ".join(filter(None, (block.section_context, block.text, block.media_context)))
    return " ".join(dict.fromkeys(filter(None, (title, section))))


def _infer_market(component: str | None, architecture: str | None) -> str | None:
    for market, rules in MARKET_INFERENCE.items():
        if component and component in rules.get("components", set()):
            return market
        if architecture and architecture in rules.get("architectures", set()):
            return market
    return None


def _url_market_hint(url: str, title: str = "") -> str | None:
    """Marché "ambiant" porté par l'URL/le titre d'une page (chantier 2 item 1) : une page
    ``/applications/medical/`` porte le marché « Médical » pour tous ses blocs, même quand le
    mot n'apparaît nulle part dans le texte du bloc lui-même -- aujourd'hui la seule source du
    marché.

    Ne renvoie une valeur que si l'URL/le titre ne portent qu'UN SEUL marché sans ambiguïté :
    une page hub générique ("/applications/") ou listant plusieurs marchés dans son fil
    d'Ariane ne doit jamais imposer un marché arbitraire à tous ses blocs.
    """
    parsed = urlparse(url)
    path_words = unquote(parsed.path).replace("-", " ").replace("_", " ").replace("/", " ")
    text = f"{path_words} {title}"
    labels = {label for label, _ in _match_all_labels(text, MARKETS)}
    return next(iter(labels)) if len(labels) == 1 else None


def _partial_candidate_dims(
    block: ContentBlock, section: str, page_market: str | None,
) -> tuple[str | None, str | None, str | None, str] | None:
    """Filet de repêchage pour les faits à 2 dimensions sur 3 (chantier 2 item 2) : quand
    _relation_evidence() ne trouve pas de relation complète market+component+operation, un
    bloc qui porte encore au moins 2 des 3 dimensions (section locale, marché de la page en
    dernier recours) n'est plus jeté -- il devient un fait ``fact_status='partial'`` en file de
    revue humaine au lieu de contribuer aux 374 rejets ``relation_too_weak`` par run de l'audit.

    Refuse toujours les blocs multi-contextes ou dont la fenêtre est ambiguë (plusieurs marchés/
    composants distincts) : un fait partiel doit rester un fait, pas une supposition bruitée.
    Refuse aussi les zones "publication"/"news"/"project" (voir _section_role), pour la même
    raison que _relation_evidence leur interdit déjà de nourrir une relation structurée : une
    bibliographie ou un fil d'actualités n'est pas une déclaration d'application commerciale.
    """
    if (
        _is_multi_context_block(block)
        or _relation_window_is_ambiguous(section)
        or _section_role(block) in {"publication", "news", "project"}
    ):
        return None
    market, component, operation = _core_labels(section)
    market = market or page_market
    if sum(value is not None for value in (market, component, operation)) != 2:
        return None
    return market, component, operation, section


def _is_noise_block(block: ContentBlock) -> bool:
    direct = " ".join(filter(None, (block.heading, block.text, block.media_context)))
    norm = _normalize_text(direct)
    if len(norm) < 18:
        return True
    if _DOI_PATTERN.search(norm):
        return True
    noise_hits = sum(_contains_term(norm, term) for term in NAVIGATION_NOISE)
    if noise_hits >= 2:
        return True
    # A short fragment with no sentence-ending punctuation that also carries a menu/breadcrumb
    # marker is very likely nav debris ("Read more", "Navigation R&D Femtosecond
    # Micromachining"), not an editorial claim -- a real sentence has sentence structure.
    if not any(mark in direct for mark in ".!?") and len(direct.split()) <= 8:
        if any(_contains_term(norm, term) for term in _MENU_FRAGMENT_TERMS):
            return True
    return False


def _candidate(actor_name: str, url: str, title: str, block: ContentBlock, mode: str = "block-rules",
               context_text: str | None = None, structured_blocks: list[ContentBlock] | None = None,
               diagnostics: dict[str, int] | None = None, page_market: str | None = None,
               source_date: str | None = None, date_confidence: str | None = None) -> dict | None:
    """Tente de transformer UN bloc de contenu en UN "fait marché" complet ou partiel (ou
    renvoie None).

    Pipeline, dans l'ordre (chaque étape peut arrêter net et renvoyer None) :
    1. Rejeter les blocs de bruit (menu, mentions légales...) -- _is_noise_block.
    2. Exiger un contexte laser ultra-rapide (_laser_match) : sans ça, pas de fait du tout.
    3. Valider une "relation" locale entre marché/composant/opération (_relation_evidence),
       y compris le marché "ambiant" porté par l'URL de la page (``page_market``, chantier 2
       item 1) quand rien de local n'en fournit un -- c'est ici que la fenêtre de texte
       réellement retenue comme preuve est choisie.
    4. Si aucune relation complète n'est trouvée, tenter un fait à 2 dimensions sur 3
       (_partial_candidate_dims, chantier 2 item 2) plutôt que de jeter le bloc.
    5. Re-résoudre market/component/operation à partir de CETTE fenêtre validée (pas de la page
       entière), pour que le fait ne mélange jamais des infos venues d'ailleurs sur la page.
    6. Ajouter les dimensions complémentaires (procédé, matériau, performance...) et calculer
       la maturité industrielle, puis un score de confiance.
    7. Construire le dict candidat final avec sa citation et ses clés de déduplication.
    Create a market application only when the three core dimensions form a local relation,
    or a partial one when only two do."""
    _inc_diagnostic(diagnostics, "blocks_examined")
    if _is_noise_block(block):
        _inc_diagnostic(diagnostics, "noise_block")
        return None

    section = context_text or " ".join(filter(None, (block.section_context, block.text, block.media_context)))
    if not _laser_match(_laser_context(title, block, section)):
        _inc_diagnostic(diagnostics, "no_laser_context")
        return None
    _inc_diagnostic(diagnostics, "laser_blocks")

    group_text = " ".join([section, *[" ".join(filter(None, (b.h2, b.h3, b.heading, b.text))) for b in (structured_blocks or [])]])
    group_market, group_component, group_operation = _core_labels(group_text)
    if not group_market:
        _inc_diagnostic(diagnostics, "missing_market")
    if not group_component:
        _inc_diagnostic(diagnostics, "missing_component")
    if not group_operation:
        _inc_diagnostic(diagnostics, "missing_operation")

    if _is_multi_context_block(block):
        _inc_diagnostic(diagnostics, "multi_context_block")
    supplied_neighbors = structured_blocks or []
    safe_neighbors = [b for b in supplied_neighbors if _editorial_group_key(b) == _editorial_group_key(block)]
    if supplied_neighbors and len(safe_neighbors) < len(supplied_neighbors):
        _inc_diagnostic(diagnostics, "cross_group_rejected", len(supplied_neighbors) - len(safe_neighbors))

    relation_strength, relation_text = _relation_evidence(block, safe_neighbors, diagnostics, page_market=page_market)
    is_partial = False
    market: str | None
    component: str | None
    operation: str | None
    market_hits: list[str] = []
    component_hits: list[str] = []
    operation_hits: list[str] = []
    if relation_strength:
        # Core dimensions are resolved from the validated relation window, not from the whole
        # page -- except "page_context", whose window intentionally never contains a market
        # word (that's the whole point): the page-level hint stands in for it directly.
        if relation_strength == "page_context" and page_market:
            market, market_hits = page_market, []
        else:
            market, market_hits = _match_label_details(relation_text, MARKETS)
        component, component_hits = _match_label_details(relation_text, COMPONENTS)
        operation, operation_hits = _match_label_details(relation_text, OPERATIONS)
        if component:
            inferred_market = _infer_market(component, None)
            explicit_markets = {label for label, _ in _match_all_labels(relation_text, MARKETS)}
            if inferred_market and inferred_market in explicit_markets:
                market = inferred_market
                market_hits = _match_label_details(relation_text, MARKETS)[1]
        if not (market and component and operation):
            _inc_diagnostic(diagnostics, "incomplete_relation_core")
            return None
    else:
        partial = _partial_candidate_dims(block, section, page_market)
        if not partial:
            if _is_multi_context_block(block):
                _inc_diagnostic(diagnostics, "multi_context_rejected")
            _inc_diagnostic(diagnostics, "relation_too_weak")
            return None
        market, component, operation, relation_text = partial
        relation_strength = "partial"
        is_partial = True
        _inc_diagnostic(diagnostics, "relation_partial_accepted")

    # Complementary dimensions may use the local section, but they can never substitute a core one.
    process, _ = _match_label_details(section, PROCESS_TECHNOLOGIES)
    architecture, _ = _match_label_details(section, APPLICATION_ARCHITECTURES)
    material, _ = _match_label_details(section, MATERIALS)
    performance, _ = _match_label_details(section, PERFORMANCE_TERMS)

    maturity_class, maturity = _detect_maturity(relation_text + " " + section)
    bucket = "existing" if maturity_class == "existing" else ("pending" if is_partial else "radar")
    if maturity_class == "unknown":
        _inc_diagnostic(diagnostics, "maturity_unknown")
    direct_laser = _laser_match(relation_text)
    confidence_by_strength = {"direct": 0.78, "structured": 0.74, "contextual": 0.70, "page_context": 0.66, "partial": 0.55}
    confidence = confidence_by_strength.get(relation_strength, 0.70)
    confidence += 0.05 if direct_laser else 0.0
    confidence += 0.02 * sum(value is not None for value in (process, architecture, material, performance))
    if maturity_class != "unknown":
        confidence += 0.04
    confidence = max(0.45 if is_partial else 0.70, min(0.98, confidence))

    # Quote the relation itself. This makes the proof shown in the UI auditable and prevents a
    # high-scoring but unrelated sentence elsewhere in the section from being displayed.
    quote_terms = list(dict.fromkeys([*market_hits, *component_hits, *operation_hits]))
    quote = _quote(relation_text, quote_terms) or relation_text[:700]
    if not quote:
        _inc_diagnostic(diagnostics, "empty_quote")
        return None

    group = market_fact_key(actor_name, bucket, market, component, operation)
    app_key = application_key(actor_name, market, component, operation)
    # Keyed on the fact + url + quote, not the DOM block: two different blocks that happen
    # to expose the exact same quote for the same fact are the same proof to a reader, and
    # must collapse to one row instead of showing as duplicate sources.
    source_fingerprint = hashlib.sha256(f"{group}|{url}|{quote}".encode()).hexdigest()
    fact_fingerprint = hashlib.sha256(group.encode()).hexdigest()

    result = {
        "kind": "market_application",
        "actor": actor_name,
        "bucket": bucket,
        "market": market,
        "component": component,
        "operation": operation,
        "process": process,
        "architecture": architecture,
        "material": material,
        "performance": performance,
        "maturity": maturity,
        "maturity_class": maturity_class,
        "fact_status": "partial" if is_partial else "validated",
        # Chantier 4 : industrial_stage n'est plus qu'une étiquette de maturité -- process/
        # architecture/material/performance vivent déjà dans leurs propres colonnes (voir
        # db._migrate_industrial_stage_concatenation pour le nettoyage des lignes existantes).
        "stage": maturity[:240],
        "url": url,
        "title": title,
        "quote": quote,
        # Chantier 4 : toujours vrai ici -- `quote` est systématiquement extrait de
        # relation_text/section, un sous-texte réel du bloc, jamais reformulé.
        "is_verbatim": True,
        "evidence_type": classify_evidence_type(quote),
        "source_date": source_date,
        "date_confidence": date_confidence,
        "group": group,
        "fact_key": group,
        "application_key": app_key,
        "fingerprint": fact_fingerprint,
        "source_fingerprint": source_fingerprint,
        "block_heading": block.heading,
        "block_path": block.path,
        "mode": mode,
        "confidence": round(confidence, 2),
        "relation_strength": relation_strength,
        "relation_evidence": relation_text[:900],
        "source_role": _section_role(block),
    }
    _inc_diagnostic(diagnostics, "candidate_valid")
    _inc_diagnostic(diagnostics, f"relation_{relation_strength}")
    return result

def _offer_candidates(actor_name: str, url: str, title: str, block: ContentBlock, *, page_type: str,
                      mode: str = "block-rules", source_date: str | None = None,
                      date_confidence: str | None = None) -> list[dict]:
    """Comme _candidate(), mais pour une "offre" (capacité/prestation) : plus permissif, car il
    n'exige PAS un triplet marché/composant/opération complet -- une simple opération ou un
    procédé laser détecté sur une page de type service/capability/technology/product suffit à
    produire une offre (voir db.offers). Peut renvoyer plusieurs offres pour un même bloc si
    plusieurs opérations/procédés y sont mentionnés.
    Extract competitor offer/capability facts without inventing a market or component."""
    if _is_noise_block(block):
        return []
    direct = " ".join(filter(None, (block.heading, block.text, block.media_context)))
    section = " ".join(filter(None, (block.section_context, block.text, block.media_context)))
    if not _laser_match(_laser_context(title, block, section)):
        return []

    operations = _match_all_labels(section, OPERATIONS)
    processes = _match_all_labels(section, PROCESS_TECHNOLOGIES)
    materials = _match_all_labels(section, MATERIALS)
    performances = _match_all_labels(section, PERFORMANCE_TERMS)
    bucket, maturity = _detect_maturity(section)

    # Offer pages can be valuable even without a market/component. On generic pages, demand explicit capability evidence.
    page_offer_type = page_type if page_type in {"service", "capability", "technology", "product"} else "capability"
    capabilities: list[tuple[str, str | None, str | None]] = []
    for operation_label, _ in operations:
        capabilities.append((operation_label, operation_label, None))
    for process_label, _ in processes:
        capabilities.append((process_label, None, process_label))

    if not capabilities:
        if page_type not in {"service", "capability", "technology", "product"}:
            return []
        # A laser-focused service page is still a useful competitor signal even if the block is generic.
        if not _laser_match(direct):
            return []
        capabilities.append(("Procédés laser femtoseconde / ultrarapides", None, None))

    material = materials[0][0] if materials else None
    performance = performances[0][0] if performances else None
    results: list[dict] = []
    for capability, operation, process in capabilities:
        terms: list[str] = []
        if operation:
            terms.extend(_matching_terms(direct, OPERATIONS))
        if process:
            terms.extend(_matching_terms(direct, PROCESS_TECHNOLOGIES))
        terms.extend(_matching_terms(direct, MATERIALS))
        terms.extend(_matching_terms(direct, PERFORMANCE_TERMS))
        quote = _quote(block.text, terms)
        if not quote:
            continue
        fact_key = offer_fact_key(actor_name, page_offer_type, capability, operation, process)
        confidence = 0.70 + (0.08 if _laser_match(direct) else 0.0) + (0.05 if page_type in {"service", "capability", "technology"} else 0.0)
        confidence = min(0.97, confidence)
        results.append({
            "kind": "offer",
            "actor": actor_name,
            "offer_type": page_offer_type,
            "capability": capability,
            "operation": operation,
            "process": process,
            "material": material,
            "performance": performance,
            "maturity": maturity,
            "stage": maturity,
            "page_type": page_type,
            "url": url,
            "title": title,
            "quote": quote,
            "is_verbatim": True,
            "evidence_type": classify_evidence_type(quote),
            "source_date": source_date,
            "date_confidence": date_confidence,
            "fact_key": fact_key,
            "fingerprint": hashlib.sha256(fact_key.encode()).hexdigest(),
            "source_fingerprint": hashlib.sha256(f"{fact_key}|{url}|{quote}".encode()).hexdigest(),
            "block_heading": block.heading,
            "block_path": block.path,
            "mode": mode,
            "confidence": round(confidence, 2),
        })
    return results


def _validate_ai_value(value: object, allowed: set[str]) -> str:
    """Garde-fou anti-hallucination : ne garde la valeur renvoyée par l'IA que si elle est
    EXACTEMENT (chaîne pour chaîne) l'un des libellés autorisés, sinon "Non identifié"."""
    text = str(value or "").strip()
    if not text or text == "Non identifié":
        return "Non identifié"
    return text if text in allowed else "Non identifié"


def _ai_mode_label(ollama: AiClient) -> str:
    """Étiquette de traçabilité stockée dans `mode` (ex: "anthropic:claude-...") pour savoir
    quel modèle/fournisseur a produit un candidat donné."""
    provider = "anthropic" if isinstance(ollama, AnthropicClient) else "ollama"
    return f"{provider}:{ollama.model}"


# Observed in real Claude Haiku output: when the model genuinely can't identify a dimension
# (e.g. a publication snippet about a process with no clear physical component), it sometimes
# answers with a placeholder like "<UNKNOWN>" instead of leaving the field blank. These are not
# real proposals -- queuing them would put dead entries in front of whoever reviews the queue.
_NON_PROPOSAL_SENTINELS = {
    "unknown", "<unknown>", "n a", "na", "none", "non identifie", "non applicable", "aucun", "aucune",
}

# Repère d'observation, PAS un filtre (voir _ai_candidates) : en dessous, le modèle s'est
# déclaré peu sûr de son propre fait. Conservé à sa valeur historique (l'ancien seuil de rejet)
# pour que `ai_fact_low_confidence` reste comparable d'une passe à l'autre de part et d'autre du
# changement du 01/09/2026.
AI_LOW_CONFIDENCE_MARK = 0.72


def _is_real_proposal(value: str) -> bool:
    return bool(value) and _normalize_text(value) not in _NON_PROPOSAL_SENTINELS


def _vocabulary_candidate(
    actor_name: str,
    url: str,
    title: str,
    quote: str,
    block: ContentBlock,
    proposed: dict[str, str],
    resolved: dict[str, str | None],
) -> dict | None:
    """Build a triage record when an AI-proposed core label matches no known lexicon entry.

    Only genuinely-proposed-but-unresolved dimensions are queued -- a dimension the model left
    blank, or answered with a non-answer placeholder (see _NON_PROPOSAL_SENTINELS), isn't a
    vocabulary gap worth a human's time. Resolved dimensions are kept alongside for context
    (e.g. "component already resolved to Stents, but market has no match").
    """
    missing = {key: value for key, value in proposed.items() if _is_real_proposal(value) and not resolved.get(key)}
    if not missing:
        return None
    known = {key: value for key, value in resolved.items() if value}
    fingerprint = hashlib.sha256(
        "|".join([actor_name, url, quote, json.dumps(missing, sort_keys=True, ensure_ascii=False)]).encode()
    ).hexdigest()
    return {
        "kind": "vocabulary_candidate",
        "actor": actor_name,
        "url": url,
        "title": title,
        "quote": quote[:500],
        "block_heading": block.heading,
        "proposed_labels": missing,
        "resolved_labels": known,
        "fingerprint": fingerprint,
    }


def _ai_candidates(
    actor_name: str, url: str, title: str, blocks: list[ContentBlock], ollama: AiClient,
    diagnostics: dict[str, int] | None = None, exclude_indices: set[int] | None = None,
    source_date: str | None = None, date_confidence: str | None = None,
) -> list[dict]:
    """AI fallback for market applications the deterministic lexicon rejected, with an
    open-vocabulary escape hatch (chantier 2 item 3).

    ``exclude_indices`` (block indices _candidate() already turned into a validated/partial
    fact this pass) keeps the model focused on blocks the lexicon found nothing in -- that is
    where it can actually add value, instead of re-processing ground the rules already
    covered. When the AI's answer for all three core dimensions normalizes to a KNOWN label,
    it is queued for human review as-is: it no longer has to also be independently confirmed
    by the deterministic local-section lexicon (that requirement meant the AI could only ever
    agree with the rules, never surface something the rules missed -- see the audit's
    ``ai_known_label_not_independently_confirmed`` telemetry, 71 proposals -> 1 validated). The
    verbatim-quote requirement and the confidence floor below are the safeguards that replace
    it. When at least one of the three doesn't match any known label, the fact is queued in
    ``vocabulary_candidates`` instead, carrying the model's actual proposed wording. Either way
    nothing from this function is ever auto-published: every candidate it returns carries
    ``fact_status='review'``, gated behind human validation (evidence.review_status='review').
    Complementary dimensions (process/architecture/material/performance/maturity) stay
    restricted to the known lexicons in both cases.
    """
    relevant: list[tuple[int, ContentBlock, str]] = []
    for i, block in enumerate(blocks):
        if exclude_indices and i in exclude_indices:
            continue
        if _is_noise_block(block):
            continue
        direct, section = _context_for_block(title, blocks, i)
        if _laser_match(_laser_context(title, block, section)) and (
            _laser_match(direct) or any(_match_label(section, lex) for lex in (MARKETS, COMPONENTS, OPERATIONS))
        ):
            relevant.append((i, block, section))
        if len(relevant) >= 12:
            break
    if not relevant:
        _inc_diagnostic(diagnostics, "ai_no_relevant_blocks")
        return []
    if not ollama.available():
        _inc_diagnostic(diagnostics, "ai_unavailable")
        return []
    if ai_cost_cap_reached(ollama):
        _inc_diagnostic(diagnostics, "ai_cost_cap_reached")
        return []
    _inc_diagnostic(diagnostics, "ai_pages_queried")
    _inc_diagnostic(diagnostics, "ai_relevant_blocks_sent", len(relevant))

    prompt = json.dumps({
        "actor": actor_name, "url": url,
        "allowed_labels": {
            "markets": list(MARKETS), "components": list(COMPONENTS), "operations": list(OPERATIONS),
            "process_technologies": list(PROCESS_TECHNOLOGIES), "application_architectures": list(APPLICATION_ARCHITECTURES),
            "materials": list(MATERIALS), "performance": list(PERFORMANCE_TERMS),
            "maturity": [stage for stage, _, _ in MATURITY_RULES],
        },
        "blocks": [dict(index=i, context=section[:2500], **block_payload(block)) for i, block, section in relevant],
    }, ensure_ascii=False)
    system = (
        "Analyse uniquement des applications marché explicitement documentées. "
        "Pour market, component et operation : utilise en priorité un des libellés connus listés dans allowed_labels "
        "si le contenu correspond clairement. Si le contenu décrit une application, un composant ou une opération réels "
        "mais qui ne correspond à AUCUN libellé connu, propose un libellé court et précis en français plutôt que de "
        "forcer une correspondance approximative ou d'inventer une valeur non justifiée par le texte. "
        "Pour process_technology, application_architecture, material, performance et maturity, utilise uniquement les "
        "labels autorisés listés. Ne remplace jamais un composant manquant par un matériau, une architecture ou un "
        "procédé, et ne remplace jamais une opération manquante par un procédé. "
        "Réponds en JSON avec facts contenant block_index, market, component, operation, process_technology, "
        "application_architecture, material, performance, maturity, bucket, stage, quote, confidence. "
        "quote doit être une sous-chaîne exacte du bloc courant."
    )
    try:
        response = ollama.ask_json(system, prompt)
        facts = response.get("facts", []) if isinstance(response, dict) else []
    except Exception:
        _inc_diagnostic(diagnostics, "ai_call_failed")
        return []
    # Bug fix (audit v8 §2.2): this used to count len(facts) here while only iterating over
    # facts[:AI_FACTS_PER_PAGE_LIMIT] below -- two independent numbers that could silently
    # diverge. On a real run this inflated "ai_facts_proposed" to ~200x the number of facts
    # actually examined (4061 counted vs at most 1480 examinable), making the AI path look far
    # less precise than it is, while the truncation itself left no trace at all. Now
    # ai_facts_proposed reflects what's actually examined, and any excess is counted
    # separately instead of disappearing.
    AI_FACTS_PER_PAGE_LIMIT = 20
    _inc_diagnostic(diagnostics, "ai_facts_proposed", min(len(facts), AI_FACTS_PER_PAGE_LIMIT))
    if len(facts) > AI_FACTS_PER_PAGE_LIMIT:
        _inc_diagnostic(diagnostics, "ai_facts_truncated", len(facts) - AI_FACTS_PER_PAGE_LIMIT)

    block_by_index = {i: (block, section) for i, block, section in relevant}
    complementary = {
        "process_technology": set(PROCESS_TECHNOLOGIES), "application_architecture": set(APPLICATION_ARCHITECTURES),
        "material": set(MATERIALS), "performance": set(PERFORMANCE_TERMS),
    }
    maturity_to_bucket = {stage: bucket for stage, bucket, _ in MATURITY_RULES}
    mode = _ai_mode_label(ollama)
    candidates: list[dict] = []

    for fact in facts[:AI_FACTS_PER_PAGE_LIMIT]:
        try:
            original_index = int(fact["block_index"])
            block, section = block_by_index[original_index]
            quote = str(fact["quote"]).strip()
            confidence = float(fact.get("confidence", 0))
        except (KeyError, ValueError, TypeError, IndexError):
            _inc_diagnostic(diagnostics, "ai_fact_malformed")
            continue
        if not quote or quote not in block.text:
            _inc_diagnostic(diagnostics, "ai_fact_quote_not_verbatim")
            continue
        # Audit du 01/09/2026 : ce seuil était de loin la première cause de perte du chemin IA
        # (171 faits jetés sur 330 pertes cumulées, contre 21 `ai_fact_validated` au total), et
        # il jetait sur un nombre que le MODÈLE s'attribue lui-même. Il s'appliquait de surcroît
        # AVANT la branche vocabulaire ci-dessous : un fait à 0.70 ne devenait donc même pas un
        # candidat vocabulaire, il disparaissait sans trace exploitable.
        #
        # Il n'achetait aucune sécurité réelle : rien de cette fonction n'est jamais
        # auto-publié (fact_status='review' plus bas, cf. docstring), donc la relecture humaine
        # couvrait déjà exactement ce que ce seuil prétendait couvrir -- il ne coûtait que du
        # rappel. Même conclusion que le §10.7 de l'audit veille pour les offres : une confiance
        # numérique n'a pas la résolution nécessaire pour piloter une file de revue ; ce sont
        # les critères STRUCTURELS qui décident. Ici c'est la citation verbatim (vérifiée juste
        # au-dessus), qui reste, elle, un rejet ferme.
        #
        # La valeur reste enregistrée et ressort telle quelle dans field_confidence : elle sert
        # désormais à PRIORISER la file (review_queue._priority pondère par l'incertitude), plus
        # à supprimer en silence. Le compteur garde son nom et son seuil d'origine pour que la
        # télémétrie reste comparable aux passes antérieures -- il mesure maintenant une
        # observation ("le modèle s'est déclaré peu sûr"), non plus un rejet.
        if confidence < AI_LOW_CONFIDENCE_MARK:
            _inc_diagnostic(diagnostics, "ai_fact_low_confidence")

        fields = {key: _validate_ai_value(fact.get(key), values) for key, values in complementary.items()}

        proposed = {
            "market": str(fact.get("market") or "").strip(),
            "component": str(fact.get("component") or "").strip(),
            "operation": str(fact.get("operation") or "").strip(),
        }
        # A resolved core dimension means the AI's answer is *exactly* one of the known
        # canonical labels (allowed_labels lists those verbatim in the prompt) -- this is a
        # membership check, not a lexical/keyword match: _match_label() looks for descriptive
        # keywords *inside* a piece of text ("medical" inside a sentence), which is a
        # different question from "is this string itself one of our label names".
        resolved: dict[str, str | None] = {
            key: None if (value := _validate_ai_value(fact.get(key), core)) == "Non identifié" else value
            for key, core in (("market", set(MARKETS)), ("component", set(COMPONENTS)), ("operation", set(OPERATIONS)))
        }
        if not all(resolved.values()):
            vocabulary_candidate = _vocabulary_candidate(actor_name, url, title, quote, block, proposed, resolved)
            if vocabulary_candidate:
                candidates.append(vocabulary_candidate)
                _inc_diagnostic(diagnostics, "ai_vocabulary_queued")
            else:
                _inc_diagnostic(diagnostics, "ai_fact_nothing_proposed")
            continue

        # Known-label path (chantier 2 item 3): the AI's wording resolving to a known label on
        # its own IS enough -- the verbatim-quote check above and the confidence floor are the
        # safeguards, not agreement with the deterministic local-section lexicon. Requiring
        # that agreement made the AI a pure confirmation echo of the rules (see the function
        # docstring); this diagnostic split just keeps visibility into how often the AI's
        # answer would have matched the rules anyway vs. genuinely surfaced something new.
        deterministic_core = {
            "market": _match_label(section, MARKETS),
            "component": _match_label(section, COMPONENTS),
            "operation": _match_label(section, OPERATIONS),
        }
        if any(deterministic_core[key] != resolved[key] for key in resolved):
            _inc_diagnostic(diagnostics, "ai_fact_independent_of_lexicon")
        else:
            _inc_diagnostic(diagnostics, "ai_fact_lexicon_confirmed")
        _inc_diagnostic(diagnostics, "ai_fact_validated")

        maturity = _validate_ai_value(fact.get("maturity"), set(maturity_to_bucket))
        maturity_class = maturity_to_bucket.get(maturity, "unknown")
        bucket = "existing" if maturity_class == "existing" else "radar"
        market, component, operation = resolved["market"], resolved["component"], resolved["operation"]
        assert market is not None and component is not None and operation is not None  # guaranteed by all(resolved.values()) above
        fact_key = market_fact_key(actor_name, bucket, market, component, operation)
        app_key = application_key(actor_name, market, component, operation)
        # Chantier 4 : ne plus rattacher process/architecture/material/performance au texte de
        # stage -- ils vivent déjà dans leurs propres colonnes (voir le dict candidat plus bas).
        # `stage` reste la description de maturité proposée par le modèle telle quelle.
        stage = str(fact.get("stage", maturity if maturity != "Non identifié" else "Maturité à confirmer")).strip()

        candidates.append({
            "kind": "market_application",
            "actor": actor_name,
            "bucket": bucket,
            "market": market,
            "component": component,
            "operation": operation,
            "process": None if fields["process_technology"] == "Non identifié" else fields["process_technology"],
            "architecture": None if fields["application_architecture"] == "Non identifié" else fields["application_architecture"],
            "material": None if fields["material"] == "Non identifié" else fields["material"],
            "performance": None if fields["performance"] == "Non identifié" else fields["performance"],
            "maturity": maturity if maturity != "Non identifié" else "Maturité à confirmer",
            "maturity_class": maturity_class,
            "fact_status": "review",
            "stage": stage[:240],
            "url": url,
            "title": title,
            "quote": quote[:700],
            # Chantier 4 : toujours vrai -- `quote` doit déjà être une sous-chaîne exacte du
            # bloc (voir "ai_fact_quote_not_verbatim" plus haut), jamais une reformulation.
            "is_verbatim": True,
            "evidence_type": classify_evidence_type(quote),
            "source_date": source_date,
            "date_confidence": date_confidence,
            "group": fact_key,
            "fact_key": fact_key,
            "application_key": app_key,
            "fingerprint": hashlib.sha256(fact_key.encode()).hexdigest(),
            "source_fingerprint": hashlib.sha256(f"{fact_key}|{url}|{quote}".encode()).hexdigest(),
            "block_heading": block.heading,
            "block_path": block.path,
            "mode": mode,
            "confidence": min(0.98, max(0.0, confidence)),
        })
    return candidates


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    """Prefer the richest/highest-confidence representation of the same language-independent fact."""
    best: dict[str, dict] = {}
    for candidate in candidates:
        key = str(candidate.get("fact_key") or candidate.get("group") or candidate.get("fingerprint"))
        current = best.get(key)
        if current is None:
            best[key] = candidate
            continue
        current_rank = (float(current.get("confidence", 0)), sum(current.get(k) is not None for k in ("process", "material", "performance", "architecture")))
        new_rank = (float(candidate.get("confidence", 0)), sum(candidate.get(k) is not None for k in ("process", "material", "performance", "architecture")))
        if new_rank > current_rank:
            best[key] = candidate
    return list(best.values())


# === Bloc 5/6 : le crawler (scrape_actors) et sa file de priorité "coverage-first" ===
def adaptive_decision(priority: bool, document_count: int, block_count: int, errors: int, source_count: int) -> tuple[str, bool]:
    """Décide, après un crawl, si cet acteur doit passer en stratégie "adaptive" (IA de secours
    activée lors de la prochaine collecte marché) : soit parce qu'il est marqué priority, soit
    parce que le crawl a été manifestement anormal (0 document, 0 bloc, ou trop d'erreurs)."""
    anomaly = document_count == 0 or block_count == 0 or errors >= max(1, source_count // 2)
    return ("adaptive" if priority or anomaly else "generic", anomaly)


def _push_crawl_item(
    heap: list[tuple[int, int, int, dict]],
    queued_scores: dict[str, int],
    counter: itertools.count,
    item: dict,
) -> None:
    """Ajoute une URL candidate à la file de crawl (un tas/heap min, donc on stocke le score
    négatif pour que le plus haut score sorte en premier). Ignore l'ajout si une URL équivalente
    est déjà en file avec un score égal ou meilleur (queued_scores), pour éviter les doublons ;
    `counter` sert uniquement de départage stable quand deux items ont exactement le même
    (score, profondeur), car un tuple ne peut pas comparer deux dicts entre eux."""
    key = canonical_url(item["url"])
    score = int(item.get("score", 0))
    if score <= queued_scores.get(key, -1):
        return
    queued_scores[key] = score
    heapq.heappush(heap, (-score, int(item.get("depth", 0)), next(counter), item))


def _effective_crawl_score(item: dict, visited_by_type: dict[str, int], profile: dict) -> int:
    """Score "effectif" d'un item de la file : son score de base, PLUS un gros bonus temporaire
    (`coverage_boost`) si son type de page (item["page_type"]) n'a pas encore atteint son
    objectif de couverture (profile["coverage_targets"]) -- c'est ce qui fait que le crawler
    va chercher en priorité au moins une page de chaque famille stratégique avant d'approfondir."""
    base = int(item.get("score", 0))
    page_type = str(item.get("page_type") or "other")
    target = int(profile.get("coverage_targets", {}).get(page_type, 0))
    if target and int(visited_by_type.get(page_type, 0)) < target:
        base += int(profile.get("crawl", {}).get("coverage_boost", 500))
        order = list(profile.get("coverage_order", ()))
        if page_type in order:
            base += max(0, len(order) - order.index(page_type))
    return base


def _pop_crawl_item(
    heap: list[tuple[int, int, int, dict]],
    queued_scores: dict[str, int],
    visited: set[str],
    visited_by_type: dict[str, int],
    profile: dict,
) -> tuple[dict, int] | None:
    """Select the best current item with a temporary boost for uncovered strategic families.

    Budgets are small, so rescoring the queue on each pop is simpler and more correct than keeping stale
    heap priorities after coverage state changes.
    """
    candidates: list[tuple[int, int, int, int, dict]] = []
    while heap:
        neg_score, depth, seq, item = heapq.heappop(heap)
        key = canonical_url(item["url"])
        base_score = -int(neg_score)
        if key in visited or base_score < queued_scores.get(key, base_score):
            continue
        candidates.append((_effective_crawl_score(item, visited_by_type, profile), base_score, -depth, -seq, item))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
    chosen = candidates[0]
    for _, base_score, neg_depth, neg_seq, item in candidates[1:]:
        heapq.heappush(heap, (-base_score, -neg_depth, -neg_seq, item))
    return chosen[4], chosen[1]


def _source_coverage(actor_id: int, profile: dict | None = None) -> dict[str, dict[str, int | str]]:
    """État de couverture par type de page pour un acteur (combien découvertes, combien
    "prêtes" i.e. répondant en 2xx sans être ambiguës), utilisé à la fois pour piloter le
    crawl (voir _effective_crawl_score) et pour l'afficher dans /api/profiles."""
    coverage: dict[str, dict[str, int | str]] = {}
    with connect(ACTORS_DB) as db:
        rows = db.execute(
            """SELECT page_type, COUNT(*) AS discovered,
                      SUM(CASE WHEN last_http_status BETWEEN 200 AND 299 AND ambiguous=0 THEN 1 ELSE 0 END) AS ready
               FROM actor_sources WHERE actor_id=? AND active=1 AND page_type IS NOT NULL
               GROUP BY page_type""",
            (actor_id,),
        ).fetchall()
    strategic = set((profile or {}).get("coverage_targets", {}).keys()) or {"application", "project", "news"}
    for row in rows:
        discovered = int(row["discovered"] or 0)
        ready = int(row["ready"] or 0)
        status = "ready" if ready else "pending"
        if discovered and not ready and str(row["page_type"]) in strategic:
            status = "error"
        coverage[str(row["page_type"])] = {"discovered": discovered, "ready": ready, "status": status}
    for category in strategic:
        coverage.setdefault(category, {"discovered": 0, "ready": 0, "status": "missing"})
    return coverage


# Détection d'anomalie de source (plan d'action web-scraping, priorité #1) : voir
# db.actor_sources.anomaly_detected_at et le commentaire de source_metrics pour le contexte
# complet -- un site qui refond son HTML continue de répondre HTTP 200 tout en effondrant
# silencieusement l'extraction, sans qu'aucun signal existant (last_http_status, health_score)
# ne le détecte.
SOURCE_METRICS_RETENTION = 20
ANOMALY_MIN_HISTORY = 3
ANOMALY_MIN_BASELINE_BLOCKS = 3
ANOMALY_DROP_RATIO = 0.3


def _detect_content_anomaly(db, source_id: int, block_count: int) -> tuple[str | None, str | None]:
    """Compare le nombre de blocs de ce fetch à la médiane historique de la MÊME page
    (source_metrics, écrit à CHAQUE fetch réussi -- contrairement à page_versions, qui n'archive
    qu'au moment d'un changement de content_hash et n'a donc aucune profondeur d'historique pour
    une page restée visuellement stable pendant des mois).

    Renvoie (anomaly_detected_at, anomaly_detail) à écrire dans actor_sources, ou (None, None)
    si rien n'est détecté -- ce qui couvre aussi bien "pas encore assez d'historique pour juger"
    que "l'anomalie précédemment signalée s'est résorbée" : l'appelant écrase toujours ces deux
    colonnes avec le résultat de cet appel, jamais un patch conditionnel.
    """
    history = [
        row["block_count"] for row in db.execute(
            "SELECT block_count FROM source_metrics WHERE source_id=? ORDER BY id DESC LIMIT ?",
            (source_id, SOURCE_METRICS_RETENTION),
        ).fetchall()
    ]
    if len(history) < ANOMALY_MIN_HISTORY:
        return None, None
    baseline = statistics.median(history)
    if baseline < ANOMALY_MIN_BASELINE_BLOCKS:
        return None, None
    if block_count < baseline * ANOMALY_DROP_RATIO:
        detail = (
            f"{block_count} bloc(s) extrait(s) contre une médiane historique de {baseline:.0f} "
            f"sur {len(history)} passages -- possible refonte HTML ou contenu rendu en JS."
        )
        return utc_now(), detail
    return None, None


def _diff_page_blocks(previous_blocks_json: str | None, new_blocks_json: str | None) -> list[dict]:
    """§5.E.1/§8.3 audit veille (30/08/2026) : page_versions archive déjà le blocks_json d'avant
    un changement de contenu détecté, mais rien ne le comparait. Pour la majorité du corpus
    (pages service/application/product, sans date de publication exploitable -- voir
    date_confidence), ce diff est la SEULE date fiable que ce crawler puisse produire : il
    transforme "quand je l'ai vu" (created_at) en "quand ils l'ont écrit" (borné par la
    fréquence de crawl). Volontairement fondé sur des ensembles (empreinte de bloc, libellé de
    lexique, token numérique), pas un vrai diff ligne à ligne -- cohérent avec le reste du
    module (lexique déterministe, sans dépendance NLP). Renvoie une liste de dicts prêts à
    insérer dans page_changes (change_type/term/detail/old_value/new_value)."""
    try:
        previous_blocks = json.loads(previous_blocks_json) if previous_blocks_json else []
    except (TypeError, ValueError):
        previous_blocks = []
    try:
        new_blocks = json.loads(new_blocks_json) if new_blocks_json else []
    except (TypeError, ValueError):
        new_blocks = []
    if not previous_blocks and not new_blocks:
        return []

    changes: list[dict] = []

    previous_by_fp = {b["fingerprint"]: b for b in previous_blocks if b.get("fingerprint")}
    new_by_fp = {b["fingerprint"]: b for b in new_blocks if b.get("fingerprint")}
    for fp, block in new_by_fp.items():
        if fp not in previous_by_fp:
            changes.append({
                "change_type": "block_added", "term": None,
                "detail": (block.get("text") or block.get("heading") or "")[:200],
                "old_value": None, "new_value": None,
            })
    for fp, block in previous_by_fp.items():
        if fp not in new_by_fp:
            changes.append({
                "change_type": "block_removed", "term": None,
                "detail": (block.get("text") or block.get("heading") or "")[:200],
                "old_value": None, "new_value": None,
            })

    previous_text = " ".join(filter(None, (b.get("text") for b in previous_blocks)))
    new_text = " ".join(filter(None, (b.get("text") for b in new_blocks)))
    previous_labels: set[str] = set()
    new_labels: set[str] = set()
    for lexicon in (MARKETS, COMPONENTS, OPERATIONS, PROCESS_TECHNOLOGIES, MATERIALS, PERFORMANCE_TERMS):
        previous_labels |= {label for label, _ in _match_all_labels(previous_text, lexicon)}
        new_labels |= {label for label, _ in _match_all_labels(new_text, lexicon)}
    for label in sorted(new_labels - previous_labels):
        changes.append({
            "change_type": "lexicon_term_appeared", "term": label,
            "detail": None, "old_value": None, "new_value": None,
        })

    # Numeric specs are compared per matching block path (same editorial slot), not globally --
    # a spec appearing in a NEW block is already covered by block_added above; this is only for
    # a spec changing INSIDE a block that persisted across both versions.
    previous_by_path = {b["path"]: b for b in previous_blocks if b.get("path")}
    new_by_path = {b["path"]: b for b in new_blocks if b.get("path")}
    for path, new_block in new_by_path.items():
        previous_block = previous_by_path.get(path)
        if not previous_block or previous_block.get("text") == new_block.get("text"):
            continue
        old_specs = numeric_spec_tokens(previous_block.get("text"))
        new_specs = numeric_spec_tokens(new_block.get("text"))
        if old_specs != new_specs and (old_specs or new_specs):
            changes.append({
                "change_type": "numeric_spec_changed", "term": path, "detail": None,
                "old_value": ", ".join(sorted(old_specs)) or None,
                "new_value": ", ".join(sorted(new_specs)) or None,
            })
    return changes


def scrape_actors(max_pages_per_actor: int | None = None, actor_names: list[str] | None = None) -> dict:
    """Crawle chaque acteur actif l'un après l'autre. Pour chaque acteur :
    1. Charge son profil de crawl (site_profiles.get_site_profile) et son budget de pages.
    2. Insère ses seed_paths comme sources de départ (en plus de sa page d'accueil).
    3. Construit une file de priorité (heap) à partir de toutes ses sources déjà connues.
    4. Boucle : dépile le meilleur item (_pop_crawl_item, qui applique le boost de couverture),
       télécharge la page (_fetch), la parse (hybrid.parse_document), enregistre son empreinte
       de contenu (pour détecter les changements d'une collecte à l'autre), et pousse ses liens
       sortants dans la file pour continuer l'exploration -- jusqu'à épuiser le budget de pages
       ou la file elle-même.
    5. À la fin, construit un profil de site (hybrid.build_profile), décide de la stratégie
       adaptive/generic (adaptive_decision) et écrit tout l'état dans site_profiles.

    ``actor_names``, when given, restricts the run to those actors (same convention as
    scrape_market's own filter) -- meant for a one-off targeted crawl (e.g. an actor that was
    added after the last full run and never crawled) without paying for a full-roster pass.
    Deterministic dynamic crawler with per-site profiles and a priority queue."""
    with connect(ACTORS_DB) as db:
        run_id = db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (utc_now(), "running")).lastrowid
        actors = db.execute("SELECT * FROM actors WHERE active=1 ORDER BY priority DESC,name").fetchall()
    if actor_names:
        wanted = set(actor_names)
        actors = [row for row in actors if row["name"] in wanted]

    scanned = changed = errors = discovered = profiled = fallback = 0
    ollama = get_ai_client()

    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        for actor_row in actors:
            actor = dict(actor_row)
            site_profile = get_site_profile(actor)
            budget = int(max_pages_per_actor or crawl_budget(site_profile, bool(actor["priority"])))
            max_depth = int(site_profile.get("crawl", {}).get("max_depth", 2))

            with connect(ACTORS_DB) as db:
                for seed_url in seed_urls(site_profile, actor["official_url"]):
                    seed_type, seed_score = classify_source(seed_url, profile=site_profile)
                    source_kind = "official" if canonical_url(seed_url) == canonical_url(actor["official_url"]) else "profile-seed"
                    if source_kind == "official":
                        seed_type, seed_score = "homepage", max(seed_score, 60)
                    else:
                        seed_score = max(seed_score, 95)
                    db.execute(
                        """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,source_score,discovery_depth,discovery_reason,discovered_at)
                           VALUES(?,?,?,?,?,0,'profile-seed',?)
                           ON CONFLICT(url) DO UPDATE SET actor_id=excluded.actor_id,active=1,
                               source_score=MAX(actor_sources.source_score,excluded.source_score),
                               page_type=CASE WHEN actor_sources.page_type IS NULL OR actor_sources.page_type='' THEN excluded.page_type ELSE actor_sources.page_type END,
                               discovered_at=COALESCE(actor_sources.discovered_at,excluded.discovered_at)""",
                        (actor["id"], seed_url, source_kind, seed_type, seed_score, utc_now()),
                    )
                for sitemap_page_url in _discover_sitemap_urls(client, actor["official_url"], site_profile):
                    sitemap_type, sitemap_score = classify_source(sitemap_page_url, profile=site_profile)
                    if sitemap_type == "ignore":
                        continue
                    existed = db.execute("SELECT id FROM actor_sources WHERE url=?", (sitemap_page_url,)).fetchone()
                    db.execute(
                        """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,source_score,discovery_depth,discovery_reason,discovered_at)
                           VALUES(?,?,'sitemap',?,?,0,'sitemap',?)
                           ON CONFLICT(url) DO UPDATE SET
                               source_score=MAX(actor_sources.source_score,excluded.source_score),
                               page_type=CASE WHEN actor_sources.page_type IS NULL OR actor_sources.page_type='' THEN excluded.page_type ELSE actor_sources.page_type END,
                               discovered_at=COALESCE(actor_sources.discovered_at,excluded.discovered_at)""",
                        (actor["id"], sitemap_page_url, sitemap_type, sitemap_score, utc_now()),
                    )
                    discovered += int(existed is None)
                initial_sources = db.execute(
                    "SELECT * FROM actor_sources WHERE actor_id=? AND active=1",
                    (actor["id"],),
                ).fetchall()

            heap: list[tuple[int, int, int, dict]] = []
            queued_scores: dict[str, int] = {}
            visited: set[str] = set()
            visited_by_type: dict[str, int] = defaultdict(int)
            counter = itertools.count()

            for source_row in initial_sources:
                source = dict(source_row)
                page_type = normalize_page_type(source.get("page_type"))
                score = int(source.get("source_score") or 0)
                if not page_type or score <= 0:
                    page_type, score = classify_source(source["url"], profile=site_profile)
                if source.get("source_kind") == "official":
                    score = max(score, 60)
                _push_crawl_item(heap, queued_scores, counter, {
                    "source_id": source["id"],
                    "url": source["url"],
                    "page_type": page_type,
                    "score": score,
                    "depth": int(source.get("discovery_depth") or 0),
                    "parent_url": source.get("parent_url"),
                    "reason": source.get("discovery_reason") or source.get("source_kind") or "stored",
                })

            documents: list[tuple[str, ParsedDocument]] = []
            actor_errors = 0
            successful_visits = 0
            attempts = 0
            max_attempts = max(budget * 3, budget + 4)

            while heap and successful_visits < budget and attempts < max_attempts:
                popped = _pop_crawl_item(heap, queued_scores, visited, visited_by_type, site_profile)
                if not popped:
                    break
                item, score = popped
                url = item["url"]
                depth = int(item.get("depth", 0))
                key = canonical_url(url)
                if key in visited:
                    continue
                visited.add(key)
                attempts += 1

                source_id = item.get("source_id")
                if not source_id:
                    with connect(ACTORS_DB) as db:
                        row = db.execute("SELECT id FROM actor_sources WHERE url=?", (url,)).fetchone()
                        source_id = row["id"] if row else None
                if not source_id:
                    continue

                try:
                    with connect(ACTORS_DB) as db:
                        previous = db.execute(
                            """SELECT content_hash,blocks_json,last_title,last_checked_at,etag,last_modified_header
                               FROM actor_sources WHERE id=?""",
                            (source_id,),
                        ).fetchone()
                    # GET conditionnel (plan d'action web-scraping, priorité #3) : envoyer
                    # If-None-Match/If-Modified-Since quand un fetch précédent nous a laissé de
                    # quoi -- un 304 réutilise ce qu'on a déjà, sans corps à retélécharger.
                    conditional_headers: dict[str, str] = {}
                    if previous and previous["etag"]:
                        conditional_headers["If-None-Match"] = previous["etag"]
                    if previous and previous["last_modified_header"]:
                        conditional_headers["If-Modified-Since"] = previous["last_modified_header"]
                    response = _fetch(client, url, headers=conditional_headers or None)
                    resolved_url = str(response.url)
                    new_etag = response.headers.get("etag")
                    new_last_modified = response.headers.get("last-modified")

                    if response.status_code == 304:
                        # Rien n'a changé côté serveur -- on ne reparse rien, on garde tout le
                        # reste (content_hash, blocks_json, anomaly_detected_at...) tel quel.
                        stamp = utc_now()
                        with connect(ACTORS_DB) as db:
                            db.execute(
                                "UPDATE actor_sources SET last_http_status=304,last_checked_at=?,last_error=NULL WHERE id=?",
                                (stamp, source_id),
                            )
                        scanned += 1
                        successful_visits += 1
                        continue

                    if is_pdf_response(response.headers.get("content-type", ""), resolved_url):
                        document = parse_pdf_document(response.content, resolved_url, profile=site_profile)
                    else:
                        document = parse_document(response.text, resolved_url, profile=site_profile)
                    documents.append((resolved_url, document))
                    digest = hashlib.sha256("|".join(block.fingerprint for block in document.blocks).encode()).hexdigest()

                    previous_hash = previous["content_hash"] if previous else None
                    is_changed = bool(previous_hash and previous_hash != digest)
                    blocks_json = json.dumps([block_payload(block) for block in document.blocks[:40]], ensure_ascii=False)
                    stamp = utc_now()
                    fetched_type, fetched_score = classify_source(
                        resolved_url,
                        document.title,
                        title=document.title,
                        h1=document.h1,
                        profile=site_profile,
                    )
                    fetched_score = max(int(fetched_score), int(document.page_score))
                    visited_by_type[fetched_type] += 1

                    # Named distinctly from the per-actor `block_count` computed after this loop
                    # (line ~2155) -- this one is per-page, feeding source_metrics/anomaly detection.
                    page_block_count = len(document.blocks)
                    text_chars = sum(len(block.text) for block in document.blocks)

                    with connect(ACTORS_DB) as db:
                        if is_changed:
                            # Archive the version about to be overwritten below -- see
                            # PAGE_VERSIONS_RETENTION and db.page_versions' docstring.
                            db.execute(
                                """INSERT INTO page_versions(source_id,content_hash,blocks_json,title,captured_at,archived_at)
                                   VALUES(?,?,?,?,?,?)""",
                                (source_id, previous_hash, previous["blocks_json"], previous["last_title"], previous["last_checked_at"], stamp),
                            )
                            db.execute(
                                """DELETE FROM page_versions WHERE source_id=? AND id NOT IN (
                                       SELECT id FROM page_versions WHERE source_id=? ORDER BY id DESC LIMIT ?
                                   )""",
                                (source_id, source_id, PAGE_VERSIONS_RETENTION),
                            )
                            for change in _diff_page_blocks(previous["blocks_json"], blocks_json):
                                db.execute(
                                    """INSERT INTO page_changes(source_id,change_type,term,detail,old_value,new_value,detected_at)
                                       VALUES(?,?,?,?,?,?,?)""",
                                    (
                                        source_id, change["change_type"], change["term"], change["detail"],
                                        change["old_value"], change["new_value"], stamp,
                                    ),
                                )
                        anomaly_at, anomaly_detail = _detect_content_anomaly(db, source_id, page_block_count)
                        db.execute(
                            "INSERT INTO source_metrics(source_id,block_count,text_chars,captured_at) VALUES(?,?,?,?)",
                            (source_id, page_block_count, text_chars, stamp),
                        )
                        db.execute(
                            """DELETE FROM source_metrics WHERE source_id=? AND id NOT IN (
                                   SELECT id FROM source_metrics WHERE source_id=? ORDER BY id DESC LIMIT ?
                               )""",
                            (source_id, source_id, SOURCE_METRICS_RETENTION),
                        )
                        # §4.A audit veille (30/08/2026, Lot 2 §2.3) : une page carrières est
                        # crawlée mais exclue du pipeline marché (voir _select_market_sources) --
                        # son seul usage est ce signal de recrutement, extrait ici plutôt que
                        # dans un passage séparé, puisque le contenu de la page est déjà en main.
                        if fetched_type == "careers":
                            for block in document.blocks:
                                signal = _extract_career_signal(block)
                                if signal:
                                    upsert_actor_event(
                                        db, actor["id"], "hiring", signal, source_url=resolved_url,
                                        # Une page carrieres liste souvent plusieurs intitules sous la
                                        # meme URL : la source seule ne suffit pas a les distinguer.
                                        dedupe_on_description=True,
                                    )
                        # §8.2 audit veille (30/08/2026, Lot 2 §2.4) : jamais crawlés, juste
                        # tracés -- voir hybrid._meaningful_links pour la distinction domaine
                        # racine (admis)/externe (ici uniquement).
                        for outbound in document.outbound_links:
                            db.execute(
                                """INSERT OR IGNORE INTO outbound_links(source_id,target_url,target_host,label,first_seen_at)
                                   VALUES(?,?,?,?,?)""",
                                (source_id, outbound["url"], outbound["host"], outbound.get("label") or None, stamp),
                            )
                        db.execute(
                            """UPDATE actor_sources SET content_hash=?,last_http_status=?,last_checked_at=?,last_title=?,
                                      page_type=?,source_score=?,extraction_mode=?,structure_hash=?,blocks_json=?,last_error=NULL,
                                      ambiguous=?,discovery_depth=?,parent_url=COALESCE(parent_url,?),discovery_reason=COALESCE(discovery_reason,?),
                                      published_date=?,anomaly_detected_at=?,anomaly_detail=?,render_required=?,
                                      etag=?,last_modified_header=?,
                                      last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END WHERE id=?""",
                            (
                                digest, response.status_code, stamp, document.title, fetched_type, fetched_score,
                                document.extraction_method, document.structure_hash, blocks_json, int(not document.blocks),
                                depth, item.get("parent_url"), item.get("reason"), document.published_date,
                                anomaly_at, anomaly_detail, int(document.render_required),
                                new_etag, new_last_modified,
                                int(is_changed), stamp, source_id,
                            ),
                        )

                        depth_limit = max_depth + 1 if fetched_type in HIGH_VALUE_PAGE_TYPES else max_depth
                        if depth < depth_limit:
                            for link in document.links:
                                link_depth = depth + 1
                                existed = db.execute("SELECT id FROM actor_sources WHERE url=?", (link["url"],)).fetchone()
                                db.execute(
                                    """INSERT INTO actor_sources(
                                           actor_id,url,source_kind,page_type,source_score,discovery_depth,discovery_context,
                                           discovery_reason,parent_url,active,discovered_at
                                       ) VALUES(?,?,?,?,?,?,?,?,?,1,?)
                                       ON CONFLICT(url) DO UPDATE SET
                                           page_type=excluded.page_type,
                                           source_score=MAX(actor_sources.source_score, excluded.source_score),
                                           discovery_depth=excluded.discovery_depth,
                                           discovery_context=excluded.discovery_context,
                                           discovery_reason=excluded.discovery_reason,
                                           parent_url=excluded.parent_url,
                                           active=1,
                                           discovered_at=COALESCE(actor_sources.discovered_at,excluded.discovered_at)""",
                                    (
                                        actor["id"], link["url"], "discovered", link["category"], int(link["score"]),
                                        link_depth, link["context"], link.get("reason"), resolved_url, utc_now(),
                                    ),
                                )
                                discovered += int(existed is None)
                                source_row = db.execute("SELECT id FROM actor_sources WHERE url=?", (link["url"],)).fetchone()
                                if source_row:
                                    _push_crawl_item(heap, queued_scores, counter, {
                                        "source_id": source_row["id"],
                                        "url": link["url"],
                                        "page_type": link["category"],
                                        "score": int(link["score"]),
                                        "depth": link_depth,
                                        "parent_url": resolved_url,
                                        "reason": link.get("reason"),
                                    })

                    scanned += 1
                    successful_visits += 1
                    changed += int(is_changed)
                except Exception as exc:
                    errors += 1
                    actor_errors += 1
                    with connect(ACTORS_DB) as db:
                        db.execute(
                            "UPDATE actor_sources SET last_checked_at=?,last_http_status=0,last_error=?,ambiguous=1 WHERE id=?",
                            (utc_now(), str(exc)[:300], source_id),
                        )

            coverage = _source_coverage(actor["id"], site_profile)
            profile, generated_by, confidence = build_profile(actor, documents, ollama, coverage=coverage, site_profile=site_profile)
            encoded, digest = profile_json(profile)
            block_count = sum(len(document.blocks) for _, document in documents)
            strategy, anomaly = adaptive_decision(bool(actor["priority"]), len(documents), block_count, actor_errors, max(1, len(visited)))
            fallback += int(anomaly and not actor["priority"])

            strategic_names = list(site_profile.get("coverage_targets", {}).keys())
            core = [coverage[name] for name in strategic_names]
            coverage_discovered = sum(int(item["discovered"]) > 0 for item in core)
            coverage_ready = sum(int(item["ready"]) > 0 for item in core)
            if anomaly or (coverage_discovered > 0 and coverage_ready == 0):
                status = "degraded"
            elif coverage_discovered > coverage_ready:
                status = "partial"
            else:
                status = "ready"

            with connect(ACTORS_DB) as db:
                db.execute(
                    """UPDATE site_profiles SET strategy=?,status=?,profile_json=?,profile_hash=?,confidence=?,generated_by=?,
                              version=version+1,last_profiled_at=?,needs_reprofile=?,failure_count=CASE WHEN ? THEN failure_count+1 ELSE 0 END,
                              health_score=?,last_error=?,coverage_json=?,coverage_ready=?,coverage_discovered=? WHERE actor_id=?""",
                    (
                        strategy, status, encoded, digest, confidence, generated_by, utc_now(), int(anomaly), int(anomaly),
                        0.3 if anomaly else (0.7 if status == "partial" else 1.0),
                        "Structure inexploitable ou erreurs répétées" if anomaly else None,
                        json.dumps(coverage, ensure_ascii=False), coverage_ready, coverage_discovered, actor["id"],
                    ),
                )
                db.execute(
                    "UPDATE actors SET last_scraped_at=?,last_status=? WHERE id=?",
                    (utc_now(), "ok" if documents else "error", actor["id"]),
                )
            profiled += 1

    with connect(ACTORS_DB) as db:
        db.execute(
            "UPDATE collection_runs SET finished_at=?,status=?,scanned=?,changed=?,errors=?,message=? WHERE id=?",
            (
                utc_now(), "completed", scanned, changed, errors,
                f"{profiled} profils; {discovered} pages découvertes; file dynamique coverage-first; {fallback} bascules adaptatives",
                run_id,
            ),
        )
    # §9.1 audit veille (30/08/2026, Lot 2 §2.2) : purge du reliquat non traité à chaque run --
    # voir db.purge_stale_backlog pour les critères exacts (page_type='other', jamais visitée,
    # découverte il y a plus de BACKLOG_PURGE_DAYS jours).
    backlog_purged = purge_stale_backlog(db_path=ACTORS_DB)
    return {
        "scanned": scanned,
        "changed": changed,
        "errors": errors,
        "discovered": discovered,
        "profiled": profiled,
        "adaptive_fallbacks": fallback,
        "backlog_purged": backlog_purged,
        "ollama": ollama.available(),
        "crawler": "deterministic-coverage-first-v2",
    }

# === Bloc 6/6 : scrape_market() et scrape_technology(), les deux autres collectes ===
def _source_family(page_type: str | None) -> str:
    """Regroupe les types de page détaillés (service/capability/product, application/
    case_study/market...) en quelques "familles" plus larges, utilisées pour équilibrer la
    sélection de pages à analyser (voir _select_market_sources)."""
    page_type = normalize_page_type(page_type)
    if page_type in {"service", "capability", "product"}:
        return "service_capability"
    if page_type in {"application", "case_study", "market"}:
        return "application_market"
    if page_type == "technology":
        return "technology"
    if page_type == "project":
        return "project"
    if page_type == "news":
        return "news"
    return "other"


# Chantier 2 item 4 : la collecte marché plafonnait à 120 pages au total pour tous les
# acteurs (3,4 pages/acteur/passe selon l'audit), un budget calibré pour un pilote plutôt
# qu'un observatoire. scrape_market() calcule maintenant son budget par défaut comme
# MARKET_PAGES_PER_ACTOR * nb d'acteurs actifs (ex: 10 x 35 = 350) au lieu d'une constante fixe.
MARKET_PAGES_PER_ACTOR = 10


def _select_market_sources(max_pages: int = 350) -> list[dict]:
    """Choisit, parmi TOUTES les pages déjà crawlées (actor_sources), lesquelles analyser pour
    en extraire des faits marché -- en 3 passes successives (voir `take()` plus bas) :
    1. Une page par (acteur, famille) pour tous les acteurs -- garantit une couverture large
       avant qu'un acteur "riche" ne monopolise le budget max_pages.
    2. Pour les acteurs priority ou avec un SITE_OVERRIDE dédié, approfondit selon leurs
       market_source_quotas (ex: jusqu'à 6 pages "application_market" pour FEMTOprint).
    3. Remplit le budget restant par score décroissant, toutes familles confondues.

    Une page dont le contenu n'a pas changé depuis sa dernière analyse marché (content_hash ==
    market_extracted_hash, colonne mise à jour par scrape_market après coup) est exclue d'office
    -- chantier 2 item 4 : "ne re-analyser que les pages dont le content_hash a changé".
    Coverage-balanced source selection: first one page/family/actor, then deepen priority actors."""
    with connect(ACTORS_DB) as db:
        rows = [dict(row) for row in db.execute(
            """SELECT a.id AS actor_id,a.name,a.priority,a.official_url,p.strategy,
                      s.id AS source_id,s.url,s.page_type,s.source_score,s.blocks_json,
                      s.last_title,s.last_checked_at,s.extraction_mode,s.content_hash,s.published_date
               FROM actor_sources s
               JOIN actors a ON a.id=s.actor_id
               LEFT JOIN site_profiles p ON p.actor_id=a.id
               WHERE s.active=1 AND COALESCE(s.page_type,'') NOT IN ('ignore','careers')
                 AND (s.last_http_status IS NULL OR s.last_http_status BETWEEN 200 AND 399)
                 AND (s.content_hash IS NULL OR s.market_extracted_hash IS NULL OR s.content_hash!=s.market_extracted_hash)
               ORDER BY a.priority DESC,a.name,s.source_score DESC,s.id"""
        ).fetchall()]

    by_actor: dict[int, list[dict]] = defaultdict(list)
    by_actor_family: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    actor_meta: dict[int, dict] = {}
    for row in rows:
        actor_id = int(row["actor_id"])
        row["source_family"] = _source_family(row.get("page_type"))
        by_actor[actor_id].append(row)
        by_actor_family[actor_id][row["source_family"]].append(row)
        actor_meta.setdefault(actor_id, row)

    selected: list[dict] = []
    selected_ids: set[int] = set()
    selected_counts: dict[tuple[int, str], int] = defaultdict(int)
    cursors: dict[tuple[int, str], int] = defaultdict(int)
    actor_ids = sorted(by_actor, key=lambda aid: (-int(actor_meta[aid]["priority"]), str(actor_meta[aid]["name"])))
    family_order = ["service_capability", "application_market", "technology", "project", "news", "other"]

    def take(actor_id: int, family: str, count: int) -> None:
        """Take the next best rows from a pre-grouped bucket without rescanning/sorting it."""
        if len(selected) >= max_pages or count <= 0:
            return
        key = (actor_id, family)
        options = by_actor_family[actor_id].get(family, [])
        cursor = cursors[key]
        taken = 0
        while cursor < len(options) and taken < count and len(selected) < max_pages:
            row = options[cursor]
            cursor += 1
            source_id = int(row["source_id"])
            if source_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(source_id)
            selected_counts[key] += 1
            taken += 1
        cursors[key] = cursor

    # Pass 1: broad coverage for every actor before one rich category can monopolise the global limit.
    for family in family_order:
        for actor_id in actor_ids:
            take(actor_id, family, 1)
            if len(selected) >= max_pages:
                return selected

    # Pass 2: deepen actors we've deliberately invested crawl effort in -- either flagged
    # priority, or carrying a hand-written SITE_OVERRIDE (a site profile is itself a signal
    # that this actor's structure was worth the analysis, independent of the priority flag).
    for actor_id in actor_ids:
        meta = actor_meta[actor_id]
        if not int(meta["priority"]) and meta["name"] not in SITE_OVERRIDES:
            continue
        profile = get_site_profile({"name": meta["name"], "official_url": meta["official_url"]})
        quotas = profile.get("market_source_quotas", {})
        for family in family_order:
            already = selected_counts[(actor_id, family)]
            take(actor_id, family, max(0, int(quotas.get(family, 0)) - already))
            if len(selected) >= max_pages:
                return selected

    # Pass 3: fill remaining capacity by score, still preserving deterministic ordering.
    remaining = [row for row in rows if int(row["source_id"]) not in selected_ids]
    remaining.sort(key=lambda row: (-int(row["priority"]), -int(row.get("source_score") or 0), str(row["name"]), int(row["source_id"])))
    selected.extend(remaining[: max(0, max_pages - len(selected))])
    return selected[:max_pages]


def _stored_blocks(source: dict, *, max_age_hours: int = 24) -> list[ContentBlock] | None:
    """Si la page a déjà été crawlée récemment (actor_sources.blocks_json, < max_age_hours),
    reconstruit ses ContentBlock depuis le JSON stocké plutôt que de re-télécharger/re-parser
    la page -- évite une requête HTTP redondante pendant scrape_market() quand scrape_actors()
    vient de tourner juste avant. Renvoie None si rien d'utilisable n'est stocké.
    Reuse blocks produced by the actor crawl when they are recent and structurally valid."""
    raw = source.get("blocks_json")
    checked_at = source.get("last_checked_at")
    if not raw or not checked_at:
        return None
    try:
        checked = datetime.fromisoformat(str(checked_at))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - checked.astimezone(timezone.utc) > timedelta(hours=max_age_hours):
            return None
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None

    blocks: list[ContentBlock] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        blocks.append(ContentBlock(
            heading=str(item.get("heading") or ""),
            text=str(item.get("text") or ""),
            path=str(item.get("path") or ""),
            media_context=str(item.get("media_context") or ""),
            h1=str(item.get("h1") or ""),
            h2=str(item.get("h2") or ""),
            h3=str(item.get("h3") or ""),
        ))
    return blocks or None


def _ensure_market_fact_status_column(db) -> None:
    """Keep direct unit-test/maintenance connections compatible with additive schema changes."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)").fetchall()}
    if "fact_status" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN fact_status TEXT NOT NULL DEFAULT 'review'")
    if "last_seen_at" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN last_seen_at TEXT")
    if "application_key" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN application_key TEXT")
    if "is_verbatim" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN is_verbatim INTEGER NOT NULL DEFAULT 1")
    if "architecture" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN architecture TEXT")
    if "evidence_type" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN evidence_type TEXT")
    if "date_confidence" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN date_confidence TEXT")
    if "is_backfill" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN is_backfill INTEGER")
    source_columns = {row[1] for row in db.execute("PRAGMA table_info(evidence_sources)").fetchall()}
    if "is_verbatim" not in source_columns:
        db.execute("ALTER TABLE evidence_sources ADD COLUMN is_verbatim INTEGER NOT NULL DEFAULT 1")
    db.execute(
        """CREATE TABLE IF NOT EXISTS evidence_bucket_transitions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               evidence_id INTEGER NOT NULL,
               from_bucket TEXT,
               to_bucket TEXT NOT NULL,
               changed_at TEXT NOT NULL
           )"""
    )


def _upsert_market_candidate(db, candidate: dict) -> tuple[int, int]:
    """Store one canonical fact and one or more multilingual source proofs.

    Facts are looked up by ``application_key`` (actor+market+component+operation, independent
    of maturity) instead of the legacy bucket-inclusive ``fact_key``, so an application that
    moves from radar to industrial production between two collections updates the same row
    instead of forking a second one. Maturity only ever moves forward automatically
    (radar -> existing, tracked in ``evidence_bucket_transitions``); a later radar-level signal
    for an already-existing application is kept as supporting evidence but does not downgrade
    the canonical bucket. Callers that don't supply ``application_key`` (legacy/hand-built
    candidates, e.g. in tests) fall back to ``fact_key``, preserving the previous bucket-scoped
    behaviour rather than crashing.
    """
    _ensure_market_fact_status_column(db)
    stamp = utc_now()
    fact_key = candidate["fact_key"]
    app_key = candidate.get("application_key") or fact_key
    row = db.execute(
        "SELECT id,field_confidence,bucket,date_confidence,created_at FROM evidence WHERE application_key=?", (app_key,)
    ).fetchone()
    fact_added = 0
    if row:
        evidence_id = int(row["id"])
        old_confidence = float(row["field_confidence"] or 0)
        old_bucket = row["bucket"]
        new_bucket = candidate["bucket"]
        upgrades_bucket = BUCKET_RANK.get(new_bucket, -1) > BUCKET_RANK.get(old_bucket, -1)
        effective_bucket = new_bucket if upgrades_bucket else old_bucket
        if upgrades_bucket and new_bucket != old_bucket:
            db.execute(
                "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
                (evidence_id, old_bucket, new_bucket, stamp),
            )
        # Validity is independent from maturity and from confidence ranking: a fresh deterministic
        # relation may validate an older review fact even when its numeric confidence is not higher.
        if candidate.get("fact_status") == "validated":
            db.execute(
                "UPDATE evidence SET fact_status='validated',review_status='accepted',bucket=?,updated_at=? WHERE id=?",
                (effective_bucket, stamp, evidence_id),
            )
        if float(candidate.get("confidence", 0)) > old_confidence:
            # Revue chronologie du 30/08/2026 : date_confidence ne peut que monter en rang
            # (published > observed_only > unknown, voir db.DATE_CONFIDENCE_RANK) -- une
            # réobservation qui ne trouve plus de date de publication réelle ne doit jamais
            # dégrader une ligne déjà confirmée 'published'. is_backfill n'est recalculé que
            # quand cette ligne obtient une date de publication fiable CE tour-ci, à partir du
            # created_at d'origine (jamais réécrit) -- pas de `stamp` : le fait a été vu pour la
            # première fois à created_at, pas maintenant.
            candidate_date_confidence = candidate.get("date_confidence")
            if candidate_date_confidence and DATE_CONFIDENCE_RANK.get(candidate_date_confidence, -1) >= DATE_CONFIDENCE_RANK.get(row["date_confidence"], -1):
                next_date_confidence = candidate_date_confidence
                next_source_date = candidate.get("source_date")
                next_is_backfill = compute_is_backfill(row["created_at"], next_source_date, candidate_date_confidence)
            else:
                # COALESCE ci-dessous conserve alors les 3 valeurs existantes -- surtout ne pas
                # laisser source_date changer seul : sinon la date affichée se déconnecte du
                # date_confidence/is_backfill qu'elle a servi à calculer la fois précédente.
                next_date_confidence = None
                next_source_date = None
                next_is_backfill = None
            db.execute(
                """UPDATE evidence SET industrial_stage=?,source_url=?,source_title=?,source_date=COALESCE(?,source_date),
                          quote=?,is_verbatim=?,evidence_type=COALESCE(?,evidence_type),field_confidence=?,
                          laser_process=COALESCE(?,laser_process),material=COALESCE(?,material),performance=COALESCE(?,performance),
                          architecture=COALESCE(?,architecture),
                          maturity_level=COALESCE(?,maturity_level),relation_strength=COALESCE(?,relation_strength),source_role=COALESCE(?,source_role),
                          date_confidence=COALESCE(?,date_confidence),is_backfill=COALESCE(?,is_backfill),
                          bucket=?,fact_status=?,review_status=?,updated_at=? WHERE id=?""",
                (
                    candidate["stage"], candidate["url"], candidate["title"], next_source_date,
                    candidate["quote"], int(candidate.get("is_verbatim", True)), candidate.get("evidence_type"), candidate["confidence"],
                    candidate.get("process"), candidate.get("material"), candidate.get("performance"),
                    candidate.get("architecture"), candidate.get("maturity"),
                    candidate.get("relation_strength"), candidate.get("source_role"),
                    next_date_confidence, next_is_backfill, effective_bucket,
                    candidate.get("fact_status", "validated"), "accepted" if candidate.get("fact_status") == "validated" else "review",
                    stamp, evidence_id,
                ),
            )
    else:
        fact_status = candidate.get("fact_status", "validated")
        review_status = "accepted" if fact_status == "validated" else "review"
        # Revue chronologie du 30/08/2026 : `stamp` == created_at de cette ligne, jamais réécrit
        # ensuite (voir last_seen_at plus bas) -- c'est donc bien la première observation à
        # utiliser pour is_backfill, y compris pour cette toute première insertion.
        is_backfill = compute_is_backfill(stamp, candidate.get("source_date"), candidate.get("date_confidence"))
        evidence_id = db.execute(
            """INSERT INTO evidence(
                   actor_name,bucket,market,component,operation,industrial_stage,source_url,source_title,source_date,
                   quote,is_verbatim,evidence_type,source_group,date_confidence,is_backfill,
                   fingerprint,fact_key,application_key,evidence_kind,language,review_status,created_at,updated_at,block_heading,block_path,
                   extraction_mode,field_confidence,laser_process,material,performance,architecture,maturity_level,relation_strength,relation_evidence,source_role,fact_status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'market_application',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate["actor"], candidate["bucket"], candidate["market"], candidate["component"], candidate["operation"],
                candidate["stage"], candidate["url"], candidate["title"], candidate.get("source_date"),
                candidate["quote"], int(candidate.get("is_verbatim", True)), candidate.get("evidence_type"), fact_key,
                candidate.get("date_confidence"), is_backfill,
                candidate["fingerprint"], fact_key, app_key, language_from_url(candidate["url"]), review_status, stamp, stamp,
                candidate["block_heading"], candidate["block_path"], candidate["mode"], candidate["confidence"],
                candidate.get("process"), candidate.get("material"), candidate.get("performance"), candidate.get("architecture"), candidate.get("maturity"),
                candidate.get("relation_strength"), candidate.get("relation_evidence"), candidate.get("source_role"), fact_status,
            ),
        ).lastrowid
        db.execute(
            "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
            (evidence_id, None, candidate["bucket"], stamp),
        )
        fact_added = 1

    # ``updated_at`` means the canonical representation changed; ``last_seen_at`` means
    # the fact was observed again. Keeping both is essential for monthly-delta reporting.
    db.execute("UPDATE evidence SET last_seen_at=? WHERE id=?", (stamp, evidence_id))

    source_added = upsert_fact_source(
        db, "evidence_sources", evidence_id,
        source_url=candidate["url"], source_title=candidate["title"],
        source_date=candidate.get("source_date"), quote=candidate["quote"],
        is_verbatim=int(candidate.get("is_verbatim", True)),
        language=language_from_url(candidate["url"]),
        block_heading=candidate["block_heading"], block_path=candidate["block_path"],
        extraction_mode=candidate["mode"], field_confidence=candidate["confidence"],
        relation_strength=candidate.get("relation_strength"),
        relation_evidence=candidate.get("relation_evidence"),
        source_role=candidate.get("source_role"),
        fingerprint=candidate["source_fingerprint"], created_at=stamp,
    )
    return fact_added, source_added


def _offer_review_reasons(candidate: dict, source_count_after: int) -> list[str]:
    """§10.7 audit veille (30/08/2026, correction de la recommandation §9.9) : field_confidence
    n'a pas la résolution nécessaire pour piloter la file de revue des offres -- 72% de la masse
    tient sur deux valeurs (0.78/0.83), 19% sont NULL. Un seuil ne peut produire que deux régimes
    inutilisables (flaguer 19 offres ou 274). Route vers la revue sur des critères STRUCTURELS,
    explicables et corrélés aux modes d'échec observés, au lieu d'un score continu déguisé.
    ``source_count_after`` est le nombre de source_url DISTINCTES sur cette offre en comptant
    celle en cours d'ajout -- une offre encore vue sur une seule page n'est jamais "confirmée",
    même si on la revoit plusieurs fois sur cette même page."""
    reasons: list[str] = []
    if not _has_predicate(candidate["quote"]):
        reasons.append("no_predicate")
    if not candidate.get("page_type"):
        reasons.append("page_type_null")
    if urlparse(candidate["url"]).path in ("", "/"):
        reasons.append("homepage_citation")
    if not candidate.get("operation"):
        reasons.append("operation_null")
    if source_count_after <= 1:
        reasons.append("single_unconfirmed_source")
    return reasons


def _upsert_offer_candidate(db, candidate: dict) -> tuple[int, int]:
    """Même logique que _upsert_market_candidate mais pour la table `offers` (clé fact_key
    directe, sans distinction bucket/application_key puisqu'une offre n'a pas de maturité
    "métier" à faire progresser). review_status est calculé à chaque passage (insertion, et
    mise à jour quand la confiance augmente) via _offer_review_reasons -- §10.7 audit veille,
    plus jamais 'accepted' d'office. Renvoie (1 si nouvelle offre créée, 1 si nouvelle preuve
    créée)."""
    stamp = utc_now()
    row = db.execute(
        "SELECT id,field_confidence,date_confidence,created_at FROM offers WHERE fact_key=?", (candidate["fact_key"],)
    ).fetchone()
    fact_added = 0
    existing_urls = (
        {r[0] for r in db.execute("SELECT DISTINCT source_url FROM offer_sources WHERE offer_id=?", (int(row["id"]),))}
        if row else set()
    )
    source_count_after = len(existing_urls | {candidate["url"]})
    review_status = "review" if _offer_review_reasons(candidate, source_count_after) else "accepted"
    if row:
        offer_id = int(row["id"])
        if float(candidate.get("confidence", 0)) > float(row["field_confidence"] or 0):
            # Revue chronologie du 30/08/2026 : même règle que _upsert_market_candidate --
            # date_confidence ne redescend jamais, is_backfill n'est recalculé que quand cette
            # ligne obtient une date de publication fiable ce tour-ci, à partir de son
            # created_at d'origine (première observation), pas de `stamp`.
            candidate_date_confidence = candidate.get("date_confidence")
            if candidate_date_confidence and DATE_CONFIDENCE_RANK.get(candidate_date_confidence, -1) >= DATE_CONFIDENCE_RANK.get(row["date_confidence"], -1):
                next_date_confidence = candidate_date_confidence
                next_source_date = candidate.get("source_date")
                next_is_backfill = compute_is_backfill(row["created_at"], next_source_date, candidate_date_confidence)
            else:
                # COALESCE ci-dessous conserve alors les 3 valeurs existantes -- voir
                # _upsert_market_candidate ci-dessus pour le raisonnement complet.
                next_date_confidence = None
                next_source_date = None
                next_is_backfill = None
            db.execute(
                """UPDATE offers SET industrial_stage=?,source_url=?,source_title=?,source_date=COALESCE(?,source_date),
                          quote=?,is_verbatim=?,evidence_type=COALESCE(?,evidence_type),field_confidence=?,
                          material=COALESCE(?,material),performance=COALESCE(?,performance),
                          date_confidence=COALESCE(?,date_confidence),is_backfill=COALESCE(?,is_backfill),
                          updated_at=? WHERE id=?""",
                (
                    candidate["stage"], candidate["url"], candidate["title"], next_source_date,
                    candidate["quote"], int(candidate.get("is_verbatim", True)), candidate.get("evidence_type"), candidate["confidence"],
                    candidate.get("material"), candidate.get("performance"),
                    next_date_confidence, next_is_backfill, stamp, offer_id,
                ),
            )
    else:
        # Revue chronologie du 30/08/2026 : `stamp` == created_at de cette ligne (première
        # observation), voir _upsert_market_candidate ci-dessus pour le même raisonnement.
        is_backfill = compute_is_backfill(stamp, candidate.get("source_date"), candidate.get("date_confidence"))
        offer_id = db.execute(
            """INSERT INTO offers(
                   actor_name,offer_type,capability,operation,laser_process,material,performance,industrial_stage,page_type,
                   source_url,source_title,source_date,quote,is_verbatim,evidence_type,date_confidence,is_backfill,
                   fact_key,fingerprint,review_status,field_confidence,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate["actor"], candidate["offer_type"], candidate["capability"], candidate.get("operation"), candidate.get("process"),
                candidate.get("material"), candidate.get("performance"), candidate["stage"], candidate["page_type"], candidate["url"],
                candidate["title"], candidate.get("source_date"), candidate["quote"], int(candidate.get("is_verbatim", True)),
                candidate.get("evidence_type"), candidate.get("date_confidence"), is_backfill,
                candidate["fact_key"], candidate["fingerprint"], review_status, candidate["confidence"], stamp, stamp,
            ),
        ).lastrowid
        fact_added = 1

    # review_status is recomputed here regardless of the confidence branch above: source_count_
    # after can cross the single-source threshold (the dominant _offer_review_reasons signal)
    # even when this pass's own confidence doesn't beat the stored field_confidence.
    db.execute("UPDATE offers SET last_seen_at=?,review_status=? WHERE id=?", (stamp, review_status, offer_id))

    source_added = upsert_fact_source(
        db, "offer_sources", offer_id,
        source_url=candidate["url"], source_title=candidate["title"],
        source_date=candidate.get("source_date"), quote=candidate["quote"],
        is_verbatim=int(candidate.get("is_verbatim", True)),
        language=language_from_url(candidate["url"]),
        block_heading=candidate["block_heading"], block_path=candidate["block_path"],
        extraction_mode=candidate["mode"], field_confidence=candidate["confidence"],
        fingerprint=candidate["source_fingerprint"], created_at=stamp,
    )
    return fact_added, source_added


def _upsert_vocabulary_candidate(db, candidate: dict) -> int:
    """Queue one AI-proposed label that matched no known lexicon entry for human review.

    Returns 1 when a new row was created, 0 when an existing one was just refreshed
    (``last_seen_at`` bumped) -- mirrors the fact-added/source-added return convention used
    by the other _upsert_* helpers in this module.
    """
    stamp = utc_now()
    row = db.execute("SELECT id FROM vocabulary_candidates WHERE fingerprint=?", (candidate["fingerprint"],)).fetchone()
    if row:
        db.execute("UPDATE vocabulary_candidates SET last_seen_at=? WHERE id=?", (stamp, row["id"]))
        return 0
    db.execute(
        """INSERT INTO vocabulary_candidates(
               actor_name,source_url,source_title,quote,block_heading,proposed_labels,resolved_labels,
               fingerprint,review_status,created_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)""",
        (
            candidate["actor"], candidate["url"], candidate["title"], candidate["quote"], candidate["block_heading"],
            json.dumps(candidate["proposed_labels"], ensure_ascii=False),
            json.dumps(candidate["resolved_labels"], ensure_ascii=False),
            candidate["fingerprint"], stamp, stamp,
        ),
    )
    return 1


def scrape_market(max_pages: int | None = None, actor_names: list[str] | None = None) -> dict:
    """Run the market extraction pass.

    ``max_pages``, when omitted, is resolved to ``MARKET_PAGES_PER_ACTOR`` times the number of
    active actors (chantier 2 item 4) instead of a fixed constant, so the budget scales with
    the roster instead of quietly starving it as more actors are added.

    ``actor_names``, when given, restricts the run to those actors (matched against
    ``source["name"]``) -- meant for trying a prompt/provider change on 2-3 actors before
    opening it to the full roster, without touching the selection/coverage logic itself.

    Déroulé, pour chaque page sélectionnée (_select_market_sources, qui exclut déjà les pages
    inchangées depuis leur dernière analyse) :
    1. Récupère ses blocs de contenu -- depuis le cache (_stored_blocks) si récent, sinon en
       re-téléchargeant la page.
    2. Calcule le marché "ambiant" porté par l'URL/le titre de la page (_url_market_hint,
       chantier 2 item 1), passé à _candidate() pour les blocs qui n'en portent aucun localement.
    3. Pour chaque bloc : tente _candidate() (fait marché déterministe, complet ou partiel --
       chantier 2 item 2) et _offer_candidates() (capacité concurrente déterministe).
    4. Si le profil de l'acteur est "adaptive", complète avec _ai_candidates() (repli IA) sur
       les seuls blocs que le lexique déterministe n'a pas su transformer en fait ce passage
       (chantier 2 item 3) -- jamais publié directement, toujours fact_status='review'.
    5. Déduplique les candidats de la page (_dedupe_candidates), puis les écrit en base via
       _upsert_market_candidate / _upsert_offer_candidate / _upsert_vocabulary_candidate, et
       marque la page comme analysée à son content_hash actuel (market_extracted_hash) pour
       qu'un run suivant ne la ré-analyse pas tant qu'elle n'a pas changé.
    Les compteurs (`diagnostics`) et jetons IA consommés sont journalisés dans collection_runs
    pour pouvoir diagnostiquer une collecte a posteriori sans avoir à la relancer.
    """
    _load_custom_lexicon_entries()
    with connect(MARKET_DB) as db:
        run_id = db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (utc_now(), "running")).lastrowid
    if max_pages is None:
        with connect(ACTORS_DB) as db:
            actor_count = int(db.execute("SELECT COUNT(*) FROM actors WHERE active=1").fetchone()[0])
        max_pages = MARKET_PAGES_PER_ACTOR * max(1, actor_count)
    sources = _select_market_sources(max_pages=max_pages)
    if actor_names:
        wanted = set(actor_names)
        sources = [source for source in sources if source["name"] in wanted]
    scanned = market_added = offers_added = sources_added = vocabulary_queued = rejected = errors = 0
    diagnostics: dict[str, int] = defaultdict(int)
    ollama = get_ai_client()

    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        for source in sources:
            try:
                cached_blocks = _stored_blocks(source)
                if cached_blocks:
                    blocks = cached_blocks
                    source_url = source["url"]
                    source_title = source.get("last_title") or ""
                    page_type = normalize_page_type(source.get("page_type"))
                    published_date = source.get("published_date")
                    diagnostics["stored_pages_reused"] += 1
                else:
                    response = _fetch(client, source["url"])
                    resolved_url = str(response.url)
                    site_profile = get_site_profile({"name": source["name"], "official_url": source["official_url"]})
                    if is_pdf_response(response.headers.get("content-type", ""), resolved_url):
                        document = parse_pdf_document(response.content, resolved_url, profile=site_profile)
                    else:
                        document = parse_document(response.text, resolved_url, profile=site_profile)
                    blocks = document.blocks
                    source_url = resolved_url
                    source_title = document.title
                    page_type = document.page_type
                    published_date = document.published_date
                    diagnostics["network_pages_fetched"] += 1

                # Chantier 4 : date de publication de la page si connue, sinon date d'observation
                # (jamais NULL -- un fait sans aucune date ne peut ni vieillir ni se comparer).
                # Revue chronologie du 30/08/2026 : date_confidence porte désormais la trace de
                # laquelle des deux c'était (voir db.classify_source_date) -- sans cette trace,
                # /api/monthly ne pouvait pas distinguer un vrai signal récent d'une vieille page
                # simplement recrawlée aujourd'hui (voir _upsert_market_candidate/
                # _upsert_offer_candidate pour l'usage de date_confidence à l'écriture).
                source_date, date_confidence = classify_source_date(published_date)
                page_market = _url_market_hint(source_url, source_title)

                market_candidates: list[dict] = []
                offer_candidates: list[dict] = []
                covered_indices: set[int] = set()

                for index, block in enumerate(blocks):
                    _, section = _context_for_block(source_title, blocks, index)
                    structured = _structured_neighbors(blocks, index)
                    candidate = _candidate(
                        source["name"], source_url, source_title, block, context_text=section,
                        structured_blocks=structured, diagnostics=diagnostics, page_market=page_market,
                        source_date=source_date, date_confidence=date_confidence,
                    )
                    if candidate:
                        market_candidates.append(candidate)
                        covered_indices.add(index)
                    offer_candidates.extend(
                        _offer_candidates(
                            source["name"], source_url, source_title, block, page_type=page_type,
                            source_date=source_date, date_confidence=date_confidence,
                        )
                    )

                # AI now focuses on blocks the deterministic lexicon rejected this pass, not the
                # ones it already resolved -- see _ai_candidates' docstring (chantier 2 item 3).
                vocabulary_candidates: list[dict] = []
                if source.get("strategy") == "adaptive":
                    for candidate in _ai_candidates(
                        source["name"], source_url, source_title, blocks, ollama, diagnostics,
                        exclude_indices=covered_indices, source_date=source_date, date_confidence=date_confidence,
                    ):
                        if candidate.get("kind") == "vocabulary_candidate":
                            vocabulary_candidates.append(candidate)
                        else:
                            market_candidates.append(candidate)

                market_candidates = _dedupe_candidates(market_candidates)
                offer_candidates = _dedupe_candidates(offer_candidates)
                vocabulary_candidates = _dedupe_candidates(vocabulary_candidates)
                scanned += 1

                # Mark this page as analysed at its current content_hash regardless of outcome
                # (even zero candidates is a completed analysis) so the next run's unchanged-hash
                # filter in _select_market_sources skips it until the page actually changes.
                with connect(ACTORS_DB) as actors_db:
                    actors_db.execute(
                        "UPDATE actor_sources SET market_extracted_hash=? WHERE id=?",
                        (source.get("content_hash"), source["source_id"]),
                    )

                if not market_candidates and not offer_candidates and not vocabulary_candidates:
                    rejected += 1
                    continue

                with connect(MARKET_DB) as db:
                    for candidate in market_candidates:
                        fact_added, proof_added = _upsert_market_candidate(db, candidate)
                        market_added += fact_added
                        sources_added += proof_added
                    for candidate in offer_candidates:
                        fact_added, proof_added = _upsert_offer_candidate(db, candidate)
                        offers_added += fact_added
                        sources_added += proof_added
                    for candidate in vocabulary_candidates:
                        vocabulary_queued += _upsert_vocabulary_candidate(db, candidate)
            except Exception as exc:
                errors += 1
                with connect(ACTORS_DB) as db:
                    db.execute("UPDATE actor_sources SET last_error=?,ambiguous=1 WHERE id=?", (str(exc)[:300], source["source_id"]))

    with connect(MARKET_DB) as db:
        db.execute(
            """UPDATE collection_runs SET finished_at=?,status=?,scanned=?,added=?,offers_added=?,sources_added=?,rejected=?,errors=?,
                      blocks_examined=?,laser_blocks=?,candidate_count=?,diagnostics_json=?,message=?,
                      ai_input_tokens=?,ai_output_tokens=? WHERE id=?""",
            (
                utc_now(), "completed", scanned, market_added, offers_added, sources_added, rejected, errors,
                int(diagnostics.get("blocks_examined", 0)), int(diagnostics.get("laser_blocks", 0)),
                int(diagnostics.get("candidate_valid", 0)), json.dumps(dict(sorted(diagnostics.items())), ensure_ascii=False),
                "Collecte v3.4: relation directe/contextuelle/structurée; validité séparée de la maturité; télémétrie des rejets",
                ollama.total_input_tokens, ollama.total_output_tokens,
                run_id,
            ),
        )
    return {
        "scanned": scanned,
        "market_added": market_added,
        "offers_added": offers_added,
        "proof_sources_added": sources_added,
        "vocabulary_queued": vocabulary_queued,
        "rejected": rejected,
        "errors": errors,
        "ollama": ollama.available(),
        "ai_provider": "anthropic" if isinstance(ollama, AnthropicClient) else "ollama",
        "ai_input_tokens": ollama.total_input_tokens,
        "ai_output_tokens": ollama.total_output_tokens,
        "source_selection": "coverage-balanced-v3.4",
        "diagnostics": dict(sorted(diagnostics.items())),
    }


def upsert_document_technology_signal(
    db, title: str, abstract: str, source_url: str, actor_name: str | None = None,
) -> int:
    """§4.C.2 audit veille (30/08/2026, Lot 3 §3.5) : "axe + maturité sur les documents
    Crossref/OpenAlex -- branchement de lexiques existants." Un document est déjà filtré
    on-topic avant d'être inséré dans `documents` (voir scrape_technology/
    openalex.collect_openalex_publications) ; ce qui manquait était de le classer sur les MÊMES
    lexiques que cordis.py utilise déjà pour les projets (PROCESS_TECHNOLOGIES, MATURITY_RULES)
    plutôt que de le laisser sans axe technologique. Un document peut porter plusieurs axes.
    Même garde-fou anti-faux-positif que is_on_topic() : un axe générique
    (_GENERIC_PROCESS_AXES) ne compte que si un vrai terme laser est aussi présent dans le
    texte, jamais seul. Renvoie le nombre de nouveaux signaux créés (0 si déjà connus ou aucun
    axe détecté)."""
    text = f"{title} {abstract or ''}"
    labels = {label for label, _ in _match_all_labels(text, PROCESS_TECHNOLOGIES)}
    if not _laser_match(text):
        labels -= _GENERIC_PROCESS_AXES
    if not labels:
        return 0

    bucket, stage = _detect_maturity(text)
    if bucket == "unknown":
        bucket = "radar"
    actor_names = [actor_name] if actor_name else []
    added = 0
    for axis in sorted(labels):
        # Discriminant sur l'URL du document (stable, toujours disponible), pas son titre --
        # deux documents distincts ne partagent jamais une URL, contrairement à un titre qui
        # pourrait coïncider.
        fact_key = technology_signal_key(axis, source_url)
        quote = _quote(text, PROCESS_TECHNOLOGIES.get(axis, {}).get("any_of", ()))
        created, _ = upsert_technology_signal(
            db,
            fact_key=fact_key,
            axis=axis,
            maturity_stage=stage,
            bucket=bucket,
            actor_names=actor_names,
            source_url=source_url,
            quote=quote,
            field_confidence=0.7,
            source_title=title,
        )
        added += created
    return added


def scrape_technology(limit: int = 80, lookback_days: int = 60) -> dict:
    """Collect recent publication signals from several targeted Crossref queries.

    This remains a publication collector; patents and funded-project registries require
    dedicated connectors and are intentionally not inferred from Crossref metadata.
    """
    with connect(TECH_DB) as db:
        run_id = db.execute(
            "INSERT INTO collection_runs(started_at,status) VALUES(?,?)",
            (utc_now(), "running"),
        ).lastrowid
        existing_documents = int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    # The first run backfills a wider horizon; subsequent runs stay focused on recent change.
    effective_lookback = max(1, 730 if existing_documents == 0 else lookback_days)
    from_date = (date.today() - timedelta(days=effective_lookback)).isoformat()
    per_query = max(5, min(40, (max(1, limit) + len(TECHNOLOGY_QUERIES) - 1) // len(TECHNOLOGY_QUERIES)))

    scanned = relevant = added = errors = technology_signals_added = 0
    pooled: dict[str, dict] = {}
    messages: list[str] = []

    try:
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            for query in TECHNOLOGY_QUERIES:
                try:
                    response = client.get(
                        "https://api.crossref.org/works",
                        params={
                            "query.bibliographic": query,
                            "rows": per_query,
                            "filter": f"from-pub-date:{from_date}",
                            "sort": "published",
                            "order": "desc",
                            "select": "DOI,title,URL,published,abstract",
                        },
                    )
                    response.raise_for_status()
                    items = ((response.json() or {}).get("message") or {}).get("items") or []
                    scanned += len(items)
                    for item in items:
                        title = " ".join(item.get("title") or []).strip()
                        url = str(item.get("URL") or "").strip()
                        abstract = re.sub(r"<[^>]+>", " ", item.get("abstract") or "")
                        abstract = re.sub(r"\s+", " ", abstract).strip()[:3000]
                        if not title or not url:
                            continue

                        # Crossref search can return spectroscopy/biology papers despite a laser query.
                        # Require an explicit ultrafast/femtosecond signal in title or abstract.
                        if not _laser_match(f"{title} {abstract}"):
                            continue

                        doi = str(item.get("DOI") or "").strip() or None
                        key = (doi or url).casefold()
                        pooled.setdefault(key, {
                            "title": title,
                            "url": url,
                            "doi": doi,
                            "published": "-".join(
                                str(part)
                                for part in (((item.get("published") or {}).get("date-parts") or [[]])[0])
                            ) or None,
                            "abstract": abstract,
                        })
                except Exception as exc:
                    errors += 1
                    messages.append(f"{query}: {str(exc)[:140]}")

        relevant = len(pooled)
        # connect() commits only once, at the end of this `with` block; any exception raised
        # inside it rolls back every insert made so far in the loop, not just the offending item.
        # One malformed Crossref record must not discard an otherwise-good batch, so each item is
        # isolated here instead of letting it escape to the `with` block.
        with connect(TECH_DB) as db:
            for item in list(pooled.values())[: max(1, limit)]:
                try:
                    # Ecriture deleguee a db.upsert_document, partagee avec openalex.py et
                    # patent.py : meme dedoublonnage sur empreinte, meme rafraichissement de
                    # last_seen_at, meme calcul date_confidence/is_backfill. Crossref est la
                    # seule des trois sources a ne pas attribuer d'acteur (collecte generique) :
                    # actor_name reste NULL, et openalex.py le renseignera plus tard sur la
                    # meme ligne s'il retrouve la publication.
                    inserted, _ = upsert_document(
                        db,
                        document_type="publication",
                        title=item["title"],
                        source_url=item["url"],
                        fingerprint_source=item["doi"] or item["url"],
                        published_at=item["published"],
                        doi=item["doi"],
                        abstract=item["abstract"],
                    )
                    added += inserted
                    # §4.C.2 audit veille (Lot 3 §3.5) : appelé pour chaque document, nouveau ou
                    # déjà connu -- idempotent (fact_key dédoublonne), donc sans coût à reclasser
                    # un document déjà vu qui n'avait pas encore de signal.
                    technology_signals_added += upsert_document_technology_signal(
                        db, item["title"], item["abstract"], item["url"],
                    )
                except Exception as exc:
                    errors += 1
                    messages.append(f"{item.get('title', '?')[:60]}: {str(exc)[:140]}")

        status = "completed" if not errors or relevant else "failed"
        message = (
            f"Crossref: {len(TECHNOLOGY_QUERIES)} requêtes, fenêtre {effective_lookback} j, "
            f"{relevant} documents pertinents"
        )
        if messages:
            message += f"; {errors} requête(s) en erreur"
    except Exception as exc:
        errors += 1
        status = "failed"
        message = str(exc)[:300]

    with connect(TECH_DB) as db:
        db.execute(
            """UPDATE collection_runs
               SET finished_at=?,status=?,scanned=?,added=?,errors=?,message=?
               WHERE id=?""",
            (utc_now(), status, scanned, added, errors, message, run_id),
        )
    return {
        "scanned": scanned,
        "relevant": relevant,
        "added": added,
        "technology_signals_added": technology_signals_added,
        "errors": errors,
        "queries": len(TECHNOLOGY_QUERIES),
        "lookback_days": effective_lookback,
    }

