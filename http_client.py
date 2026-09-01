"""Identité HTTP sortante commune à tous les connecteurs.

Avant ce module, chaque connecteur (cordis, openalex, patent, press, gleif, firmographics,
wayback_retrodating, demand_signals, capabilities) redéclarait son propre timeout et allait
chercher ``HEADERS`` dans ``scrapers.py`` -- c'est-à-dire dans le crawler, qui n'a rien à voir
avec eux. Neuf constantes de timeout coexistaient, dont quatre strictement identiques, et deux
connecteurs (patent, wayback_retrodating) n'envoyaient aucun en-tête : leurs requêtes sortaient
donc sans User-Agent ni contact, contrairement aux sept autres.

Ce module porte les deux seules choses qu'un appel sortant doit partager : QUI nous sommes
(``HEADERS``) et COMBIEN DE TEMPS on attend (``TIMEOUTS``, nommés par usage plutôt que par
connecteur, puisque plusieurs connecteurs partagent le même besoin).
"""

from __future__ import annotations

import os

import httpx

CRAWLER_CONTACT = os.getenv("CRAWLER_CONTACT", "").strip()
USER_AGENT = "ObservatoireLaser/3.4.1-optimized (+local-relation deterministic crawler" + (
    f"; contact: {CRAWLER_CONTACT})" if CRAWLER_CONTACT else ")"
)
# Tout appel sortant de l'observatoire s'identifie avec ces en-têtes, sans exception : c'est la
# contrepartie de politesse du crawl, et le seul moyen pour un site tiers de nous joindre.
HEADERS = {"User-Agent": USER_AGENT}

# Profils nommés par USAGE. "crawl" est le plus serré (petits sites industriels/institutionnels
# qu'il ne faut pas faire attendre), "api" couvre les API publiques bien dimensionnées, "slow"
# les services connus pour répondre lentement (archives, offices de brevets), "bulk" les
# téléchargements de jeux de données complets.
TIMEOUTS: dict[str, httpx.Timeout] = {
    "crawl": httpx.Timeout(18.0, connect=8.0),
    "api": httpx.Timeout(20.0, connect=8.0),
    "slow": httpx.Timeout(30.0, connect=10.0),
    "bulk": httpx.Timeout(180.0, connect=15.0),
}


def connector_client(profile: str = "api", *, follow_redirects: bool = True) -> httpx.Client:
    """Client HTTP pour un connecteur, toujours identifié.

    Passer par cette fabrique plutôt que d'appeler ``httpx.Client`` directement garantit que les
    en-têtes d'identification sont présents -- c'est précisément ce que patent.py et
    wayback_retrodating.py omettaient en construisant leur client à la main.
    """
    if profile not in TIMEOUTS:
        raise ValueError(f"Profil de timeout inconnu : {profile!r} (attendus : {sorted(TIMEOUTS)})")
    return httpx.Client(headers=HEADERS, timeout=TIMEOUTS[profile], follow_redirects=follow_redirects)
