"""Signaux de demande via appels d'offres publics (§4.B.3 audit veille, 30/08/2026, Lot 4 §15) :
"un cahier des charges mentionnant du micro-usinage femtoseconde est un signal d'achat, pas un
signal de discours." Sort la dimension marché du miroir de l'offre : jusqu'ici `market.db` ne
documentait que ce que les acteurs suivis DISENT faire (offers/evidence), jamais ce que le marché
ACHÈTE réellement.

Deux sources, toutes deux vérifiées en direct (curl) avant d'écrire ce module -- jamais assumées
depuis la seule documentation :

- **TED** (Tenders Electronic Daily, UE) : POST api.ted.europa.eu/v3/notices/search, requête en
  syntaxe Lucene-like (``FT~"phrase" AND PD>=YYYYMMDD``), accès anonyme, sans clé.
- **BOAMP** (France) : GET boamp-datadila.opendatasoft.com/api/**explore/v2.0** -- PAS
  ``/api/records/1.0/search/``, un ancien mirroir OpenDataSoft v1 dont les données se sont
  révélées figées vers 2015/2022 en vérifiant (triées "plus récent d'abord", la date la plus
  récente restait 2015) : piège trouvé puis écarté avant d'écrire ce module. Requête ODSQL
  (``where=search(objet,"phrase")``), accès anonyme, sans clé.

Réutilise ``scrapers.TECHNOLOGY_QUERIES`` pour TED (déjà vérifiées pour Crossref/OpenAlex, et TED
indexe en 24 langues). BOAMP en revanche ne publie qu'en français -- interroger ``objet`` avec les
requêtes ANGLAISES de TECHNOLOGY_QUERIES ne renvoyait donc jamais rien (0 résultat sur les 6
requêtes en vérifiant en production, 30/08/2026), pas parce qu'aucun marché français n'existe :
"laser femtoseconde" tout court renvoyait au même moment 3 résultats réels, dont un daté du
2026-03-24. D'où ``BOAMP_QUERIES``, un jeu de requêtes françaises séparé (mêmes termes que
``scrapers.LASER_RULES``/le vocabulaire déjà utilisé ailleurs dans ce projet).

**Troisième source depuis le 15/09/2026 : le cofinancement industriel ANR.** Une entreprise qui
met de l'argent dans un projet ANR dont l'objet nomme une OPÉRATION laser achète cette
technologie, exactement au sens de ce module -- un signal d'achat, pas un signal de discours. La
donnée est déjà en cache local (``national_projects.py`` télécharge les jeux ANR_01/ANR_02) et
elle porte ce que TED et BOAMP ne portent pas : le marché visé, l'opération et souvent la pièce.
Elle ne coûte aucune requête réseau.

Le tri est serré, et les deux conditions sont cumulatives : le partenaire doit être une
ENTREPRISE (colonne ``Categorie_organisme``, qui distingue PME/ETI/GE du laboratoire), et le
projet doit nommer une opération laser. Sans la seconde, un fabricant de sources partenaire d'un
projet de spectroscopie compterait comme acheteur de micro-usinage. Mesuré sur les jeux du
02/09/2026 : 66 projets nomment une opération, 21 participations d'entreprises, 19 entreprises
distinctes -- Becton Dickinson sur la texturation de pièces polymères médicales, Anthogyr sur la
bio-activation d'implants, Heyrmoules et Itech sur les moules.

``scrapers.is_on_topic()`` filtre les deux sources publiques en sortie -- une recherche plein texte peut
matcher des tokens sans rapport. Limite connue et acceptée plutôt que contournée : TED renvoie
souvent un ``notice-title`` bureaucratique qui ne répète pas les termes ayant fait matcher la
requête (ex: un marché suédois réel trouvé via "femtosecond laser micromachining" s'intitule
"Arbitrary shape laser based micromachining system" dans ses 24 traductions, sans jamais dire
"femtosecond") -- filtré ici comme faux négatif plutôt que d'affaiblir is_on_topic() pour ce seul
connecteur, au prix de rater occasionnellement un signal réel mais mal titré.

Hors scope, volontairement : la seconde moitié du §4.B.3, "offres d'emploi des donneurs d'ordre".
Aucune API publique fiable identifiée, et "donneur d'ordre" (client final, pas concurrent) n'est
même pas un type d'entité qui existe dans ce schéma aujourd'hui -- l'ajouter correctement
demanderait de définir cette notion d'abord, pas juste brancher une source de plus.
"""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from db import MARKET_DB, connect, utc_now
from http_client import connector_client
from lexicon import DOCUMENT_OPERATIONS, MARKETS, match_all_labels
from national_projects import ANR_CACHE_DIR, ANR_PROJECT_URL_TEMPLATE  # noqa: F401  (patchable)
from scrapers import TECHNOLOGY_QUERIES, is_on_topic

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
BOAMP_SEARCH_URL = "https://boamp-datadila.opendatasoft.com/api/explore/v2.0/catalog/datasets/boamp/records"

