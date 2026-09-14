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

import os
import re
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from actor_discovery import _known_actor_names_and_domains, _normalize_name, upsert_actor_candidate
from db import ACTORS_DB, TECH_DB, connect, resolve_unlinked_document, rows, upsert_document, utc_now
from http_client import CRAWLER_CONTACT, connector_client
from scrapers import TECHNOLOGY_QUERIES, is_on_topic, upsert_document_technology_signal

OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "").strip()
OPENALEX_API = "https://api.openalex.org"

OPENALEX_LOOKBACK_DAYS_DEFAULT = 365
# Taille de page, plus un plafond de sécurité -- ce n'était qu'un plafond de 25 jusqu'au
# 10/09/2026, et c'est ce qui rendait les gros instituts invisibles. _fetch_recent_works trie
# par DATE sur toute leur production, tous sujets confondus, puis coupait : mesuré sur 365
# jours, CEIT publie 119 travaux dont 3 sur le laser, et AUCUN des 3 n'était dans les 25 plus
# récents ; Fraunhofer ILT 95 dont 3, aucun ; IWS 4 dont 1 seul visible ; LZH 4 dont 2. Onze
# publications laser sur 35 perdues par le seul effet de la coupe.
#
# 100 aurait suffi sur ces mesures-là, mais c'est un coup de chance : Fraunhofer IPT publie
# 176 travaux par an, et une publication laser en 150e position serait manquée pareil. La
# lecture se pagine donc jusqu'au plafond de sécurité, qui ne sert qu'à borner un institut
# hors norme.
OPENALEX_WORKS_PER_PAGE = 100
OPENALEX_WORKS_PER_ACTOR = 600

# Vocabulaire laser envoyé À OPENALEX, pour aller chercher l'historique profond d'un institut
# sans se faire tronquer par son volume. Mesuré le 13/09/2026 sur dix ans et vingt institutions :
# 10 806 travaux tous sujets confondus, dont 7 instituts au-delà du plafond de 600 (Fraunhofer
# IPT 1 219, JOANNEUM 1 826, Tekniker 1 094) -- et 653 seulement avec ce filtre, tous atteignables.
# Sans lui, « tout savoir » sur un institut généraliste est impossible : ses publications laser
# sont noyées dans sa production, et la coupe tombe avant elles.
#
# Volontairement LARGE, et c'est sans risque : la sur-inclusion est rattrapée en local par
# _work_is_on_topic(), qui reste le seul juge de la pertinence. C'est l'inverse qui coûterait --
# un terme oublié ici est un travail jamais vu.
OPENALEX_TOPIC_SEARCH = (
    'femtosecond OR ultrafast OR ultrashort OR "ultra-short pulse" OR "fs laser" '
    'OR ultrakurzpulslaser OR "laser ablation" OR "laser micromachining" '
    'OR "laser texturing" OR "laser structuring" OR "laser welding" OR "laser drilling"'
)

# Ce qui compte comme une production de recherche. Le pendant de
# scrapers.CROSSREF_PUBLICATION_TYPES, qui existait depuis l'audit du 09/09/2026 -- ce chemin-ci
# n'avait aucun filtre de type, et la collecte du 10/09 l'a fait voir : un erratum de Tekniker,
# un dépôt de données d'IREPA et les « Supplementary Data » d'un article Fraunhofer ILT déjà
# présent sont entrés comme des publications à part entière, la dernière en doublon de son
# propre article.
#
# Liste d'autorisation et non d'exclusion : un type inconnu ne doit pas entrer par défaut. Elle
# est plus large que celle de Crossref parce qu'OpenAlex nomme les actes de conférence à part,
# et qu'ils portent 45 publications du corpus -- les écarter viderait le fond industriel
# (Amplitude, Oxford Lasers et IREPA publient surtout en conférence).
OPENALEX_PUBLICATION_TYPES = frozenset({
    "article", "conference-paper", "conference-abstract", "preprint", "review", "book-chapter",
})
# §4.D/§10.8 audit veille (30/08/2026, Lot 3 §3.2/§3.3) : recherche globale, indépendante de
# tout acteur suivi -- complète le passage actor-scoped ci-dessous (qui ne trouve que les
# co-institutions des travaux d'un acteur DÉJÀ tracké, donc jamais une institution 100% hors du
# réseau connu). Pas de per-page trop élevé : autant de requêtes que de TECHNOLOGY_QUERIES.
GLOBAL_SEARCH_WORKS_PER_QUERY = 25


