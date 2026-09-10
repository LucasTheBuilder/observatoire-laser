"""Intégration CORDIS (chantier 3 de l'audit collecte) : projets européens Horizon Europe.

Sort l'observatoire du mono-source (chantier 1/2 ne touchaient qu'au site web de l'acteur) en
croisant le jeu de données ouvert CORDIS "HORIZON Projects" avec les acteurs suivis, pour
remplir -- sans jamais deviner -- trois tables jusque-là vides ou saisies à la main :

- actor_events    : une ligne "participation au projet X" datée, sourcée sur la page CORDIS.
- actor_relations : les autres organisations du même consortium (partenaires réels observés,
                     pas des suppositions).
- technology_signals : un axe technologique (SLE, LIPSS, TGV...) SEULEMENT quand le titre/
                     objectif du projet contient explicitement un terme du lexique
                     TECHNOLOGY_AXES (procédé + capacité machine), déjà utilisé par scrapers.py
                     -- jamais un axe inventé à partir du seul nom du projet.

Source de données : le jeu "HORIZON Projects" (CSV), publié mensuellement par CORDIS sur
data.europa.eu (URL confirmée manuellement, pas de clé API requise), téléchargé et mis en cache
localement -- pas de re-téléchargement à chaque run. Portée volontairement limitée à Horizon
Europe (2021-2027) ; H2020/FP7 sont des jeux de données séparés, hors scope de cette première
intégration (voir data.europa.eu pour les étendre de la même façon si besoin).

Limite documentée (voir CORDIS_MATCH_BLOCKLIST) : Fraunhofer-Gesellschaft est UNE seule entité
juridique dans CORDIS -- tous ses instituts (dont ILT) partagent la même organisationID et la
même adresse de Munich, sans aucun moyen de distinguer une participation spécifique à un
institut. Associer les projets de l'entité mère à "Fraunhofer ILT" serait une sur-attribution
non vérifiable ; le nom est donc explicitement exclu du matching automatique plutôt que deviné.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import unicodedata
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from actor_discovery import upsert_actor_candidate
from db import (
    ACTORS_DB,
    DATA_DIR,
    TECH_DB,
    connect,
    technology_signal_key,
    upsert_actor_event,
    upsert_fact_source,
    upsert_technology_signal,
    utc_now,
)
from http_client import connector_client
from lexicon import TECHNOLOGY_AXES, best_quote, detect_maturity, is_on_topic, match_label_details

# Les trois programmes-cadres publiés par CORDIS, du plus récent au plus ancien. Jusqu'au
# 10/09/2026 seul HORIZON était lu, ce qui rendait invisible tout l'historique : sur les 66
# projets du cœur procédé recensés par l'audit du 10/09/2026, 51 sont H2020 ou FP7. Un
# observatoire qui ne voit que le programme en cours ne peut ni tracer une trajectoire d'acteur
# sur quinze ans, ni dire qu'un sujet est ancien.
#
# L'ordre compte : _project_details et les collecteurs parcourent les caches dans cet ordre et
# gardent la PREMIÈRE occurrence d'un identifiant. Un projet prolongé d'un programme à l'autre
# garde ainsi sa fiche la plus récente.
CORDIS_PROGRAMMES: tuple[tuple[str, str], ...] = (
    ("horizon", "https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip"),
    ("h2020", "https://cordis.europa.eu/data/cordis-h2020projects-csv.zip"),
    ("fp7", "https://cordis.europa.eu/data/cordis-fp7projects-csv.zip"),
)
CORDIS_CACHE_DIR = DATA_DIR / "cordis_cache"

# Conservés pour les appelants (et les tests) qui ne raisonnent que sur le programme courant.
CORDIS_PROJECTS_ZIP_URL = CORDIS_PROGRAMMES[0][1]
CORDIS_CACHE_PATH = CORDIS_CACHE_DIR / "horizon_projects.zip"
# CORDIS republie ce jeu de données une fois par mois (voir la page du dataset sur
# data.europa.eu, "Accrual Periodicity: monthly") -- inutile de le re-télécharger plus souvent.
CORDIS_CACHE_MAX_AGE_DAYS = 25
# Telechargement du dump complet : profil "bulk" de http_client (voir TIMEOUTS).

# Alias explicite quand le nom légal utilisé par CORDIS diffère du nom suivi dans l'app --
# même principe que site_profiles.SITE_OVERRIDES : la liste reste courte et volontaire, jamais
# une tentative de fuzzy-matching générique.
CORDIS_NAME_ALIASES: dict[str, str] = {
    "IREPA LASER": "IREPA",
    "Laser Zentrum Hannover": "LASER ZENTRUM HANNOVER",
}

# Acteurs dont le nom CORDIS ne peut pas être distingué de façon fiable d'une entité plus large
# (voir la note Fraunhofer dans le docstring du module) : jamais matchés automatiquement.
CORDIS_MATCH_BLOCKLIST: set[str] = {"Fraunhofer ILT"}

# Garde-fou anti faux positifs : un alias plus court que ça (une fois normalisé) est trop
# générique pour être cherché sans risque dans ~300k lignes d'organisations.
MIN_ALIAS_LENGTH = 4

# Un consortium européen peut compter des dizaines de partenaires (grands projets
# d'infrastructure) ; ne retenir que les plus significatifs évite de noyer actor_relations.
MAX_PARTNERS_PER_PROJECT = 15
_ROLE_PRIORITY = {"coordinator": 0, "participant": 1, "associatedPartner": 2}


def _normalize_org_text(value: str) -> str:
    """Majuscules, sans accents, ponctuation réduite à des espaces simples -- pour que deux
    graphies d'un même nom d'organisation (avec/sans accent, tiret/espace) se comparent égales."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def _actor_alias(actor_name: str) -> str:
    return _normalize_org_text(CORDIS_NAME_ALIASES.get(actor_name, actor_name))


