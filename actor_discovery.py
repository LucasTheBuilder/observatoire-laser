"""Découverte d'acteurs (§4.D audit veille, 30/08/2026, Lot 3 §3.2) : "actors.review_status=
'candidate' existe déjà en base et n'est jamais alimenté -- le schéma l'attend." La totalité de
la croissance du périmètre était manuelle (29 des 55 acteurs ajoutés un par un via POST
/api/actors) alors que les matériaux pour la détecter sont déjà en base.

Un candidat n'entre JAMAIS directement dans `actors` : il s'accumule dans `actor_candidates`
(occurrences, sources, score = nombre de sources indépendantes) et n'est promu en acteur réel
(promote_candidate, review_status='candidate' -- rejoint alors la file /api/review?queue=actors
du Lot 1 §1.1 pour la décision finale) qu'après validation humaine. Même gouvernance que
vocabulary_candidates -> custom_lexicon_entries, le modèle explicitement cité par l'audit comme
référence.

Trois sources, toutes des sous-produits d'un pipeline déjà écrit ailleurs (rien de nouveau à
collecter) :
- CORDIS : `actor_relations.related_name` jamais rattaché à un acteur connu
  (related_actor_id IS NULL) -- cordis.py écrit déjà CES lignes pour CHAQUE partenaire de
  consortium, matché ou non ; seul le filtre "matché seulement" les laissait invisibles.
- outbound_links (Lot 2 §2.4) : un hôte externe cité en contenu (jamais en navigation) sur
  plusieurs sites d'acteurs DISTINCTS -- "un hôte externe qui revient sur cinq sites d'acteurs
  différents est un candidat acteur de très bonne qualité, bien meilleur qu'une mention presse."
  Le seuil retenu ici est 2, pas 5 : sur un corpus qui grossit encore, exiger 5 laisserait
  presque tout passer sous le radar pendant des mois.
- OpenAlex : institution co-autrice récurrente sur des travaux on-topic d'un acteur suivi (voir
  openalex.py, qui appelle upsert_actor_candidate directement pendant sa propre collecte plutôt
  que de refaire un appel API séparé ici).

Mentions de presse non appariées (4e source listée par l'audit) volontairement omises : press.py
ne fait aujourd'hui AUCUNE extraction de nom d'organisation (seul un matching par mot-clé sur des
noms déjà connus) -- une heuristique de reconnaissance de nom sans vraie extraction d'entités
nommées produirait plus de bruit que de signal, à l'inverse de la discipline du reste de ce
projet (jamais fabriquer un fait sur une hypothèse faible).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

from db import ACTORS_DB, connect, create_actor, update_actor_classification, utc_now

# Hôtes qui ne seront jamais un candidat acteur, quel que soit le nombre de sites qui les citent
# -- réseaux sociaux, plateformes techniques génériques (newsletter, cookies, CDN...). Constaté
# en production (Lot 2 §2.4 : youtube.com/twitter.com/facebook.com/linkedin.com dominaient déjà
# le top 10 des hôtes sortants).
NON_ACTOR_HOSTS = frozenset({
    "youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com", "linkedin.com",
    "instagram.com", "xing.com", "cookiedatabase.org", "subscribepage.io", "google.com",
    "goo.gl", "bit.ly", "vimeo.com", "wordpress.com", "wordpress.org", "wp.com", "gravatar.com",
    "schema.org", "w3.org", "apple.com", "microsoft.com", "adobe.com", "github.com",
    "maps.google.com", "calendly.com", "eventbrite.com", "mailchimp.com",
})

# Voir docstring du module : pas 5 (le seuil littéral de l'audit) mais 2, pour rester exploitable
# sur un corpus qui grossit encore.
MIN_OUTBOUND_ACTOR_RECURRENCE = 2


def _root_domain(host: str) -> str:
    """Même heuristique que hybrid._root_domain (2 derniers labels, sans liste de suffixes
    publics) -- dupliquée plutôt qu'importée, ce module n'a pas d'autre lien avec hybrid.py.
    Nécessaire ici : un hôte comme de.linkedin.com (sous-domaine pays) ne matche jamais
    NON_ACTOR_HOSTS/known_domains en comparaison exacte, seulement en domaine racine."""
    labels = host.lower().removeprefix("www.").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _normalize_name(value: str) -> str:
    """Majuscules, sans accents, ponctuation réduite à des espaces simples -- même principe que
    cordis._normalize_org_text, pour que deux graphies d'un même nom se déduplique."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def _known_actor_names_and_domains(db) -> tuple[set[str], set[str]]:
    rows = db.execute("SELECT name,official_url FROM actors").fetchall()
    names = {_normalize_name(row["name"]) for row in rows}
    domains = set()
    for row in rows:
        host = urlparse(row["official_url"]).netloc.lower().removeprefix("www.")
        if host:
            domains.add(host)
    return names, domains