def _domain(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return host.removeprefix("www.")


def _mailto_params() -> dict[str, str]:
    """OpenAlex's "polite pool" (faster, more reliable responses) just wants a contact email
    on every request -- reuses the same CRAWLER_CONTACT env var scrapers.py already puts in
    its own User-Agent, rather than a second contact setting. Since February 2026, OpenAlex also
    requires api_key on every request under its usage-based pricing (a modest free daily budget
    per key; unauthenticated requests hit that wall almost immediately, which is exactly the 429
    "Insufficient budget" seen in production before OPENALEX_API_KEY was configured, 30/08/2026)."""
    params = {"mailto": CRAWLER_CONTACT} if CRAWLER_CONTACT else {}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    return params


def _search_terms(actor_name: str, official_url: str) -> list[str]:
    """Les façons de NOMMER l'institution, de la plus fidèle à la plus large.

    `actors.name` est un nom d'usage choisi pour la lecture humaine, pas un nom d'institution :
    « AIMEN Technology Centre », « Sirris Laser Innovation Lab », « JOANNEUM RESEARCH MATERIALS »
    ne renvoient RIEN sur /institutions, alors qu'OpenAlex connaît parfaitement Sirris (997
    travaux), Joanneum Research (4 404) et l'Asociación de Investigación Metalúrgica (928).
    Le radical du domaine (`sirris`, `joanneum`, `aimen`) les retrouve tous les trois.

    Ce qui ne change pas : l'égalité de domaine reste la SEULE preuve d'identité acceptée, et
    l'ambiguïté reste un refus. Élargir la recherche ne relâche donc aucune garde -- ça donne
    seulement plus d'occasions de tomber sur la bonne fiche. Mesuré le 10/09/2026 sur les 65
    acteurs actifs : 16 résolus avant, 20 après, aucune résolution changée parmi les 16.
    """
    stem = _domain(official_url).split(".")[0]
    words = re.findall(r"[^\W_]+", actor_name, flags=re.UNICODE)
    seen: set[str] = set()
    terms: list[str] = []
    for candidate in (actor_name, stem, " ".join(words[:2]), *words[:1]):
        candidate = candidate.strip()
        if len(candidate) > 2 and candidate.casefold() not in seen:
            seen.add(candidate.casefold())
            terms.append(candidate)
    return terms


def _find_institution(client: httpx.Client, actor_name: str, official_url: str) -> dict | None:
    """Cherche l'institution OpenAlex correspondant à cet acteur, et ne l'accepte QUE si son
    homepage_url partage le même domaine que le site officiel déjà vérifié de l'acteur --
    voir le docstring du module. Ambiguïté (0 ou plusieurs domaines correspondants) => None,
    jamais un choix arbitraire.

    Plusieurs formulations sont essayées (voir _search_terms) parce qu'OpenAlex indexe des noms
    d'institutions, pas les noms d'usage de notre roster. La première qui produit UNE seule
    fiche au bon domaine gagne ; plusieurs fiches au bon domaine (TRUMPF Germany / TRUMPF UK,
    Coherent US / Coherent DE) restent un refus, comme avant.
    """
    target_domain = _domain(official_url)
    if not target_domain:
        return None
    for term in _search_terms(actor_name, official_url):
        try:
            response = client.get(
                f"{OPENALEX_API}/institutions",
                params={"search": term, "per_page": 10, **_mailto_params()},
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except Exception:
            return None
        matches = [row for row in results if row.get("homepage_url") and _domain(row["homepage_url"]) == target_domain]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return None


def _fetch_recent_works(client: httpx.Client, institution_id: str, from_date: str) -> list[dict]:
    """Toute la production de l'institution depuis `from_date`, paginée par curseur.

    Une page tronquée n'est pas un échantillon : le tri par date porte sur TOUS les sujets, donc
    couper à N travaux revient à ne voir le laser que chez les acteurs qui ne publient que ça.
    Voir OPENALEX_WORKS_PER_ACTOR pour la mesure. Une page en erreur interrompt la pagination et
    renvoie ce qui a déjà été lu -- perdre la suite vaut mieux que perdre le début.
    """
    works: list[dict] = []
    cursor: str | None = "*"
    while cursor and len(works) < OPENALEX_WORKS_PER_ACTOR:
        try:
            response = client.get(
                f"{OPENALEX_API}/works",
                params={
                    "filter": f"authorships.institutions.id:{institution_id},from_publication_date:{from_date}",
                    "sort": "publication_date:desc",
                    "per-page": OPENALEX_WORKS_PER_PAGE,
                    "cursor": cursor,
                    "select": "id,doi,title,type,publication_date,primary_location,authorships,abstract_inverted_index",
                    **_mailto_params(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            break
        works += payload.get("results", [])
        cursor = (payload.get("meta") or {}).get("next_cursor")
    return works[:OPENALEX_WORKS_PER_ACTOR]


def _fetch_topic_works(client: httpx.Client, institution_id: str, from_date: str) -> list[dict]:
    """Les travaux LASER de cette institution, demandés comme tels à OpenAlex.

    Jumelle de _fetch_recent_works, et les deux sont nécessaires pour des raisons opposées :
    celle-ci trie par pertinence de sujet et atteint donc l'historique profond d'un institut
    généraliste, mais dépend du vocabulaire envoyé au serveur ; celle-là ne dépend d'aucun
    vocabulaire mais coupe après OPENALEX_WORKS_PER_ACTOR travaux tous sujets confondus. Leur
    union ne perd ni l'un ni l'autre, pour une requête de plus par institution.
    """
    works: list[dict] = []
    cursor: str | None = "*"
    while cursor and len(works) < OPENALEX_WORKS_PER_ACTOR:
        try:
            response = client.get(
                f"{OPENALEX_API}/works",
                params={
                    "filter": (
                        f"authorships.institutions.id:{institution_id},"
                        f"from_publication_date:{from_date},"
                        f"title_and_abstract.search:{OPENALEX_TOPIC_SEARCH}"
                    ),
                    "per-page": OPENALEX_WORKS_PER_PAGE,
                    "cursor": cursor,
                    "select": "id,doi,title,type,publication_date,primary_location,authorships,abstract_inverted_index",
                    **_mailto_params(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            break
        works += payload.get("results", [])
        cursor = (payload.get("meta") or {}).get("next_cursor")
    return works[:OPENALEX_WORKS_PER_ACTOR]


def _fetch_global_on_topic_works(client: httpx.Client, query: str, from_date: str) -> list[dict]:
    """§4.D/§10.8 audit veille (30/08/2026, Lot 3 §3.2/§3.3) : recherche OpenAlex GLOBALE (pas
    scopée à `authorships.institutions.id` comme _fetch_recent_works) -- utilise le paramètre
    `search` sur l'ensemble du corpus. Complète _fetch_recent_works : ce dernier ne trouve
    jamais une institution qui n'a encore co-publié avec AUCUN acteur suivi, aussi pertinente
    soit-elle. Réutilise les mêmes requêtes déjà vérifiées pour Crossref
    (scrapers.TECHNOLOGY_QUERIES) plutôt que d'en inventer de nouvelles."""
    try:
        response = client.get(
            f"{OPENALEX_API}/works",
            params={
                "search": query,
                "filter": f"from_publication_date:{from_date}",
                "sort": "publication_date:desc",
                "per-page": GLOBAL_SEARCH_WORKS_PER_QUERY,
                "select": "id,doi,title,type,publication_date,primary_location,authorships,abstract_inverted_index",
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

    Le cas du laser INSTRUMENT DE MESURE (spectroscopie d'absorption transitoire, pompe-sonde)
    est écarté par is_on_topic() lui-même depuis le 09/09/2026 -- voir
    lexicon.is_laser_the_instrument. Il n'est donc pas retesté ici : le doubler laisserait
    croire que is_on_topic ne s'en charge pas.
    """
    return is_on_topic(title)


def _co_institutions(work: dict, exclude_institution_id: str) -> list[tuple[str, str | None]]:
    """§4.D audit veille (30/08/2026, Lot 3 §3.2) : institutions co-autrices d'un travail
    on-topic d'un acteur suivi, hors l'institution de l'acteur lui-même -- signal de découverte
    d'acteur (voir actor_discovery.py), pas un signal marché/technologie. Renvoie (nom, code
    pays OpenAlex -- ISO 3166-1 alpha-2, ou None) : capturé tel quel, jamais deviné, pour
    préremplir la promotion d'un candidat (actor_candidates.country)."""
    results: list[tuple[str, str | None]] = []
    for authorship in work.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            institution_id = str(institution.get("id") or "").rsplit("/", 1)[-1]
            display_name = str(institution.get("display_name") or "").strip()
            if display_name and institution_id != exclude_institution_id:
                results.append((display_name, institution.get("country_code") or None))
    return results


def _abstract(work: dict) -> str:
    """Reconstruit le résumé depuis l'index inversé d'OpenAlex (mot -> positions).

    OpenAlex ne redistribue pas les résumés en texte suivi -- c'est une contrainte de droits,
    pas un caprice de format -- mais publie l'index inversé, dont la reconstruction est exacte
    et documentée par OpenAlex lui-même.

    Ce qu'il change : le classement d'un document ne reposait que sur son titre, douze mots où
    rien ne tient. Mesuré le 14/09/2026 sur les 302 publications du corpus, dont 260 ont un
    résumé (86 %, longueur médiane 1 126 caractères) : 370 étiquettes -> 887, et les documents
    sans aucune famille passent de 76 à 25. La citation attachée à chaque étiquette devient
    surtout une VRAIE preuve -- une phrase du résumé qui porte le terme -- au lieu du titre
    répété faute de mieux.

    Renvoie "" plutôt que None quand OpenAlex n'a pas de résumé : c'est ce qu'attendent
    upsert_document et upsert_document_technology_signal, et une chaîne vide dit la même chose
    qu'une absence sans obliger chaque appelant à la tester.
    """
    index = work.get("abstract_inverted_index")
    if not isinstance(index, dict) or not index:
        return ""
    positions: dict[int, str] = {}
    for mot, places in index.items():
        for place in places or ():
            if isinstance(place, int):
                positions[place] = mot
    return " ".join(positions[rang] for rang in sorted(positions))[:6000]


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
    return {
        "title": title, "url": url, "doi": doi,
        "published_at": work.get("publication_date"), "abstract": _abstract(work),
    }


def _upsert_document(db, actor_name: str, item: dict) -> tuple[int, int]:
    """Adaptateur : traduit un `item` OpenAlex en colonnes `documents`, puis delegue l'ecriture
    a db.upsert_document (dedoublonnage sur empreinte + rattachement tardif d'un actor_name),
    partagee avec patent.py. L'empreinte derive du DOI quand il existe, sinon de l'URL."""
    return upsert_document(
        db,
        document_type="publication",
        title=item["title"],
        source_url=item["url"],
        fingerprint_source=item["doi"] or item["url"],
        actor_name=actor_name,
        published_at=item["published_at"],
        doi=item["doi"],
        abstract=item.get("abstract") or None,
    )

# Combien de DOI par appel à /works. OpenAlex accepte un filtre `doi:a|b|c` ; 25 tient
# largement sous la limite de longueur d'URL et évite de tout redemander si un seul DOI est
# malformé.
UNLINKED_DOIS_PER_CALL = 25


def _work_institutions(work: dict) -> tuple[list[str], list[str]]:
    """Noms d'institutions et codes pays d'un travail, dédoublonnés, dans l'ordre des auteurs."""
    noms: list[str] = []
    pays: list[str] = []
    for authorship in work.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            nom = str(institution.get("display_name") or "").strip()
            if nom and nom not in noms:
                noms.append(nom)
            code = institution.get("country_code")
            if code and code not in pays:
                pays.append(code)
    return noms, pays


def resolve_unlinked_documents(client: httpx.Client, actor_by_institution: dict[str, str]) -> dict:
    """Demande à OpenAlex qui signe les publications en file, et rend au corpus celles d'un acteur.

    La file est alimentée par la passe thématique Crossref, qui cherche par sujet et n'attribue
    jamais d'acteur (voir db.unlinked_documents). Une publication d'un acteur suivi y arrive donc
    orpheline, et seule cette résolution peut la reconnaître : `actor_by_institution` est indexé
    par ID d'institution OpenAlex, pas par nom, parce que le roster écrit « Fraunhofer ILT » là où
    OpenAlex écrit « Fraunhofer Institute for Laser Technology » -- un rapprochement par nom
    raterait précisément les acteurs qu'on veut récupérer. Le cas est réel : une publication CNRS
    a dû être rattrapée à la main le 13/09/2026, avant que cette fonction n'existe.

    Ce qui n'est pas promu garde ses institutions en base : c'est ce qu'affiche la page « Hors
    roster », et ce qui permet de voir quels laboratoires reviennent assez souvent pour mériter
    d'entrer dans le roster.

    Ne résout que les lignes à DOI jamais résolues -- une publication sans DOI n'est pas
    interrogeable, et une déjà résolue ne change plus de signataires.
    """
    en_attente = rows(
        TECH_DB,
        "SELECT fingerprint,doi FROM unlinked_documents WHERE doi IS NOT NULL AND resolved_at IS NULL",
    )
    promus = resolus = 0
    for start in range(0, len(en_attente), UNLINKED_DOIS_PER_CALL):
        lot = en_attente[start : start + UNLINKED_DOIS_PER_CALL]
        try:
            response = client.get(
                f"{OPENALEX_API}/works",
                params={
                    "filter": "doi:" + "|".join(f"https://doi.org/{row['doi']}" for row in lot),
                    "per-page": UNLINKED_DOIS_PER_CALL,
                    "select": "doi,authorships",
                    **_mailto_params(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue
        par_doi = {
            str(work.get("doi") or "").removeprefix("https://doi.org/").casefold(): work
            for work in payload.get("results", [])
        }
        with connect(TECH_DB) as db:
            for row in lot:
                work = par_doi.get((row["doi"] or "").casefold())
                if work is None:
                    continue
                noms, pays = _work_institutions(work)
                identifiants = {
                    str(institution.get("id") or "").rsplit("/", 1)[-1]
                    for authorship in work.get("authorships") or []
                    for institution in authorship.get("institutions") or []
                }
                acteur = next(
                    (actor_by_institution[i] for i in identifiants if i in actor_by_institution), None
                )
                resolus += 1
                promus += int(resolve_unlinked_document(
                    db, fingerprint=row["fingerprint"], institutions=noms, countries=pays,
                    actor_name=acteur,
                ))
    return {"unlinked_resolved": resolus, "unlinked_promoted": promus}


def collect_openalex_publications(lookback_days: int = OPENALEX_LOOKBACK_DAYS_DEFAULT) -> dict:
    """Point d'entrée (voir app.py: collectors["openalex"])."""
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT name,official_url FROM actors WHERE active=1").fetchall()]
        known_names, _known_domains = _known_actor_names_and_domains(db)
    from_date = (date.today() - timedelta(days=max(1, lookback_days))).isoformat()

    with connect(TECH_DB) as db:
        run_id = db.execute("INSERT INTO collection_runs(started_at,status) VALUES(?,?)", (utc_now(), "running")).lastrowid

    matched_actors = added = attributed = off_topic = candidates_added = technology_signals_added = errors = 0
    # Construite au fil de la boucle plutôt que dans une seconde passe : chaque institution y est
    # déjà résolue, et la redemander coûterait un appel par acteur pour rien.
    actor_by_institution: dict[str, str] = {}
    with connector_client("api") as client:
        for actor in actors:
            try:
                institution = _find_institution(client, actor["name"], actor["official_url"])
                if not institution:
                    continue
                matched_actors += 1
                institution_id = str(institution["id"]).rsplit("/", 1)[-1]
                actor_by_institution[institution_id] = actor["name"]
                # Deux lectures, unies par l'identifiant du travail : la récente tous sujets
                # (aucune dépendance à un vocabulaire) et celle par sujet (aucune dépendance au
                # volume de l'institut). Voir _fetch_topic_works pour ce que chacune rattrape.
                works = list({
                    str(work.get("id") or index): work
                    for index, work in enumerate([
                        *_fetch_recent_works(client, institution_id, from_date),
                        *_fetch_topic_works(client, institution_id, from_date),
                    ])
                }.values())
                with connect(TECH_DB) as db, connect(ACTORS_DB) as adb:
                    for work in works:
                        item = _parse_work(work)
                        if not item:
                            continue
                        # Le type se teste AVANT le filtre lexical : un erratum sur un article
                        # pertinent est lexicalement pertinent, et c'est bien pour ça qu'il
                        # entrait. Même ordre que scrape_technology pour Crossref.
                        if work.get("type") not in OPENALEX_PUBLICATION_TYPES:
                            off_topic += 1
                            continue
                        if not _work_is_on_topic(item["title"]):
                            off_topic += 1
                            continue
                        inserted, was_attributed = _upsert_document(db, actor["name"], item)
                        added += inserted
                        attributed += was_attributed
                        # Titre ET résumé depuis le 14/09/2026 : le titre seul plafonnait le
                        # classement (voir _abstract). Le filtre d'entrée _work_is_on_topic,
                        # lui, reste sur le titre -- un résumé qui mentionne le femtoseconde en
                        # passant ne fait pas d'un article un article de procédé.
                        technology_signals_added += upsert_document_technology_signal(
                            db, item["title"], item.get("abstract", ""), item["url"], actor["name"],
                        )
                        # §4.D audit veille (Lot 3 §3.2) : une institution co-autrice récurrente
                        # sur des travaux on-topic est un candidat acteur -- voir actor_discovery.py.
                        # Exclut aussi les co-institutions qui sont déjà un AUTRE acteur suivi
                        # (ex: ALPHANOV et Fraunhofer ILT co-auteurs) -- un acteur déjà réel ne
                        # doit jamais redevenir un "candidat".
                        for co_name, co_country in _co_institutions(work, institution_id):
                            if _normalize_name(co_name) in known_names:
                                continue
                            candidates_added += upsert_actor_candidate(
                                adb, co_name, "openalex",
                                context=f"co-auteur avec {actor['name']} sur un travail on-topic",
                                source_url=item["url"], country=co_country,
                            )
            except Exception:
                errors += 1

        # Après la boucle, avec la carte complète : la file alimentée par la passe thématique
        # Crossref (qui tourne toujours AVANT celle-ci, voir app._collect_monthly) est résolue
        # ici, et ce qui appartient à un acteur suivi rejoint le corpus.
        try:
            unlinked = resolve_unlinked_documents(client, actor_by_institution)
        except Exception:
            errors += 1
            unlinked = {"unlinked_resolved": 0, "unlinked_promoted": 0}

    with connect(TECH_DB) as db:
        db.execute(
            "UPDATE collection_runs SET finished_at=?,status=?,scanned=?,added=?,errors=?,message=? WHERE id=?",
            (
                utc_now(), "completed", matched_actors, added, errors,
                f"OpenAlex : {matched_actors} institutions vérifiées par domaine, {added} nouvelles publications, "
                f"{attributed} déjà connues ré-attribuées à un acteur, {off_topic} hors sujet filtrées, "
                f"{candidates_added} candidats acteurs (co-institutions), {technology_signals_added} signaux technologiques, "
                f"{unlinked['unlinked_resolved']} publications en file résolues dont {unlinked['unlinked_promoted']} rendues au corpus",
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
        **unlinked,
        "errors": errors,
    }


def discover_global_actor_candidates(lookback_days: int = 60) -> dict:
    """§4.D/§10.8 audit veille (30/08/2026, Lot 3 §3.2/§3.3, "élargir le périmètre hors
    Europe") : point d'entrée séparé de collect_openalex_publications() -- une recherche
    globale par mot-clé (search=) est un usage différent de l'API que la recherche par
    institution (filter=authorships.institutions.id:...), avec son propre budget de requêtes.
    Ne dépend d'AUCUN acteur déjà suivi : c'est la seule des deux façons d'interroger OpenAlex
    qui peut découvrir une institution qui n'a encore jamais co-publié avec un acteur tracké
    (voir _fetch_global_on_topic_works). Chaque requête (scrapers.TECHNOLOGY_QUERIES, déjà
    vérifiées pour Crossref) est indépendante du pays de l'institution -- aucun biais européen,
    contrairement à collect_openalex_publications (qui part TOUJOURS d'un acteur suivi, donc
    européen aujourd'hui)."""
    with connect(ACTORS_DB) as db:
        known_names, _known_domains = _known_actor_names_and_domains(db)
    from_date = (date.today() - timedelta(days=max(1, lookback_days))).isoformat()

    works_scanned = works_on_topic = candidates_added = errors = 0
    seen_work_ids: set[str] = set()
    with connector_client("api") as client, connect(ACTORS_DB) as adb:
        for query in TECHNOLOGY_QUERIES:
            try:
                works = _fetch_global_on_topic_works(client, query, from_date)
            except Exception:
                errors += 1
                continue
            for work in works:
                work_id = str(work.get("id") or "")
                if not work_id or work_id in seen_work_ids:
                    continue
                seen_work_ids.add(work_id)
                works_scanned += 1
                item = _parse_work(work)
                if not item or not _work_is_on_topic(item["title"]):
                    continue
                works_on_topic += 1
                for co_name, co_country in _co_institutions(work, ""):
                    if _normalize_name(co_name) in known_names:
                        continue
                    candidates_added += upsert_actor_candidate(
                        adb, co_name, "openalex",
                        context=f"recherche globale on-topic : {item['title'][:120]}",
                        source_url=item["url"], country=co_country,
                    )

    return {
        "queries": len(TECHNOLOGY_QUERIES),
        "works_scanned": works_scanned,
        "works_on_topic": works_on_topic,
        "actor_candidates_added": candidates_added,
        "errors": errors,
    }
