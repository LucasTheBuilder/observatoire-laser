"""Identité de groupe via GLEIF/LEI (§8.5 audit veille, 30/08/2026, Lot 3 §3.6) : "le LEI mérite
d'être fait en premier : il résout à lui seul les rattachements de groupe, qui sont votre angle
mort sur les consolidations." Identifiant univoque, gratuit, multi-pays, sans clé API (voir
GLEIF_API, api.gleif.org) -- vérifié en direct contre la vraie API avant d'écrire ce module.

Même discipline que firmographics.py (SIREN français) : JAMAIS de matching automatique par nom.
Un essai en direct contre la vraie API GLEIF (30/08/2026) a reproduit exactement le même risque
que celui déjà documenté par firmographics.py pour le registre français :
  - "CEIT" (notre acteur espagnol) ne renvoie que des "CEIT S.R.L." italiennes sans rapport.
  - "HEF" (3 lettres, trop générique) renvoie 651 fiducies/trusts britanniques.
  - "LASEA" renvoie aussi "Lasea AB", une société suédoise INACTIVE sans rapport, à côté du bon
    "LASER ENGINEERING APPLICATIONS" belge (dont "LASEA" est justement le nom commercial déclaré
    à GLEIF -- confirmé en consultant la fiche).
  - "GFH GmbH" (notre acteur, fabricant laser de Deggendorf) matche par erreur "GFH Holding
    GmbH" -- même sigle, mais Munich, une ville différente (croisé avec gfh-gmbh.de) : entité
    distincte, exclue.
GLEIF_LEI_ALIASES reste donc court et vérifié à la main (adresse/pays GLEIF croisés avec le site
officiel de l'acteur) avant tout ajout, exactement comme FRENCH_REGISTRY_ALIASES -- jamais
élargi automatiquement. La plupart des petits acteurs suivis (centres technologiques, PME,
instituts Fraunhofer) n'ont tout simplement pas de LEI : ce n'est pas une lacune de ce module,
le LEI est surtout requis pour les transactions financières/valeurs mobilières, pas pour
l'immatriculation générale d'une entreprise.

parent_group n'est écrit QUE quand GLEIF porte lui-même une relation de maison mère (direct-
parent, ou à défaut ultimate-parent) pour cette LEI. L'absence de relation (HTTP 404 -- le cas le
plus fréquent : le reporting Level 2 "who owns whom" est déclaratif et partiel, voir gleif.org)
laisse parent_group inchangé en base, jamais mis à NULL ni deviné autrement.
"""

from __future__ import annotations

import httpx

from db import ACTORS_DB, connect, utc_now
from scrapers import HEADERS

GLEIF_API = "https://api.gleif.org/api/v1"
GLEIF_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
GLEIF_REGISTRY_LABEL = "GLEIF (api.gleif.org, LEI)"

# Un LEI par acteur, vérifié à la main le 30/08/2026 contre https://api.gleif.org/api/v1/
# lei-records (adresse/pays/statut croisés avec le site officiel de l'acteur) -- voir le
# docstring du module pour les faux positifs concrets écartés en chemin (CEIT, HEF, GFH GmbH,
# LASEA-vs-Lasea-AB). Liste volontairement courte, n'avance jamais toute seule.
GLEIF_LEI_ALIASES: dict[str, str] = {
    "3D-Micromac": "5299009VA55U7RPKCK18",
    "KMLT": "9676001CRC57O7F8E370",
    "LASEA": "549300CLQG879RZUGS10",
    "LightFab": "391200387VZPKMQATH71",
    "Meliad": "9695000SF8PAABFBLR38",
    "Tekniker": "959800G45JTQFCJ4L394",
}


def _record_url(lei: str) -> str:
    # Vérifié en direct (30/08/2026) : ce lien profond de l'UI publique GLEIF résout bien vers
    # la fiche du LEI demandé (route SPA côté client, jamais interprétée par le serveur).
    return f"https://search.gleif.org/#/record/{lei}"


