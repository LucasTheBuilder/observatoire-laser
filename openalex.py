"""OpenAlex : publications scientifiques rattachées aux acteurs suivis (chantier 3 de l'audit).

Contrairement à scrapers.scrape_technology() (6 requêtes thématiques génériques sur Crossref,
jamais attribuées à un acteur -- documents.actor_name toujours NULL, même quand le collecteur
tourne), ce module cherche les publications PAR institution : pour chaque acteur actif, il
cherche l'institution OpenAlex correspondante, ne l'accepte que si son ``homepage_url`` partage
le même domaine que ``actors.official_url`` (le site officiel de l'acteur, déjà vérifié quand
l'acteur a été ajouté), puis récupère ses publications récentes.

Cette vérification par domaine est ce qui rend le module sûr SANS liste d'alias à courir à la
main (contrairement à cordis.py/firmographics.py, qui ont dû se rabattre sur une petite liste
vérifiée un par un faute d'un signal de vérification automatique fiable) : ``official_url`` a
déjà été validé par un humain à la création de l'acteur, donc un domaine identique est une
preuve d'identité forte, vérifiée manuellement sur un échantillon avant d'écrire ce module
(ALPHANOV, IREPA LASER, Fraunhofer ILT, AIMEN, Laser Zentrum Hannover, CEIT, Tekniker
résolvent tous correctement par ce critère ; HiLASE/MANUTECH USD/FEMTO Engineering/Pulsar
Photonics ne matchent simplement aucune institution OpenAlex -- ignorés, jamais devinés).

À la différence de CORDIS (une seule entité Fraunhofer-Gesellschaft pour tous les instituts),
OpenAlex indexe Fraunhofer ILT comme institution à part entière (homepage propre), donc ce
module PEUT lui attribuer des publications en toute sécurité.

La vérification par domaine protège l'identité de l'institution, pas la pertinence du contenu :
pour un institut généraliste, les OPENALEX_WORKS_PER_ACTOR publications les plus récentes
peuvent n'avoir aucun rapport avec le laser. _work_is_on_topic() filtre donc chaque titre avec
les mêmes lexiques que le reste du pipeline (scrapers.LASER_RULES/PROCESS_TECHNOLOGIES), comme
scrapers.scrape_technology() le fait déjà pour Crossref -- voir audit v8 §2.1.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from actor_discovery import upsert_actor_candidate
from db import ACTORS_DB, TECH_DB, compute_is_backfill, connect, utc_now
from scrapers import CRAWLER_CONTACT, HEADERS, is_on_topic, upsert_document_technology_signal

OPENALEX_API = "https://api.openalex.org"
OPENALEX_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
OPENALEX_LOOKBACK_DAYS_DEFAULT = 365
OPENALEX_WORKS_PER_ACTOR = 25


def _domain(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return host.removeprefix("www.")


def _mailto_params() -> dict[str, str]:
    """OpenAlex's "polite pool" (faster, more reliable responses) just wants a contact email
    on every request -- reuses the same CRAWLER_CONTACT env var scrapers.py already puts in
    its own User-Agent, rather than a second contact setting."""
    return {"mailto": CRAWLER_CONTACT} if CRAWLER_CONTACT else {}


def _find_institution(client: httpx.Client, actor_name: str, official_url: str) -> dict | None:
    """Cherche l'institution OpenAlex correspondant à cet acteur, et ne l'accepte QUE si son
    homepage_url partage le même domaine que le site officiel déjà vérifié de l'acteur --
    voir le docstring du module. Ambiguïté (0 ou plusieurs domaines correspondants) => None,
    jamais un choix arbitraire.
    """
    target_domain = _domain(official_url)
    if not target_domain:
        return None
    try:
        response = client.get(
            f"{OPENALEX_API}/institutions",
            params={"search": actor_name, "per_page": 5, **_mailto_params()},
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except Exception:
        return None
    matches = [row for row in results if row.get("homepage_url") and _domain(row["homepage_url"]) == target_domain]
    return matches[0] if len(matches) == 1 else None


def _fetch_recent_works(client: httpx.Client, institution_id: str, from_date: str) -> list[dict]:
    try:
        response = client.get(
            f"{OPENALEX_API}/works",
            params={
                "filter": f"authorships.institutions.id:{institution_id},from_publication_date:{from_date}",
                "sort": "publication_date:desc",
                "per-page": OPENALEX_WORKS_PER_ACTOR,
                "select": "id,doi,title,publication_date,primary_location,authorships",
                **_mailto_params(),
            },
        )
        response.raise_for_status()
        return response.json().get("results", [])
    except Exception:
        return []


def _work_is_on_topic(title: str) -> bool:
    """Une publication n'est retenue que si son titre relève du laser ultra-rapide, via
    scrapers.is_on_topic() -- même filtre que cordis.py, voir son docstring pour le détail des
    lexiques utilisés. Même principe que scrapers.scrape_technology() pour Crossref. Sans ce
    filtre, _fetch_recent_works ne fait que trier par date de publication : pour un institut
    généraliste, les publications les plus récentes sur n'importe quel sujet (poultry farming,
    fusion inertielle, Six Sigma...) écrasent mécaniquement la production laser -- voir audit v8
    §2.1 (123/203 publications hors sujet avant ce filtre).
    """
    return is_on_topic(title)


def _co_institutions(work: dict, exclude_institution_id: str) -> list[str]:
    """§4.D audit veille (30/08/2026, Lot 3 §3.2) : institutions co-autrices d'un travail
    on-topic d'un acteur suivi, hors l'institution de l'acteur lui-même -- signal de découverte
    d'acteur (voir actor_discovery.py), pas un signal marché/technologie."""
    names: list[str] = []
    for authorship in work.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            institution_id = str(institution.get("id") or "").rsplit("/", 1)[-1]
            display_name = str(institution.get("display_name") or "").strip()
            if display_name and institution_id != exclude_institution_id:
                names.append(display_name)
    return names