def _contains_whole_phrase(haystack: str, phrase: str) -> bool:
    """True si `phrase` apparaît dans `haystack` à des frontières de mot (haystack/phrase déjà
    normalisés par _normalize_org_text) -- évite qu'un alias de 4 lettres matche à l'intérieur
    d'un mot plus long sans rapport."""
    if not phrase:
        return False
    return re.search(rf"(?<![A-Z0-9]){re.escape(phrase)}(?![A-Z0-9])", haystack) is not None


def _project_url(project_id: str) -> str:
    return f"https://cordis.europa.eu/project/id/{project_id}"


def _project_is_on_topic(project: dict[str, str]) -> bool:
    """Un projet CORDIS n'est retenu comme événement/relation observatoire que s'il relève du
    laser ultra-rapide, jugé sur son titre + objectif via scrapers.is_on_topic() -- le même
    filtre que celui appliqué aux publications OpenAlex, voir son docstring pour le détail des
    lexiques utilisés. Auparavant ce filtre n'existait que pour technology_signals (voir
    _upsert_technology_signal plus bas) : un centre technologique généraliste matché par alias
    (Tekniker, CEIT) faisait donc remonter TOUS ses projets européens -- recyclage, ferroviaire,
    sciences sociales... -- comme événements/partenariats de l'observatoire laser. Voir audit v8
    §2.1 (73/83 projets CORDIS hors sujet, 84% du graphe réseau issu de ces deux seuls acteurs).
    """
    text = f"{project.get('title') or ''} {project.get('objective') or ''}"
    return is_on_topic(text)


def programme_cache_path(programme: str) -> Path:
    """Le cache local d'un programme-cadre. "horizon" garde son nom historique, pour ne pas
    invalider un cache de 37 Mo déjà téléchargé chez l'utilisateur."""
    name = "horizon_projects.zip" if programme == "horizon" else f"{programme}_projects.zip"
    return CORDIS_CACHE_DIR / name


def _ensure_programme_cache(client: httpx.Client, programme: str, url: str) -> Path:
    """Télécharge le ZIP d'un programme-cadre si le cache local est absent ou trop vieux.

    Téléchargement en streaming vers un fichier temporaire puis renommage atomique, pour
    qu'un run interrompu (crash, timeout) ne laisse jamais un cache à moitié écrit derrière lui.
    """
    path = programme_cache_path(programme)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days < CORDIS_CACHE_MAX_AGE_DAYS:
            return path
    tmp_path = path.with_suffix(".zip.part")
    # Pas de timeout par requête : le client est construit avec le profil "bulk" de
    # http_client (180 s), calibré précisément pour ce téléchargement.
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
    tmp_path.replace(path)
    return path


