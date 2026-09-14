"""Projets nationaux et régionaux : les financements que CORDIS ne voit pas.

``cordis.py`` ne connaît qu'un seul guichet, Horizon Europe. Or un acteur de l'observatoire
travaille aussi -- et souvent d'abord -- sur des projets financés chez lui : ANR et France 2030
en France, RAPID à l'Agence de l'innovation de défense, Innovate UK et EPSRC au Royaume-Uni,
BMBF en Allemagne, FEDER et appels à projets régionaux à l'échelle d'une région. Ces
participations n'apparaissaient nulle part : la page "Technologie laser" annonçait "projets
européens" et disait vrai, faute de mieux.

Ce module remplit le même contrat que cordis.py, avec les mêmes garanties :

- rien n'est écrit pour un projet dont le titre/résumé ne relève pas du laser ultra-rapide
  (``lexicon.is_on_topic``, exactement le filtre appliqué aux projets CORDIS et aux
  publications OpenAlex) ;
- rien n'est écrit pour une organisation qui n'est pas un acteur suivi, reconnu par le même
  appariement de nom que CORDIS (``cordis._contains_whole_phrase`` sur un nom normalisé) ;
- chaque ligne porte l'URL de la page publique qui la prouve.

Trois sources, de nature différente -- et c'est volontaire, parce que les guichets nationaux
ne publient pas tous leurs lauréats de la même façon :

1. ANR (France) -- le jeu de données ouvert "Appels à projets ANR : projets retenus et
   participants identifiés" (13 450 projets depuis 2005, partenaires nommés), servi par l'API
   Opendatasoft du portail data.enseignementsup-recherche.gouv.fr. Couvre aussi, sous le même
   toit, les programmes que l'ANR opère pour d'autres : ASTRID et ASTRID Maturation (financés
   par la DGA/AID), les appels du PIA et de France 2030.

2. UKRI Gateway to Research (Royaume-Uni) -- l'API publique de UKRI (pas de clé), qui couvre
   Innovate UK, EPSRC, STFC, ISCF et les autres conseils : le guichet national britannique,
   celui par lequel passent les projets collaboratifs des prestataires UK suivis. GtR
   enregistre aussi la participation britannique à des programmes européens ("Horizon Europe
   Guarantee", "EU") : l'échelle se lit donc sur le guichet du projet, pas sur le pays de
   l'organisation -- voir GTR_EUROPEAN_FUNDERS.

3. Mentions de programme dans les pages déjà collectées -- pour tous les guichets qui ne
   publient AUCUNE liste de lauréats exploitable. RAPID est le cas type : vérifié le
   13/09/2026, ni le ministère des Armées ni data.gouv.fr ne publient la liste des projets
   RAPID financés ; la seule trace publique d'un projet RAPID est la page de l'entreprise qui
   l'annonce. Même chose pour les appels régionaux (R&D Booster en Auvergne-Rhône-Alpes), le
   FUI, le FEDER opéré par une région, le BMBF allemand. Cette passe ne devine rien : elle
   relit le texte que le crawl a DÉJÀ stocké (``actor_sources.blocks_json``), et ne retient un
   bloc que s'il nomme un programme connu ET relève du laser ultra-rapide. Le résultat est un
   signal faible, écrit en ``review_status='pending'`` -- il passe par la file de relecture,
   contrairement aux deux premières sources qui sont sourcées sur une fiche projet officielle.

Limite assumée : l'Allemagne, premier pays de la base par le nombre d'acteurs, n'a pas de
source structurée ici. Le Förderkatalog du Bund (foerderportal.bund.de/foekat) est la
référence, mais c'est une application JSP à session, sans API ni export stable -- la scraper
donnerait un collecteur qui casse à la première refonte. Les projets BMBF entrent donc par la
passe 3, quand l'institut les annonce sur son propre site, et pas autrement.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx

from cordis import (
    CORDIS_MATCH_BLOCKLIST,
    MAX_PARTNERS_PER_PROJECT,
    _actor_alias,
    _contains_whole_phrase,
    _normalize_org_text,
    _upsert_actor_relation,
)
from db import (
    ACTORS_DB,
    TECH_DB,
    connect,
    technology_signal_key,
    upsert_actor_event,
    upsert_fact_source,
    upsert_technology_signal,
    utc_now,
)
from http_client import connector_client
from lexicon import (
    TECHNOLOGY_AXES,
    Lexicon,
    best_quote,
    detect_maturity,
    is_on_topic,
    match_all_labels,
    match_label_details,
)

# --- Source 1 : ANR ------------------------------------------------------------------------
#
# API Explore v2.1 d'Opendatasoft (portail open data du MESR). Pas de clé, pas de quota
# documenté ; une requête par acteur suivi, filtrée côté serveur sur le nom du partenaire.
ANR_RECORDS_URL = (
    "https://data.enseignementsup-recherche.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "fr-esr-aap-anr-projets-retenus-participants-identifies/records"
)
# 100 est le maximum accepté par l'API sur ce paramètre. Aucun acteur suivi n'approche ce
# nombre de projets ANR (le mieux doté en compte 3), donc pas de pagination : si un acteur
# venait à le dépasser, `anr_projects_truncated` le signale dans le rapport plutôt que de
# laisser croire à une collecte complète.
ANR_PAGE_SIZE = 100
# 3 033 des 13 450 projets n'ont pas de `lien_projet`. La fiche existe pourtant toujours à
# cette adresse -- vérifié le 13/09/2026 : anr.fr/Projet-ANR-14-CE16-0008 rend bien la fiche
# du projet, et anr.fr/Projet-ANR-99-ZZZZ-9999 répond 404. Ce n'est donc pas une URL devinée,
# c'est le gabarit canonique du site, contrôlé sur un code valide et un code invalide.
ANR_PROJECT_URL_TEMPLATE = "https://anr.fr/Projet-{code}"

# --- Source 2 : UKRI Gateway to Research -----------------------------------------------------
GTR_API_BASE = "https://gtr.ukri.org/gtr/api"
# L'API refuse toute taille de page inférieure à 10 ("Page size cannot be less than 10").
GTR_PAGE_SIZE = 100
GTR_MAX_PROJECT_PAGES = 5
# Un même établissement a plusieurs fiches dans GtR ("OXFORD LASERS LIMITED", "Oxford Lasers
# Ltd", "Oxford Lasers Ltd" à nouveau) : on interroge toutes celles dont le nom contient
# l'alias suivi, et on dédoublonne les projets sur leur identifiant.
# Relevé du 14/09/2026 : TWI en a 9 à lui seul ("TWI", "TWI LIMITED", "TWI  LIMITED"...),
# chacune portant ses propres projets -- la borne précédente (5) en perdait la moitié.
GTR_MAX_ORG_MATCHES = 12
# GtR renvoie ce texte à la place de l'objectif quand le résumé n'a jamais été saisi. Le
# prendre pour un résumé remplirait `quote` avec une phrase qui ne parle pas du projet.
GTR_ABSENT_ABSTRACT = "abstracts are not currently available in gtr"
# La fiche lisible par un humain, celle qu'on met en source -- pas l'URL de l'API.
GTR_PROJECT_URL_TEMPLATE = "https://gtr.ukri.org/projects?ref={ref}"
# GtR n'est pas QUE le guichet national : il enregistre aussi la participation britannique à
# des programmes européens. Relevé du 14/09/2026 sur les 10 fiches du MTC et les 9 de TWI,
# `leadFunder` prend une douzaine de valeurs, dont deux qui ne sont pas de l'argent national --
# "Horizon Europe Guarantee" (le dispositif qui a pris le relais après le Brexit) et "EU".
# Les écrire "projet national" serait faux sur l'étiquette la plus visible de la fiche.
# Tout le reste (EPSRC, Innovate UK, ISCF, ATI, APC, UKRI FLF, SPF...) est bien national.
GTR_EUROPEAN_FUNDERS = {"horizon europe guarantee", "eu", "horizon 2020", "erc"}

# --- Source 3 : mentions de programme dans les pages déjà collectées --------------------------
#
# Même moteur de règles que tous les lexiques du projet (voir lexicon.match_all_labels) : un
# programme n'est reconnu que sur un terme explicite, et les sigles courts exigent un contexte
# (`requires_any`) pour ne pas confondre "RAPID" le régime d'aide avec "rapid" l'adjectif, ou
# "PIA" le plan d'investissement avec n'importe quel acronyme de trois lettres.
FUNDING_PROGRAMS: Lexicon = {
    "RAPID (AID/DGA)": {
        "regex": (r"\brapid\b",),
        "requires_any": ("dga", "aid", "innovation duale", "agence de l'innovation de défense",
                         "ministère des armées", "direction générale de l'armement"),
    },
    "ANR": {
        "any_of": ("agence nationale de la recherche",),
        "regex": (r"\banr\b", r"\bANR-\d{2}-",),
    },
    "France 2030 / PIA": {
        "any_of": ("france 2030", "investissements d'avenir", "investissement d'avenir",
                   "programme d'investissements d'avenir"),
        "regex": (r"\bpia\s?[1-4]\b",),
    },
    "Bpifrance": {"any_of": ("bpifrance", "bpi france")},
    "FUI": {
        "any_of": ("fonds unique interministériel",),
        "regex": (r"\bfui\b",),
    },
    "ADEME": {"regex": (r"\bademe\b",)},
    "Innovate UK": {"any_of": ("innovate uk", "technology strategy board")},
    "EPSRC / UKRI": {
        "any_of": ("engineering and physical sciences research council", "ukri"),
        "regex": (r"\bepsrc\b", r"\bstfc\b"),
    },
    "BMBF / BMWK (Allemagne)": {
        "any_of": ("bundesministerium für bildung und forschung",
                   "bundesministerium für wirtschaft", "gefördert vom bmbf", "zim-projekt"),
        "regex": (r"\bbmbf\b", r"\bbmwk\b", r"\bbmwi\b"),
    },
    "Innosuisse / FNS (Suisse)": {
        "any_of": ("innosuisse", "commission for technology and innovation",
                   "swiss national science foundation", "fonds national suisse"),
    },
    "Eurostars / EUREKA": {"any_of": ("eurostars", "eureka")},
    "Enterprise Ireland / SFI": {
        "any_of": ("enterprise ireland", "science foundation ireland"),
        "regex": (r"\bsfi\b",),
    },
    "CDTI (Espagne)": {
        "any_of": ("centro para el desarrollo tecnológico",),
        "regex": (r"\bcdti\b",),
    },
    "Vinnova / Business Finland / RVO": {
        "any_of": ("vinnova", "business finland", "rijksdienst voor ondernemend nederland",
                   "topsector", "nwo"),
    },
    "FEDER / ERDF": {
        "any_of": ("fonds européen de développement régional",
                   "european regional development fund"),
        "regex": (r"\bfeder\b", r"\berdf\b"),
    },
    "Interreg": {"any_of": ("interreg",)},
    "Appel à projets régional": {
        "any_of": ("r&d booster", "conseil régional", "conseil départemental",
                   "région auvergne-rhône-alpes", "région nouvelle-aquitaine",
                   "région bourgogne-franche-comté", "région grand est", "région occitanie",
                   "région bretagne", "région hauts-de-france", "région normandie",
                   "région pays de la loire", "région centre-val de loire",
                   "région provence-alpes-côte d'azur", "région île-de-france"),
    },
}

# Échelle de financement et pays du guichet, pour chaque libellé ci-dessus. Séparé de la
# Lexicon parce que celle-ci ne porte que des règles lexicales (voir lexicon.LexiconRule) --
# y glisser des métadonnées casserait son type et son moteur.
#
# "regional" au sens du guichet, pas du bénéficiaire : le FEDER est un fonds européen, mais il
# est programmé et attribué par une région, et c'est ainsi qu'un acteur en parle ("avec le
# soutien de la Région Grand Est – FEDER"). Interreg, transfrontalier, reste européen.
FUNDING_PROGRAM_SCOPES: dict[str, tuple[str, str]] = {
    "RAPID (AID/DGA)": ("national", "France"),
    "ANR": ("national", "France"),
    "France 2030 / PIA": ("national", "France"),
    "Bpifrance": ("national", "France"),
    "FUI": ("national", "France"),
    "ADEME": ("national", "France"),
    "Innovate UK": ("national", "Royaume-Uni"),
    "EPSRC / UKRI": ("national", "Royaume-Uni"),
    "BMBF / BMWK (Allemagne)": ("national", "Allemagne"),
    "Innosuisse / FNS (Suisse)": ("national", "Suisse"),
    "Eurostars / EUREKA": ("europeen", "Europe"),
    "Enterprise Ireland / SFI": ("national", "Irlande"),
    "CDTI (Espagne)": ("national", "Espagne"),
    "Vinnova / Business Finland / RVO": ("national", "Europe du Nord"),
    "FEDER / ERDF": ("regional", "Union européenne"),
    "Interreg": ("europeen", "Union européenne"),
    "Appel à projets régional": ("regional", "France"),
}

SCOPE_LABELS = {"europeen": "européen", "national": "national", "regional": "régional"}

# Types d'événement écrits dans actor_events. Distincts de 'cordis_project' pour qu'une revue
# puisse trier par guichet sans relire les descriptions.
EVENT_ANR = "anr_project"
EVENT_GTR = "ukri_project"
EVENT_MENTION = "funding_program_mention"


# Formes juridiques retirées de l'alias avant comparaison. Un registre national épelle la
# sienne comme il veut -- GtR connaît "Laser Micromachining Limited" là où la base suit "Laser
# Micromachining Ltd" -- et exiger le suffixe faisait manquer l'organisation entière (mesuré le
# 14/09/2026 : 0 fiche retenue sur 25 renvoyées, alors que deux d'entre elles étaient la bonne).
_LEGAL_SUFFIXES = (
    "LTD", "LTD.", "LIMITED", "PLC", "INC", "LLC", "GMBH", "MBH", "AG", "KG", "SA", "SAS",
    "SARL", "SRL", "SPA", "BV", "NV", "AB", "AS", "OY", "APS", "EV", "EEIG", "SE",
)
# Plus court que CORDIS (MIN_ALIAS_LENGTH = 4), et pour une raison de fond : CORDIS cherche
# l'alias dans ~300 000 lignes d'organisations téléchargées en vrac, où un sigle de trois
# lettres est un aimant à faux positifs. Ici l'API fait d'abord la recherche par nom, le
# rapprochement local ne fait que VÉRIFIER une poignée de candidats, et le filtre laser passe
# derrière. Avec 4, TWI -- centre de recherche britannique majeur, 9 fiches dans GtR -- et HEF
# n'étaient tout simplement jamais interrogés.
MIN_MATCH_ALIAS_LENGTH = 3


def match_alias(actor_name: str) -> str:
    """Le nom d'un acteur réduit à ce qui l'identifie vraiment dans un registre tiers.

    Trois réductions, toutes mesurées sur des cas réels du 14/09/2026 :

    - le sigle entre parenthèses part avec elles : "Manufacturing Technology Centre (MTC)"
      donnait l'alias "MANUFACTURING TECHNOLOGY CENTRE MTC", qui ne matche aucune des trois
      autres fiches GtR du même centre ("THE MANUFACTURING TECHNOLOGY CENTRE LIMITED"...) ;
    - la forme juridique finale part aussi (voir _LEGAL_SUFFIXES) ;
    - si ces coupes laissent moins de MIN_MATCH_ALIAS_LENGTH caractères, on garde le nom
      entier : mieux vaut ne pas apparier qu'apparier sur deux lettres.
    """
    alias = _actor_alias(re.sub(r"\([^)]*\)", " ", actor_name))
    words = alias.split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    reduced = " ".join(words)
    return reduced if len(reduced) >= MIN_MATCH_ALIAS_LENGTH else alias


def _tracked_aliases(actors: list[dict[str, Any]]) -> dict[str, str]:
    """Les alias de nom utilisés pour reconnaître un acteur suivi dans une base tierce.

    Même normalisation et même liste noire que CORDIS -- deux collecteurs qui appariraient les
    noms différemment finiraient par rattacher le même projet à deux acteurs différents --,
    mais l'alias est réduit par match_alias() et le seuil de longueur est le seuil local : les
    registres nationaux interrogés ici ne posent pas le même risque de faux positif que le
    dump CORDIS. Voir MIN_MATCH_ALIAS_LENGTH pour le raisonnement complet.
    """
    return {
        actor["name"]: match_alias(actor["name"])
        for actor in actors
        if actor["name"] not in CORDIS_MATCH_BLOCKLIST and len(match_alias(actor["name"])) >= MIN_MATCH_ALIAS_LENGTH
    }


def _find_tracked_actor(related_name: str, aliases: dict[str, str]) -> str | None:
    normalized = _normalize_org_text(related_name)
    for actor_name, alias in aliases.items():
        if _contains_whole_phrase(normalized, alias):
            return actor_name
    return None


# =============================================================================================
# Source 1 : ANR
# =============================================================================================

def _anr_partner_names(record: dict[str, Any]) -> list[str]:
    """Les organisations d'un projet ANR, telles que le jeu de données les nomme.

    `libelle_de_partenaire` est un champ multivalué aligné avec `sigle_de_partenaire` et
    `identifiant_de_partenaire` ; il contient des chaînes vides pour les partenaires dont
    seul un libellé générique était connu à la saisie (`type_d_identifiant = 'lib_generique'`).
    """
    names = record.get("libelle_de_partenaire") or []
    if isinstance(names, str):  # l'API sert une chaîne quand le projet n'a qu'un partenaire
        names = [names]
    return [str(name).strip() for name in names if str(name).strip()]


def _anr_partner_acronyms(record: dict[str, Any]) -> list[str]:
    """Les sigles des partenaires (`sigle_de_partenaire`), champ multivalué aligné sur
    `libelle_de_partenaire` -- souvent le SEUL endroit où un acteur apparaît sous le nom que
    la base suit (IREIS, LabHC, CETHIL...)."""
    acronyms = record.get("sigle_de_partenaire") or []
    if isinstance(acronyms, str):
        acronyms = [acronyms]
    return [str(value).strip() for value in acronyms if str(value).strip()]


def _anr_project_url(record: dict[str, Any]) -> str:
    return (record.get("lien_projet") or "").strip() or ANR_PROJECT_URL_TEMPLATE.format(
        code=record.get("code_du_projet") or ""
    )


def _anr_text(record: dict[str, Any]) -> str:
    return f"{record.get('titre') or ''} {record.get('resume') or ''}".strip()


def anr_records_for_alias(client: httpx.Client, alias: str) -> list[dict[str, Any]]:
    """Les projets ANR où `alias` apparaît comme partenaire, sigle ou coordinateur.

    Le filtre est posé côté serveur (ODSQL ``search()``), donc une seule requête par acteur au
    lieu du téléchargement des 13 450 projets. ``search()`` est tolérant (insensible à la
    casse et aux accents, découpé en mots), ce qui est exactement ce qu'il faut pour trouver
    un candidat -- mais pas pour l'accepter : l'appariement strict est refait ensuite, en
    local, par ``_contains_whole_phrase`` sur les noms réellement renvoyés.
    """
    # `alias` sort de _normalize_org_text : majuscules, chiffres et espaces uniquement. Aucun
    # guillemet ne peut donc s'y trouver et casser le littéral ODSQL ci-dessous.
    where = (
        f'search(libelle_de_partenaire,"{alias}") '
        f'or search(sigle_de_partenaire,"{alias}") '
        f'or search(coordinateur_du_projet,"{alias}")'
    )
    response = client.get(ANR_RECORDS_URL, params={"where": where, "limit": ANR_PAGE_SIZE})
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("results") or [])


def _collect_anr(client: httpx.Client, aliases: dict[str, str], report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Passe ANR : renvoie ``{code du projet: {"record":..., "actors": {noms}}}`` on-topic."""
    found: dict[str, dict[str, Any]] = {}
    for actor_name, alias in aliases.items():
        try:
            records = anr_records_for_alias(client, alias)
        except Exception:
            report["errors"] += 1
            continue
        if len(records) == ANR_PAGE_SIZE:
            report["anr_projects_truncated"] += 1
        for record in records:
            code = (record.get("code_du_projet") or "").strip()
            if not code:
                continue
            # L'appariement strict : le nom d'une organisation du projet doit contenir l'alias
            # à des frontières de mot. Sans ce contrôle, `search()` ramènerait un projet pour
            # un simple mot voisin dans un libellé plus long.
            #
            # Les SIGLES comptent autant que les libellés, et les oublier a coûté un vrai
            # projet : l'ANR inscrit IREIS sous "INSTITUT DE RECHERCHES EN INGENIERIE DES
            # SURFACES", le sigle "IREIS" ne vivant que dans `sigle_de_partenaire`. La requête
            # serveur, elle, interrogeait bien ce champ -- le projet TOPOINJECTION revenait de
            # l'API, passait le filtre laser, et se faisait refuser ici. Vérifier ce que l'on
            # a interrogé, ni plus ni moins.
            candidates = (
                _anr_partner_names(record)
                + _anr_partner_acronyms(record)
                + [record.get("coordinateur_du_projet") or ""]
            )
            if not any(_contains_whole_phrase(_normalize_org_text(name), alias) for name in candidates):
                continue
            if not is_on_topic(_anr_text(record)):
                report["projects_off_topic"] += 1
                continue
            entry = found.setdefault(code, {"record": record, "actors": set()})
            entry["actors"].add(actor_name)
    return found


