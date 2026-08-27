"""Capacités chiffrées (chantier 5) et certifications/salle blanche (§3.2, segment
prestataires industriels / job-shops) de l'audit collecte : ce que l'acteur SAIT FAIRE en
chiffres (finesse de gravure, tolérance, format de pièce, cadence, longueurs d'onde, durée
d'impulsion, matériaux qualifiés, taille de série) et ce qu'il DÉTIENT comme accréditation
(normes ISO, classe de salle blanche) -- par opposition à ce qu'il EST (firmographics.py) ou à
ce qu'il DÉMONTRE par marché (evidence/offers). Remplit ``capability_spec`` et, pour les
certifications/salle blanche, ``actor_facts``.

L'audit groupe ces deux familles dans la même phrase ("Extraire systématiquement : normes ISO,
classe de salle blanche, nombre et type de systèmes laser, taille de lot mini/maxi, tolérances
annoncées, matériaux qualifiés") parce que ce sont les mêmes pages qui les portent -- ce module
les extrait donc dans la même passe plutôt que de re-télécharger deux fois les mêmes URLs.

Aucune nouvelle collecte web : ce module relit les pages déjà crawlées et classées
product/equipment/capability/service/about par scrapers.scrape_actors() (blocks_json en cache
dans actor_sources, ou re-téléchargées à la volée si le cache est absent -- jamais recrawlées
en profondeur, ce module ne découvre aucune nouvelle URL). service/about sont inclus en plus du
trio initial du chantier 5 : une certification ISO ou une classe de salle blanche se déclare
typiquement sur une page "Qualité"/"À propos"/"Services", rarement sur une fiche produit --
contrairement aux specs numériques (chantier 5), dont le contexte de gating (mots-clés dans le
même bloc) reste tout aussi fiable quel que soit le type de page d'origine.

Extraction déterministe uniquement (regex bornées par une unité ET, pour les champs les plus
ambigus, un mot de contexte dans le même bloc éditorial) -- jamais d'IA, même logique que
db.classify_evidence_type. Une ligne capability_spec par acteur ; chaque champ numérique
retient la MEILLEURE valeur trouvée sur l'ensemble de ses pages :
  - min pour min_feature_size_um, tolerance_um, pulse_duration_fs (plus petit = plus fin/rapide)
  - max pour max_part_size_mm, throughput_units_per_h (plus grand = plus capable)
``wavelengths_nm``/``materials_qualified`` sont des listes JSON (toutes les valeurs distinctes
trouvées, pas une seule). ``batch_size_range`` est stocké comme la citation brute du passage
matché (pas reformulé) -- même principe que evidence.quote_verbatim : une plage de lot est trop
spécifique au contexte de la phrase pour être normalisée sans risque de trahir la source.

Certifications (``actor_facts``, dimension='certification') : un code reconnu (ISO 9001, ISO
13485, ISO 14001, AS9100, IATF 16949, ITAR, Nadcap, ISO/IEC 17025) matché littéralement --
contrairement à db._CERTIFICATION_RE (utilisé pour classify_evidence_type, où un simple signal
"il y a un code ISO quelque part" suffit), chaque code est vérifié individuellement pour ne
jamais enregistrer un code non pertinent (ex: ISO 8601, un format de date, ne matche aucun de
ces motifs). Classe de salle blanche (``actor_facts``, dimension='differentiator', par cohérence
avec le reste des différenciateurs déjà stockés là -- pas de nouvelle colonne/CHECK à migrer) :
ISO 14644 (ISO 1-9) ou Federal Standard 209E (Class 1 à 100000), acceptée seulement si un mot de
contexte ("cleanroom"/"salle blanche"/...) apparaît dans le même bloc -- un "ISO 7" isolé n'est
pas une classe de salle blanche. Écrit dans actor_profile.cleanroom_iso_class délibérément PAS
choisi : cette colonne partage un source_url/as_of_date unique par acteur avec firmographics.py
(chantier 5), et lui faire porter une deuxième source créerait exactement le risque de
mauvaise attribution que la décision du chantier 4/5 (pas de provenance par-champ) a refusé de
résoudre -- actor_facts, où chaque ligne a déjà son propre source_url, n'a pas ce problème.

Hors scope, honnêtement absent plutôt que deviné : une "référence client nommée" comme preuve de
capacité (même exclusion que db.classify_evidence_type, pour la même raison -- pas de
reconnaissance d'entité nommée fiable ici). Un champ resté NULL signifie "aucun signal
déterministe trouvé", jamais "zéro" ou "aucune capacité".
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

import httpx

from db import ACTORS_DB, connect, utc_now
from hybrid import is_pdf_response, parse_document, parse_pdf_document
from scrapers import HEADERS, MATERIALS, TIMEOUT, _fetch, _match_all_labels, _stored_blocks
from site_profiles import get_site_profile

CAPABILITY_PAGE_TYPES = ("product", "equipment", "capability", "service", "about")
# Bounds capacity of work per actor -- current data (81 product + 60 equipment + 16 capability
# pages across 34 actors) never gets close to this, it only guards against one actor's page
# count growing unchecked as the crawl deepens over time.
MAX_PAGES_PER_ACTOR = 25

_WAVELENGTH_RE = re.compile(r"\b(\d{3,4})\s*nm\b", re.IGNORECASE)
_PULSE_FS_RE = re.compile(r"\b(\d[\d.,]*)\s*fs\b", re.IGNORECASE)
# "±" is an unambiguous tolerance marker by itself -- no context word needed, unlike
# min_feature_size_um/max_part_size_mm below which share the same µm/mm units as other specs.
_TOLERANCE_RE = re.compile(r"±\s*(\d[\d.,]*)\s*(?:µm|um|microns?|microm[eè]tres?)\b", re.IGNORECASE)
_UM_VALUE_RE = re.compile(r"\b(\d[\d.,]*)\s*(?:µm|um|microns?|microm[eè]tres?)\b", re.IGNORECASE)
_MM_VALUE_RE = re.compile(r"\b(\d[\d.,]*)\s*mm\b", re.IGNORECASE)
_THROUGHPUT_RE = re.compile(
    r"\b(\d[\d.,]*)\s*(?:pieces?|pi[eè]ces?|parts?|units?|unit[ée]s?)\s*(?:/|par|per)\s*(?:h\b|hour|heure|hr\b)",
    re.IGNORECASE,
)
_BATCH_RANGE_RE = re.compile(
    r"\b(?:from|de)\s+\d[\d\s]{0,9}\s+(?:to|à)\s+\d[\d\s,]{0,12}\s*(?:pieces?|pi[eè]ces?|parts?|units?|unit[ée]s?)\b",
    re.IGNORECASE,
)

# Un chiffre en µm/mm seul est ambigu (une page produit regorge de dimensions sans rapport) --
# ces deux champs n'acceptent une valeur que si un mot de ce contexte apparaît dans le MÊME bloc
# éditorial (même granularité que scrapers._candidate, pas une fenêtre de caractères arbitraire).
# Deliberately NOT "resolution"/"résolution" alone: found in production data attached to a
# stage motor's positioning resolution ("0.005 µm resolution" on an XY servo stage), a
# completely different spec from the laser's achievable machining feature size -- the number
# was real, but the label would have been wrong. Only phrases specific to the optical/machining
# feature itself are kept.
_FEATURE_SIZE_CONTEXT = (
    "feature size", "spot size", "minimum feature",
    "line width", "taille de spot", "largeur de trait", "beam waist",
)
_PART_SIZE_CONTEXT = (
    "travel", "course", "work area", "work envelope", "worktable", "plateau",
    "format maximal", "dimensions maxi", "taille maximale", "zone de travail", "work volume",
)

_WAVELENGTH_MIN_NM = 200
_WAVELENGTH_MAX_NM = 2200
_PULSE_MIN_FS = 5
_PULSE_MAX_FS = 100_000

# §3.2 : "Certification et capacité industrielle sont les deux critères d'achat de ce segment
# ... 9 certifications en base." Chaque code est un motif dédié (pas une regex générique type
# "ISO \d+") pour ne jamais enregistrer un numéro ISO non pertinent (ISO 8601 est un format de
# date, pas une certification) comme preuve de conformité.
_CERTIFICATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ISO 9001", re.compile(r"\biso\s?9001\b", re.IGNORECASE)),
    ("ISO 13485", re.compile(r"\biso\s?13485\b", re.IGNORECASE)),
    ("ISO 14001", re.compile(r"\biso\s?14001\b", re.IGNORECASE)),
    ("AS9100", re.compile(r"\bas\s?9100\b", re.IGNORECASE)),
    ("IATF 16949", re.compile(r"\biatf\s?16949\b", re.IGNORECASE)),
    ("ITAR", re.compile(r"\bitar\b", re.IGNORECASE)),
    ("Nadcap", re.compile(r"\bnadcap\b", re.IGNORECASE)),
    ("ISO/IEC 17025", re.compile(r"\biso\s?(?:/\s?iec\s?)?17025\b", re.IGNORECASE)),
)

# Classe de salle blanche : ISO 14644 (ISO 1 à 9, la norme actuelle) ou Federal Standard 209E
# (Class 1 à 100000, retiré officiellement mais toujours utilisé dans l'industrie). Un chiffre
# seul est sans signification -- accepté uniquement si un mot de ce contexte apparaît dans le
# même bloc (même principe que _FEATURE_SIZE_CONTEXT/_PART_SIZE_CONTEXT ci-dessus).
_CLEANROOM_CONTEXT = ("cleanroom", "clean room", "clean-room", "salle blanche", "classe de propreté")
_CLEANROOM_ISO_CLASS_RE = re.compile(r"\biso\s*(?:class\s*)?([1-9])\b", re.IGNORECASE)
_CLEANROOM_FED_CLASS_RE = re.compile(r"\bclass\s*(100000|10000|1000|100|10|1)\b", re.IGNORECASE)


def _parse_number(raw: str) -> float | None:
    """Convertit '1,5' (FR) ou '1,500' (thousands, EN) ou '1500' en float, sans jamais deviner
    au-delà de ce que la ponctuation indique sans ambiguïté."""
    cleaned = raw.strip().replace(" ", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in terms)


def _block_text(block: Any) -> str:
    return " ".join(filter(None, (block.h1, block.h2, block.h3, block.heading, block.text)))


def _extract_capabilities(block_texts: list[str]) -> dict[str, Any]:
    wavelengths: set[int] = set()
    materials: set[str] = set()
    pulse_values: list[float] = []
    tolerance_values: list[float] = []
    feature_values: list[float] = []
    part_size_values: list[float] = []
    throughput_values: list[float] = []
    batch_quote: str | None = None

    for text in block_texts:
        if not text.strip():
            continue
        for match in _WAVELENGTH_RE.finditer(text):
            nm_value = int(match.group(1))
            if _WAVELENGTH_MIN_NM <= nm_value <= _WAVELENGTH_MAX_NM:
                wavelengths.add(nm_value)
        for match in _PULSE_FS_RE.finditer(text):
            value = _parse_number(match.group(1))
            if value is not None and _PULSE_MIN_FS <= value <= _PULSE_MAX_FS:
                pulse_values.append(value)
        tolerance_spans = [match.span() for match in _TOLERANCE_RE.finditer(text)]
        for match in _TOLERANCE_RE.finditer(text):
            value = _parse_number(match.group(1))
            if value is not None and 0 < value <= 1000:
                tolerance_values.append(value)
        if _contains_any(text, _FEATURE_SIZE_CONTEXT):
            for match in _UM_VALUE_RE.finditer(text):
                # A "±2 µm" tolerance is not a feature size, even when both share one block
                # with a feature-size keyword elsewhere in the sentence -- skip any µm value
                # already claimed by the (more specific) tolerance match above.
                if any(match.start() < end and match.end() > start for start, end in tolerance_spans):
                    continue
                value = _parse_number(match.group(1))
                if value is not None and 0 < value <= 5000:
                    feature_values.append(value)
        if _contains_any(text, _PART_SIZE_CONTEXT):
            for match in _MM_VALUE_RE.finditer(text):
                value = _parse_number(match.group(1))
                if value is not None and 0 < value <= 5000:
                    part_size_values.append(value)
        for match in _THROUGHPUT_RE.finditer(text):
            value = _parse_number(match.group(1))
            if value is not None and value > 0:
                throughput_values.append(value)
        if batch_quote is None:
            batch_match = _BATCH_RANGE_RE.search(text)
            if batch_match:
                batch_quote = batch_match.group(0).strip()
        for label, _hits in _match_all_labels(text, MATERIALS):
            materials.add(label)

    return {
        "min_feature_size_um": min(feature_values) if feature_values else None,
        "tolerance_um": min(tolerance_values) if tolerance_values else None,
        "max_part_size_mm": max(part_size_values) if part_size_values else None,
        "throughput_units_per_h": max(throughput_values) if throughput_values else None,
        "wavelengths_nm": sorted(wavelengths),
        "pulse_duration_fs": min(pulse_values) if pulse_values else None,
        "materials_qualified": sorted(materials),
        "batch_size_range": batch_quote,
    }


def _extract_certifications(block_texts: list[str]) -> list[str]:
    found: set[str] = set()
    for text in block_texts:
        for label, pattern in _CERTIFICATION_PATTERNS:
            if pattern.search(text):
                found.add(label)
    return sorted(found)


def _extract_cleanroom_class(block_texts: list[str]) -> str | None:
    """Norme ISO 14644 préférée si trouvée (c'est la norme en vigueur) ; sinon Federal Standard
    209E. Les deux échelles ne sont pas convertibles l'une en l'autre -- pas de mélange, on
    retient la meilleure classe (le plus petit chiffre) au sein de l'échelle trouvée."""
    iso_values: list[int] = []
    fed_values: list[int] = []
    for text in block_texts:
        if not _contains_any(text, _CLEANROOM_CONTEXT):
            continue
        iso_values.extend(int(match.group(1)) for match in _CLEANROOM_ISO_CLASS_RE.finditer(text))
        fed_values.extend(int(match.group(1)) for match in _CLEANROOM_FED_CLASS_RE.finditer(text))
    if iso_values:
        return f"ISO {min(iso_values)}"
    if fed_values:
        return f"Class {min(fed_values)}"
    return None


def _upsert_actor_fact(db, actor_id: int, dimension: str, value: str, source_url: str) -> int:
    """Comme db.add_actor_fact, mais idempotent -- add_actor_fact est le point d'entrée
    manuel/API, celui-ci sert un collecteur qui repasse sur les mêmes pages à chaque run et ne
    doit jamais réinsérer le même fait. Le dédoublonnage est en SOUS-CHAÎNE (pas juste égalité
    exacte) : en production, plusieurs acteurs avaient déjà une entrée saisie à la main avant ce
    collecteur (ex: "ISO 9001:2015", "DIN EN ISO 13485:2016") -- un match exact contre notre
    libellé canonique ("ISO 9001") les aurait ratées et créé un doublon visible dans la fiche.
    Une entrée existante qui contient déjà notre libellé, dans n'importe quelle casse, est
    considérée comme couvrant le même fait."""
    existing_values = [
        row["value"] for row in db.execute(
            "SELECT value FROM actor_facts WHERE actor_id=? AND dimension=?", (actor_id, dimension),
        ).fetchall()
    ]
    if any(value.casefold() in existing.casefold() for existing in existing_values):
        return 0
    db.execute(
        "INSERT INTO actor_facts(actor_id,dimension,value,source_url,review_status,created_at) VALUES(?,?,?,?,'verified',?)",
        (actor_id, dimension, value, source_url, utc_now()),
    )
    return 1


def _upsert_capability_spec(db, actor_id: int, fields: dict[str, Any], source_url: str) -> int:
    """Une ligne par acteur, comme firmographics._upsert_actor_profile -- un rafraîchissement
    remplace l'enveloppe précédente plutôt que de l'ignorer."""
    stamp = utc_now()
    wavelengths_json = json.dumps(fields["wavelengths_nm"], ensure_ascii=False) if fields["wavelengths_nm"] else None
    materials_json = json.dumps(fields["materials_qualified"], ensure_ascii=False) if fields["materials_qualified"] else None
    values = (
        fields["min_feature_size_um"], fields["tolerance_um"], fields["max_part_size_mm"],
        fields["throughput_units_per_h"], wavelengths_json, fields["pulse_duration_fs"],
        materials_json, fields["batch_size_range"], source_url, stamp[:10], stamp,
    )
    existing = db.execute("SELECT actor_id FROM capability_spec WHERE actor_id=?", (actor_id,)).fetchone()
    if existing:
        db.execute(
            """UPDATE capability_spec SET min_feature_size_um=?,tolerance_um=?,max_part_size_mm=?,
                      throughput_units_per_h=?,wavelengths_nm=?,pulse_duration_fs=?,materials_qualified=?,
                      batch_size_range=?,source_url=?,as_of_date=?,updated_at=?
               WHERE actor_id=?""",
            values + (actor_id,),
        )
        return 0
    db.execute(
        """INSERT INTO capability_spec(
               actor_id,min_feature_size_um,tolerance_um,max_part_size_mm,throughput_units_per_h,
               wavelengths_nm,pulse_duration_fs,materials_qualified,batch_size_range,source_url,as_of_date,
               created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (actor_id,) + values + (stamp,),
    )
    return 1


def collect_capability_specs() -> dict:
    """Point d'entrée (voir app.py: collectors["capabilities"]) -- remplit capability_spec ET,
    dans la même passe de pages, actor_facts (certifications, classe de salle blanche)."""
    with connect(ACTORS_DB) as db:
        rows = [dict(row) for row in db.execute(
            f"""SELECT a.id AS actor_id,a.name,a.official_url,
                       s.id AS source_id,s.url,s.page_type,s.blocks_json,s.last_checked_at
                FROM actor_sources s
                JOIN actors a ON a.id=s.actor_id
                WHERE a.active=1 AND s.active=1
                  AND s.page_type IN ({",".join("?" * len(CAPABILITY_PAGE_TYPES))})
                  AND (s.last_http_status IS NULL OR s.last_http_status BETWEEN 200 AND 399)
                ORDER BY a.name,s.id""",
            CAPABILITY_PAGE_TYPES,
        ).fetchall()]

    by_actor: dict[int, list[dict]] = defaultdict(list)
    actor_meta: dict[int, dict] = {}
    for row in rows:
        actor_id = int(row["actor_id"])
        by_actor[actor_id].append(row)
        actor_meta.setdefault(actor_id, row)

    actors_scanned = pages_analyzed = pages_fetched = profiles_added = profiles_updated = errors = 0
    certifications_added = cleanroom_facts_added = 0
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=TIMEOUT) as client:
        for actor_id, sources in by_actor.items():
            meta = actor_meta[actor_id]
            actors_scanned += 1
            block_texts: list[str] = []
            primary_source_url: str | None = None
            for source in sources[:MAX_PAGES_PER_ACTOR]:
                try:
                    # No freshness gate here (unlike scrape_market's 24h _stored_blocks reuse):
                    # a datasheet doesn't go stale from one run to the next, so any cached parse
                    # is worth reusing rather than re-fetched.
                    blocks = _stored_blocks(source, max_age_hours=10 ** 6)
                    if blocks is None:
                        response = _fetch(client, source["url"])
                        resolved_url = str(response.url)
                        profile = get_site_profile({"name": meta["name"], "official_url": meta["official_url"]})
                        if is_pdf_response(response.headers.get("content-type", ""), resolved_url):
                            document = parse_pdf_document(response.content, resolved_url, profile=profile)
                        else:
                            document = parse_document(response.text, resolved_url, profile=profile)
                        blocks = document.blocks
                        pages_fetched += 1
                    pages_analyzed += 1
                    if primary_source_url is None:
                        primary_source_url = source["url"]
                    block_texts.extend(_block_text(block) for block in blocks)
                except Exception:
                    errors += 1
                    continue
            if not block_texts or primary_source_url is None:
                continue
            fields = _extract_capabilities(block_texts)
            certifications = _extract_certifications(block_texts)
            cleanroom_class = _extract_cleanroom_class(block_texts)
            if not any(fields.values()) and not certifications and not cleanroom_class:
                continue
            with connect(ACTORS_DB) as db:
                if any(fields.values()):
                    created = _upsert_capability_spec(db, actor_id, fields, primary_source_url)
                    profiles_added += created
                    profiles_updated += int(not created)
                for label in certifications:
                    certifications_added += _upsert_actor_fact(db, actor_id, "certification", label, primary_source_url)
                if cleanroom_class:
                    cleanroom_facts_added += _upsert_actor_fact(
                        db, actor_id, "differentiator", f"Salle blanche {cleanroom_class}", primary_source_url,
                    )

    return {
        "actors_with_pages": len(by_actor),
        "actors_scanned": actors_scanned,
        "pages_analyzed": pages_analyzed,
        "pages_fetched": pages_fetched,
        "profiles_added": profiles_added,
        "profiles_updated": profiles_updated,
        "certifications_added": certifications_added,
        "cleanroom_facts_added": cleanroom_facts_added,
        "errors": errors,
    }