def _parse_work(work: dict) -> dict | None:
    title = str(work.get("title") or "").strip()
    if not title:
        return None
    doi = work.get("doi")
    if doi:
        doi = str(doi).removeprefix("https://doi.org/")
    location = work.get("primary_location") or {}
    url = location.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else None) or work.get("id")
    if not url:
        return None
    return {"title": title, "url": url, "doi": doi, "published_at": work.get("publication_date")}


def _upsert_document(db, actor_name: str, item: dict) -> tuple[int, int]:
    """Renvoie (1 si nouvelle ligne insérée, 1 si actor_name vient d'être renseigné sur une
    ligne déjà existante) -- ce deuxième cas est le correctif central de ce module : une
    publication déjà vue par scrape_technology() (Crossref générique, jamais attribué) reçoit
    enfin son actor_name plutôt que de rester orpheline."""
    fingerprint = hashlib.sha256((item["doi"] or item["url"]).casefold().encode()).hexdigest()
    stamp = utc_now()
    # Revue chronologie du 30/08/2026 : published_at vient toujours d'un champ structuré de
    # l'API OpenAlex (voir _parse_work ci-dessus), jamais d'un repli fabriqué -- donc, à la
    # différence de evidence/offers (scrapers.py), 'published'/'observed_only' peut être décidé
    # sans ambiguïté dès cette écriture. is_backfill compare cette date à `stamp`, qui EST le
    # created_at de la ligne (première insertion, jamais réécrite ensuite).
    date_confidence = "published" if item.get("published_at") else "observed_only"
    is_backfill = compute_is_backfill(stamp, item.get("published_at"), date_confidence)
    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO documents(
               actor_name,document_type,title,source_url,published_at,doi,date_confidence,is_backfill,fingerprint,created_at,last_seen_at
           ) VALUES(?,'publication',?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, item["title"], item["url"], item["published_at"], item["doi"],
            date_confidence, is_backfill, fingerprint, stamp, stamp,
        ),
    )
    inserted = int(db.total_changes > before)
    attributed = 0
    if inserted:
        return 1, 0
    db.execute("UPDATE documents SET last_seen_at=? WHERE fingerprint=?", (stamp, fingerprint))
    row = db.execute("SELECT actor_name FROM documents WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row and not row["actor_name"]:
        db.execute("UPDATE documents SET actor_name=? WHERE fingerprint=?", (actor_name, fingerprint))
        attributed = 1
    return 0, attributed


def collect_openalex_publications(lookback_days: int = OPENALEX_LOOKBACK_DAYS_DEFAULT) -> dict:
    """Point d'entrée (voir app.py: collectors["openalex"])."""
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT name,official_url FROM actors WHERE active=1").fetchall()]
    from_date = (date.today() - timedelta(days=max(1, lookback_days))).isoformat()

    with connect(TECH_DB) as db:
        run_id = db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (utc_now(), "running")).lastrowid

    matched_actors = added = attributed = off_topic = candidates_added = technology_signals_added = errors = 0
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=OPENALEX_TIMEOUT) as client:
        for actor in actors:
            try:
                institution = _find_institution(client, actor["name"], actor["official_url"])
                if not institution:
                    continue
                matched_actors += 1
                institution_id = str(institution["id"]).rsplit("/", 1)[-1]
                works = _fetch_recent_works(client, institution_id, from_date)
                with connect(TECH_DB) as db, connect(ACTORS_DB) as adb:
                    for work in works:
                        item = _parse_work(work)
                        if not item:
                            continue
                        if not _work_is_on_topic(item["title"]):
                            off_topic += 1
                            continue
                        inserted, was_attributed = _upsert_document(db, actor["name"], item)
                        added += inserted
                        attributed += was_attributed
                        # §4.C.2 audit veille (Lot 3 §3.5) : titre seul, comme _work_is_on_topic
                        # ci-dessus -- OpenAlex n'expose l'abstract qu'en index inversé, pas
                        # demandé ici (même choix que le reste de ce module : pas de nouveau
                        # champ API tant que le titre suffit).
                        technology_signals_added += upsert_document_technology_signal(
                            db, item["title"], "", item["url"], actor["name"],
                        )
                        # §4.D audit veille (Lot 3 §3.2) : une institution co-autrice récurrente
                        # sur des travaux on-topic est un candidat acteur -- voir actor_discovery.py.
                        for co_name in _co_institutions(work, institution_id):
                            candidates_added += upsert_actor_candidate(
                                adb, co_name, "openalex",
                                context=f"co-auteur avec {actor['name']} sur un travail on-topic",
                                source_url=item["url"],
                            )
            except Exception:
                errors += 1

    with connect(TECH_DB) as db:
        db.execute(
            "UPDATE collection_runs SET finished_at=?,status=?,scanned=?,added=?,errors=?,message=? WHERE id=?",
            (
                utc_now(), "completed", matched_actors, added, errors,
                f"OpenAlex : {matched_actors} institutions vérifiées par domaine, {added} nouvelles publications, "
                f"{attributed} déjà connues ré-attribuées à un acteur, {off_topic} hors sujet filtrées, "
                f"{candidates_added} candidats acteurs (co-institutions), {technology_signals_added} signaux technologiques",
                run_id,
            ),
        )
    return {
        "actors_matched": matched_actors,
        "documents_added": added,
        "documents_attributed": attributed,
        "documents_off_topic": off_topic,
        "actor_candidates_added": candidates_added,
        "technology_signals_added": technology_signals_added,
        "errors": errors,
    }