# =============================================================================================
# Source 2 : UKRI Gateway to Research
# =============================================================================================

def _gtr_get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Un GET sur GtR, où "rien trouvé" se dit 404 et n'est pas une panne.

    Mesuré le 14/09/2026 sur les 77 acteurs suivis : l'API répond 404 -- et non 200 avec une
    liste vide -- dès qu'une recherche d'organisation ne ramène rien. Or 60 des 77 acteurs ne
    sont pas britanniques et n'ont, normalement, aucun projet UKRI. Laisser remonter ces 404
    comptait 33 "erreurs" par collecte pour un fonctionnement parfaitement nominal, et noyait
    les vraies pannes (réseau, 500) dans un compteur qu'on apprend à ignorer.
    """
    response = client.get(
        f"{GTR_API_BASE}/{path}", params=params, headers={"Accept": "application/json"},
    )
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return response.json()


def gtr_organisation_ids(client: httpx.Client, actor_name: str, alias: str) -> list[str]:
    """Les fiches d'organisation GtR dont le nom contient l'alias suivi, à des frontières de mot.

    La recherche de GtR est large (1 641 résultats pour "Oxford Lasers", dont des laboratoires
    universitaires sans rapport) : elle sert à trouver des candidats, jamais à les accepter.
    """
    payload = _gtr_get(client, "organisations", {"q": actor_name, "s": 25})
    matched = []
    for organisation in payload.get("organisation") or []:
        name = (organisation.get("name") or "").strip()
        identifier = (organisation.get("id") or "").strip()
        if identifier and _contains_whole_phrase(_normalize_org_text(name), alias):
            matched.append(identifier)
    return matched[:GTR_MAX_ORG_MATCHES]


def gtr_projects_for_organisation(client: httpx.Client, organisation_id: str) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for page in range(1, GTR_MAX_PROJECT_PAGES + 1):
        payload = _gtr_get(
            client, f"organisations/{organisation_id}/projects", {"s": GTR_PAGE_SIZE, "p": page},
        )
        batch = payload.get("project") or []
        projects.extend(batch)
        if len(batch) < GTR_PAGE_SIZE:
            break
    return projects


def _gtr_abstract(project: dict[str, Any]) -> str:
    abstract = (project.get("abstractText") or "").strip()
    return "" if abstract.lower().startswith(GTR_ABSENT_ABSTRACT) else abstract


def _gtr_reference(project: dict[str, Any]) -> str | None:
    identifiers = ((project.get("identifiers") or {}).get("identifier")) or []
    for identifier in identifiers:
        value = (identifier.get("value") or "").strip()
        if value:
            return value
    return None


def _gtr_text(project: dict[str, Any]) -> str:
    return f"{project.get('title') or ''} {_gtr_abstract(project)}".strip()


def _gtr_scope(funder: str) -> str:
    """L'échelle du financement d'un projet GtR, lue sur son guichet (voir
    GTR_EUROPEAN_FUNDERS) : national par défaut, européen pour les quelques guichets qui
    n'en sont pas. Jamais déduite du pays de l'organisation -- ce qui compte est qui paie."""
    return "europeen" if funder.strip().lower() in GTR_EUROPEAN_FUNDERS else "national"


def _gtr_partners(project: dict[str, Any]) -> list[dict[str, str]]:
    participants = ((project.get("participantValues") or {}).get("participant")) or []
    return [
        {"name": (row.get("organisationName") or "").strip(), "role": (row.get("role") or "participant")}
        for row in participants
        if (row.get("organisationName") or "").strip()
    ]


def _collect_gtr(client: httpx.Client, aliases: dict[str, str], report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Passe UKRI : renvoie ``{id de projet GtR: {"project":..., "actors": {noms}}}`` on-topic."""
    found: dict[str, dict[str, Any]] = {}
    for actor_name, alias in aliases.items():
        try:
            organisation_ids = gtr_organisation_ids(client, actor_name, alias)
        except Exception:
            report["errors"] += 1
            continue
        for organisation_id in organisation_ids:
            try:
                projects = gtr_projects_for_organisation(client, organisation_id)
            except Exception:
                report["errors"] += 1
                continue
            for project in projects:
                project_id = (project.get("id") or "").strip()
                if not project_id or not _gtr_reference(project):
                    # Sans référence de subvention, il n'existe pas de page publique à citer
                    # comme source ; l'URL de l'API n'en est pas une pour un lecteur.
                    continue
                if not is_on_topic(_gtr_text(project)):
                    report["projects_off_topic"] += 1
                    continue
                entry = found.setdefault(project_id, {"project": project, "actors": set()})
                entry["actors"].add(actor_name)
    return found