DEMAND_SIGNALS_LOOKBACK_DAYS_DEFAULT = 180
RESULTS_PER_QUERY = 10

# BOAMP.fr ne publie qu'en français -- voir le docstring du module. "laser femtoseconde" est le
# seul terme vérifié avec des correspondances réelles en production (30/08/2026, 3 résultats,
# dont un du 2026-03-24) ; les autres restent des variantes plausibles du même vocabulaire
# (scrapers.LASER_RULES) plutôt que des traductions inventées au hasard.
BOAMP_QUERIES = (
    "laser femtoseconde",
    "laser ultra-rapide",
    "micro-usinage laser",
    "texturation laser femtoseconde",
    "gravure laser femtoseconde",
)


def _ted_notice_title(notice: dict) -> str:
    titles = notice.get("notice-title") or {}
    return titles.get("eng") or next(iter(titles.values()), "") or ""


def _ted_buyer_name(notice: dict) -> str | None:
    buyers = notice.get("buyer-name") or {}
    for names in buyers.values():
        if names:
            return str(names[0])
    return None


def _ted_notice_url(notice: dict, number: str) -> str:
    html_links = ((notice.get("links") or {}).get("html")) or {}
    return html_links.get("ENG") or next(iter(html_links.values()), None) or f"https://ted.europa.eu/en/notice/-/detail/{number}"


def _parse_ted_notice(notice: dict) -> dict[str, Any] | None:
    number = notice.get("publication-number")
    if not number:
        return None
    published_at = (notice.get("publication-date") or "")[:10] or None
    return {
        "source": "TED", "external_id": number, "title": _ted_notice_title(notice),
        "buyer_name": _ted_buyer_name(notice), "published_at": published_at,
        "url": _ted_notice_url(notice, number),
    }


def _fetch_ted_notices(client: httpx.Client, query: str, from_date_yyyymmdd: str) -> list[dict]:
    payload = {
        "query": f'FT~"{query}" AND PD>={from_date_yyyymmdd}',
        "fields": ["publication-number", "notice-title", "publication-date", "buyer-name", "links"],
        "limit": RESULTS_PER_QUERY,
        "scope": "ALL",
    }
    response = client.post(TED_SEARCH_URL, json=payload)
    response.raise_for_status()
    return response.json().get("notices") or []


def _parse_boamp_record(fields: dict) -> dict[str, Any] | None:
    idweb = fields.get("idweb")
    if not idweb:
        return None
    return {
        "source": "BOAMP", "external_id": idweb, "title": fields.get("objet") or "",
        "buyer_name": fields.get("nomacheteur"), "published_at": fields.get("dateparution"),
        "url": fields.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:{idweb}",
    }


def _fetch_boamp_records(client: httpx.Client, query: str, from_date_iso: str) -> list[dict]:
    escaped_query = query.replace('"', "")
    params = {
        "where": f'search(objet, "{escaped_query}") and dateparution >= date\'{from_date_iso}\'',
        "order_by": "dateparution desc",
        "limit": str(RESULTS_PER_QUERY),
    }
    response = client.get(BOAMP_SEARCH_URL, params=params)
    response.raise_for_status()
    return [row["record"]["fields"] for row in response.json().get("records") or []]


def _upsert_demand_signal(db, signal_type: str, item: dict) -> int:
    fingerprint = hashlib.sha256(f"{item['source']}|{item['external_id']}".encode()).hexdigest()
    stamp = utc_now()
    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO demand_signals(
               signal_type,source,buyer_name,title,published_at,source_url,fingerprint,created_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            signal_type, item["source"], item["buyer_name"], item["title"][:500],
            item["published_at"], item["url"], fingerprint, stamp, stamp,
        ),
    )
    inserted = int(db.total_changes > before)
    if not inserted:
        db.execute("UPDATE demand_signals SET last_seen_at=? WHERE fingerprint=?", (stamp, fingerprint))
    return inserted


