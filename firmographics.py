"""Socle firmographique (chantiers 3 et 5 de l'audit collecte) : ce que l'acteur EST en tant
qu'entreprise (année de création, forme juridique, tranche d'effectif), par opposition à ce
qu'il FAIT (marché, technologie), déjà couvert par evidence/offers. Remplit ``actor_profile``.

Portée volontairement étroite : France uniquement (Companies House UK, Handelsregister DE,
Registro Mercantil ES restent à faire), et une liste d'ALIAS SIREN vérifiés À LA MAIN plutôt
qu'un matching automatique par nom. Un essai de matching par nom seul, fait pendant le
développement de ce module contre la vraie API ``recherche-entreprises.api.gouv.fr``, a produit
des faux positifs concrets :
  - "MANUTECH" seul renvoie une société homonyme sans rapport (Benesse-les-Dax, activité
    logistique) au lieu de MANUTECH-USD (Saint-Étienne).
  - "IREPA LASER" seul renvoie le comité social et économique de l'entité plutôt que l'entité
    elle-même -- son nom légal INSEE est tronqué en "INDUST RECHERCH PROCEDES APPLICAT LASER",
    qui ne contient même pas la sous-chaîne "IREPA".
  - "FEMTO Engineering" et "HEF" n'ont donné aucune correspondance fiable du tout.
Rattacher les données légales/financières d'une mauvaise entreprise à un acteur suivi serait une
erreur d'attribution silencieuse -- pire que l'absence de donnée. Chercher directement PAR SIREN
(numérique, univoque une fois vérifié) élimine ce risque ; c'est pourquoi FRENCH_REGISTRY_ALIASES
reste court et n'avance jamais tout seul (aucune tentative de deviner un SIREN non vérifié).

Hors scope, explicitement non fabriqué : revenue_eur, parent_group, sites_json,
cleanroom_iso_class, laser_systems_count (aucune source publique gratuite fiable identifiée pour
ces champs) et la table ``capability_spec`` de l'audit (specs machine chiffrées : nécessiterait
une extraction numérique depuis des datasheets PDF, un chantier à part entière).
"""

from __future__ import annotations

import re
from typing import Any

from db import ACTORS_DB, connect, utc_now
from http_client import connector_client

FRENCH_REGISTRY_API = "https://recherche-entreprises.api.gouv.fr/search"

FRENCH_REGISTRY_LABEL = "France (INSEE/RNE, recherche-entreprises.api.gouv.fr)"

# SIREN vérifié à la main, un par un, contre recherche-entreprises.api.gouv.fr (recoupé avec
# l'adresse du siège et/ou le nom commercial déjà connus pour l'acteur) -- voir le docstring du
# module pour pourquoi ce n'est PAS un matching automatique par nom.
FRENCH_REGISTRY_ALIASES: dict[str, str] = {
    "ALPHANOV": "493635817",       # siège Institut d'Optique d'Aquitaine, Talence ; sigle="ALPHANOV" exact
    "MANUTECH USD": "753487164",   # nom_raison_sociale="MANUTECH-USD" exact ; siège Saint-Étienne
    "IREPA LASER": "402256184",    # nom_commercial="IREPA LASER" exact ; siège Illkirch (Strasbourg)
}

_DATE_RE = re.compile(r"^(\d{4})-\d{2}-\d{2}")


def _parse_registry_row(row: dict[str, Any]) -> dict[str, Any]:
    """Traduit un enregistrement de l'API en champs actor_profile, sans rien décoder au-delà
    de ce que l'API fournit littéralement -- nature_juridique/tranche_effectif_salarie sont des
    codes INSEE standardisés, stockés tels quels plutôt que traduits en libellé pour ne jamais
    risquer une décodification erronée d'un code peu documenté."""
    match = _DATE_RE.match(str(row.get("date_creation") or ""))
    return {
        "founded_year": int(match.group(1)) if match else None,
        "legal_form_code": row.get("nature_juridique"),
        "headcount_bracket_code": row.get("tranche_effectif_salarie"),
        "registry_id": row.get("siren"),
        "registry_active": row.get("etat_administratif") == "A",
    }


def _upsert_actor_profile(db, actor_id: int, fields: dict[str, Any], source_url: str) -> int:
    """Une ligne par acteur (clé primaire = actor_id) : un rafraîchissement remplace toujours
    l'ancienne valeur plutôt que de l'ignorer -- contrairement aux faits marché, une donnée
    firmographique n'a pas plusieurs "preuves" concurrentes à départager."""
    stamp = utc_now()
    existing = db.execute("SELECT actor_id FROM actor_profile WHERE actor_id=?", (actor_id,)).fetchone()
    if existing:
        db.execute(
            """UPDATE actor_profile SET founded_year=?,legal_form_code=?,headcount_bracket_code=?,
                      registry_id=?,registry_name=?,registry_active=?,source_url=?,as_of_date=?,updated_at=?
               WHERE actor_id=?""",
            (
                fields["founded_year"], fields["legal_form_code"], fields["headcount_bracket_code"],
                fields["registry_id"], FRENCH_REGISTRY_LABEL, int(fields["registry_active"]), source_url,
                stamp[:10], stamp, actor_id,
            ),
        )
        return 0
    db.execute(
        """INSERT INTO actor_profile(
               actor_id,founded_year,legal_form_code,headcount_bracket_code,
               registry_id,registry_name,registry_active,source_url,as_of_date,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            actor_id, fields["founded_year"], fields["legal_form_code"], fields["headcount_bracket_code"],
            fields["registry_id"], FRENCH_REGISTRY_LABEL, int(fields["registry_active"]), source_url,
            stamp[:10], stamp, stamp,
        ),
    )
    return 1


def collect_french_registry() -> dict:
    """Point d'entrée (voir app.py: collectors["firmographics"]).

    Cherche directement PAR SIREN (jamais par nom) pour chaque alias vérifié dont l'acteur
    existe encore et est actif ; vérifie que la réponse porte bien le SIREN demandé avant
    d'écrire quoi que ce soit (garde-fou contre une réponse d'API malformée/vide plutôt qu'une
    vraie protection de matching, puisque le SIREN est déjà univoque par construction).
    """
    with connect(ACTORS_DB) as db:
        actor_ids = {
            row["name"]: row["id"]
            for row in db.execute("SELECT id,name FROM actors WHERE active=1").fetchall()
        }

    matched = added = updated = errors = 0
    with connector_client("api") as client:
        for actor_name, siren in FRENCH_REGISTRY_ALIASES.items():
            actor_id = actor_ids.get(actor_name)
            if not actor_id:
                continue  # actor renamed/deactivated/removed since this alias was verified
            try:
                response = client.get(FRENCH_REGISTRY_API, params={"q": siren, "page": 1, "per_page": 1})
                response.raise_for_status()
                results = response.json().get("results", [])
                row = next((r for r in results if r.get("siren") == siren), None)
                if not row:
                    errors += 1
                    continue
                matched += 1
                fields = _parse_registry_row(row)
                source_url = f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"
                with connect(ACTORS_DB) as db:
                    created = _upsert_actor_profile(db, actor_id, fields, source_url)
                added += created
                updated += int(not created)
            except Exception:
                errors += 1

    return {
        "aliases_configured": len(FRENCH_REGISTRY_ALIASES),
        "matched": matched,
        "profiles_added": added,
        "profiles_updated": updated,
        "errors": errors,
    }
