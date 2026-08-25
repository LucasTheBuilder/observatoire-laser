from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from db import ACTORS_DB, BUCKET_RANK, MARKET_DB, TECH_DB, application_key, connect, market_fact_key, offer_fact_key, utc_now
from hybrid import (
    AnthropicClient,
    ContentBlock,
    OllamaClient,
    ParsedDocument,
    block_payload,
    build_profile,
    canonical_url,
    classify_source,
    get_ai_client,
    normalize_page_type,
    parse_document,
    profile_json,
)
from site_profiles import SITE_OVERRIDES, crawl_budget, get_site_profile, seed_urls

AiClient = OllamaClient | AnthropicClient

CRAWLER_CONTACT = os.getenv("CRAWLER_CONTACT", "").strip()
USER_AGENT = "ObservatoireLaser/3.4.1-optimized (+local-relation deterministic crawler" + (
    f"; contact: {CRAWLER_CONTACT})" if CRAWLER_CONTACT else ")"
)
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = httpx.Timeout(18.0, connect=8.0)

# Politeness: a fixed per-host delay plus bounded retries on transient failures. Both are
# deliberately conservative defaults for small industrial/institutional sites that are not
# built to absorb bursty traffic; override via env vars if a faster/slower pace is needed.
CRAWL_DELAY_SECONDS = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
CRAWL_MAX_RETRIES = int(os.getenv("CRAWL_MAX_RETRIES", "2"))
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
ROBOTS_CACHE_TTL_SECONDS = 6 * 3600

_robots_cache: dict[str, tuple[float, "robotparser.RobotFileParser | None"]] = {}
_last_request_at: dict[str, float] = {}


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