def ensure_all_caches(client: httpx.Client) -> list[Path]:
    """Les trois caches, dans l'ordre de CORDIS_PROGRAMMES.

    Un programme qui échoue au téléchargement ne prive pas des autres : FP7 est un dump figé
    depuis 2013 et son indisponibilité ponctuelle ne doit pas bloquer la collecte du programme
    en cours. Renvoie les caches réellement disponibles.
    """
    caches: list[Path] = []
    for programme, url in CORDIS_PROGRAMMES:
        try:
            caches.append(_ensure_programme_cache(client, programme, url))
        except Exception:
            existing = programme_cache_path(programme)
            if existing.exists():
                caches.append(existing)
    return caches


def _ensure_cache(client: httpx.Client) -> Path:
    """Le cache du programme en cours, pour les appelants qui n'en veulent qu'un."""
    return _ensure_programme_cache(client, *CORDIS_PROGRAMMES[0])


def _csv_rows(zip_path: Path, member: str) -> Iterator[dict[str, str]]:
    """Lit un membre CSV (séparateur ';', comme tous les exports CORDIS) du ZIP en streaming,
    sans jamais charger le fichier entier en mémoire -- organization.csv/project.csv font
    plusieurs dizaines de Mo chacun pour l'ensemble d'Horizon Europe."""
    with zipfile.ZipFile(zip_path) as archive, archive.open(member) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter=";")


def _actor_aliases(actors: list[dict[str, Any]]) -> dict[str, str]:
    return {
        actor["name"]: _actor_alias(actor["name"])
        for actor in actors
        if actor["name"] not in CORDIS_MATCH_BLOCKLIST and len(_actor_alias(actor["name"])) >= MIN_ALIAS_LENGTH
    }


def _match_projects(zip_path: Path, aliases: dict[str, str]) -> dict[str, set[str]]:
    """Passe 1 (lecture complète d'organization.csv, une seule fois) : pour chaque acteur
    suivi, l'ensemble des project_id où son alias apparaît comme organisation participante."""
    matches: dict[str, set[str]] = {name: set() for name in aliases}
    for row in _csv_rows(zip_path, "organization.csv"):
        project_id = row.get("projectID")
        org_name = _normalize_org_text(row.get("name", ""))
        if not project_id or not org_name:
            continue
        for actor_name, alias in aliases.items():
            if _contains_whole_phrase(org_name, alias):
                matches[actor_name].add(project_id)
    return {name: ids for name, ids in matches.items() if ids}


def _find_tracked_actor(related_name: str, aliases: dict[str, str]) -> str | None:
    """Un partenaire de consortium est-il LUI-MÊME un acteur suivi ? Réutilise les mêmes alias
    que _match_projects, pour que actor_relations.related_actor_id se remplisse quand c'est le
    cas (ex: deux acteurs suivis coparticipants d'un même projet)."""
    normalized = _normalize_org_text(related_name)
    for actor_name, alias in aliases.items():
        if _contains_whole_phrase(normalized, alias):
            return actor_name
    return None