# =============================================================================================
# Source 3 : mentions de programme dans les pages déjà collectées
# =============================================================================================

def _page_blocks(raw: str | None) -> list[dict[str, Any]]:
    try:
        blocks = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return [block for block in blocks if isinstance(block, dict)]


def _block_text(block: dict[str, Any]) -> str:
    """Titre + corps : ce sur quoi on CHERCHE. Le titre porte souvent le nom du projet
    ("Projet RAPID DUALIS"), il ne peut donc pas être ignoré à la détection."""
    return re.sub(r"\s+", " ", f"{block.get('heading') or ''} {block.get('text') or ''}").strip()


def _block_quote_source(block: dict[str, Any]) -> str:
    """Le corps seul : ce qu'on CITE. Le modèle de bloc du crawl répète le titre dans
    `heading`, `h2` et en tête de `text` ; le concaténer une fois de plus donnerait une
    citation qui commence par trois fois le même mot. Le texte reste verbatim -- on choisit un
    champ plus étroit, on ne réécrit pas la source."""
    return re.sub(r"\s+", " ", block.get("text") or "").strip() or _block_text(block)


# Un bloc "Financement" ne dit presque jamais de quoi parle le projet : il dit qui paie.
# ALPhANOV écrit « This project has been funded by the French government under the France 2030
# program » dans un bloc de deux lignes, et ce qu'est le projet (contrôle de procédé laser pour
# électrodes de batteries) se lit dans le bloc juste au-dessus. Le sujet se juge donc sur une
# FENÊTRE de blocs voisins, pas sur le bloc porteur du nom de programme.
#
# La largeur de la fenêtre est mesurée, pas choisie au jugé (relevé du 14/09/2026 sur les
# 1 235 pages en base) : ±0 bloc ne retient que 3 mentions et laisse passer Femtocell, FUI
# 3D-Hybride et NEUROCHIP ; la page entière en retient 26, dont le projet TRIDEN de HEF
# (tribologie pour énergies décarbonées, aucun rapport avec l'ultra-rapide) -- exactement la
# contamination que l'audit v8 §2.1 avait corrigée sur CORDIS. ±1 bloc en retient 11, dont 8
# sont de vrais projets. C'est cette largeur qui est retenue, et c'est aussi pourquoi le
# résultat part en 'pending' : le tri des trois restants est un travail de relecture, pas un
# travail de lexique.
MENTION_CONTEXT_BLOCKS = 1