def _throttle(url: str) -> None:
    """Enforce a minimum delay between two requests to the same host."""
    if CRAWL_DELAY_SECONDS <= 0:
        return
    host = urlparse(url).netloc.lower()
    if not host:
        return
    now = time.monotonic()
    last = _last_request_at.get(host)
    if last is not None:
        remaining = CRAWL_DELAY_SECONDS - (now - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_request_at[host] = time.monotonic()


def _fetch(client: httpx.Client, url: str) -> httpx.Response:
    """GET one URL while respecting robots.txt, per-host throttling and transient-error retries."""
    if not _robots_allowed(client, url):
        raise PermissionError(f"robots.txt interdit: {url}")
    attempt = 0
    while True:
        _throttle(url)
        try:
            response = client.get(url)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt >= CRAWL_MAX_RETRIES:
                raise
            time.sleep(2 ** attempt)
            attempt += 1
            continue
        if response.status_code in RETRYABLE_STATUS_CODES and attempt < CRAWL_MAX_RETRIES:
            time.sleep(2 ** attempt)
            attempt += 1
            continue
        response.raise_for_status()
        return response

TECHNOLOGY_QUERIES = (
    "femtosecond laser micromachining",
    "ultrafast laser processing manufacturing",
    "femtosecond laser surface texturing",
    "selective laser etching femtosecond",
    "femtosecond laser glass microfabrication",
    "femtosecond laser semiconductor processing",
)

# A lexicon rule maps a few well-known keys (any_of/all_of/regex/requires_any/exclude) to
# tuples of terms/patterns. Annotating the lexicons below lets mypy check every call site
# that takes a lexicon (_rule_match_terms, _match_label, _match_all_labels, ...) instead of
# widening them all to plain dicts.
LexiconRule = dict[str, tuple[str, ...]]
Lexicon = dict[str, LexiconRule]

LASER_RULES: Lexicon = {
    "femtosecond": {"any_of": ("femtosecond", "femtoseconde")},
    "fs laser": {"regex": (r"\bfs[ -]?laser\b",)},
    "ultrafast": {"any_of": ("ultrafast",)},
    "ultrashort pulse": {"any_of": ("ultra-short pulse", "ultrashort pulse", "ultrashort-pulse", "ultra short pulse", "ultrashort")},
    "USP laser": {"regex": (r"\busp(?:[ -]?laser)?\b",), "requires_any": ("laser", "pulse", "machining", "processing")},
    "UKP laser": {"regex": (r"\bukp(?:[ -]?laser)?\b",), "requires_any": ("laser", "pulse", "bearbeitung")},
    "Ultrakurzpulslaser": {"any_of": ("ultrakurzpulslaser", "ultrakurzpuls laser")},
}

MARKETS: Lexicon = {
    "Médical": {"any_of": ("medical", "medtech", "surgical", "healthcare", "biomedical")},
    "Batteries": {"any_of": ("battery", "batteries", "energy storage", "battery cell")},
    "Optique": {"any_of": ("optical", "optique", "lens", "lenses", "optics")},
    "Semi-conducteurs": {"any_of": ("semiconductor", "semi-conducteur", "microelectronics", "microélectronique")},
    "Aéronautique": {"any_of": ("aeronautic", "aeronautical", "aviation", "aircraft", "aerospace")},
    "Spatial": {"any_of": ("spacecraft", "satellite", "space propulsion", "space industry", "spatial")},
    "Défense": {"any_of": ("defence", "defense", "military", "défense")},
    "Automobile": {"any_of": ("automotive", "automobile", "e-mobility", "electric vehicle")},
    "Luxe": {"any_of": ("luxury", "luxe", "horlogerie", "watchmaking")},
    "Quantum": {"any_of": ("quantum", "ion trap", "ion traps", "quantum computing", "quantum sensing", "quantum cryptography")},
    "Photonique": {"any_of": ("photonic", "photonics", "photonique")},
    "Sciences de la vie": {"any_of": ("life sciences", "drug discovery", "cell therapy", "cell therapies", "antibody isolation", "single-cell analysis", "single cell analysis", "biophotonics")},
    "Photovoltaïque": {"any_of": ("photovoltaic", "photovoltaics", "solar cell", "solar cells", "pv cell", "photovoltaïque")},
}

COMPONENTS: Lexicon = {
    "Composants en Nitinol pour cathéters": {"all_of": ("nitinol", "catheter")},
    "Lentilles intraoculaires (IOL)": {"any_of": ("intraocular lens", "intraocular lenses"), "regex": (r"\biol\b",)},
    "Stents": {"any_of": ("stent",)},
    "Cathéters": {"any_of": ("catheter", "cathéter")},
    "Guidewires": {"any_of": ("guidewire", "guide wire")},
    "Aiguilles médicales": {"any_of": ("medical needle", "surgical needle", "needle")},
    "Implants": {"any_of": ("implant",)},
    "Électrodes de batteries": {"any_of": ("battery electrode", "electrode", "électrode")},
    "Collecteurs de courant": {"any_of": ("current collector", "battery foil", "electrode foil", "busbar", "battery tab")},
    "Wafers": {"any_of": ("semiconductor wafer", "silicon wafer", "glass wafer", "wafer")},
    "Interposeurs en verre": {"any_of": ("glass interposer", "glass interposer substrate")},
    "Substrats": {"any_of": ("glass substrate", "ceramic substrate", "silicon substrate", "substrate")},
    "Packaging avancé": {"any_of": ("advanced packaging", "semiconductor package", "chip package")},
    "MEMS": {"regex": (r"\bmems\b",)},
    "MicroLED": {"any_of": ("microled", "micro-led")},
    "PCB": {"any_of": ("printed circuit board",), "regex": (r"\bpcb\b",)},
    "Microcanaux": {"any_of": ("microchannel", "micro-channel", "microcanal")},
    "Dispositifs microfluidiques": {"any_of": ("microfluidic device", "microfluidic chip", "lab-on-chip", "lab on chip")},
    "Composants en verre": {"any_of": ("glass component", "composant en verre", "microstructured glass", "fused silica", "borosilicate glass")},
    "Fibres optiques": {"any_of": ("optical fiber", "optical fibre")},
    "Guides d'onde": {"any_of": ("waveguide", "wave guide")},
    "Buses": {"any_of": ("nozzle", "buse")},
    "Injecteurs": {"any_of": ("injector", "injecteur")},
    "Aubes / composants turbine": {"any_of": ("turbine blade", "turbine component", "aube")},
    "Capteurs": {"any_of": ("sensor", "capteur")},
    "Pièges à ions": {"any_of": ("ion trap", "ion traps", "piège à ions", "pièges à ions")},
}

OPERATIONS: Lexicon = {
    "Micro-usinage": {"any_of": ("micromachining", "micro-machining", "micro machining")},
    "Microdécoupe": {"any_of": ("microcutting", "micro-cutting", "laser cutting", "microdécoupe", "découpe laser", "tube cutting")},
    "Microperçage": {"any_of": ("microdrilling", "micro-drilling", "laser drilling", "microperçage", "perçage laser")},
    "Texturation": {"any_of": ("texturing", "surface texturing", "texturation", "surface structuring", "structuration")},
    "Fonctionnalisation de surface": {"any_of": ("surface functionalization", "surface functionalisation", "functional surface", "functionalized surface", "functionalised surface")},
    "Ablation": {"any_of": ("ablation", "selective ablation")},
    "Soudage": {"any_of": ("welding", "soudage", "micro-welding", "microwelding")},
    "Gravure": {"any_of": ("engraving", "gravure")},
    "Scribing": {"any_of": ("laser scribing", "scribing")},
    "Dicing": {"any_of": ("laser dicing", "stealth dicing", "dicing")},
    "Nettoyage": {"any_of": ("laser cleaning", "nettoyage laser")},
    "Polissage": {"any_of": ("laser polishing", "polishing")},
    "Modification interne": {"any_of": ("internal modification", "in-volume modification", "volume modification", "bulk modification")},
    "Écriture de guide d'onde": {"any_of": ("waveguide writing", "direct laser writing of waveguide")},
    "Debonding": {"any_of": ("laser debonding", "debonding")},
    "Rainurage": {"any_of": ("grooving", "laser grooving")},
    "Milling": {"any_of": ("laser milling", "micromilling", "micro-milling")},
    "Fabrication additive": {"any_of": ("additive manufacturing", "laser additive manufacturing", "directed energy deposition", "fabrication additive", "metal 3d printing")},
    "Tournage laser": {"any_of": ("laser turning", "tournage laser")},
}

PROCESS_TECHNOLOGIES: Lexicon = {
    "SLE": {"any_of": ("selective laser etching", "selective laser-induced etching", "selective laser induced etching", "laser assisted etching", "laser-assisted etching", "isle process"), "regex": (r"\bSLE\b",), "requires_any": ("laser", "etching", "glass", "silica")},
    "LIPSS": {"any_of": ("laser-induced periodic surface structures", "laser induced periodic surface structures"), "regex": (r"\bLIPSS\b",)},
    "LSFL": {"regex": (r"\bLSFL\b",)},
    "HSFL": {"regex": (r"\bHSFL\b",)},
}

APPLICATION_ARCHITECTURES: Lexicon = {
    "TGV": {"any_of": ("through glass via", "through-glass via", "through glass vias", "through-glass vias"), "regex": (r"\bTGVs?\b",), "requires_any": ("glass", "via", "interposer", "semiconductor", "packaging")},
}

MATERIALS: Lexicon = {
    "Verre": {"any_of": ("glass", "fused silica", "borosilicate", "quartz glass", "verre")},
    "Saphir": {"any_of": ("sapphire", "saphir")},
    "Silicium": {"any_of": ("silicon", "silicium")},
    "Nitinol": {"any_of": ("nitinol", "ni-ti", "niti")},
    "Céramique": {"any_of": ("ceramic", "alumina", "zirconia", "céramique", "céramiques")},
    "Polymère": {"any_of": ("polymer", "polymeric", "peek", "polyimide", "polymère", "polymères")},
    "Métal": {"any_of": ("stainless steel", "titanium", "aluminium", "aluminum", "copper", "nickel", "titane", "acier inoxydable", "cuivre", "métaux")},
    "Composite": {"any_of": ("composite", "cfrp", "carbon fiber reinforced polymer", "cmc", "ceramic matrix composite")},
    "Magnésium": {"any_of": ("magnesium", "magnésium")},
}

PERFORMANCE_TERMS: Lexicon = {
    "Productivité": {"any_of": ("high throughput", "throughput", "high-speed processing", "high speed processing", "large-area processing", "large area processing")},
    "Parallélisation": {"any_of": ("parallel processing", "multibeam", "multi-beam", "beam splitting", "diffractive optical element", "polygon scanner")},
    "Haute puissance": {"any_of": ("high average power", "high-power ultrafast", "high power ultrafast", "high repetition rate", "mhz processing")},
}


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

# Explicit negation/contrast markers. A sentence or window carrying one of these cannot
# establish a positive relation even when it lexically contains market+component+operation
# terms (e.g. "unlike laser cutting, we use..." or "n'offre pas de découpe laser pour...").
# Deliberately excludes ambiguous cues such as "without"/"sans": those are routinely used
# descriptively in this domain ("contactless cutting" / "découpe sans contact") rather than
# to negate the claim, and a false rejection there would just widen the recall gap further.
NEGATION_CUES = (
    "unlike", "contrairement à", "contrairement a",
    "rather than", "instead of", "plutôt que", "plutot que", "au lieu de",
    "no longer", "not yet", "not currently",
    "does not", "do not", "did not", "cannot", "can not", "will not",
    "doesn't", "don't", "didn't", "isn't", "aren't", "wasn't", "weren't",
    "won't", "can't", "couldn't", "wouldn't", "shouldn't",
    "ne propose pas", "n'offre pas", "ne fait pas", "ne fabrique pas", "ne fournit pas",
    "n'est pas encore", "ne sont pas encore",
)

# Conservative market inference used only for display when the market is not explicit.
# Inferred values never count as an independent acceptance signal.
MARKET_INFERENCE = {
    "Médical": {"components": {"Stents", "Cathéters", "Guidewires", "Aiguilles médicales", "Lentilles intraoculaires (IOL)", "Composants en Nitinol pour cathéters"}},
    "Batteries": {"components": {"Électrodes de batteries", "Collecteurs de courant"}},
    "Semi-conducteurs": {"components": {"Wafers", "Interposeurs en verre", "Packaging avancé", "MEMS", "MicroLED", "PCB"}, "architectures": {"TGV"}},
}

MATURITY_RULES = (
    ("Production", "existing", ("mass production", "volume production", "series production", "serial production", "production industrielle", "production en série", "production line", "manufacturing line", "high-volume manufacturing", "commercial production", "customer production", "contract manufacturing", "job shop", "manufacturing services", "small series", "small batch", "lohnfertigung", "auftragsfertigung", "lavorazione conto terzi", "conto terzi", "fabricación por contrato", "fabricacion por contrato", "subcontratación", "subcontratacion")),
    ("Industrialisation", "radar", ("industrialization", "industrialisation", "industrial implementation", "industrialiser", "to industrialize", "scale-up", "scaling-up", "production-ready", "manufacturing integration")),
    ("Pré-industrialisation", "radar", ("pilot line", "ligne pilote", "pilot production", "pre-series", "présérie", "pre-production", "qualification", "process qualification", "production trial")),
    ("Prototype", "radar", ("prototype", "prototyping", "demonstrator", "technology demonstrator")),
    ("R&D", "radar", ("proof of concept", "feasibility study", "process development", "research project", "development program", "project aims", "projet vise", "collaborative project")),
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


def _normalize_text(text: str) -> str:
    """Normalize Unicode, punctuation variants and whitespace without losing semantics."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00ad", "").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


MORPHOLOGY_VARIANTS = {
    "stent": ("stents",),
    "catheter": ("catheters", "cathéter", "cathéters"),
    "guidewire": ("guidewires",),
    "guide wire": ("guide wires",),
    "needle": ("needles",),
    "medical needle": ("medical needles",),
    "surgical needle": ("surgical needles",),
    "implant": ("implants",),
    "electrode": ("electrodes", "électrode", "électrodes"),
    "battery electrode": ("battery electrodes",),
    "current collector": ("current collectors",),
    "battery foil": ("battery foils",),
    "electrode foil": ("electrode foils",),
    "busbar": ("busbars",),
    "battery tab": ("battery tabs",),
    "wafer": ("wafers",),
    "semiconductor wafer": ("semiconductor wafers",),
    "silicon wafer": ("silicon wafers",),
    "glass wafer": ("glass wafers",),
    "substrate": ("substrates",),
    "glass substrate": ("glass substrates",),
    "ceramic substrate": ("ceramic substrates",),
    "silicon substrate": ("silicon substrates",),
    "microchannel": ("microchannels",),
    "micro-channel": ("micro-channels",),
    "glass component": ("glass components",),
    "optical fiber": ("optical fibers",),
    "optical fibre": ("optical fibres",),
    "waveguide": ("waveguides",),
    "wave guide": ("wave guides",),
    "nozzle": ("nozzles",),
    "injector": ("injectors",),
    "turbine blade": ("turbine blades",),
    "turbine component": ("turbine components",),
    "sensor": ("sensors",),
    "capteur": ("capteurs",),
}

@lru_cache(maxsize=1024)
def _term_variants(term: str) -> tuple[str, ...]:
    norm = _normalize_text(term)
    variants = MORPHOLOGY_VARIANTS.get(norm, ())
    return tuple(dict.fromkeys((term, *variants)))


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile a safe lexical pattern.

    Single alphanumeric terms use word boundaries; multi-word/hyphenated expressions
    allow flexible spaces/hyphens. This avoids substring matches such as 'sle' inside
    unrelated words while preserving industrial spelling variants.
    """
    norm = _normalize_text(term)
    pieces = [re.escape(p) for p in re.split(r"[\s-]+", norm) if p]
    if not pieces:
        return re.compile(r"a^")
    body = r"[\s-]+".join(pieces)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _contains_term_normalized(norm_text: str, term: str) -> bool:
    """Match one lexical term against text that has already been normalized."""
    return any(bool(_term_pattern(variant).search(norm_text)) for variant in _term_variants(term))


def _contains_term(text: str, term: str) -> bool:
    return _contains_term_normalized(_normalize_text(text), term)


def _rule_match_terms(text: str, rule: LexiconRule) -> list[str]:
    """Return only the actual lexical evidence supporting a complete rule."""
    norm = _normalize_text(text)
    excludes = rule.get("exclude", ())
    if excludes and any(_contains_term_normalized(norm, term) for term in excludes):
        return []

    all_of = rule.get("all_of", ())
    if all_of and not all(_contains_term_normalized(norm, term) for term in all_of):
        return []

    hits: list[str] = []
    if all_of:
        hits.extend(term for term in all_of if _contains_term_normalized(norm, term))
    for term in rule.get("any_of", ()):
        if _contains_term_normalized(norm, term):
            hits.append(term)
    for pattern in rule.get("regex", ()):
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            hits.append(match.group(0))

    if not hits:
        return []
    requires_any = rule.get("requires_any", ())
    if requires_any and not any(_contains_term_normalized(norm, term) for term in requires_any):
        return []
    return list(dict.fromkeys(hits))


def _rule_matches(text: str, rule: LexiconRule) -> bool:
    return bool(_rule_match_terms(text, rule))


def _specificity_score(rule: LexiconRule, hits: list[str]) -> tuple[int, int, int]:
    """Favor explicit multi-term rules and longer evidence over generic labels."""
    all_bonus = 3 if rule.get("all_of") else 0
    regex_bonus = 1 if rule.get("regex") else 0
    lexical_weight = sum(len(_normalize_text(hit)) for hit in hits)
    return (all_bonus + regex_bonus + len(hits), lexical_weight, len(rule.get("all_of", ())))


def _match_label_details(text: str, lexicon: Lexicon) -> tuple[str | None, list[str]]:
    matches: list[tuple[tuple[int, int, int], str, list[str]]] = []
    for label, rule in lexicon.items():
        hits = _rule_match_terms(text, rule)
        if hits:
            matches.append((_specificity_score(rule, hits), label, hits))
    if not matches:
        return None, []
    matches.sort(key=lambda item: item[0], reverse=True)
    _, label, hits = matches[0]
    return label, hits


def _match_label(text: str, lexicon: Lexicon) -> str | None:
    return _match_label_details(text, lexicon)[0]


def _match_all_labels(text: str, lexicon: Lexicon) -> list[tuple[str, list[str]]]:
    """Return all matching canonical labels, ordered by specificity, not just the first one."""
    matches: list[tuple[tuple[int, int, int], str, list[str]]] = []
    for label, rule in lexicon.items():
        hits = _rule_match_terms(text, rule)
        if hits:
            matches.append((_specificity_score(rule, hits), label, hits))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [(label, hits) for _, label, hits in matches]


def _matching_terms(text: str, lexicon: Lexicon) -> list[str]:
    hits: list[str] = []
    for rule in lexicon.values():
        hits.extend(_rule_match_terms(text, rule))
    return list(dict.fromkeys(hits))


def _laser_match(text: str) -> bool:
    return any(_rule_matches(text, rule) for rule in LASER_RULES.values())


def _detect_maturity(text: str) -> tuple[str, str]:
    """Return the most mature explicit stage, using boundary-safe matching."""
    for stage, bucket, terms in MATURITY_RULES:
        if any(_contains_term(text, term) for term in terms):
            return bucket, stage
    return "unknown", "Maturité industrielle non déterminée"


def _quote(text: str, terms: tuple[str, ...] | list[str]) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    def score(sentence: str) -> tuple[int, int]:
        hits = sum(_contains_term(sentence, term) for term in terms)
        return hits, min(len(sentence), 700)
    ranked = sorted((s.strip() for s in sentences if s.strip()), key=score, reverse=True)
    return (ranked[0] if ranked else text)[:700]


def _path_parts(path: str | None) -> tuple[str, ...]:
    if not path:
        return ()
    return tuple(part.strip() for part in re.split(r"\s*>\s*|/|\\", path) if part.strip())


def _is_ancestor_path(candidate: str | None, current: str | None) -> bool:
    cand = _path_parts(candidate)
    cur = _path_parts(current)
    return bool(cand and cur and len(cand) < len(cur) and cur[:len(cand)] == cand)


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
    return any(_contains_term(text, cue) for cue in NEGATION_CUES)


def _section_role(block: ContentBlock) -> str:
    """Describe the local editorial zone without excluding it from market evidence."""
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
    return (
        _match_label_details(text, MARKETS)[0],
        _match_label_details(text, COMPONENTS)[0],
        _match_label_details(text, OPERATIONS)[0],
    )


def _editorial_group_key(block: ContentBlock) -> str:
    """Return the strict micro-context identifier for relation sharing."""
    return block.editorial_group_id


def _is_isolated_editorial_item(block: ContentBlock) -> bool:
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
    if diagnostics is not None:
        diagnostics[key] = int(diagnostics.get(key, 0)) + amount


def _relation_evidence(
    block: ContentBlock,
    structured_blocks: list[ContentBlock] | None = None,
    diagnostics: dict[str, int] | None = None,
) -> tuple[str | None, str]:
    """Validate Market ↔ Component ↔ Operation without cross-context recombination.

    ``direct`` is sentence-local; ``contextual`` uses only the block's immediate heading or a
    two-sentence window; ``structured`` may cross blocks only when they share the exact
    ``editorial_group_id``. Repeated cards/list items are isolated micro-contexts. A window
    carrying an explicit negation/contrast cue (see NEGATION_CUES) is skipped even when it
    would otherwise satisfy the lexical relation, since the sentence is denying or contrasting
    the claim rather than making it (e.g. "unlike laser cutting, we use...").
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
            else:
                return "structured", combined[:1400]

    return None, ""

def _laser_context(title: str, block: ContentBlock, section_context: str | None = None) -> str:
    section = section_context or " ".join(filter(None, (block.section_context, block.text, block.media_context)))
    return " ".join(dict.fromkeys(filter(None, (title, section))))


def _independent_core_count(details: dict[str, tuple[str | None, list[str]]]) -> int:
    used: set[str] = set()
    count = 0
    for key in ("operation", "process", "component", "architecture", "market"):
        label, hits = details[key]
        if not label:
            continue
        normalized = {_normalize_text(hit) for hit in hits if hit}
        if normalized and normalized.issubset(used):
            continue
        used.update(normalized)
        count += 1
    return count


def _infer_market(component: str | None, architecture: str | None) -> str | None:
    for market, rules in MARKET_INFERENCE.items():
        if component and component in rules.get("components", set()):
            return market
        if architecture and architecture in rules.get("architectures", set()):
            return market
    return None


def _is_noise_block(block: ContentBlock) -> bool:
    direct = " ".join(filter(None, (block.heading, block.text, block.media_context)))
    norm = _normalize_text(direct)
    if len(norm) < 18:
        return True
    noise_hits = sum(_contains_term(norm, term) for term in NAVIGATION_NOISE)
    return noise_hits >= 2


def _candidate(actor_name: str, url: str, title: str, block: ContentBlock, mode: str = "block-rules",
               context_text: str | None = None, structured_blocks: list[ContentBlock] | None = None,
               diagnostics: dict[str, int] | None = None) -> dict | None:
    """Create a market application only when the three core dimensions form a local relation."""
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

    relation_strength, relation_text = _relation_evidence(block, safe_neighbors, diagnostics)
    if not relation_strength:
        if _is_multi_context_block(block):
            _inc_diagnostic(diagnostics, "multi_context_rejected")
        _inc_diagnostic(diagnostics, "relation_too_weak")
        return None

    # Core dimensions are resolved from the validated relation window, not from the whole page.
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

    # Complementary dimensions may use the local section, but they can never substitute a core one.
    process, _ = _match_label_details(section, PROCESS_TECHNOLOGIES)
    architecture, _ = _match_label_details(section, APPLICATION_ARCHITECTURES)
    material, _ = _match_label_details(section, MATERIALS)
    performance, _ = _match_label_details(section, PERFORMANCE_TERMS)

    maturity_class, maturity = _detect_maturity(relation_text + " " + section)
    bucket = "existing" if maturity_class == "existing" else "radar"
    if maturity_class == "unknown":
        _inc_diagnostic(diagnostics, "maturity_unknown")
    direct_laser = _laser_match(relation_text)
    confidence = 0.78 if relation_strength == "direct" else (0.74 if relation_strength == "structured" else 0.70)
    confidence += 0.05 if direct_laser else 0.0
    confidence += 0.02 * sum(value is not None for value in (process, architecture, material, performance))
    if maturity_class != "unknown":
        confidence += 0.04
    confidence = max(0.70, min(0.98, confidence))

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
    stage_parts = [maturity]
    if process:
        stage_parts.append(f"Procédé: {process}")
    if architecture:
        stage_parts.append(f"Architecture: {architecture}")
    if material:
        stage_parts.append(f"Matériau: {material}")
    if performance:
        stage_parts.append(f"Performance: {performance}")

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
        "fact_status": "validated",
        "stage": " | ".join(stage_parts)[:240],
        "url": url,
        "title": title,
        "quote": quote,
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
                      mode: str = "block-rules") -> list[dict]:
    """Extract competitor offer/capability facts without inventing a market or component."""
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
    text = str(value or "").strip()
    if not text or text == "Non identifié":
        return "Non identifié"
    return text if text in allowed else "Non identifié"


def _ai_mode_label(ollama: AiClient) -> str:
    provider = "anthropic" if isinstance(ollama, AnthropicClient) else "ollama"
    return f"{provider}:{ollama.model}"


# Observed in real Claude Haiku output: when the model genuinely can't identify a dimension
# (e.g. a publication snippet about a process with no clear physical component), it sometimes
# answers with a placeholder like "<UNKNOWN>" instead of leaving the field blank. These are not
# real proposals -- queuing them would put dead entries in front of whoever reviews the queue.
_NON_PROPOSAL_SENTINELS = {
    "unknown", "<unknown>", "n a", "na", "none", "non identifie", "non applicable", "aucun", "aucune",
}


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
    diagnostics: dict[str, int] | None = None,
) -> list[dict]:
    """Conservative AI fallback for market applications, with an open-vocabulary escape hatch.

    The AI may resolve wording, but it cannot invent or substitute Market / Component /
    Operation on the strict path: when its answer for all three normalizes to a known label,
    that label must also be independently supported by the deterministic local-section
    lexicon, exactly as before. When at least one of the three doesn't match any known label,
    the fact isn't discarded outright -- it's queued in ``vocabulary_candidates`` for human
    review instead, carrying the model's actual proposed wording (not just "unresolved").
    Complementary dimensions (process/architecture/material/performance/maturity) stay
    restricted to the known lexicons in both cases.
    """
    relevant: list[tuple[int, ContentBlock, str]] = []
    for i, block in enumerate(blocks):
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
    _inc_diagnostic(diagnostics, "ai_facts_proposed", len(facts))

    block_by_index = {i: (block, section) for i, block, section in relevant}
    complementary = {
        "process_technology": set(PROCESS_TECHNOLOGIES), "application_architecture": set(APPLICATION_ARCHITECTURES),
        "material": set(MATERIALS), "performance": set(PERFORMANCE_TERMS),
    }
    maturity_to_bucket = {stage: bucket for stage, bucket, _ in MATURITY_RULES}
    mode = _ai_mode_label(ollama)
    candidates: list[dict] = []

    for fact in facts[:20]:
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
        if confidence < 0.72:
            _inc_diagnostic(diagnostics, "ai_fact_low_confidence")
            continue

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

        # Known-label path: the AI's wording resolving to a known label is not enough on its
        # own -- that label must also be independently present via the deterministic
        # local-section lexicon, exactly as before this open-vocabulary escape hatch existed.
        deterministic_core = {
            "market": _match_label(section, MARKETS),
            "component": _match_label(section, COMPONENTS),
            "operation": _match_label(section, OPERATIONS),
        }
        if any(deterministic_core[key] != resolved[key] for key in resolved):
            _inc_diagnostic(diagnostics, "ai_known_label_not_independently_confirmed")
            continue
        _inc_diagnostic(diagnostics, "ai_fact_validated")

        maturity = _validate_ai_value(fact.get("maturity"), set(maturity_to_bucket))
        maturity_class = maturity_to_bucket.get(maturity, "unknown")
        bucket = "existing" if maturity_class == "existing" else "radar"
        market, component, operation = resolved["market"], resolved["component"], resolved["operation"]
        assert market is not None and component is not None and operation is not None  # guaranteed by all(resolved.values()) above
        fact_key = market_fact_key(actor_name, bucket, market, component, operation)
        app_key = application_key(actor_name, market, component, operation)
        extras = []
        for key, label in (("process_technology", "Procédé"), ("application_architecture", "Architecture"), ("material", "Matériau"), ("performance", "Performance")):
            if fields[key] != "Non identifié":
                extras.append(f"{label}: {fields[key]}")
        stage = str(fact.get("stage", maturity if maturity != "Non identifié" else "Maturité à confirmer")).strip()
        if extras:
            stage = f"{stage} | {' | '.join(extras)}"

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


def adaptive_decision(priority: bool, document_count: int, block_count: int, errors: int, source_count: int) -> tuple[str, bool]:
    anomaly = document_count == 0 or block_count == 0 or errors >= max(1, source_count // 2)
    return ("adaptive" if priority or anomaly else "generic", anomaly)


def _push_crawl_item(
    heap: list[tuple[int, int, int, dict]],
    queued_scores: dict[str, int],
    counter: itertools.count,
    item: dict,
) -> None:
    key = canonical_url(item["url"])
    score = int(item.get("score", 0))
    if score <= queued_scores.get(key, -1):
        return
    queued_scores[key] = score
    heapq.heappush(heap, (-score, int(item.get("depth", 0)), next(counter), item))


def _effective_crawl_score(item: dict, visited_by_type: dict[str, int], profile: dict) -> int:
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


def scrape_actors(max_pages_per_actor: int | None = None) -> dict:
    """Deterministic dynamic crawler with per-site profiles and a priority queue."""
    with connect(ACTORS_DB) as db:
        run_id = db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (utc_now(), "running")).lastrowid
        actors = db.execute("SELECT * FROM actors WHERE active=1 ORDER BY priority DESC,name").fetchall()

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
                        """INSERT INTO actor_sources(actor_id,url,source_kind,page_type,source_score,discovery_depth,discovery_reason)
                           VALUES(?,?,?,?,?,0,'profile-seed')
                           ON CONFLICT(url) DO UPDATE SET actor_id=excluded.actor_id,active=1,
                               source_score=MAX(actor_sources.source_score,excluded.source_score),
                               page_type=CASE WHEN actor_sources.page_type IS NULL OR actor_sources.page_type='' THEN excluded.page_type ELSE actor_sources.page_type END""",
                        (actor["id"], seed_url, source_kind, seed_type, seed_score),
                    )
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
                    response = _fetch(client, url)
                    resolved_url = str(response.url)
                    document = parse_document(response.text, resolved_url, profile=site_profile)
                    documents.append((resolved_url, document))
                    digest = hashlib.sha256("|".join(block.fingerprint for block in document.blocks).encode()).hexdigest()

                    with connect(ACTORS_DB) as db:
                        previous = db.execute("SELECT content_hash FROM actor_sources WHERE id=?", (source_id,)).fetchone()
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

                    with connect(ACTORS_DB) as db:
                        db.execute(
                            """UPDATE actor_sources SET content_hash=?,last_http_status=?,last_checked_at=?,last_title=?,
                                      page_type=?,source_score=?,extraction_mode=?,structure_hash=?,blocks_json=?,last_error=NULL,
                                      ambiguous=?,discovery_depth=?,parent_url=COALESCE(parent_url,?),discovery_reason=COALESCE(discovery_reason,?),
                                      last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END WHERE id=?""",
                            (
                                digest, response.status_code, stamp, document.title, fetched_type, fetched_score,
                                document.extraction_method, document.structure_hash, blocks_json, int(not document.blocks),
                                depth, item.get("parent_url"), item.get("reason"), int(is_changed), stamp, source_id,
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
                                           discovery_reason,parent_url,active
                                       ) VALUES(?,?,?,?,?,?,?,?,?,1)
                                       ON CONFLICT(url) DO UPDATE SET
                                           page_type=excluded.page_type,
                                           source_score=MAX(actor_sources.source_score, excluded.source_score),
                                           discovery_depth=excluded.discovery_depth,
                                           discovery_context=excluded.discovery_context,
                                           discovery_reason=excluded.discovery_reason,
                                           parent_url=excluded.parent_url,
                                           active=1""",
                                    (
                                        actor["id"], link["url"], "discovered", link["category"], int(link["score"]),
                                        link_depth, link["context"], link.get("reason"), resolved_url,
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
    return {
        "scanned": scanned,
        "changed": changed,
        "errors": errors,
        "discovered": discovered,
        "profiled": profiled,
        "adaptive_fallbacks": fallback,
        "ollama": ollama.available(),
        "crawler": "deterministic-coverage-first-v2",
    }

def _source_family(page_type: str | None) -> str:
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


def _select_market_sources(max_pages: int = 120) -> list[dict]:
    """Coverage-balanced source selection: first one page/family/actor, then deepen priority actors."""
    with connect(ACTORS_DB) as db:
        rows = [dict(row) for row in db.execute(
            """SELECT a.id AS actor_id,a.name,a.priority,a.official_url,p.strategy,
                      s.id AS source_id,s.url,s.page_type,s.source_score,s.blocks_json,
                      s.last_title,s.last_checked_at,s.extraction_mode
               FROM actor_sources s
               JOIN actors a ON a.id=s.actor_id
               LEFT JOIN site_profiles p ON p.actor_id=a.id
               WHERE s.active=1 AND COALESCE(s.page_type,'')!='ignore'
                 AND (s.last_http_status IS NULL OR s.last_http_status BETWEEN 200 AND 399)
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
    """Reuse blocks produced by the actor crawl when they are recent and structurally valid."""
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


def _language_from_url(url: str) -> str | None:
    parts = [part.casefold() for part in urlparse(url).path.split("/") if part]
    if not parts:
        return None
    if parts[0] in {"fr", "fr-fr"}:
        return "fr"
    if parts[0] in {"en", "en-gb", "en-us"}:
        return "en"
    if parts[0] in {"de", "de-de"}:
        return "de"
    return None


def _ensure_market_fact_status_column(db) -> None:
    """Keep direct unit-test/maintenance connections compatible with additive schema changes."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)").fetchall()}
    if "fact_status" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN fact_status TEXT NOT NULL DEFAULT 'review'")
    if "last_seen_at" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN last_seen_at TEXT")
    if "application_key" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN application_key TEXT")
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
    row = db.execute("SELECT id,field_confidence,bucket FROM evidence WHERE application_key=?", (app_key,)).fetchone()
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
            db.execute(
                """UPDATE evidence SET industrial_stage=?,source_url=?,source_title=?,quote=?,field_confidence=?,
                          laser_process=COALESCE(?,laser_process),material=COALESCE(?,material),performance=COALESCE(?,performance),
                          maturity_level=COALESCE(?,maturity_level),relation_strength=COALESCE(?,relation_strength),source_role=COALESCE(?,source_role),
                          bucket=?,fact_status=?,review_status=?,updated_at=? WHERE id=?""",
                (
                    candidate["stage"], candidate["url"], candidate["title"], candidate["quote"], candidate["confidence"],
                    candidate.get("process"), candidate.get("material"), candidate.get("performance"), candidate.get("maturity"),
                    candidate.get("relation_strength"), candidate.get("source_role"), effective_bucket,
                    candidate.get("fact_status", "validated"), "accepted" if candidate.get("fact_status") == "validated" else "review",
                    stamp, evidence_id,
                ),
            )
    else:
        fact_status = candidate.get("fact_status", "validated")
        review_status = "accepted" if fact_status == "validated" else "review"
        evidence_id = db.execute(
            """INSERT INTO evidence(
                   actor_name,bucket,market,component,operation,industrial_stage,source_url,source_title,quote,source_group,
                   fingerprint,fact_key,application_key,evidence_kind,language,review_status,created_at,updated_at,block_heading,block_path,
                   extraction_mode,field_confidence,laser_process,material,performance,maturity_level,relation_strength,relation_evidence,source_role,fact_status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'market_application',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate["actor"], candidate["bucket"], candidate["market"], candidate["component"], candidate["operation"],
                candidate["stage"], candidate["url"], candidate["title"], candidate["quote"], fact_key,
                candidate["fingerprint"], fact_key, app_key, _language_from_url(candidate["url"]), review_status, stamp, stamp,
                candidate["block_heading"], candidate["block_path"], candidate["mode"], candidate["confidence"],
                candidate.get("process"), candidate.get("material"), candidate.get("performance"), candidate.get("maturity"),
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

    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO evidence_sources(
               evidence_id,source_url,source_title,quote,language,block_heading,block_path,extraction_mode,field_confidence,relation_strength,relation_evidence,source_role,
               fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence_id, candidate["url"], candidate["title"], candidate["quote"], _language_from_url(candidate["url"]),
            candidate["block_heading"], candidate["block_path"], candidate["mode"], candidate["confidence"],
            candidate.get("relation_strength"), candidate.get("relation_evidence"), candidate.get("source_role"),
            candidate["source_fingerprint"], stamp,
        ),
    )
    source_added = int(db.total_changes > before)
    return fact_added, source_added


def _upsert_offer_candidate(db, candidate: dict) -> tuple[int, int]:
    stamp = utc_now()
    row = db.execute("SELECT id,field_confidence FROM offers WHERE fact_key=?", (candidate["fact_key"],)).fetchone()
    fact_added = 0
    if row:
        offer_id = int(row["id"])
        if float(candidate.get("confidence", 0)) > float(row["field_confidence"] or 0):
            db.execute(
                """UPDATE offers SET industrial_stage=?,source_url=?,source_title=?,quote=?,field_confidence=?,
                          material=COALESCE(?,material),performance=COALESCE(?,performance),updated_at=? WHERE id=?""",
                (
                    candidate["stage"], candidate["url"], candidate["title"], candidate["quote"], candidate["confidence"],
                    candidate.get("material"), candidate.get("performance"), stamp, offer_id,
                ),
            )
    else:
        offer_id = db.execute(
            """INSERT INTO offers(
                   actor_name,offer_type,capability,operation,laser_process,material,performance,industrial_stage,page_type,
                   source_url,source_title,quote,fact_key,fingerprint,review_status,field_confidence,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?)""",
            (
                candidate["actor"], candidate["offer_type"], candidate["capability"], candidate.get("operation"), candidate.get("process"),
                candidate.get("material"), candidate.get("performance"), candidate["stage"], candidate["page_type"], candidate["url"],
                candidate["title"], candidate["quote"], candidate["fact_key"], candidate["fingerprint"], candidate["confidence"], stamp, stamp,
            ),
        ).lastrowid
        fact_added = 1

    db.execute("UPDATE offers SET last_seen_at=? WHERE id=?", (stamp, offer_id))

    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO offer_sources(
               offer_id,source_url,source_title,quote,language,block_heading,block_path,extraction_mode,field_confidence,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            offer_id, candidate["url"], candidate["title"], candidate["quote"], _language_from_url(candidate["url"]),
            candidate["block_heading"], candidate["block_path"], candidate["mode"], candidate["confidence"], candidate["source_fingerprint"], stamp,
        ),
    )
    source_added = int(db.total_changes > before)
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


def scrape_market(max_pages: int = 120, actor_names: list[str] | None = None) -> dict:
    """Run the market extraction pass.

    ``actor_names``, when given, restricts the run to those actors (matched against
    ``source["name"]``) -- meant for trying a prompt/provider change on 2-3 actors before
    opening it to the full roster, without touching the selection/coverage logic itself.
    """
    _load_custom_lexicon_entries()
    with connect(MARKET_DB) as db:
        run_id = db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (utc_now(), "running")).lastrowid
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
                    diagnostics["stored_pages_reused"] += 1
                else:
                    response = _fetch(client, source["url"])
                    site_profile = get_site_profile({"name": source["name"], "official_url": source["official_url"]})
                    document = parse_document(response.text, str(response.url), profile=site_profile)
                    blocks = document.blocks
                    source_url = str(response.url)
                    source_title = document.title
                    page_type = document.page_type
                    diagnostics["network_pages_fetched"] += 1

                market_candidates: list[dict] = []
                offer_candidates: list[dict] = []

                for index, block in enumerate(blocks):
                    _, section = _context_for_block(source_title, blocks, index)
                    structured = _structured_neighbors(blocks, index)
                    candidate = _candidate(
                        source["name"], source_url, source_title, block, context_text=section,
                        structured_blocks=structured, diagnostics=diagnostics,
                    )
                    if candidate:
                        market_candidates.append(candidate)
                    offer_candidates.extend(
                        _offer_candidates(source["name"], source_url, source_title, block, page_type=page_type)
                    )

                # AI remains a conservative secondary route for complete market facts only.
                vocabulary_candidates: list[dict] = []
                if source.get("strategy") == "adaptive":
                    for candidate in _ai_candidates(source["name"], source_url, source_title, blocks, ollama, diagnostics):
                        if candidate.get("kind") == "vocabulary_candidate":
                            vocabulary_candidates.append(candidate)
                        else:
                            market_candidates.append(candidate)

                market_candidates = _dedupe_candidates(market_candidates)
                offer_candidates = _dedupe_candidates(offer_candidates)
                vocabulary_candidates = _dedupe_candidates(vocabulary_candidates)
                scanned += 1
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

    scanned = relevant = added = errors = 0
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
        with connect(TECH_DB) as db:
            for item in list(pooled.values())[: max(1, limit)]:
                fingerprint = hashlib.sha256((item["doi"] or item["url"]).casefold().encode()).hexdigest()
                stamp = utc_now()
                before = db.total_changes
                db.execute(
                    """INSERT OR IGNORE INTO documents(
                           document_type,title,source_url,published_at,doi,abstract,fingerprint,created_at,last_seen_at
                       ) VALUES('publication',?,?,?,?,?,?,?,?)""",
                    (
                        item["title"], item["url"], item["published"], item["doi"],
                        item["abstract"], fingerprint, stamp, stamp,
                    ),
                )
                inserted = int(db.total_changes > before)
                added += inserted
                if not inserted:
                    db.execute(
                        "UPDATE documents SET last_seen_at=? WHERE fingerprint=?",
                        (stamp, fingerprint),
                    )

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
        "errors": errors,
        "queries": len(TECHNOLOGY_QUERIES),
        "lookback_days": effective_lookback,
    }