def upsert_actor_candidate(
    db, name: str, source_type: str, *, context: str | None = None, source_url: str | None = None,
    country: str | None = None, suggested_official_url: str | None = None,
) -> int:
    """Enregistre une occurrence d'un candidat depuis une source donnée. Renvoie 1 si cette
    occurrence était nouvelle (fait avancer le score), 0 si déjà vue. `db` doit être une
    connexion ACTORS_DB déjà ouverte (appelée depuis plusieurs collecteurs, jamais sa propre
    transaction). `country`/`suggested_official_url` : uniquement quand la source les porte
    elle-même (jamais devinés) -- COALESCE ne les écrase jamais une fois connus, y compris par
    une occurrence ultérieure qui ne les fournit pas."""
    normalized = _normalize_name(name)
    if not normalized:
        return 0
    stamp = utc_now()
    row = db.execute("SELECT id FROM actor_candidates WHERE normalized_name=?", (normalized,)).fetchone()
    if row:
        candidate_id = int(row["id"])
        db.execute(
            "UPDATE actor_candidates SET last_seen_at=?,country=COALESCE(country,?),suggested_official_url=COALESCE(suggested_official_url,?) WHERE id=?",
            (stamp, country, suggested_official_url, candidate_id),
        )
    else:
        candidate_id = db.execute(
            "INSERT INTO actor_candidates(name,normalized_name,country,suggested_official_url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (name[:200], normalized, country, suggested_official_url, stamp, stamp),
        ).lastrowid

    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO actor_candidate_sources(candidate_id,source_type,context,source_url,created_at)
           VALUES(?,?,?,?,?)""",
        (candidate_id, source_type, (context or "")[:300], source_url, stamp),
    )
    added = int(db.total_changes > before)
    if added:
        # Score = nombre de SOURCES INDÉPENDANTES distinctes (§4.D : "nombre de sources
        # indépendantes × on-topic du contexte" -- le on-topic est déjà une porte d'entrée en
        # amont de chaque source, jamais un poids numérique séparé inventé ici).
        distinct_sources = db.execute(
            "SELECT COUNT(DISTINCT source_type) FROM actor_candidate_sources WHERE candidate_id=?", (candidate_id,)
        ).fetchone()[0]
        db.execute("UPDATE actor_candidates SET score=? WHERE id=?", (distinct_sources, candidate_id))
    return added


def _discover_from_cordis(db, known_names: set[str]) -> int:
    added = 0
    for row in db.execute(
        "SELECT related_name,note,source_url FROM actor_relations WHERE related_actor_id IS NULL"
    ).fetchall():
        if _normalize_name(row["related_name"]) in known_names:
            continue
        added += upsert_actor_candidate(db, row["related_name"], "cordis", context=row["note"], source_url=row["source_url"])
    return added


def _discover_from_outbound_links(db, known_names: set[str], known_domains: set[str]) -> int:
    added = 0
    # Comparé par DOMAINE RACINE, pas par hôte exact : de.linkedin.com/fr.linkedin.com/...
    # partagent tous linkedin.com comme racine, et un seul hôte exact ne recoupe jamais tous
    # les sous-domaines pays d'une même plateforme (constaté en production, 30/08/2026).
    non_actor_roots = {_root_domain(host) for host in NON_ACTOR_HOSTS}
    known_roots = {_root_domain(host) for host in known_domains}
    for row in db.execute(
        """SELECT ol.target_host,ol.target_url,ol.label,COUNT(DISTINCT s.actor_id) AS actor_count
           FROM outbound_links ol JOIN actor_sources s ON s.id=ol.source_id
           GROUP BY ol.target_host HAVING actor_count>=?""",
        (MIN_OUTBOUND_ACTOR_RECURRENCE,),
    ).fetchall():
        host = row["target_host"]
        if _root_domain(host) in non_actor_roots or _root_domain(host) in known_roots:
            continue
        name = row["label"].strip() if row["label"] and row["label"].strip() else host
        if _normalize_name(name) in known_names:
            continue
        added += upsert_actor_candidate(
            db, name, "outbound_link",
            context=f"{row['actor_count']} sites d'acteurs distincts", source_url=row["target_url"],
        )
    return added


def discover_actor_candidates() -> dict:
    """Point d'entrée (voir app.py: collectors["actor_discovery"]) pour les 2 sources
    autonomes (CORDIS, outbound_links) -- OpenAlex alimente actor_candidates directement
    pendant sa propre collecte (voir openalex.py), pas ici, pour ne pas refaire d'appel API."""
    with connect(ACTORS_DB) as db:
        known_names, known_domains = _known_actor_names_and_domains(db)
        cordis_added = _discover_from_cordis(db, known_names)
        outbound_added = _discover_from_outbound_links(db, known_names, known_domains)
    return {"cordis_candidates": cordis_added, "outbound_link_candidates": outbound_added}