def discover_topic_scoped_candidates(zip_path: Path, aliases: dict[str, str]) -> int:
    """Découverte d'acteurs TOPIC-scoped (extension du §4.D audit veille, 30/08/2026, Lot 3
    §3.2/§3.3 : "élargir le périmètre hors Europe").

    _match_projects (et donc actor_relations/actor_candidates via actor_discovery._discover_
    from_cordis) est ACTOR-scoped : il ne trouve que les consortiums qui contiennent DÉJÀ un
    acteur suivi. Un projet européen 100% hors du réseau connu -- ex: seuls participants une
    université coréenne et une PME japonaise sur un projet d'usinage laser ultra-rapide -- est
    donc invisible à ce jour, quel que soit son degré de pertinence. Vérifié sur les données
    réelles avant d'écrire cette fonction : organization.csv couvre bien 190 pays (1765 lignes
    US, 287 CN, 240 JP, 145 KR...), CORDIS n'est donc pas un jeu de données limité à l'Europe --
    seule la façon dont il était interrogé l'était.

    Deux passes complètes supplémentaires (comme _match_projects/_consortiums_for_projects) :
    project.csv pour les project_id on-topic (titre+objectif, is_on_topic -- même filtre que
    _project_is_on_topic), puis organization.csv pour toutes leurs organisations, candidates SAUF
    celles qui matchent déjà un alias suivi. country/organizationURL sont capturés tels quels
    (jamais devinés) pour préremplir une promotion -- voir actor_discovery.upsert_actor_candidate.
    """
    on_topic_project_ids: set[str] = set()
    for row in _csv_rows(zip_path, "project.csv"):
        project_id = row.get("id")
        if not project_id:
            continue
        text = f"{row.get('title') or ''} {row.get('objective') or ''}"
        if is_on_topic(text):
            on_topic_project_ids.add(project_id)
    if not on_topic_project_ids:
        return 0

    added = 0
    with connect(ACTORS_DB) as db:
        for row in _csv_rows(zip_path, "organization.csv"):
            project_id = row.get("projectID")
            if project_id not in on_topic_project_ids:
                continue
            org_name = (row.get("name") or "").strip()
            if not org_name:
                continue
            if _find_tracked_actor(org_name, aliases):
                continue
            added += upsert_actor_candidate(
                db, org_name, "cordis",
                context=f"Projet CORDIS on-topic {project_id} ({row.get('country') or '?'})",
                source_url=_project_url(project_id),
                country=(row.get("country") or None),
                suggested_official_url=(row.get("organizationURL") or None),
            )
    return added


def _consortiums_for_projects(zip_path: Path, project_ids: set[str]) -> dict[str, list[dict[str, str]]]:
    """Passe 2 (deuxième lecture complète d'organization.csv) : la liste des organisations
    participantes pour, seulement, les projets déjà identifiés comme pertinents en passe 1 --
    borne la mémoire à O(projets pertinents) au lieu de O(tout Horizon Europe)."""
    consortiums: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not project_ids:
        return consortiums
    for row in _csv_rows(zip_path, "organization.csv"):
        project_id = row.get("projectID")
        if project_id in project_ids:
            consortiums[project_id].append(row)
    for rows in consortiums.values():
        rows.sort(key=lambda row: _ROLE_PRIORITY.get(row.get("role", ""), 9))
    return consortiums


def available_caches() -> list[Path]:
    """Les caches de programme présents localement, du plus récent au plus ancien.

    Sans effet de bord : ne télécharge rien. Sert aux lecteurs (relecture d'un projet déjà en
    base, nettoyage rétroactif) qui doivent pouvoir retrouver un projet quel que soit son
    programme, sans déclencher 125 Mo de téléchargement au passage.
    """
    return [path for programme, _ in CORDIS_PROGRAMMES if (path := programme_cache_path(programme)).exists()]


def project_details_across_programmes(project_ids: set[str], caches: list[Path] | None = None) -> dict[str, dict[str, str]]:
    """Relit des projets par identifiant dans TOUS les caches disponibles.

    Un identifiant CORDIS est unique tous programmes confondus, mais un projet n'existe que
    dans le dump de SON programme : chercher dans le seul cache Horizon ne retrouvait donc
    aucun projet H2020 ou FP7 -- soit 51 des 66 projets du cœur procédé recensés par l'audit du
    10/09/2026. La première occurrence gagne (voir l'ordre de CORDIS_PROGRAMMES).
    """
    remaining = set(project_ids)
    found: dict[str, dict[str, str]] = {}
    for zip_path in caches if caches is not None else available_caches():
        if not remaining:
            break
        for project_id, row in _project_details(zip_path, remaining).items():
            found[project_id] = row
        remaining -= set(found)
    return found


def _project_details(zip_path: Path, project_ids: set[str]) -> dict[str, dict[str, str]]:
    """Une lecture de project.csv, filtrée aux mêmes projets pertinents."""
    details: dict[str, dict[str, str]] = {}
    if not project_ids:
        return details
    for row in _csv_rows(zip_path, "project.csv"):
        if row.get("id") in project_ids:
            details[row["id"]] = row
    return details