# Catégories que l'ANR réserve aux entreprises. « Divers privé » en fait partie : il couvre les
# structures privées non classées, où l'on trouve bureaux d'études et jeunes pousses.
# Le mot qui vaut la peine d'ouvrir les sept vocabulaires. Volontairement plus large que
# LASER_RULES -- il ne décide de rien, il ne fait qu'éviter un travail inutile, et is_on_topic()
# reste seul juge derrière lui.
ANR_ULTRAFAST_HINT = re.compile(
    r"femtoseconde|femtosecond|picoseconde|picosecond|ultra[- ]?court|ultra[- ]?bref"
    r"|ultra[- ]?rapide|ultrafast|ultrashort|laser",
    re.IGNORECASE,
)

ANR_INDUSTRY_CATEGORIES = frozenset({
    "PME (petite et moyenne entreprise)",
    "ETI (entreprise de taille intermédiaire)",
    "GE (grande entreprise)",
    "Divers privé",
    "Entreprises Privées",
})


def _anr_cache_dir() -> Path:
    """Le dossier de cache ANR, relu à CHAQUE appel.

    Résolu ici plutôt que capturé à l'import : un test qui remplace le cache par un dossier
    temporaire doit pouvoir le faire sans recharger le module, et sans que la suite se mette à
    lire les 136 Mo réels des jeux ANR.
    """
    return ANR_CACHE_DIR


def _anr_project_texts() -> dict[str, dict[str, Any]]:
    """Les projets ANR en cache dont l'objet nomme une OPÉRATION laser.

    Lit le cache déposé par national_projects.py -- ce module ne télécharge rien et ne fait
    aucune requête : si le cache est absent (ANR jamais collectée), il n'y a simplement pas de
    signal de cofinancement, ce qui est la vérité et non une erreur.

    L'opération est la condition qui rend le signal honnête. « femtoseconde » dans un résumé ne
    dit pas qu'on achète du procédé ; « texturation », « perçage » ou « soudage » si.

    Resserrement mesuré puis ÉCARTÉ (15/09/2026) : exiger l'opération dans le TITRE plutôt que
    dans tout le texte ferait passer de 82 projets à 21. Ça gagnerait quelques faux positifs
    (LYNRED sur des nanocristaux colloïdaux, III-V Lab sur des lasers à cascade quantique) et
    perdrait ACTIVATE (« Croissance de matériaux sous activation par laser ultrarapide »),
    MAGIC (texturation, MANUTECH USD) et AWOCAT (bio-activation d'implants, Anthogyr) -- des
    signaux réels. Précision mesurée à ~80 % sur les 25 lignes produites, avec le projet et les
    opérations lues affichés sur chaque ligne : le lecteur peut trancher, ce qu'un rappel
    amputé ne lui permettrait pas.
    """
    projets: dict[str, dict[str, Any]] = {}
    for chemin in sorted(_anr_cache_dir().glob("*projets.csv")):
        try:
            with chemin.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
                for ligne in csv.DictReader(fh, delimiter=";"):
                    code = (ligne.get("Projet.Code_Decision") or "").strip()
                    titre = (ligne.get("Projet.Titre.Francais")
                             or ligne.get("Projet.Titre.Anglais") or "").strip()
                    if not code or not titre:
                        continue
                    texte = " ".join(filter(None, (
                        titre, ligne.get("Projet.Titre.Anglais"),
                        ligne.get("Projet.Resume.Francais"), ligne.get("Projet.Resume.Anglais"))))
                    # Pré-filtre bon marché avant le lexique. Les jeux ANR pèsent 136 Mo et
                    # portent tous les projets financés depuis 2005, tous domaines confondus :
                    # appeler is_on_topic() sur chacun, c'est dérouler sept vocabulaires sur
                    # cent mille résumés. Cette regex écarte 99 % des lignes en un balayage.
                    if not ANR_ULTRAFAST_HINT.search(texte):
                        continue
                    if not is_on_topic(texte):
                        continue
                    operations = [label for label, _ in match_all_labels(texte, DOCUMENT_OPERATIONS)]
                    if not operations:
                        continue
                    projets[code] = {
                        "acronyme": (ligne.get("Projet.Acronyme") or "").strip(),
                        "titre": titre,
                        "edition": (ligne.get("AAP.Edition") or "").strip(),
                        "operations": operations,
                        "marches": [label for label, _ in match_all_labels(texte, MARKETS)],
                    }
        except OSError:
            continue
    return projets