def _fetch_lei_record(client: httpx.Client, lei: str) -> dict:
    response = client.get(f"{GLEIF_API}/lei-records/{lei}")
    response.raise_for_status()
    return response.json()["data"]


def _fetch_parent_name(client: httpx.Client, lei: str, relation: str) -> str | None:
    """relation: 'direct-parent' ou 'ultimate-parent'. 404 = aucune relation reportée à GLEIF
    pour ce LEI (le cas le plus fréquent, voir docstring du module) -- pas une erreur."""
    response = client.get(f"{GLEIF_API}/lei-records/{lei}/{relation}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return str(response.json()["data"]["attributes"]["entity"]["legalName"]["name"])


def _upsert_actor_profile(
    db, actor_id: int, *, lei: str, active: bool, parent_group: str | None, source_url: str,
) -> int:
    """Une ligne par acteur (clé primaire = actor_id), même contrat que firmographics.
    _upsert_actor_profile. parent_group utilise COALESCE plutôt qu'un remplacement inconditionnel
    : contrairement à registry_id/registry_name/registry_active (toujours réécrits par le
    dernier passage GLEIF), une absence de relation ne doit jamais effacer une valeur déjà
    connue -- voir le docstring du module."""
    stamp = utc_now()
    existing = db.execute("SELECT actor_id FROM actor_profile WHERE actor_id=?", (actor_id,)).fetchone()
    if existing:
        db.execute(
            """UPDATE actor_profile SET registry_id=?,registry_name=?,registry_active=?,
                      parent_group=COALESCE(?,parent_group),source_url=?,as_of_date=?,updated_at=?
               WHERE actor_id=?""",
            (lei, GLEIF_REGISTRY_LABEL, int(active), parent_group, source_url, stamp[:10], stamp, actor_id),
        )
        return 0
    db.execute(
        """INSERT INTO actor_profile(
               actor_id,registry_id,registry_name,registry_active,parent_group,source_url,as_of_date,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (actor_id, lei, GLEIF_REGISTRY_LABEL, int(active), parent_group, source_url, stamp[:10], stamp, stamp),
    )
    return 1


def collect_gleif_group_identity() -> dict:
    """Point d'entrée (voir app.py: collectors["gleif"]).

    Cherche directement PAR LEI (jamais par nom) pour chaque alias vérifié dont l'acteur existe
    encore et est actif -- même garde-fou de principe que firmographics.collect_french_registry.
    """
    with connect(ACTORS_DB) as db:
        actor_ids = {
            row["name"]: row["id"]
            for row in db.execute("SELECT id,name FROM actors WHERE active=1").fetchall()
        }

    matched = added = updated = parent_groups_found = errors = 0
    with httpx.Client(headers=HEADERS, timeout=GLEIF_TIMEOUT) as client:
        for actor_name, lei in GLEIF_LEI_ALIASES.items():
            actor_id = actor_ids.get(actor_name)
            if not actor_id:
                continue  # actor renamed/deactivated/removed since this alias was verified
            try:
                record = _fetch_lei_record(client, lei)
                active = record["attributes"]["entity"].get("status") == "ACTIVE"
                parent_group = (
                    _fetch_parent_name(client, lei, "direct-parent")
                    or _fetch_parent_name(client, lei, "ultimate-parent")
                )
                matched += 1
                parent_groups_found += int(parent_group is not None)
                with connect(ACTORS_DB) as profile_db:
                    created = _upsert_actor_profile(
                        profile_db, actor_id, lei=lei, active=active,
                        parent_group=parent_group, source_url=_record_url(lei),
                    )
                added += created
                updated += int(not created)
            except Exception:
                errors += 1

    return {
        "aliases_configured": len(GLEIF_LEI_ALIASES),
        "matched": matched,
        "profiles_added": added,
        "profiles_updated": updated,
        "parent_groups_found": parent_groups_found,
        "errors": errors,
    }