def _upsert_actor_relation(db, actor_id: int, related_name: str, related_actor_id: int | None, note: str, source_url: str) -> int:
    """Dédoublonne sur (actor_id, related_name) plutôt que par projet : si les deux mêmes
    organisations se retrouvent dans plusieurs consortiums au fil des runs, une seule ligne de
    relation suffit (le premier projet observé reste la source affichée)."""
    existing = db.execute(
        "SELECT id FROM actor_relations WHERE actor_id=? AND related_name=?", (actor_id, related_name),
    ).fetchone()
    if existing:
        return 0
    db.execute(
        """INSERT INTO actor_relations(actor_id,related_actor_id,related_name,relation_type,note,source_url,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (actor_id, related_actor_id, related_name, "partner", note, source_url, utc_now()),
    )
    return 1


def _upsert_technology_signal(db, *, axis: str, project: dict[str, str], actor_names: list[str], quote: str) -> tuple[int, int]:
    """Même schéma d'upsert que scrapers._upsert_market_candidate : une ligne technology_signals
    par (axis, projet) -- voir db.technology_signal_key --, potentiellement partagée entre
    plusieurs acteurs suivis d'un même consortium (actor_names fusionne au lieu de dupliquer)."""
    stamp = utc_now()
    project_name = project.get("acronym") or project.get("title") or project["id"]
    fact_key = technology_signal_key(axis, project_name)
    bucket, stage = detect_maturity(project.get("objective") or "")
    if bucket == "unknown":
        bucket = "radar"  # projet de R&D financé par l'UE : pré-industriel par défaut, jamais "existing" par supposition
    source_url = _project_url(project["id"])

    added, signal_id = upsert_technology_signal(
        db,
        fact_key=fact_key,
        axis=axis,
        maturity_stage=stage,
        bucket=bucket,
        actor_names=actor_names,
        source_url=source_url,
        quote=quote,
        field_confidence=0.9,
        project_name=project_name,
        source_title=project.get("title"),
    )

    source_added = upsert_fact_source(
        db, "technology_signal_sources", signal_id,
        source_url=source_url, source_title=project.get("title"), quote=quote,
        language="en", field_confidence=0.9,
        fingerprint=hashlib.sha256(f"{fact_key}|{source_url}|{quote}".encode()).hexdigest(),
        created_at=stamp,
    )
    return added, source_added


def collect_cordis(*, cache_path: Path | None = None, limit_projects: int | None = None) -> dict:
    """Point d'entrée principal (voir app.py: collectors["cordis"]).

    ``cache_path`` court-circuite le téléchargement/cache -- utilisé par les tests avec un
    petit fichier ZIP fabriqué à la main plutôt que le vrai jeu de données Horizon Europe.
    ``limit_projects`` borne le nombre de projets réellement écrits en base (la passe 1, qui
    doit lire tout organization.csv pour SAVOIR quels projets sont pertinents, n'est jamais
    bornée par ce paramètre -- il n'existe que pour garder les tests rapides côté écriture).
    """
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT id,name FROM actors WHERE active=1").fetchall()]
    aliases = _actor_aliases(actors)
    actors_by_name = {actor["name"]: actor["id"] for actor in actors}

    try:
        if cache_path is not None:
            zip_path = cache_path
        else:
            with connector_client("bulk") as client:
                zip_path = _ensure_cache(client)
    except Exception as exc:
        return {
            "error": str(exc)[:300], "actors_matched": 0, "projects_matched": 0, "projects_off_topic": 0,
            "events_added": 0, "relations_added": 0, "signals_added": 0, "signal_sources_added": 0,
            "topic_scoped_candidates_added": 0, "errors": 0,
        }

    matches = _match_projects(zip_path, aliases)
    project_actor_names: dict[str, set[str]] = defaultdict(set)
    for actor_name, project_ids in matches.items():
        for project_id in project_ids:
            project_actor_names[project_id].add(actor_name)
    all_project_ids = set(project_actor_names)
    if limit_projects is not None:
        all_project_ids = set(list(all_project_ids)[:limit_projects])
        project_actor_names = {pid: names for pid, names in project_actor_names.items() if pid in all_project_ids}

    consortiums = _consortiums_for_projects(zip_path, all_project_ids)
    projects = _project_details(zip_path, all_project_ids)

    events_added = relations_added = signals_added = signal_sources_added = errors = 0
    projects_off_topic = 0
    with connect(ACTORS_DB) as actors_db, connect(TECH_DB) as tech_db:
        for project_id, actor_names in project_actor_names.items():
            project = projects.get(project_id)
            if not project:
                continue
            if not _project_is_on_topic(project):
                # Le projet matche un acteur suivi (même alias d'organisation) mais son
                # titre/objectif ne relève pas du laser ultra-rapide : ni événement, ni
                # relation, ni signal techno n'est créé pour lui (voir _project_is_on_topic).
                projects_off_topic += 1
                continue
            source_url = _project_url(project_id)
            try:
                for actor_name in actor_names:
                    actor_id = actors_by_name[actor_name]
                    label = project.get("acronym") or project_id
                    description = f"Participation au projet européen {label} ({project.get('title') or ''})".strip()[:500]
                    events_added += upsert_actor_event(
                        actors_db, actor_id, "cordis_project", description, source_url=source_url,
                        event_date=project.get("startDate") or None,
                        # Participation a un projet europeen, sourcee sur une page CORDIS stable :
                        # pas un signal faible, contrairement a la presse ou aux offres d'emploi.
                        review_status="verified",
                    )

                    own_alias = aliases[actor_name]
                    for org in consortiums.get(project_id, [])[:MAX_PARTNERS_PER_PROJECT]:
                        related_name = (org.get("name") or "").strip()
                        short_name = (org.get("shortName") or "").strip()
                        # A consortium can list the actor's own entity under its full legal
                        # name (e.g. CORDIS registers LASEA as "Laser Engineering Applications
                        # SA", shortName "LASEA") -- the alias never appears as a substring of
                        # that legal name, so checking only `name` let the actor's own row slip
                        # through as if it were a distinct partner (a self-loop in the network
                        # graph: LASEA -> "Laser Engineering Applications SA"). Checking
                        # shortName too catches this without touching _match_projects' broader
                        # project-discovery matching, which is unaffected by this bug.
                        is_own_row = _contains_whole_phrase(_normalize_org_text(related_name), own_alias) or (
                            short_name and _contains_whole_phrase(_normalize_org_text(short_name), own_alias)
                        )
                        if not related_name or is_own_row:
                            continue  # skip the actor's own consortium row(s)
                        related_actor_id = actors_by_name.get(_find_tracked_actor(related_name, aliases) or "")
                        note = f"Consortium {label} ({org.get('role') or 'participant'})"[:240]
                        relations_added += _upsert_actor_relation(
                            actors_db, actor_id, related_name[:180], related_actor_id, note, source_url,
                        )

                axis, hits = match_label_details(f"{project.get('title') or ''} {project.get('objective') or ''}", TECHNOLOGY_AXES)
                if axis:
                    quote = best_quote(project.get("objective") or project.get("title") or "", hits)[:700]
                    if quote:
                        added, source_added = _upsert_technology_signal(
                            tech_db, axis=axis, project=project, actor_names=sorted(actor_names), quote=quote,
                        )
                        signals_added += added
                        signal_sources_added += source_added
            except Exception:
                errors += 1

    # §4.D/§10.8 audit veille (Lot 3 §3.2/§3.3) : passe indépendante, topic-scoped -- ne dépend
    # pas de project_actor_names (qui ne connaît que les consortiums d'un acteur déjà suivi).
    # Voir discover_topic_scoped_candidates pour le raisonnement complet.
    try:
        topic_scoped_candidates = discover_topic_scoped_candidates(zip_path, aliases)
    except Exception:
        topic_scoped_candidates = 0
        errors += 1

    return {
        "actors_matched": len(matches),
        "projects_matched": len(project_actor_names),
        "projects_off_topic": projects_off_topic,
        "events_added": events_added,
        "relations_added": relations_added,
        "signals_added": signals_added,
        "signal_sources_added": signal_sources_added,
        "topic_scoped_candidates_added": topic_scoped_candidates,
        "errors": errors,
    }