def promote_candidate(
    candidate_id: int, *, role: str, official_url: str | None = None, country: str | None = None,
    reviewed_by: str | None = None,
) -> dict:
    """Crée un vrai acteur (review_status='candidate' -- rejoint /api/review?queue=actors pour
    la décision finale, voir review_queue.py Lot 1 §1.1) à partir d'un candidat validé. `role`
    reste toujours obligatoire, saisi par l'humain qui promeut -- rien dans les sources de ce
    module ne le détermine de façon fiable. `official_url`/`country` sont optionnels : à défaut,
    la valeur suggérée par la source (candidate.suggested_official_url/country -- ex: le vrai
    organizationURL/country de organization.csv pour un candidat CORDIS) est utilisée si connue.
    Un humain qui la fournit explicitement l'emporte toujours sur la suggestion -- jamais
    l'inverse, et jamais une valeur devinée quand ni l'un ni l'autre n'est disponible."""
    with connect(ACTORS_DB) as db:
        row = db.execute(
            "SELECT id,name,review_status,country,suggested_official_url FROM actor_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Actor candidate {candidate_id} not found")
        if row["review_status"] != "pending":
            raise ValueError(f"Actor candidate {candidate_id} already {row['review_status']}")
        name = row["name"]
        official_url = official_url or row["suggested_official_url"]
        country = country or row["country"]
        if not official_url:
            raise ValueError("official_url is required: no suggested_official_url was captured for this candidate")
        if not country:
            raise ValueError("country is required: no country was captured for this candidate")
    # create_actor()/update_actor_classification() open their OWN connection each -- called
    # outside the block above rather than nested inside it, matching how every other caller in
    # this codebase uses them (never nested inside another open `with connect()`).
    actor_id = create_actor(name, country, role, official_url)
    update_actor_classification(actor_id, review_status="candidate")
    with connect(ACTORS_DB) as db:
        db.execute(
            "UPDATE actor_candidates SET review_status='promoted',promoted_actor_id=?,reviewed_by=?,reviewed_at=? WHERE id=?",
            (actor_id, reviewed_by, utc_now(), candidate_id),
        )
    return {"candidate_id": candidate_id, "actor_id": actor_id, "status": "promoted"}


def reject_candidate(candidate_id: int, *, reviewed_by: str | None = None, reject_reason: str | None = None) -> dict:
    with connect(ACTORS_DB) as db:
        updated = db.execute(
            "UPDATE actor_candidates SET review_status='rejected',reviewed_by=?,reviewed_at=?,reject_reason=? WHERE id=? AND review_status='pending'",
            (reviewed_by, utc_now(), reject_reason, candidate_id),
        ).rowcount
    if not updated:
        raise ValueError(f"Actor candidate {candidate_id} not found or already reviewed")
    return {"candidate_id": candidate_id, "status": "rejected"}