def _anr_cofunding_signals() -> list[dict[str, Any]]:
    """Un signal par participation d'entreprise à l'un de ces projets.

    Le titre du signal porte l'opération et le marché lus par le lexique : c'est ce qui rend la
    ligne lisible sur la page Marché sans avoir à rouvrir le projet, et c'est aussi la preuve du
    classement -- les mots viennent du texte ANR, pas d'une interprétation.
    """
    projets = _anr_project_texts()
    if not projets:
        return []
    signaux: list[dict[str, Any]] = []
    for chemin in sorted(_anr_cache_dir().glob("*partenaires.csv")):
        try:
            with chemin.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
                for ligne in csv.DictReader(fh, delimiter=";"):
                    code = (ligne.get("Projet.Code_Decision") or "").strip()
                    projet = projets.get(code)
                    categorie = (ligne.get("Projet.Partenaire.Categorie_organisme") or "").strip()
                    acheteur = (ligne.get("Projet.Partenaire.Nom_organisme") or "").strip()
                    if not projet or categorie not in ANR_INDUSTRY_CATEGORIES or not acheteur:
                        continue
                    lecture = " · ".join(projet["operations"])
                    if projet["marches"]:
                        lecture += f" — {', '.join(projet['marches'])}"
                    signaux.append({
                        "source": "ANR",
                        "external_id": f"{code}|{acheteur}",
                        "buyer_name": acheteur[:200],
                        "title": f"{projet['acronyme']} — {projet['titre']} [{lecture}]",
                        "published_at": projet["edition"] or None,
                        "url": ANR_PROJECT_URL_TEMPLATE.format(code=code),
                    })
        except OSError:
            continue
    return signaux


def collect_demand_signals(
    *, lookback_days: int = DEMAND_SIGNALS_LOOKBACK_DAYS_DEFAULT, include_cofunding: bool = True,
) -> dict:
    """Point d'entrée (voir app.py: collectors["demand_signals"]).

    ``include_cofunding`` n'existe que pour les tests, comme l'``include_sources`` de
    national_projects.collect_national_projects : en collecte normale les trois passes tournent.
    Les tests de TED/BOAMP le mettent à False parce que la passe ANR lit un cache de 136 Mo sur
    le disque réel -- sans ce débrayage, chaque test d'appel d'offres relisait tous les projets
    financés depuis 2005, et la suite passait de 140 s à près de dix minutes.
    """
    from_date = date.today() - timedelta(days=max(1, lookback_days))
    from_date_yyyymmdd = from_date.strftime("%Y%m%d")
    from_date_iso = from_date.isoformat()

    ted_scanned = ted_added = boamp_scanned = boamp_added = errors = 0
    with connector_client("slow", follow_redirects=False) as client, connect(MARKET_DB) as db:
        for query in TECHNOLOGY_QUERIES:
            try:
                notices = _fetch_ted_notices(client, query, from_date_yyyymmdd)
            except Exception:
                errors += 1
            else:
                for notice in notices:
                    item = _parse_ted_notice(notice)
                    if not item or not is_on_topic(item["title"]):
                        continue
                    ted_scanned += 1
                    ted_added += _upsert_demand_signal(db, "tender", item)

        for query in BOAMP_QUERIES:
            try:
                records = _fetch_boamp_records(client, query, from_date_iso)
            except Exception:
                errors += 1
            else:
                for fields in records:
                    item = _parse_boamp_record(fields)
                    if not item or not is_on_topic(item["title"]):
                        continue
                    boamp_scanned += 1
                    boamp_added += _upsert_demand_signal(db, "tender", item)

    # Le cofinancement se lit dans le cache local : hors du client HTTP, et isolé de lui --
    # TED ou BOAMP injoignable ne doit pas priver la page de cette source-là, qui ne dépend
    # d'aucun réseau.
    anr_scanned = anr_added = 0
    if not include_cofunding:
        return {
            "ted_scanned": ted_scanned, "ted_added": ted_added,
            "boamp_scanned": boamp_scanned, "boamp_added": boamp_added,
            "anr_scanned": anr_scanned, "anr_added": anr_added,
            "errors": errors,
        }
    try:
        signaux = _anr_cofunding_signals()
    except Exception:
        errors += 1
    else:
        with connect(MARKET_DB) as db:
            for item in signaux:
                anr_scanned += 1
                anr_added += _upsert_demand_signal(db, "cofunding", item)

    return {
        "ted_scanned": ted_scanned, "ted_added": ted_added,
        "boamp_scanned": boamp_scanned, "boamp_added": boamp_added,
        "anr_scanned": anr_scanned, "anr_added": anr_added,
        "errors": errors,
    }
