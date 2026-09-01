"""Registre déclaratif des sources externes sollicitées par l'app (chantier /sources, 01/09/2026) :
chaque connecteur (cordis.py, gleif.py, openalex.py, patent.py, demand_signals.py, ...) déclare sa
propre URL/label/variable d'environnement sans qu'aucun endroit ne les récapitule -- ce module
comble ce vide, sans dupliquer la moindre logique de collecte : il se contente de RÉFÉRENCER
chaque source déjà codée ailleurs et de calculer, à l'appel, si sa clé (le cas échéant) est
présente dans l'environnement.

Volontairement une simple liste de faits statiques (domaine, module, clé requise) plutôt qu'un
enrichissement en direct (santé/dernier run) : ce que fait déjà /api/collection-health à partir de
site_profiles/collection_runs, pas la responsabilité de ce module.

Statuts possibles (``SourceInfo.status``) :
- ``active``          : aucune clé requise, connecteur déjà codé.
- ``configured``       : clé requise, présente dans l'environnement.
- ``missing_key``      : clé requise, absente ou vide -- le connecteur existe mais tourne en
                          ``not_configured`` (voir patent.collect_patents).
- ``not_implemented``  : aucun connecteur codé ; recensée ici pour ne pas la ré-oublier (audit
                          firmographics.py §portée, 30/08/2026 -- Companies House/Handelsregister/
                          Registro Mercantil, écartées faute de temps, pas faute de valeur).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    domain: str
    module: str | None
    purpose: str
    target: str
    env_vars: tuple[str, ...] = ()

    @property
    def requires_key(self) -> bool:
        return bool(self.env_vars)

    @property
    def status(self) -> str:
        if self.module is None:
            return "not_implemented"
        if not self.requires_key:
            return "active"
        configured = all(os.environ.get(var, "").strip() for var in self.env_vars)
        return "configured" if configured else "missing_key"


# Une entrée par source réellement interrogée par un connecteur (ou explicitement écartée avec
# une raison documentée) -- jamais une source "prévue" sans trace dans le code. L'ordre suit
# l'ordre de collecte historique (chantier 1/2 puis 3 puis Lot 3/4 de l'audit veille).
SOURCES: tuple[SourceInfo, ...] = (
    SourceInfo(
        id="actor_websites",
        name="Sites web des acteurs suivis",
        domain="(un domaine par acteur, voir actor_sources)",
        module="scrapers.py",
        purpose="Preuves marché/offres extraites des pages officielles des acteurs suivis.",
        target="market.db (evidence/offers), actors.db (site_profiles)",
    ),
    SourceInfo(
        id="crossref",
        name="Crossref",
        domain="api.crossref.org",
        module="scrapers.py",
        purpose="Publications récentes sur des requêtes thématiques génériques (non attribuées à un acteur).",
        target="technology.db (documents)",
    ),
    SourceInfo(
        id="openalex",
        name="OpenAlex",
        domain="api.openalex.org",
        module="openalex.py",
        purpose="Publications rattachées à l'institution d'un acteur (vérifiée par domaine).",
        target="technology.db (documents), actors.db (actor_candidates)",
        env_vars=("OPENALEX_API_KEY",),
    ),
    SourceInfo(
        id="cordis",
        name="CORDIS – HORIZON Projects",
        domain="data.europa.eu / cordis.europa.eu",
        module="cordis.py",
        purpose="Projets Horizon Europe : participations, partenaires de consortium, axes technologiques.",
        target="actors.db (actor_events, actor_relations), technology.db (technology_signals)",
    ),
    SourceInfo(
        id="gleif",
        name="GLEIF (LEI)",
        domain="api.gleif.org",
        module="gleif.py",
        purpose="Identifiant légal univoque, résout les rattachements de groupe (parent_group).",
        target="actors.db (actor_profile)",
    ),
    SourceInfo(
        id="firmographics_fr",
        name="Registre des entreprises – France",
        domain="recherche-entreprises.api.gouv.fr",
        module="firmographics.py",
        purpose="Firmographie (année de création, forme juridique, tranche d'effectif) par SIREN vérifié.",
        target="actors.db (actor_profile)",
    ),
    SourceInfo(
        id="ted",
        name="TED (Tenders Electronic Daily)",
        domain="api.ted.europa.eu",
        module="demand_signals.py",
        purpose="Appels d'offres publics UE mentionnant les technologies suivies (signal de demande).",
        target="market.db",
    ),
    SourceInfo(
        id="boamp",
        name="BOAMP",
        domain="boamp-datadila.opendatasoft.com",
        module="demand_signals.py",
        purpose="Appels d'offres publics France (requêtes françaises dédiées, BOAMP ne publie qu'en FR).",
        target="market.db",
    ),
    SourceInfo(
        id="epo_ops",
        name="EPO Open Patent Services / Espacenet",
        domain="ops.epo.org",
        module="patent.py",
        purpose="Brevets par déposant suivi et par classe CPC B23K26 (signal le plus précoce, 18 mois d'avance).",
        target="technology.db (documents), actors.db (actor_candidates)",
        env_vars=("EPO_OPS_KEY", "EPO_OPS_SECRET"),
    ),
    SourceInfo(
        id="wayback_cdx",
        name="Wayback Machine (CDX)",
        domain="web.archive.org",
        module="wayback_retrodating.py",
        purpose="Rétro-datation des citations déjà en base via le plus ancien snapshot les contenant.",
        target="market.db (evidence_sources.first_appeared_at)",
    ),
    SourceInfo(
        id="press_rss",
        name="Presse spécialisée (RSS)",
        domain="laserfocusworld.com, photonics.com",
        module="press.py",
        purpose="Annonces produit/contrats/partenariats non couvertes par les sources structurées.",
        target="actors.db (actor_events)",
    ),
    SourceInfo(
        id="anthropic",
        name="Anthropic (Claude)",
        domain="api.anthropic.com",
        module="hybrid.py",
        purpose="Extraction IA de secours pour les faits marché ambigus -- outil transverse, pas une source de veille.",
        target="market.db (fact_status='partial'/'review')",
        env_vars=("ANTHROPIC_API_KEY",),
    ),
    SourceInfo(
        id="companies_house_uk",
        name="Companies House (UK)",
        domain="api.company-information.service.gov.uk",
        module=None,
        purpose="Firmographie UK -- API REST publique avec clé gratuite, connecteur jamais écrit (firmographics.py, portée France uniquement).",
        target="(non implémenté)",
        env_vars=("COMPANIES_HOUSE_API_KEY",),
    ),
    SourceInfo(
        id="handelsregister_de",
        name="Handelsregister (DE)",
        domain="handelsregister.de",
        module=None,
        purpose="Firmographie DE -- disponibilité d'une API publique gratuite non vérifiée, à confirmer avant d'écrire un connecteur.",
        target="(non implémenté)",
    ),
    SourceInfo(
        id="registro_mercantil_es",
        name="Registro Mercantil (ES)",
        domain="registradores.org",
        module=None,
        purpose="Firmographie ES -- disponibilité d'une API publique gratuite non vérifiée, à confirmer avant d'écrire un connecteur.",
        target="(non implémenté)",
    ),
)


def list_sources() -> list[dict]:
    """Vue JSON-able de SOURCES, statut de clé calculé à l'appel (jamais mis en cache : une clé
    ajoutée dans .env doit apparaître au prochain appel sans redémarrer le connecteur qui la lit)."""
    return [
        {
            "id": s.id,
            "name": s.name,
            "domain": s.domain,
            "module": s.module,
            "purpose": s.purpose,
            "target": s.target,
            "requires_key": s.requires_key,
            "env_vars": list(s.env_vars),
            "status": s.status,
        }
        for s in SOURCES
    ]