def detect_program_mentions(text: str, context: str | None = None) -> list[tuple[str, list[str]]]:
    """Les programmes de financement nommés dans `text`, ou rien si le sujet n'y est pas.

    Deux textes, deux rôles : le programme est cherché dans `text` (le bloc qui le nomme), le
    sujet dans `context` (ce bloc et ses voisins, voir MENTION_CONTEXT_BLOCKS) -- ou dans
    `text` lui-même quand aucun contexte n'est donné. C'est cette séparation qui distingue
    "IREPA LASER a un projet FEDER de micro-usinage femtoseconde" de "IREPA LASER est financé
    par le FEDER", sans exiger que la phrase qui nomme le guichet parle aussi du procédé.
    """
    if not is_on_topic(context if context is not None else text):
        return []
    return match_all_labels(text, FUNDING_PROGRAMS)


def _collect_program_mentions(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Relit le texte déjà stocké par le crawl -- aucune requête réseau dans cette passe."""
    mentions: list[dict[str, Any]] = []
    with connect(ACTORS_DB) as db:
        pages = db.execute(
            """SELECT s.actor_id, s.url, s.last_title, s.blocks_json, a.name AS actor_name
                 FROM actor_sources s JOIN actors a ON a.id = s.actor_id
                WHERE a.active = 1 AND s.blocks_json IS NOT NULL AND s.blocks_json != ''"""
        ).fetchall()
    for page in pages:
        report["pages_scanned"] += 1
        raw_blocks = _page_blocks(page["blocks_json"])
        blocks = [_block_text(block) for block in raw_blocks]
        for index, text in enumerate(blocks):
            if not text:
                continue
            low = max(0, index - MENTION_CONTEXT_BLOCKS)
            high = min(len(blocks), index + MENTION_CONTEXT_BLOCKS + 1)
            context = " ".join(blocks[low:high])
            for program, hits in detect_program_mentions(text, context):
                scope, country = FUNDING_PROGRAM_SCOPES[program]
                mentions.append({
                    "actor_id": int(page["actor_id"]),
                    "actor_name": page["actor_name"],
                    "url": page["url"],
                    "program": program,
                    "scope": scope,
                    "country": country,
                    "quote": best_quote(_block_quote_source(raw_blocks[index]), hits),
                })
    return mentions


# =============================================================================================
# Écriture
# =============================================================================================

def _project_signal(
    tech_db,
    *,
    text: str,
    project_name: str,
    source_url: str,
    source_title: str | None,
    actor_names: list[str],
    funding_scope: str,
    funding_program: str,
    language: str,
) -> tuple[int, int]:
    """Un axe technologique pour un projet, s'il est nommé dans son texte -- jamais autrement.

    Copie assumée de ``cordis._upsert_technology_signal`` : même clé de fait, même défaut de
    maturité ("radar", parce qu'un projet de R&D financé n'est pas un produit sur étagère),
    même écriture de la citation. Les deux fonctions diffèrent par ce qu'elles savent de leur
    source -- CORDIS a un objectif de projet structuré, ici on a un résumé dont la langue
    change d'un guichet à l'autre -- et les fusionner demanderait de passer huit paramètres
    pour économiser six lignes.
    """
    axis, hits = match_label_details(text, TECHNOLOGY_AXES)
    if not axis:
        return 0, 0
    quote = best_quote(text, hits)[:700]
    if not quote:
        return 0, 0

    bucket, stage = detect_maturity(text)
    if bucket == "unknown":
        bucket = "radar"
    fact_key = technology_signal_key(axis, project_name)
    added, signal_id = upsert_technology_signal(
        tech_db,
        fact_key=fact_key,
        axis=axis,
        maturity_stage=stage,
        bucket=bucket,
        actor_names=actor_names,
        source_url=source_url,
        quote=quote,
        field_confidence=0.9,
        project_name=project_name,
        source_title=source_title,
        funding_scope=funding_scope,
        funding_program=funding_program,
    )
    source_added = upsert_fact_source(
        tech_db, "technology_signal_sources", signal_id,
        source_url=source_url, source_title=source_title, quote=quote,
        language=language, field_confidence=0.9,
        fingerprint=hashlib.sha256(f"{fact_key}|{source_url}|{quote}".encode()).hexdigest(),
        created_at=utc_now(),
    )
    return added, source_added


def _write_anr(actors_db, tech_db, found: dict[str, dict[str, Any]], aliases: dict[str, str],
               actors_by_name: dict[str, int], report: dict[str, Any]) -> None:
    for code, entry in found.items():
        record, actor_names = entry["record"], sorted(entry["actors"])
        source_url = _anr_project_url(record)
        label = (record.get("acronyme") or "").strip() or code
        title = (record.get("titre") or "").strip()
        program = (record.get("programme") or "").strip()
        try:
            for actor_name in actor_names:
                actor_id = actors_by_name[actor_name]
                description = f"Participation au projet national ANR {label} ({title})".strip()[:500]
                report["events_added"] += upsert_actor_event(
                    actors_db, actor_id, EVENT_ANR, description, source_url=source_url,
                    # `date_de_debut` est une ANNÉE dans ce jeu de données ("2007"), pas une
                    # date complète : elle est écrite telle quelle plutôt que complétée d'un
                    # 01-01 qui ferait passer une approximation pour un jour connu.
                    event_date=str(record.get("date_de_debut") or "") or None,
                    # Fiche projet publique et stable sur anr.fr, comme une page CORDIS.
                    review_status="verified",
                )
                own_alias = aliases[actor_name]
                for partner in _anr_partner_names(record)[:MAX_PARTNERS_PER_PROJECT]:
                    if _contains_whole_phrase(_normalize_org_text(partner), own_alias):
                        continue  # la ligne de l'acteur lui-même, pas un partenaire
                    related_actor_id = actors_by_name.get(_find_tracked_actor(partner, aliases) or "")
                    note = f"Consortium ANR {label}"[:240]
                    report["relations_added"] += _upsert_actor_relation(
                        actors_db, actor_id, partner[:180], related_actor_id, note, source_url,
                    )

            added, source_added = _project_signal(
                tech_db,
                text=_anr_text(record),
                project_name=label,
                source_url=source_url,
                source_title=title or None,
                actor_names=actor_names,
                funding_scope="national",
                funding_program=f"ANR — {program}" if program else "ANR",
                language="fr",
            )
            report["signals_added"] += added
            report["signal_sources_added"] += source_added
        except Exception:
            report["errors"] += 1


def _write_gtr(actors_db, tech_db, found: dict[str, dict[str, Any]], aliases: dict[str, str],
               actors_by_name: dict[str, int], report: dict[str, Any]) -> None:
    for entry in found.values():
        project, actor_names = entry["project"], sorted(entry["actors"])
        reference = _gtr_reference(project)
        source_url = GTR_PROJECT_URL_TEMPLATE.format(ref=reference)
        title = (project.get("title") or "").strip()
        funder = (project.get("leadFunder") or "").strip() or "UKRI"
        scope = _gtr_scope(funder)
        scope_label = SCOPE_LABELS[scope]
        try:
            for actor_name in actor_names:
                actor_id = actors_by_name[actor_name]
                description = f"Participation au projet {scope_label} {funder} {title}".strip()[:500]
                report["events_added"] += upsert_actor_event(
                    actors_db, actor_id, EVENT_GTR, description, source_url=source_url,
                    # GtR ne renseigne `start` que pour une partie des projets (les projets
                    # Innovate UK anciens n'en ont pas) : pas de date plutôt qu'une date déduite
                    # de la date de création de la fiche.
                    event_date=(project.get("start") or None),
                    review_status="verified",
                )
                own_alias = aliases[actor_name]
                for partner in _gtr_partners(project)[:MAX_PARTNERS_PER_PROJECT]:
                    if _contains_whole_phrase(_normalize_org_text(partner["name"]), own_alias):
                        continue
                    related_actor_id = actors_by_name.get(_find_tracked_actor(partner["name"], aliases) or "")
                    note = f"Consortium {funder} ({partner['role'].lower()})"[:240]
                    report["relations_added"] += _upsert_actor_relation(
                        actors_db, actor_id, partner["name"][:180], related_actor_id, note, source_url,
                    )

            added, source_added = _project_signal(
                tech_db,
                text=_gtr_text(project),
                project_name=title or reference or "",
                source_url=source_url,
                source_title=title or None,
                actor_names=actor_names,
                funding_scope=scope,
                funding_program=funder,
                language="en",
            )
            report["signals_added"] += added
            report["signal_sources_added"] += source_added
        except Exception:
            report["errors"] += 1


def _write_mentions(actors_db, mentions: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for mention in mentions:
        scope_label = SCOPE_LABELS.get(mention["scope"], mention["scope"])
        # Plusieurs libellés nomment déjà leur pays ("BMBF / BMWK (Allemagne)") : le répéter
        # donnait "BMBF / BMWK (Allemagne) (Allemagne)".
        guichet = mention["program"]
        if mention["country"].lower() not in guichet.lower():
            guichet = f"{guichet} ({mention['country']})"
        description = (
            f"Projet {scope_label} — {guichet} "
            f"mentionné sur le site de l'acteur : « {mention['quote']} »"
        )[:500]
        try:
            report["mentions_added"] += upsert_actor_event(
                actors_db, mention["actor_id"], EVENT_MENTION, description,
                source_url=mention["url"],
                # Une page peut citer plusieurs programmes ; la source seule ne les distingue
                # pas (même raison que les pages carrières, voir upsert_actor_event).
                dedupe_on_description=True,
                # Un nom de programme trouvé dans une page n'est pas une fiche projet : la
                # page peut citer un appel auquel l'acteur a candidaté, un partenaire financé,
                # ou son propre financement structurel. C'est un signal à relire.
                review_status="pending",
            )
        except Exception:
            report["errors"] += 1


def collect_national_projects(*, include_sources: tuple[str, ...] = ("anr", "gtr", "mentions")) -> dict:
    """Point d'entrée principal (voir app.py: collectors["national_projects"]).

    ``include_sources`` n'existe que pour les tests et pour un rattrapage ciblé ; en collecte
    normale les trois passes tournent. Chacune est isolée : ANR indisponible ne doit pas priver
    l'utilisateur des projets UKRI, ni des mentions, qui ne demandent même pas de réseau.
    """
    report: dict[str, Any] = {
        "actors_matched": 0, "projects_matched": 0, "projects_off_topic": 0,
        "events_added": 0, "relations_added": 0, "signals_added": 0, "signal_sources_added": 0,
        "pages_scanned": 0, "mentions_added": 0, "anr_projects_truncated": 0, "errors": 0,
    }
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT id,name FROM actors WHERE active=1").fetchall()]
    aliases = _tracked_aliases(actors)
    actors_by_name = {actor["name"]: actor["id"] for actor in actors}

    anr_found: dict[str, dict[str, Any]] = {}
    gtr_found: dict[str, dict[str, Any]] = {}
    # Tout le réseau d'abord, l'écriture ensuite : une transaction SQLite ouverte pendant des
    # dizaines de requêtes HTTP bloquerait les autres écrivains (même raison que cordis.py,
    # qui télécharge son ZIP avant d'ouvrir sa connexion).
    if {"anr", "gtr"} & set(include_sources):
        try:
            with connector_client("api") as client:
                if "anr" in include_sources:
                    anr_found = _collect_anr(client, aliases, report)
                if "gtr" in include_sources:
                    gtr_found = _collect_gtr(client, aliases, report)
        except Exception as error:
            report["error"] = str(error)[:300]
            report["errors"] += 1

    mentions: list[dict[str, Any]] = []
    if "mentions" in include_sources:
        try:
            mentions = _collect_program_mentions(report)
        except Exception:
            report["errors"] += 1

    report["projects_matched"] = len(anr_found) + len(gtr_found)
    report["actors_matched"] = len(
        {name for entry in (*anr_found.values(), *gtr_found.values()) for name in entry["actors"]}
    )

    with connect(ACTORS_DB) as actors_db, connect(TECH_DB) as tech_db:
        _write_anr(actors_db, tech_db, anr_found, aliases, actors_by_name, report)
        _write_gtr(actors_db, tech_db, gtr_found, aliases, actors_by_name, report)
        _write_mentions(actors_db, mentions, report)
    return report
