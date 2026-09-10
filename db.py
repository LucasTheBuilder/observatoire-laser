"""Couche de persistance de l'Observatoire : schéma SQLite, migrations et fonctions CRUD.

Ce fichier ne fait AUCUN appel réseau (contrairement à scrapers.py) : c'est uniquement la
couche base de données. Il gère trois fichiers SQLite séparés (chacun avec sa propre
connexion/transaction, voir connect()) :

- ACTORS_DB (actors.db) : la liste des acteurs suivis (entreprises/labos concurrents ou
  partenaires), leurs pages web connues (actor_sources) et l'état du profil de crawl de
  chacun (site_profiles) -- alimenté par scrapers.scrape_actors().
- MARKET_DB (market.db) : les "faits marché" extraits du contenu des sites (evidence : quelle
  entreprise fait quoi, pour quel marché/composant/opération), les offres/capacités
  concurrentes (offers), et les preuves sourcées qui les justifient (evidence_sources,
  offer_sources) -- alimenté par scrapers.scrape_market().
- TECH_DB (technology.db) : les publications/documents scientifiques collectés (documents) et
  les signaux de maturité technologique transverses (technology_signals) -- alimenté par
  scrapers.scrape_technology().

init_databases() crée (ou met à jour, via des migrations additives) le schéma des trois
bases à chaque démarrage de l'application (voir app.py: lifespan). Le reste du fichier fournit
des fonctions utilitaires : connexion/transaction (connect), clés d'identité déterministes
pour dédupliquer les faits (market_fact_key, application_key, offer_fact_key,
technology_signal_key), sauvegardes (backup_all_databases), et des opérations CRUD sur les
acteurs (create_actor, update_actor_classification, delete_actor, ...).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# lexicon.py est une feuille (re, unicodedata, functools) : elle n'importe ni db ni scrapers,
# donc aucun cycle. Seul DOCUMENT_LEXICONS est utilisé ici, pour déduire la dimension d'un
# libellé d'axe (voir _reconcile_technology_signal_dimensions).
from lexicon import DOCUMENT_LEXICONS

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ACTORS_DB = DATA_DIR / "actors.db"
MARKET_DB = DATA_DIR / "market.db"
TECH_DB = DATA_DIR / "technology.db"
BACKUP_RETENTION_COUNT = int(os.getenv("BACKUP_RETENTION_COUNT", "14"))


# Liste "en dur" des acteurs suivis dès le premier démarrage : (nom, pays, rôle, priority,
# official_url). `priority=1` marque les acteurs jugés les plus importants (crawl plus
# profond/plus large, voir site_profiles.crawl_budget). init_databases() insère cette liste
# à chaque démarrage via un UPSERT (ON CONFLICT DO UPDATE), donc la modifier ici et redémarrer
# l'appli met à jour les acteurs existants sans dupliquer de lignes. Des acteurs additionnels
# peuvent aussi être ajoutés depuis l'UI via create_actor(), sans toucher à ce fichier.
ACTORS = [
    ("ALPHANOV", "France", "Centre technologique - procédés laser & micro-usinage", 1, "https://www.alphanov.com"),
    ("MANUTECH USD", "France", "Plateforme technologique femtoseconde - texturation/fonctionnalisation", 1, "https://www.manutech-usd.fr"),
    ("HEF", "France", "Référence interne - groupe industriel", 1, "https://hef.group"),
    # lasea.com, not lasea.eu: 108 des 109 sources crawlées résolvent sur lasea.com (HTTP 200,
    # vérifié en base) -- lasea.eu semble n'être conservé qu'en page d'accueil/redirection.
    # Voir audit v8 §2.7 : deux mécanismes matchent sur ce domaine (site_profiles.DOMAIN_OVERRIDES,
    # openalex._find_institution) et ne s'appliquaient donc probablement pas avec l'ancien domaine.
    ("LASEA", "Belgique", "Systèmes femtoseconde & développement d'applications", 1, "https://lasea.com"),
    ("IREPA LASER", "France", "Centre technologique - développement, industrialisation & production laser", 0, "https://www.irepa-laser.com"),
    ("Pulsar Photonics", "Allemagne", "Développement d'applications USP & fabrication sous contrat", 0, "https://www.pulsar-photonics.de"),
    ("Lightmotif", "Pays-Bas", "Micro-usinage USP & texturation - process development / contract manufacturing", 0, "https://www.lightmotif.nl"),
    ("FEMTO Engineering", "France", "Centre d'ingénierie - micro/nano-usinage femtoseconde", 0, "https://www.femto-engineering.fr"),
    ("Workshop of Photonics", "Lituanie", "Microfabrication femtoseconde - services, production & équipements", 0, "https://wophotonics.com"),
    ("LightFab", "Allemagne", "SLE / microfabrication 3D du verre", 0, "https://lightfab.de"),
    ("Femtika", "Lituanie", "Microfabrication 3D femtoseconde - équipements & contract manufacturing", 0, "https://femtika.com"),
    ("Micreon", "Allemagne", "Contract manufacturing en micro-usinage USP", 0, "https://www.micreon.de"),
    ("OpTek Systems", "Royaume-Uni", "Process development & contract laser micromachining", 0, "https://optek.humaneticsgroup.com"),
    ("Blueacre Technology", "Irlande", "Laser micromachining & Nitinol contract manufacturing - MedTech", 0, "https://blueacretechnology.com"),
    ("Oxford Lasers", "Royaume-Uni", "Contract laser micromachining & process development", 0, "https://oxfordlasers.com"),
    ("3D-Micromac", "Allemagne", "Développement de procédés laser & contract manufacturing", 0, "https://3d-micromac.com"),
    ("Fraunhofer ILT", "Allemagne", "Institut de recherche appliquée - procédés USP", 0, "https://www.ilt.fraunhofer.de/en.html"),
    ("Laser Zentrum Hannover", "Allemagne", "Institut technologique - micromachining USP", 0, "https://www.lzh.de/en"),
    ("FEMTOprint", "Suisse", "CDMO de microfabrication 3D du verre par femtoseconde/SLE", 0, "https://www.femtoprint.ch"),
    ("LightPulse Laser Precision", "Allemagne", "Développement / échantillonnage & micro-usinage USP", 0, "https://www.light-pulse.de/en-gb/"),
    # Audit P1 "acteurs manquants" (30/08/2026) : HAILTEC, MICROMACH GmbH et laserKRAFTwerk
    # avaient déjà été ajoutés directement en base via create_actor()/l'API (donc absents de
    # cette liste statique bien qu'actifs et crawlés) -- reportés ici pour qu'une installation
    # neuve les inclue aussi, avec exactement les valeurs déjà en base (ne rien réécrire).
    ("HAILTEC", "Allemagne", "Sous-traitance laser femtoseconde", 0, "https://www.hailtec.de/"),
    ("MICROMACH GmbH", "Allemagne", "Contract manufacturing laser femtoseconde/picoseconde", 0, "https://micromach.de/"),
    # laserKRAFTwerk : crawl actif mais site en 403 face à notre User-Agent auto-déclaré
    # (voir USER_AGENT ci-dessus) -- couverture actuellement limitée à la page d'accueil.
    ("laserKRAFTwerk", "Allemagne", "Marquage/gravure laser pico/femtoseconde", 0, "https://www.laserkraftwerk.de/"),
    # ACunity : spin-off Fraunhofer ILT (Aix-la-Chapelle). Activité principale = EHLA (dépôt
    # laser haute vitesse, hors périmètre USP) ; seule la ligne "Micro/Nano Processing" (perçage
    # hélicoïdal, impulsions <15 ps -- voir SEED_SOURCES) relève du laser ultra-rapide suivi ici.
    ("ACunity", "Allemagne", "Perçage/micro-usinage laser USP (Helical Drilling Optics, <15 ps)", 0, "https://acunity.de/en/"),
    # Audit Horizon 2 (30/08/2026) : deux centres technologiques T1 supplémentaires identifiés
    # comme sous-couverts (référence W6/W13 de l'audit).
    # TWI (Cambridge, RU) : programme de recherche dédié USP picoseconde (base de données de
    # paramètres procédé, transfert industriel) -- vérifié via twi-global.com, pas seulement cité.
    ("TWI", "Royaume-Uni", "Centre technologique - micro-usinage & modification de surface laser USP", 0, "https://www.twi-global.com/"),
    # BIAS (Brême, DE) : institut multi-technologie (comme Fraunhofer/Tekniker/CEIT) -- vérifié
    # que le laser ultra-rapide y est bien utilisé, pas seulement du laser conventionnel :
    # "Zur Anwendung kommen dabei gepulste Femtosekunden-, Pikosekunden- und Nanosekunden-Laser
    # sowie CW-Laser" (bias.de/laserbearbeitung), pour la microstructuration/ablation.
    ("BIAS", "Allemagne", "Institut de recherche appliquée - laser USP (structuration, ablation) & métrologie optique", 0, "https://www.bias.de/en-gb/"),
]

# Quelques URLs de pages connues, injectées d'office dans actor_sources au démarrage (avant
# même le premier crawl) pour garantir que ces pages "à forte valeur" seront visitées.
SEED_SOURCES = [
    ("ALPHANOV", "https://www.alphanov.com/en/collaborative-projects/femtocell-pilot-line-next-generation-gen4-batteries", "application"),
    ("HEF", "https://hef.group/en/glacier-project-femtosecond-laser-and-glass-cutting/", "application"),
    ("Pulsar Photonics", "https://www.pulsar-photonics.de/en/laser-contract-manufacturing/", "service"),
    ("LightFab", "https://lightfab.de/", "application"),
    ("FEMTO Engineering", "https://www.femto-engineering.fr/en/", "service"),
    ("MANUTECH USD", "https://www.manutech-usd.fr/en/", "official"),
    # ACunity's homepage is dominated by EHLA (off-topic, see ACTORS above) -- seed the actual
    # USP-relevant product page directly so the crawler doesn't have to find it on its own.
    ("ACunity", "https://acunity.de/micro-nano-processing/?lang=en_us", "application"),
    ("TWI", "https://www.twi-global.com/what-we-do/research-and-technology/current-research-programmes/twi-core-research/development-of-ultrashort-laser-micromachining-and-surface-modification-database", "technology"),
    ("BIAS", "https://www.bias.de/laserbearbeitung", "technology"),
]

# Seed values are now kept as canonical dimensions. Generation/application detail belongs in stage/quote,
# not in the component label, so seed and scraped evidence can deduplicate correctly.
LEGACY_SEED_GROUPS = (
    "ap-technologies-blueacre-acquisition-2026",
    "lightfab-glass-microfabrication",
    "femtocell-gen4-pilot-line",
    "glacier-optical-glass",
)

# Audit veille du 30/08/2026 (§10.2) : régénéré depuis les 40 lignes réellement en base
# (extraction_mode IS NULL, fact_status="validated" avant correctif) -- ce ne sont PAS des
# faits extraits par le pipeline, mais des notes de lecture saisies à la main entre le 25 et le
# 27/08/2026 (source_group in batch2-2026/deep5-2026/femtoprint-fiche-2026/optek-deep-2026/
# optionc-batch, plus 3 lignes unitaires), reformulées en français à partir de pages en anglais.
# Un exemple concret (voir l'audit) : "Sous-rubriques sur les guides d'onde en verre, ferrules
# de fibres, réseaux de trous..." pour femtoprint.ch/applications/photonics.asp -- une note de
# lecture, pas une citation extraite (aucune garantie que le texte apparaisse mot pour mot
# dans la page source, contrairement à une extraction réelle du pipeline).
#
# Régénérée pour la reproductibilité (SEED_EVIDENCE était vidée en v3.3.1 -- "legacy seeds are
# kept only as review material", un commentaire qui ne correspondait déjà plus à la réalité : la
# liste était vide, pas "gardée" -- une réinstallation ou un reset de market.db perdait ces 40
# faits en silence, sans que rien ne l'indique). MAIS : contrairement à l'ancien mécanisme,
# cette liste n'est plus appliquée automatiquement à chaque démarrage (voir init_databases, qui
# ne l'appelle plus) -- un même mécanisme sans garde empoisonnait aussi toute base FRAÎCHE (un
# nouveau déploiement, ou n'importe quel test de la suite qui appelle init_databases() se
# retrouvait avec 40 faits sur des acteurs précis qu'il n'a jamais collectés). Utiliser
# restore_seed_evidence() explicitement, à la main, seulement en cas de perte réelle de
# market.db pour CETTE installation. review_status='review' (plus 'accepted' comme avant ce
# correctif, voir _upsert_seed_evidence) : reproduire ces items les fait repasser par une vraie
# validation humaine plutôt que d'entrer validés d'office. is_verbatim=0 est appliqué
# automatiquement (voir la migration "is_verbatim=0 WHERE extraction_mode IS NULL" plus bas),
# jamais fixé ici.
SEED_EVIDENCE: list[dict[str, Any]] = [
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Quantum', "component": 'Pièges à ions', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/quantum.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": 'La page dédiée présente la microfabrication quantique et cite les pièges à ions, les capteurs quantiques, ainsi que des applications pour le calcul et la cryptographie.'},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Spatial', "component": 'Composants en verre', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/space.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": "Composants de mécanique de précision, d'optique et de photonique pour satellites, télescopes et missions d'espace profond."},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Optique', "component": 'Interposeurs en verre', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/optics.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": "Sous-rubriques sur l'optique miniaturisée, la micro-optique, le DFM, la métallisation/revêtement optique et les interposeurs en verre."},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Photonique', "component": "Guides d'onde", "operation": "Écriture de guide d'onde", "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/photonics.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": "Sous-rubriques sur les guides d'onde en verre, ferrules de fibres, réseaux de trous, éléments micro-optiques et microcomposants en verre."},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Médical', "component": 'Dispositifs microfluidiques', "operation": 'Fonctionnalisation de surface', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/medtech.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": "Sous-rubriques consacrées à l'encapsulation en verre, la microfabrication du verre, les MEMS biocompatibles, l'étanchéité hermétique et les biocapteurs microfluidiques."},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Sciences de la vie', "component": 'Dispositifs microfluidiques', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/life-sciences.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": "Renvoie notamment vers la découverte de médicaments, le diagnostic, les masters en verre, l'isolement d'anticorps, les thérapies cellulaires et l'analyse single-cell."},
    {"actor": '3D-Micromac', "bucket": 'existing', "market": 'Semi-conducteurs', "component": 'Wafers', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://3d-micromac.com/laser-micromachining/markets/', "title": '3D-Micromac — page officielle', "date": None, "quote": 'Ohmic contact formation (OCF) on SiC power device backsides; magnetic sensor (GMR/TMR) production; chip trimming and link cutting on silicon, SiC, germanium and GaAs wafers.'},
    {"actor": '3D-Micromac', "bucket": 'existing', "market": 'Photovoltaïque', "component": 'Wafers', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://3d-micromac.com/laser-micromachining/markets/', "title": '3D-Micromac — page officielle', "date": None, "quote": 'Ultra-high-speed Thermal Laser Separation for particle-free cutting of full-size silicon solar wafers into half cells or quarter cells.'},
    {"actor": '3D-Micromac', "bucket": 'existing', "market": 'Photonique', "component": "Guides d'onde", "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://3d-micromac.com/laser-micromachining/markets/', "title": '3D-Micromac — page officielle', "date": None, "quote": 'AR waveguide and eyepiece singulation on borosilicate, aluminum silicate (Gorilla), quartz and specialty glass.'},
    {"actor": 'MeKo', "bucket": 'existing', "market": 'Médical', "component": 'Stents', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://www.meko.de/en/medtech', "title": 'MeKo — page officielle', "date": None, "quote": 'NiTi Stents (Nitinol) manufactured with highly precise laser cutting combined with reliable shaping processes for perfect geometry.'},
    {"actor": 'MeKo', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://www.meko.de/en/medtech', "title": 'MeKo — page officielle', "date": None, "quote": 'Heart Valve Frames, Drug Delivery Balloons, Surgical Instruments and Bone Nails, manufactured in 316L/316LVM stainless steel, CoCr alloys (L605, MP35N, Phynox) and magnesium (Resoloy).'},
    {"actor": 'KMLT', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://kmlt.de/en/services/usp-laser-processing/', "title": 'KMLT — page officielle', "date": None, "quote": 'USP laser processing on titanium, nitinol and technical ceramics for medical device components.'},
    {"actor": 'KMLT', "bucket": 'existing', "market": 'Semi-conducteurs', "component": 'Wafers', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://kmlt.de/en/services/usp-laser-processing/', "title": 'KMLT — page officielle', "date": None, "quote": 'Precision processing of flexible printed circuit boards, ceramic substrates and wafers for microelectronics and semiconductor applications.'},
    {"actor": 'OpTek Systems', "bucket": 'existing', "market": 'Semi-conducteurs', "component": 'Wafers', "operation": 'Dicing', "stage": 'Production', "url": 'https://optek.humaneticsgroup.com/products-services/laser-processing-services/material-processing-services/cutting-dicing', "title": 'OpTek Systems — page officielle', "date": None, "quote": 'Target markets include EV components, sensor manufacturing, semiconductor & PV manufacturing, and biomedical materials.'},
    {"actor": 'OpTek Systems', "bucket": 'existing', "market": 'Photovoltaïque', "component": 'Wafers', "operation": 'Dicing', "stage": 'Production', "url": 'https://optek.humaneticsgroup.com/products-services/laser-processing-services/material-processing-services/cutting-dicing', "title": 'OpTek Systems — page officielle', "date": None, "quote": 'Target markets include EV components, sensor manufacturing, semiconductor & PV manufacturing, and biomedical materials.'},
    {"actor": 'OpTek Systems', "bucket": 'existing', "market": 'Photonique', "component": 'Fibres optiques', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://optek.humaneticsgroup.com/products-services/laser-processing-services/optical-processing-services/cleaving', "title": 'OpTek Systems — page officielle', "date": None, "quote": 'Adoption across telecoms, datacoms, fiber lasers, biomedical, and sensing applications; processes single fibers, ribbons and arrays across a wide range of fiber types and waveguides.'},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Médical', "component": 'Dispositifs microfluidiques', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/medtech.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": 'Microfluidic channels with channel widths in the 30-50 µm range; tolerances XY ±1 µm / Z ±2 µm; minimum feature <5 µm.'},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Médical', "component": 'MEMS', "operation": 'Fonctionnalisation de surface', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/medtech.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": 'Micro-optics and biocompatible MEMS, hermetic packages, micro-tweezers and grippers for minimally invasive surgery.'},
    {"actor": 'FEMTOprint', "bucket": 'existing', "market": 'Optique', "component": 'Interposeurs en verre', "operation": 'Ablation', "stage": 'Production', "url": 'https://www.femtoprint.ch/applications/optics.asp', "title": 'FEMTOprint — page officielle', "date": None, "quote": 'Monolithic integration of several optical and non-optical functions into a single monolithic glass part; wafer-scale production of glass microdevices with optical surface finish.'},
    {"actor": '3D-Micromac', "bucket": 'existing', "market": 'Médical', "component": 'Dispositifs microfluidiques', "operation": 'Fonctionnalisation de surface', "stage": 'Production', "url": 'https://3d-micromac.com/laser-micromachining/applications-laser-micromachining/laser-structuring/', "title": '3D-Micromac — page officielle', "date": None, "quote": 'Surface modification in medical device technology and microfluidics.'},
    {"actor": '3D-Micromac', "bucket": 'existing', "market": 'Photovoltaïque', "component": 'Wafers', "operation": 'Gravure', "stage": 'Production', "url": 'https://3d-micromac.com/laser-micromachining/applications-laser-micromachining/laser-structuring/', "title": '3D-Micromac — page officielle', "date": None, "quote": 'Scribing and patterning in semiconductor and photovoltaics industry; no laser damage to underlying silicon layers.'},
    {"actor": 'IREPA LASER', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Fabrication additive', "stage": 'Production', "url": 'https://www.irepa-laser.com/applications/fabrication-additive', "title": 'IREPA LASER — page officielle', "date": None, "quote": 'Marchés automobile, aérospatial et médical (prothèses, implants) ; pièces légères, résistantes et complexes, accès à des géométries impossibles par les méthodes traditionnelles.'},
    {"actor": 'KMLT', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://kmlt.de/en/services/laser-micro-cutting/', "title": 'KMLT — page officielle', "date": None, "quote": 'Serves medical technology with surgical instruments, implants, microscalpels; materials include titanium and shape-memory alloys like nitinol.'},
    {"actor": 'KMLT', "bucket": 'existing', "market": 'Luxe', "component": 'Composants en verre', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://kmlt.de/en/services/laser-micro-cutting/', "title": 'KMLT — page officielle', "date": None, "quote": 'Applications/Markets: medical technology, microelectronics, the watchmaking industry, and aerospace; materials include glass, ceramics and various plastics.'},
    {"actor": 'Fraunhofer IPT', "bucket": 'existing', "market": 'Semi-conducteurs', "component": 'Substrats', "operation": 'Texturation', "stage": 'Production', "url": 'https://www.ipt.fraunhofer.de/en/technologies/laser-technologies/laser-structuring.html', "title": 'Fraunhofer IPT — page officielle', "date": None, "quote": 'Semiconductors: substrate texturing for crystal layer growth.'},
    {"actor": 'Fraunhofer IWS', "bucket": 'existing', "market": 'Batteries', "component": 'Collecteurs de courant', "operation": 'Texturation', "stage": 'Production', "url": 'https://www.iws.fraunhofer.de/en/technologyfields/cutting-and-joining/laser-precision-processing.html', "title": 'Fraunhofer IWS — page officielle', "date": None, "quote": 'DLIP applied for current conducting foils in battery technology.'},
    {"actor": 'Micreon', "bucket": 'existing', "market": 'Médical', "component": 'Stents', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.micreon.de/service?language=en_EN', "title": 'Micreon — page officielle', "date": None, "quote": 'Damage-free laser micromachining project example: bio-stents.'},
    {"actor": 'Micreon', "bucket": 'existing', "market": 'Quantum', "component": 'Pièges à ions', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.micreon.de/service?language=en_EN', "title": 'Micreon — page officielle', "date": None, "quote": 'Damage-free laser micromachining project example: ion traps.'},
    {"actor": 'LASEA', "bucket": 'existing', "market": 'Médical', "component": 'Dispositifs microfluidiques', "operation": 'Soudage', "stage": 'Production', "url": 'https://lasea.com/applications/micro-welding-for-microfluidic-devices/', "title": 'LASEA — page officielle', "date": None, "quote": 'Primary use in diagnostics, biotech, and analytical chemistry; microfluidic devices requiring contamination-free, hermetic, high-strength seals.'},
    {"actor": 'Blueacre Technology', "bucket": 'existing', "market": 'Médical', "component": 'Stents', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://blueacretechnology.com', "title": 'Blueacre Technology — page officielle', "date": None, "quote": 'Advanced processing for state-of-the-art medical devices including medical tubing, stents, implants, microneedles and catheters.'},
    {"actor": 'Blueacre Technology', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://blueacretechnology.com', "title": 'Blueacre Technology — page officielle', "date": None, "quote": 'Advanced processing for state-of-the-art medical devices including medical tubing, stents, implants, microneedles and catheters.'},
    {"actor": 'Tekniker', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Micro-usinage', "stage": 'Production', "url": 'https://www.tekniker.es/en/research-areas/advanced-manufacturing-technologies', "title": 'Tekniker — page officielle', "date": None, "quote": 'Medical devices: peripheral nerve implants, surgical equipment.'},
    {"actor": 'FEMTO Engineering', "bucket": 'existing', "market": 'Médical', "component": 'Stents', "operation": 'Microdécoupe', "stage": 'Production', "url": 'https://www.femto-engineering.fr/realisation/ingenierie-biomedicale/', "title": 'FEMTO Engineering — page officielle', "date": None, "quote": 'Découpe laser de stents.'},
    {"actor": 'FEMTO Engineering', "bucket": 'existing', "market": 'Médical', "component": 'Dispositifs microfluidiques', "operation": 'Microperçage', "stage": 'Production', "url": 'https://www.femto-engineering.fr/realisation/ingenierie-biomedicale/', "title": 'FEMTO Engineering — page officielle', "date": None, "quote": 'Canaux microfluidiques de 100 µm de diamètre dans matériaux biocompatibles ; lab-on-chips et réacteurs microfluidiques.'},
    {"actor": 'MANUTECH USD', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Fonctionnalisation de surface', "stage": 'Production', "url": 'https://www.manutech-usd.fr/applications-manutech/', "title": 'MANUTECH USD — page officielle', "date": None, "quote": 'Ti64Al4V titanium alloy for stem cell applications; dental and bone implants; biocompatibility enhancement.'},
    {"actor": 'Femtika', "bucket": 'existing', "market": 'Médical', "component": 'Implants', "operation": 'Fonctionnalisation de surface', "stage": 'Production', "url": 'https://femtika.com/application/surface-structuring/', "title": 'Femtika — page officielle', "date": None, "quote": 'Femtosecond laser texturing for medical implant osseointegration support; contact angle between hydrophobic surface and a water drop of 150 degrees.'},
    {"actor": 'Yalosys AG', "bucket": 'existing', "market": 'Médical', "component": 'Capteurs', "operation": 'Ablation', "stage": 'Pré-industrialisation', "url": 'https://yalosys.com/rd-projects/', "title": 'Yalosys AG — page officielle', "date": None, "quote": 'Implantable biosensors for bladder monitoring and fertility hormone tracking; scaleup throughput up to 50k parts/year.'},
    {"actor": 'CEIT', "bucket": 'radar', "market": 'Batteries', "component": 'Collecteurs de courant', "operation": 'Texturation', "stage": 'Pré-industrialisation', "url": 'https://cicenergigune.com/es/noticias/cicenergigune-ceit-tecnologia-laser-superficies-cobre-baterias-litio', "title": 'CEIT y CIC energiGUNE - tecnologia laser cobre baterias litio', "date": None, "quote": 'optimizar el proceso láser de texturización del cobre a velocidades que nos permiten implementarlo a nivel industrial; colectores de corriente que posibilitan una mejor adhesión entre el electrodo y el colector'},
    {"actor": 'Yalosys AG', "bucket": 'radar', "market": 'Médical', "component": 'Implants', "operation": 'Soudage', "stage": 'R&D', "url": 'https://www.ntnphotonics.ch/project/ultrasonic-enhanced-fs-laser-welding-of-glass-to-metal/', "title": 'Ultrasonic-enhanced fs-laser welding of glass to metal - NTN Photonics Booster', "date": None, "quote": '"combine the energy application of US-welding and the very local energy delivery of fs-Laser" for "Medical devices assembly including Type III implants"; "Yalosys is doing the fs-Laser welding" and manufacturing the glass side of the samples.'},
    {"actor": 'Yalosys AG', "bucket": 'radar', "market": 'Semi-conducteurs', "component": 'Wafers', "operation": 'Microdécoupe', "stage": 'R&D', "url": 'https://www.researchgate.net/publication/379698938_Precision_Photonic_Systems_2023_Macro_pleasure_with_Microprocessing_Laser_Solutions_Yalosys_AG', "title": 'Precision Photonic Systems 2023 - Macro pleasure with Microprocessing Laser Solutions Yalosys AG (Calabrese, Mol Schneider)', "date": None, "quote": '[Source-indexed abstract, publication page returns HTTP 403 to automated fetch] IN GLASS Technology: wafer-based and efficient production of microsystems in photonics, microfluidics and MEMS for implants, thanks to USP-laser processing and modified glass wafer handling; core processes of dicing, welding and drilling in glass enabling 2.5D or 3D packaging; challenges include USP-laser processes as well as wafer handling, qualification and metrology.'},
]


def utc_now() -> str:
    """Horodatage ISO 8601 en UTC, à la seconde près -- utilisé partout comme created_at/updated_at."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Revue chronologie du 30/08/2026 (P0) : jusqu'ici, `evidence.source_date`/`offers.source_date`
# recevaient systématiquement soit la date de publication réelle de la page, soit -- faute de
# mieux -- la date d'observation elle-même (voir l'ancien `published_date or utc_now()[:10]`
# dans scrapers.scrape_market), et les deux cas étaient écrits dans la même colonne sans aucune
# trace de laquelle des deux s'était produite. Résultat : `/api/monthly` ne pouvait distinguer
# "cet acteur vient réellement de publier quelque chose" de "on vient seulement de découvrir/
# recrawler une vieille page" -- un backfill documentaire ressortait comme un signal concurrentiel
# frais. `date_confidence` restaure cette distinction dès l'écriture (jamais reconstruite après
# coup : voir _backfill_date_confidence, qui marque 'unknown' plutôt que d'inventer une réponse
# pour les lignes déjà en base avant ce correctif). `is_backfill` va plus loin : même avec une
# date de publication confirmée, un fait vu pour la première fois (created_at) bien après cette
# date reste un rattrapage documentaire, pas un mouvement récent.
DATE_CONFIDENCE_RANK = {"published": 2, "observed_only": 1, "unknown": 0}
BACKFILL_THRESHOLD_DAYS = 60


def classify_source_date(published_date: str | None) -> tuple[str, str]:
    """(source_date, date_confidence) à partir d'une date de publication extraite de la page
    (ou None si elle n'en expose aucune) -- ne fabrique jamais de date de publication : la
    valeur de repli reste la date d'observation (comme avant), mais désormais marquée
    'observed_only' plutôt que confondue avec une vraie date de publication ('published')."""
    if published_date:
        return published_date, "published"
    return utc_now()[:10], "observed_only"


def compute_is_backfill(first_seen_at: str | None, source_date: str | None, date_confidence: str | None) -> int | None:
    """1 si un fait à date de publication confirmée (date_confidence='published') n'a été vu
    pour la première fois (first_seen_at, en pratique `created_at` qui n'est jamais réécrit
    après l'insertion initiale -- voir scrapers._upsert_market_candidate/_upsert_offer_candidate)
    que plus de BACKFILL_THRESHOLD_DAYS après cette date de publication ; 0 sinon. None quand la
    date n'est pas fiable (observed_only/unknown/absente) : impossible de confirmer ou d'infirmer
    un backfill sans date de publication réelle -- ne jamais deviner."""
    if date_confidence != "published" or not first_seen_at or not source_date:
        return None
    try:
        seen = datetime.fromisoformat(first_seen_at)
        published = datetime.fromisoformat(source_date)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return int((seen - published).days > BACKFILL_THRESHOLD_DAYS)


def _backfill_date_confidence_market(db: sqlite3.Connection) -> None:
    """One-time (idempotent), MARKET_DB (evidence/offers) : source_date a pu recevoir une date
    de repli avant ce correctif sans que rien ne distingue les deux cas a posteriori (voir
    classify_source_date) -- marquées 'unknown' plutôt que de deviner, en attendant une
    réobservation qui passera par le chemin d'écriture corrigé (scrapers._upsert_market_candidate/
    _upsert_offer_candidate)."""
    for table in ("evidence", "offers"):
        db.execute(f"UPDATE {table} SET date_confidence='unknown' WHERE date_confidence IS NULL")


def _backfill_date_confidence_documents(db: sqlite3.Connection) -> None:
    """One-time (idempotent), TECH_DB (documents) : published_at n'a jamais été rempli par une
    date de repli (voir openalex.py/scrapers.scrape_technology, tous deux alimentés par une date
    structurée d'API, jamais falsifiée) -- reclassable sans ambiguïté à partir de la donnée déjà
    en base, contrairement à evidence/offers ci-dessus."""
    db.execute(
        "UPDATE documents SET date_confidence='published' "
        "WHERE date_confidence IS NULL AND published_at IS NOT NULL AND published_at!=''"
    )
    db.execute(
        "UPDATE documents SET date_confidence='observed_only' "
        "WHERE date_confidence IS NULL AND (published_at IS NULL OR published_at='')"
    )
    for row in db.execute(
        "SELECT id,created_at,published_at FROM documents WHERE is_backfill IS NULL AND date_confidence='published'"
    ).fetchall():
        is_backfill = compute_is_backfill(row["created_at"], row["published_at"], "published")
        if is_backfill is not None:
            db.execute("UPDATE documents SET is_backfill=? WHERE id=?", (is_backfill, row["id"]))


@contextmanager
def connect(path: Path):
    """Open one transaction per ``with`` block: commit only if the block exits normally.

    Any exception raised inside the block skips ``connection.commit()`` entirely, so every
    write made earlier in that same block is rolled back too -- not just the statement that
    raised. This is intentional (each block is atomic), but a loop that writes many independent
    rows inside a single ``with connect(...)`` must catch per-item errors itself if one bad item
    should not discard everything written before it (see scrape_technology in scrapers.py).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _add_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Apply small, additive migrations without replacing the user's databases."""
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


# File de revue unifiée (§5.G audit veille, 30/08/2026, Lot 1 §1.1) : traçabilité de la décision
# -- "sans motif typé, on ne peut rien apprendre des rejets". Les mêmes 3 colonnes sur les 7
# tables couvertes par review_queue.py (evidence, offers, technology_signals, actor_events,
# actor_facts, actors, vocabulary_candidates). reject_reason est validé côté API (voir
# review_queue.REJECT_REASONS), pas par une contrainte CHECK ici -- cohérent avec le reste du
# schéma, qui laisse `_add_columns` additif sans CHECK sur les colonnes ajoutées après coup.
_REVIEW_TRACE_COLUMNS = {"reviewed_by": "TEXT", "reviewed_at": "TEXT", "reject_reason": "TEXT"}


def _slug(value: str | None) -> str:
    """Normalise une chaîne en un "slug" ASCII minuscule et sans accents (ex: "Électrodes" ->
    "electrodes"). Utilisé comme brique de base des clés déterministes (market_fact_key,
    application_key, ...) pour que deux libellés qui ne diffèrent que par la casse/les accents
    produisent la même clé -- donc la même ligne en base au lieu de deux lignes dupliquées."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "non-identifie"


def _normalize_page_type_value(value: str | None) -> str:
    """Ramène un type de page au singulier canonique (ex: "applications" -> "application")."""
    raw = (value or "other").strip().casefold().replace(" ", "_")
    aliases = {
        "applications": "application", "projects": "project", "products": "product",
        "services": "service", "capabilities": "capability", "technologies": "technology",
        "markets": "market", "publications": "publication", "equipments": "equipment",
    }
    return aliases.get(raw, raw or "other")


def _normalize_existing_page_types(db: sqlite3.Connection) -> None:
    """One-way data migration: collapse legacy plural page types to canonical singular values."""
    for legacy, canonical in {
        "applications": "application", "projects": "project", "products": "product",
        "services": "service", "capabilities": "capability", "technologies": "technology",
        "markets": "market", "publications": "publication", "equipments": "equipment",
    }.items():
        db.execute("UPDATE actor_sources SET page_type=? WHERE LOWER(COALESCE(page_type,''))=?", (canonical, legacy))


# Public (no leading underscore): scrapers.py imports this rather than keeping its own copy,
# so language tagging stays identical between actor_sources/evidence_sources (written here) and
# evidence/offers (written by the crawler) instead of silently drifting apart.
# §10.6 audit veille (30/08/2026) : "language est NULL pour 49 des 71 faits (69%) --
# language_from_url() ne sait lire que les segments /en/, /de/. Les sites monolingues ou à
# sous-domaine ne sont pas typés." Élargi aux langues réellement présentes chez les acteurs en
# base (DE/CH/FR/UK/ES/IE/NL/LT/BE/CZ/IT/AT, voir la répartition géographique §10.8) et à la
# détection par sous-domaine (ex: en.example.com), pas seulement par segment de chemin.
_LANGUAGE_URL_CODES: dict[str, str] = {
    "fr": "fr", "fr-fr": "fr", "france": "fr", "francais": "fr",
    "en": "en", "en-gb": "en", "en-us": "en", "english": "en",
    "de": "de", "de-de": "de", "deutsch": "de",
    "es": "es", "es-es": "es", "espanol": "es",
    "it": "it", "it-it": "it", "italiano": "it",
    "nl": "nl", "nl-nl": "nl", "nederlands": "nl",
    "lt": "lt", "lietuviu": "lt",
    "cs": "cs", "cz": "cs", "cesky": "cs",
}


def language_from_url(url: str | None) -> str | None:
    parsed = urlparse(url or "")
    path_parts = [part for part in parsed.path.casefold().split("/") if part]
    if path_parts and path_parts[0] in _LANGUAGE_URL_CODES:
        return _LANGUAGE_URL_CODES[path_parts[0]]
    # Sous-domaine (ex: en.example.com, de.example.com) : un site sans segment /xx/ dans le
    # chemin peut quand même porter la langue dans son hôte. Le lookup exact contre
    # _LANGUAGE_URL_CODES exclut déjà les sous-domaines non-langue (www, shop, docs...) sans
    # liste d'exclusion séparée à maintenir.
    host_parts = parsed.netloc.casefold().split(".")
    if len(host_parts) > 2 and host_parts[0] in _LANGUAGE_URL_CODES:
        return _LANGUAGE_URL_CODES[host_parts[0]]
    return None


# Ces quatre fonctions "*_key" sont le cœur du mécanisme anti-duplication de l'app : elles
# transforment les champs métier d'un fait (acteur, marché, composant...) en une chaîne
# stable ("actor|bucket|market|component|operation") qui sert de clé UNIQUE en base
# (voir evidence_fact_key_uq, evidence_application_key_uq, offers.fact_key). Que le même fait
# soit observé une fois ou cent fois (dans une langue ou une autre, sur une page ou une autre),
# il produit toujours la même clé et vient donc mettre à jour la même ligne au lieu d'en créer
# une nouvelle -- c'est cette valeur qu'on cherche avec `WHERE fact_key=?` / `WHERE
# application_key=?` dans scrapers._upsert_market_candidate / _upsert_offer_candidate.
def market_fact_key(actor: str, bucket: str, market: str | None, component: str | None, operation: str | None) -> str:
    """Language-independent canonical key for one market application fact.

    market/component/operation accept None because a "partial" fact (see scrapers._candidate,
    chantier 2 item 2) may have only 2 of the 3 core dimensions -- _slug(None) falls back to a
    stable "non-identifie" placeholder, so two partial facts still dedupe correctly as long as
    the dimensions they DO share are identical.
    """
    return "|".join(_slug(value) for value in (actor, bucket, market, component, operation))


def application_key(actor: str, market: str | None, component: str | None, operation: str | None) -> str:
    """Language- and maturity-independent identity for one market application.

    Unlike ``market_fact_key``, this deliberately excludes the bucket, so an application that
    moves from radar to industrial production keeps the same evidence row instead of forking
    into a second fact. See ``evidence_bucket_transitions`` for the maturity history.
    """
    return "|".join(_slug(value) for value in (actor, market, component, operation))


# Ordre de maturité croissante : sert à savoir si un nouveau "bucket" observé pour un fait
# représente une progression (ex: radar -> existing) ou une régression qu'il ne faut pas
# appliquer automatiquement (voir scrapers._upsert_market_candidate: `upgrades_bucket`).
BUCKET_RANK = {"existing": 2, "radar": 1, "pending": 0, "rejected": -1}


def offer_fact_key(actor: str, offer_type: str, capability: str, operation: str | None, laser_process: str | None) -> str:
    """Même principe que market_fact_key, mais pour une "offre" (offers) -- une capacité/
    prestation d'un acteur qui n'est pas forcément rattachée à un marché/composant précis."""
    return "|".join(_slug(value) for value in (actor, offer_type, capability, operation or "", laser_process or ""))


# Chantier 4 (fiabiliser la preuve) : distinguer une déclaration marketing ("nous savons faire
# X") d'une preuve concrète ("300 trous/seconde sur titane", "certifié ISO 13485") -- l'audit
# note qu'aujourd'hui les deux sont stockés à égalité. Volontairement étroit et déterministe
# (comme le reste du pipeline d'extraction) : seuls deux signaux vérifiables sans ambiguïté
# comptent comme "proof" -- un chiffre accompagné d'une unité technique, ou un code de
# certification reconnu. Une "référence client nommée" (le troisième signal cité par l'audit)
# est délibérément omise : la détecter fiablement demanderait une vraie reconnaissance d'entité
# nommée, pas une regex, et un faux positif ferait passer une déclaration pour une preuve.
# 'third_party' n'est jamais renvoyé ici -- evidence/offers ne contiennent aujourd'hui que du
# contenu scrapé sur le site de l'acteur lui-même (premier parti par construction) ; la valeur
# reste réservée pour une source qui ne l'est pas (voir cordis.py/press.py, d'autres tables).
_PROOF_NUMBER_RE = re.compile(
    r"\d[\d.,]*\s*(%|°c|µm|nm|mm|cm|kg|g|w|kw|mw|hz|khz|mhz|ghz|fs|ps|ns|mj|µj|j/cm2|"
    r"pieces?|pi[eè]ces?|parts?|units?|unit[ée]s?|holes?|trous?|per second|/s|ppm|rpm)\b",
    re.IGNORECASE,
)
_CERTIFICATION_RE = re.compile(r"\b(iso\s?\d{4,5}|as\s?9100|iatf\s?16949|itar|nadcap)\b", re.IGNORECASE)


def classify_evidence_type(quote: str | None) -> str:
    """'proof' si la citation porte un chiffre technique mesuré ou une certification reconnue,
    sinon 'claim' (déclaration non chiffrée). Voir le commentaire ci-dessus pour la portée."""
    text = quote or ""
    if _CERTIFICATION_RE.search(text) or _PROOF_NUMBER_RE.search(text):
        return "proof"
    return "claim"


def numeric_spec_tokens(text: str | None) -> set[str]:
    """Les specs chiffrées (nombre + unité technique) présentes dans ``text``, au sens exact de
    _PROOF_NUMBER_RE déjà utilisé par classify_evidence_type -- réutilisé par
    scrapers._diff_page_blocks (§5.E.1 audit veille) pour détecter qu'une spec a changé entre
    deux versions d'une page, sans dupliquer la définition de ce qui compte comme une spec."""
    return {match.group(0).strip() for match in _PROOF_NUMBER_RE.finditer(text or "")}


_ARCHITECTURE_STAGE_RE = re.compile(r"Architecture:\s*([^|]+)")


def _backfill_evidence_type(db: sqlite3.Connection) -> None:
    """One-time (idempotent) backfill of evidence_type for rows written before this column
    existed -- only ever touches NULL rows, so it costs nothing on a database already caught up."""
    for table in ("evidence", "offers"):
        pending = db.execute(f"SELECT id,quote FROM {table} WHERE evidence_type IS NULL").fetchall()
        for row in pending:
            db.execute(f"UPDATE {table} SET evidence_type=? WHERE id=?", (classify_evidence_type(row["quote"]), row["id"]))


def _migrate_industrial_stage_concatenation(db: sqlite3.Connection) -> None:
    """One-time (idempotent) cleanup for the audit's "industrial_stage is a denormalized
    display field" finding: it used to concatenate maturity + process/architecture/material/
    performance into one string (see the old scrapers._candidate ``stage_parts``), even though
    those dimensions already have their own columns -- except architecture, which had none
    (added alongside evidence_type above). Extracts "Architecture: X" into that new column,
    then trims industrial_stage down to just its leading maturity segment. Only rows that still
    contain "|" are touched, so this is a no-op once the whole table has been cleaned; new rows
    never concatenate in the first place (see scrapers._candidate/_ai_candidates).
    """
    pending = db.execute("SELECT id,industrial_stage FROM evidence WHERE industrial_stage LIKE '%|%'").fetchall()
    for row in pending:
        raw = row["industrial_stage"] or ""
        leading = raw.split("|", 1)[0].strip()
        match = _ARCHITECTURE_STAGE_RE.search(raw)
        if match:
            db.execute(
                "UPDATE evidence SET industrial_stage=?,architecture=COALESCE(architecture,?) WHERE id=?",
                (leading, match.group(1).strip(), row["id"]),
            )
        else:
            db.execute("UPDATE evidence SET industrial_stage=? WHERE id=?", (leading, row["id"]))
    # offers never carried an "Architecture:" segment (that dimension doesn't apply to offers),
    # so just trim to the leading maturity segment on the rare row that still looks concatenated.
    pending_offers = db.execute("SELECT id,industrial_stage FROM offers WHERE industrial_stage LIKE '%|%'").fetchall()
    for row in pending_offers:
        leading = (row["industrial_stage"] or "").split("|", 1)[0].strip()
        db.execute("UPDATE offers SET industrial_stage=? WHERE id=?", (leading, row["id"]))


# Mirrors scrapers.MATURITY_RULES' stage labels plus the "unknown" fallback
# (scrapers._detect_maturity's own default). Kept as a literal set rather than imported --
# scrapers.py already imports from db.py, so the reverse import would be circular -- exactly
# the same trade-off _migrate_industrial_stage_concatenation already makes for _ARCHITECTURE_STAGE_RE.
_INDUSTRIAL_STAGE_CANONICAL = frozenset({
    "Production", "Industrialisation", "Pré-industrialisation", "Prototype", "R&D",
    "Maturité industrielle non déterminée",
})


def _migrate_industrial_stage_placeholder_values(db: sqlite3.Connection) -> None:
    """One-time (idempotent) cleanup for the audit's "industrial_stage ontology" finding: rows
    written before scrapers._validate_ai_value existed (or by a now-dead extraction path) left
    placeholder/garbage strings in industrial_stage instead of one of the labels
    scrapers.MATURITY_RULES defines -- observed in production: the literal '<UNKNOWN>', the
    stringified 'None' (a Python None that got str()'d instead of staying a real NULL), the
    English 'commercialized', and even a project name ('projet LUMEN') that leaked in from a
    CORDIS-style page instead of a maturity label. Today's extraction paths can no longer produce
    these -- scrapers._validate_ai_value only ever lets an AI-proposed maturity through if it
    exactly matches a known label (see its allowlist check), and scrapers._detect_maturity's
    output is always one of MATURITY_RULES' fixed labels or the "unknown" default -- so this only
    ever touches historical rows; new inserts never need it.

    Deliberately never re-derives a stage from the stored `quote`: that text is a short excerpt,
    while `bucket` was computed at insertion time from the full page section, so it stays more
    trustworthy than a fresh classification run on a fragment (re-running _detect_maturity on a
    truncated quote mostly returns "unknown" even for rows whose bucket is confidently
    'existing'/'radar' -- a regression, not a fix). Instead this maps the corrupted label onto the
    one canonical stage the row's own `bucket` already implies, reusing the correspondence the
    rest of the table already shows: bucket='existing' co-occurs with industrial_stage='Production'
    on every clean row, so a non-canonical 'existing' row becomes 'Production'; every other bucket
    ('radar','pending') falls back to 'Maturité industrielle non déterminée' -- already the normal
    label for a radar-stage row whose precise sub-stage (R&D/Prototype/Pré-industrialisation/
    Industrialisation) isn't established from the text alone. `offers` has no bucket column, so a
    non-canonical row there (none observed in production so far) falls back to the same
    unknown-stage label unconditionally.
    """
    placeholders = ",".join("?" * len(_INDUSTRIAL_STAGE_CANONICAL))
    pending = db.execute(
        f"SELECT id,bucket,industrial_stage FROM evidence "
        f"WHERE industrial_stage IS NOT NULL AND industrial_stage NOT IN ({placeholders})",
        tuple(_INDUSTRIAL_STAGE_CANONICAL),
    ).fetchall()
    for row in pending:
        replacement = "Production" if row["bucket"] == "existing" else "Maturité industrielle non déterminée"
        db.execute("UPDATE evidence SET industrial_stage=? WHERE id=?", (replacement, row["id"]))

    pending_offers = db.execute(
        f"SELECT id FROM offers "
        f"WHERE industrial_stage IS NOT NULL AND industrial_stage NOT IN ({placeholders})",
        tuple(_INDUSTRIAL_STAGE_CANONICAL),
    ).fetchall()
    for row in pending_offers:
        db.execute(
            "UPDATE offers SET industrial_stage=? WHERE id=?",
            ("Maturité industrielle non déterminée", row["id"]),
        )


# §10.10 audit veille (30/08/2026, Lot 2 §2.7) : "deux libellés d'axe coexistent pour le même
# concept [...] avec 10 lignes c'est anecdotique ; à 500 lignes, tout comptage par axe sera
# faux et personne ne s'en apercevra." Ces deux couples pré-existaient en production, écrits
# avant que cordis.py ne se limite au lexique fermé scrapers.PROCESS_TECHNOLOGIES (voir
# scrapers.py pour les 3 axes ajoutés au lexique plutôt que fusionnés, qui n'avaient encore
# aucun équivalent). Migration ponctuelle et idempotente : sans ligne portant l'ancien libellé,
# ne fait rien.
_TECHNOLOGY_AXIS_ALIASES: dict[str, str] = {
    "Monitoring + IA / digital twin": "Monitoring IA procédé",
    "Beam shaping / surfaces 3D": "Beam shaping",
    # Écrit dans PROCESS_TECHNOLOGIES le 09/09/2026, retiré le lendemain : il faisait double
    # emploi avec OPERATIONS["Soudage"], qui a absorbé ses termes, et les deux s'affichaient
    # côte à côte dans deux groupes de facettes voisins.
    "Soudage / assemblage de transparents": "Soudage",
    # Même histoire, même lendemain : OPERATIONS["Texturation"] portait déjà "texturing" et
    # "surface structuring", donc la redondance était quasi totale.
    "Texturation de surface": "Texturation",
}


def _normalize_technology_axes(db: sqlite3.Connection) -> None:
    """Canonise les libellés d'axe hérités vers leur entrée de lexique -- fusionne (union des
    actor_names, comme _upsert_technology_signal) si la ligne canonique existe déjà pour le
    même discriminant, sinon renomme la ligne en place.

    Le discriminant n'est pas toujours le projet : cordis.py dérive fact_key de (axe, projet),
    scrapers.upsert_document_technology_signal de (axe, URL du document). Cette fonction ne
    connaissait que le premier cas -- écrite quand la table ne portait que des projets -- et
    aurait donc calculé une clé fausse pour tout signal documentaire, en fusionnant entre elles
    des lignes qui n'ont rien à voir (project_name NULL pour toutes). Corrigé le 10/09/2026, à
    l'occasion du premier alias qui touche des documents.
    """
    for old_axis, new_axis in _TECHNOLOGY_AXIS_ALIASES.items():
        rows = db.execute(
            "SELECT id,project_name,source_url,actor_names FROM technology_signals WHERE axis=?", (old_axis,)
        ).fetchall()
        for row in rows:
            discriminant = row["project_name"] or row["source_url"]
            new_fact_key = technology_signal_key(new_axis, discriminant)
            existing = db.execute(
                "SELECT id,actor_names FROM technology_signals WHERE fact_key=?", (new_fact_key,)
            ).fetchone()
            if existing:
                merged = sorted(set(json.loads(row["actor_names"] or "[]")) | set(json.loads(existing["actor_names"] or "[]")))
                db.execute(
                    "UPDATE technology_signals SET actor_names=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False), existing["id"]),
                )
                # Les citations suivent le fait survivant : sans ce transfert, ON DELETE CASCADE
                # les emporterait, et la fusion ferait donc DISPARAÎTRE de la preuve. OR IGNORE
                # parce que l'empreinte est unique -- une citation déjà portée à l'identique par
                # la ligne survivante reste sur l'ancienne et part avec elle, ce qui est le
                # comportement voulu (c'est un doublon).
                db.execute(
                    "UPDATE OR IGNORE technology_signal_sources SET signal_id=? WHERE signal_id=?",
                    (existing["id"], row["id"]),
                )
                db.execute("DELETE FROM technology_signals WHERE id=?", (row["id"],))
            else:
                db.execute(
                    "UPDATE technology_signals SET axis=?,fact_key=?,fingerprint=? WHERE id=?",
                    (new_axis, new_fact_key, hashlib.sha256(new_fact_key.encode()).hexdigest(), row["id"]),
                )


# Même mapping que scrapers.MATURITY_RULES (stage -> bucket), dupliqué ici plutôt qu'importé :
# db.py ne peut pas importer scrapers.py (scrapers.py importe déjà db.py -- import circulaire).
# Seul "Production" vaut "existing" ; tout le reste, y compris "Industrialisation", vaut
# "radar" -- une paire qui *semble* se contredire (§10.10 : "deux signaux portent
# maturity_stage='Industrialisation' ET bucket='radar'") est en réalité la correspondance
# canonique voulue : vérifiée contre les 10 lignes technology_signals en production, aucune
# n'était réellement incohérente une fois cette table de référence appliquée.
TECHNOLOGY_STAGE_TO_BUCKET: dict[str, str] = {
    "Production": "existing",
    "Industrialisation": "radar",
    "Pré-industrialisation": "radar",
    "Prototype": "radar",
    "R&D": "radar",
    "Maturité industrielle non déterminée": "radar",
}


def _reconcile_technology_signal_maturity(db: sqlite3.Connection) -> None:
    """Corrige toute ligne dont bucket ne correspond pas à ce que TECHNOLOGY_STAGE_TO_BUCKET
    prescrit pour son maturity_stage -- garde-fou structurel plutôt qu'une contrainte CHECK
    (le mapping peut évoluer ; un stage non reconnu retombe sur 'radar', jamais 'existing' par
    défaut). Idempotent : ne touche que les lignes réellement incohérentes."""
    for row in db.execute("SELECT id,maturity_stage,bucket FROM technology_signals").fetchall():
        expected = TECHNOLOGY_STAGE_TO_BUCKET.get(row["maturity_stage"], "radar")
        if row["bucket"] != expected:
            db.execute("UPDATE technology_signals SET bucket=? WHERE id=?", (expected, row["id"]))


_PARTIAL_DOCUMENT_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$")


def _normalize_partial_document_dates(db: sqlite3.Connection) -> None:
    """Zero-padde les `published_at` partiels écrits avant scrapers._crossref_date.

    Crossref renvoie `[[2027, 4]]` pour un article rattaché à un numéro à paraître, et
    l'ancien "-".join stockait "2027-4" : lexicographiquement APRÈS "2027-12-01", donc mal
    trié, et affiché brut dans l'UI. On complète les composantes à deux chiffres sans jamais
    inventer le jour manquant -- la précision réelle de la date est conservée. Idempotent : ne
    réécrit que les lignes dont le format diffère.
    """
    for row in db.execute("SELECT id,published_at FROM documents WHERE published_at IS NOT NULL").fetchall():
        match = _PARTIAL_DOCUMENT_DATE_RE.match((row["published_at"] or "").strip())
        if not match:
            continue
        year, *rest = match.groups()
        normalized = "-".join([year, *(f"{int(part):02d}" for part in rest if part)])
        if normalized != row["published_at"]:
            db.execute("UPDATE documents SET published_at=? WHERE id=?", (normalized, row["id"]))


def _purge_unknown_document_axes(db: sqlite3.Connection) -> None:
    """Retire les signaux DOCUMENTAIRES dont le libellé ne vient plus d'aucun vocabulaire.

    Un signal de document est produit par le lexique et par rien d'autre (voir
    scrapers.upsert_document_technology_signal) : si son libellé quitte DOCUMENT_LEXICONS, la
    ligne n'est plus adossée à rien et ne peut plus être reproduite par une collecte. La garder
    laisserait une famille orpheline s'afficher, et
    _reconcile_technology_signal_dimensions la rangerait par défaut en 'process_technology',
    c'est-à-dire au mauvais endroit.

    Sert au retrait du 10/09/2026 : "Micro-usinage" (trop générique -- il décrit en réalité une
    découpe, une gravure ou un perçage), "Écriture de guide d'onde" (un produit, pas un geste),
    "Scribing" (mot anglais du marquage), et la dimension "bénéfice visé" entière.

    Deux exclusions : les signaux de PROJET, dont les libellés peuvent être hérités d'avant le
    lexique fermé (voir _normalize_technology_axes), et toute ligne relue par un humain --
    même garde que prune_technology_signals.
    """
    known = {label for lexicon in DOCUMENT_LEXICONS.values() for label in lexicon}
    stale = [
        row["id"] for row in db.execute(
            "SELECT id,axis FROM technology_signals WHERE project_name IS NULL AND reviewed_at IS NULL"
        ).fetchall()
        if row["axis"] not in known
    ]
    for signal_id in stale:
        db.execute("DELETE FROM technology_signals WHERE id=?", (signal_id,))


def _reconcile_technology_signal_dimensions(db: sqlite3.Connection) -> None:
    """Renseigne technology_signals.dimension à partir du vocabulaire qui possède le libellé.

    Déduite plutôt que figée : les six vocabulaires de DOCUMENT_LEXICONS ont des libellés
    disjoints, donc l'axe suffit à retrouver sa dimension. C'est ce qui rattrape sans migration
    ponctuelle les lignes écrites avant la colonne, ET celles dont le libellé a changé de
    vocabulaire -- "Fonctionnalisation de surface" est passée de PROCESS_TECHNOLOGIES à
    OPERATIONS le 09/09/2026, et ses lignes suivent d'elles-mêmes au prochain démarrage.

    Un libellé inconnu des six vocabulaires (axe écrit en texte libre avant le lexique fermé,
    voir _normalize_technology_axes) retombe sur 'process_technology' : c'était la seule
    dimension possible à l'époque où il a été écrit, donc c'est un fait, pas une supposition.
    Idempotent : ne touche que les lignes dont la dimension diffère de celle attendue.
    """
    owner = {label: dimension for dimension, lexicon in DOCUMENT_LEXICONS.items() for label in lexicon}
    for row in db.execute("SELECT id,axis,dimension FROM technology_signals").fetchall():
        expected = owner.get(row["axis"], "process_technology")
        if row["dimension"] != expected:
            db.execute("UPDATE technology_signals SET dimension=? WHERE id=?", (expected, row["id"]))


def _migrate_lei_out_of_registry_columns(db: sqlite3.Connection) -> None:
    """Déplace les LEI déjà écrits par gleif.py vers les colonnes lei_* dédiées.

    Avant la séparation des colonnes, gleif.py et firmographics.py écrivaient tous deux
    registry_id/registry_name/registry_active : le dernier collecteur exécuté écrasait
    l'identifiant de l'autre. Les lignes déjà en base portant un LEI dans registry_id sont
    donc recopiées ici vers lei/lei_active/lei_source_url/lei_as_of_date, puis leurs colonnes
    registry_* sont libérées pour le registre national.

    Idempotent à double titre : la garde ``lei IS NULL`` empêche d'écraser un LEI déjà migré,
    et registry_name est mis à NULL par la migration elle-même, donc la clause LIKE ne peut
    plus matcher au démarrage suivant.
    """
    db.execute(
        """UPDATE actor_profile
              SET lei=registry_id,
                  lei_active=registry_active,
                  lei_source_url=source_url,
                  lei_as_of_date=as_of_date,
                  registry_id=NULL, registry_name=NULL, registry_active=NULL,
                  source_url=NULL, as_of_date=NULL
            WHERE lei IS NULL AND registry_id IS NOT NULL AND registry_name LIKE 'GLEIF%'"""
    )


def _widen_actor_candidate_sources_type(db: sqlite3.Connection) -> None:
    """actor_candidate_sources.source_type gagne 'patent' (connecteur EPO OPS, Lot 3 §3.4) --
    SQLite ne sait pas ALTER un CHECK existant : seule option, reconstruire la table. Idempotent
    (contrôle le texte du CREATE TABLE en base avant de reconstruire quoi que ce soit)."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actor_candidate_sources'"
    ).fetchone()
    if row is None or "'patent'" in (row["sql"] or ""):
        return
    db.executescript(
        """
        CREATE TABLE actor_candidate_sources_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES actor_candidates(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL CHECK(source_type IN ('cordis','openalex','outbound_link','patent')),
            context TEXT,
            source_url TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(candidate_id, source_type, source_url)
        );
        INSERT INTO actor_candidate_sources_new
            (id,candidate_id,source_type,context,source_url,created_at)
            SELECT id,candidate_id,source_type,context,source_url,created_at FROM actor_candidate_sources;
        DROP TABLE actor_candidate_sources;
        ALTER TABLE actor_candidate_sources_new RENAME TO actor_candidate_sources;
        CREATE INDEX IF NOT EXISTS actor_candidate_sources_candidate_idx ON actor_candidate_sources(candidate_id);
        """
    )


def technology_signal_key(axis: str, project_name: str | None) -> str:
    """Identity for one science->industry readiness signal: the axis plus the named project it
    was observed in (not the source URL), so the same axis/project pair merges new citations
    onto one row instead of creating a duplicate every time another source confirms it."""
    return "|".join(_slug(value) for value in (axis, project_name or ""))


def _canonicalise_existing_evidence(db: sqlite3.Connection) -> None:
    """Repair legacy labels and remove old curated seeds from automatic publication."""
    placeholders = ",".join("?" for _ in LEGACY_SEED_GROUPS)
    db.execute(f"UPDATE evidence SET review_status='review' WHERE source_group IN ({placeholders})", LEGACY_SEED_GROUPS)
    db.execute("UPDATE evidence SET component='Électrodes de batteries' WHERE component='Électrodes de batteries Gen4'")
    db.execute("UPDATE evidence SET component='Microcanaux' WHERE component='Composants et microcanaux en verre'")
    db.execute("UPDATE evidence SET component='Composants en verre' WHERE component='Composants en verre optique'")
    db.execute("UPDATE evidence SET operation='Microdécoupe' WHERE operation IN ('Microdécoupe femtoseconde','Découpe femtoseconde')")
    db.execute("UPDATE evidence SET operation='SLE' WHERE operation='SLE et microstructuration 3D'")
    db.execute("UPDATE evidence SET operation='Microdécoupe' WHERE actor_name='ALPHANOV' AND operation='Découpe et structuration'")


def _migrate_evidence_fact_model(db: sqlite3.Connection) -> None:
    """Backfill language-independent facts and move each URL/quote into a source-proof table."""
    _canonicalise_existing_evidence(db)
    rows = db.execute("SELECT * FROM evidence ORDER BY id").fetchall()
    if not rows:
        return

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = market_fact_key(row["actor_name"], row["bucket"], row["market"] or "", row["component"] or "", row["operation"] or "")
        grouped.setdefault(key, []).append(row)

    for key, members in grouped.items():
        # Prefer an accepted/high-confidence row as the representative fact.
        def rank(row: sqlite3.Row) -> tuple[int, float, int]:
            status_rank = {"accepted": 2, "review": 1, "rejected": 0}.get(row["review_status"], 0)
            confidence = float(row["field_confidence"] or 0) if "field_confidence" in row.keys() else 0.0
            return status_rank, confidence, -int(row["id"])

        representative = max(members, key=rank)
        rep_id = int(representative["id"])
        db.execute(
            "UPDATE evidence SET fact_key=?,evidence_kind='market_application',language=? WHERE id=?",
            (key, language_from_url(representative["source_url"]), rep_id),
        )
        for row in members:
            # Must depend on source_url/quote, not just reuse row["fingerprint"] (the evidence
            # row's own fact-identity hash, constant across every source of that fact): reusing
            # it made every citation of the same fact collide on one fingerprint value, which is
            # exactly what let INSERT OR IGNORE silently duplicate rows once evidence_sources
            # gets a real uniqueness constraint (see evidence_sources_fingerprint_uq below) --
            # matches the fingerprint convention scrapers.py's _upsert_market_candidate uses.
            source_fingerprint = hashlib.sha256(f"{key}|{row['source_url']}|{row['quote']}".encode()).hexdigest()
            db.execute(
                """INSERT OR IGNORE INTO evidence_sources(
                       evidence_id,source_url,source_title,source_date,quote,language,block_heading,block_path,
                       extraction_mode,field_confidence,fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rep_id, row["source_url"], row["source_title"], row["source_date"], row["quote"],
                    language_from_url(row["source_url"]),
                    row["block_heading"] if "block_heading" in row.keys() else None,
                    row["block_path"] if "block_path" in row.keys() else None,
                    row["extraction_mode"] if "extraction_mode" in row.keys() else None,
                    row["field_confidence"] if "field_confidence" in row.keys() else None,
                    source_fingerprint, row["created_at"],
                ),
            )
            if int(row["id"]) != rep_id:
                db.execute("DELETE FROM evidence WHERE id=?", (row["id"],))


def _migrate_application_keys(db: sqlite3.Connection) -> None:
    """Backfill application_key and merge rows that only differed by bucket.

    Before this migration, ``market_fact_key`` embedded the bucket, so the same application
    moving from radar to industrial production created a second evidence row instead of
    updating the first. This merges those pairs, keeps the highest-maturity bucket as the
    canonical one, moves every source proof onto the surviving row, and records the merge as
    a transition (stamped with the migration time, since the real transition date predates
    this column and is not recoverable).
    """
    rows = db.execute("SELECT * FROM evidence WHERE evidence_kind='market_application'").fetchall()
    if not rows:
        return

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = application_key(row["actor_name"], row["market"] or "", row["component"] or "", row["operation"] or "")
        grouped.setdefault(key, []).append(row)

    stamp = utc_now()
    for key, members in grouped.items():
        if len(members) == 1:
            db.execute("UPDATE evidence SET application_key=? WHERE id=?", (key, members[0]["id"]))
            continue

        def rank(row: sqlite3.Row) -> tuple[int, float, int]:
            confidence = float(row["field_confidence"] or 0) if "field_confidence" in row.keys() else 0.0
            return (BUCKET_RANK.get(row["bucket"], -1), confidence, int(row["id"]))

        ordered = sorted(members, key=rank, reverse=True)
        representative = ordered[0]
        rep_id = int(representative["id"])
        db.execute("UPDATE evidence SET application_key=? WHERE id=?", (key, rep_id))

        for row in ordered[1:]:
            old_id = int(row["id"])
            db.execute("UPDATE evidence_sources SET evidence_id=? WHERE evidence_id=?", (rep_id, old_id))
            if row["bucket"] != representative["bucket"]:
                db.execute(
                    "INSERT INTO evidence_bucket_transitions(evidence_id,from_bucket,to_bucket,changed_at) VALUES(?,?,?,?)",
                    (rep_id, row["bucket"], representative["bucket"], stamp),
                )
            db.execute("DELETE FROM evidence WHERE id=?", (old_id,))


def _dedupe_source_rows(db: sqlite3.Connection, table: str, fact_id_col: str) -> None:
    """Collapse source-citation rows that cite the exact same (fact, url, quote) more than once.

    A test bug (fixed alongside this) let init_databases() run its evidence migrations against
    the real production market.db on every pytest run; those migrations computed the source
    fingerprint inconsistently, so INSERT OR IGNORE never caught the resulting re-inserts and
    real duplicate rows accumulated. Kept as a standing, idempotent cleanup (not a one-off
    script) so any future fingerprint mismatch self-heals on the next startup instead of quietly
    inflating "proofs" counts across the app.
    """
    rows = db.execute(f"SELECT id,{fact_id_col},source_url,quote FROM {table} ORDER BY id").fetchall()
    seen: dict[tuple, int] = {}
    duplicate_ids: list[int] = []
    for row in rows:
        content_key = (row[fact_id_col], row["source_url"], row["quote"])
        if content_key in seen:
            duplicate_ids.append(int(row["id"]))
        else:
            seen[content_key] = int(row["id"])
    for dup_id in duplicate_ids:
        db.execute(f"DELETE FROM {table} WHERE id=?", (dup_id,))


def _upsert_seed_evidence(db: sqlite3.Connection, item: dict[str, Any], stamp: str) -> None:
    """Audit veille §10.2 (30/08/2026) : ces faits ne viennent pas du pipeline d'extraction --
    saisis à la main, is_verbatim=0 les marque déjà comme tels (via la migration qui suit,
    is_verbatim=0 WHERE extraction_mode IS NULL, jamais rempli ici). review_status='review'
    (plutôt que l'ancien 'accepted' hardcodé) pour la même raison : un fait de seed doit
    repasser par une validation humaine réelle avant de compter comme "validé" nulle part
    (scores, matrice marché) -- jamais publié d'office simplement parce qu'il a été saisi."""
    key = market_fact_key(item["actor"], item["bucket"], item["market"], item["component"], item["operation"])
    source_fingerprint = hashlib.sha256(f"{item['url']}|{item['quote']}".encode()).hexdigest()
    row = db.execute("SELECT id FROM evidence WHERE fact_key=?", (key,)).fetchone()
    if row:
        evidence_id = int(row["id"])
    else:
        fact_fingerprint = hashlib.sha256(key.encode()).hexdigest()
        new_id = db.execute(
            """INSERT INTO evidence(
                   actor_name,bucket,market,component,operation,industrial_stage,source_url,source_title,source_date,
                   quote,source_group,fingerprint,fact_key,evidence_kind,language,review_status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'market_application',?,'review',?,?)""",
            (
                item["actor"], item["bucket"], item["market"], item["component"], item["operation"], item["stage"],
                item["url"], item["title"], item["date"], item["quote"], key, fact_fingerprint, key,
                language_from_url(item["url"]), stamp, stamp,
            ),
        ).lastrowid
        assert new_id is not None  # guaranteed by sqlite3 right after a successful AUTOINCREMENT insert
        evidence_id = new_id
    db.execute(
        """INSERT OR IGNORE INTO evidence_sources(
               evidence_id,source_url,source_title,source_date,quote,language,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (evidence_id, item["url"], item["title"], item["date"], item["quote"], language_from_url(item["url"]), source_fingerprint, stamp),
    )


def restore_seed_evidence() -> int:
    """Recovery explicite, JAMAIS appelé automatiquement (voir init_databases ci-dessous, qui ne
    l'appelle plus depuis le correctif du 30/08/2026 -- audit veille §10.2).

    SEED_EVIDENCE existe pour que ces 40 faits ne soient pas perdus si market.db est un jour
    réinitialisée/reconstruite pour CETTE installation précise -- ce n'est PAS un jeu de données
    de démarrage générique à injecter dans n'importe quelle base fraîche (un nouveau
    déploiement, ou toute base de test créée par init_databases(), n'a aucune raison de se
    retrouver avec 40 faits sur des acteurs précis qu'il n'a jamais collectés lui-même).
    L'ancienne version appelait _upsert_seed_evidence pour chaque item à CHAQUE démarrage --
    correct pour préserver les données de cette installation, mais un même mécanisme sans garde
    empoisonnait aussi toute base fraîche (dont chaque test de la suite qui appelle
    init_databases()). À invoquer à la main (script/console) seulement en cas de perte réelle de
    market.db. Renvoie le nombre de faits effectivement (ré)insérés."""
    stamp = utc_now()
    inserted = 0
    with connect(MARKET_DB) as db:
        for item in SEED_EVIDENCE:
            before = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            _upsert_seed_evidence(db, item, stamp)
            after = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            inserted += after - before
    return inserted


def _reconcile_orphaned_runs(db: sqlite3.Connection) -> None:
    """Audit veille §9.2 (30/08/2026) : si le process meurt en cours de collecte (crash, OOM,
    redéploiement en plein run), la ligne collection_runs correspondante reste à status='running'
    pour toujours -- _run_job (app.py) ne met à jour cette ligne qu'à la FIN normale d'un run,
    jamais si l'exécution est interrompue avant. Constaté en production : le run n°16 était
    resté à status='running' depuis le 30/08 13:03. Appelé à chaque démarrage (voir
    init_databases, une fois par base -- les 3 bases ont chacune leur propre collection_runs) :
    toute ligne encore 'running' au moment où ce code s'exécute ne peut être qu'un run mort
    d'un process précédent, jamais le run en cours (celui-ci ne s'insère qu'après ce point)."""
    db.execute("UPDATE collection_runs SET status='interrupted' WHERE status='running'")


# §9.1 audit veille (30/08/2026, Lot 2 §2.2) : "89,4% des URLs découvertes ne sont jamais
# visitées [...] et rien ne les purge." Sans borne, le backlog grossit indéfiniment et entre en
# concurrence avec les pages réellement informatives à chaque relance de la sélection de
# sources. Bornée aux lignes désormais horodatées (discovered_at NOT NULL, voir
# scrapers.scrape_actors) : une ligne découverte avant l'existence de cette colonne (donc sans
# date connue) n'est jamais purgée sur la seule foi d'une hypothèse.
BACKLOG_PURGE_DAYS = 90


def purge_stale_backlog(days: int = BACKLOG_PURGE_DAYS, db_path: Path | None = None) -> int:
    """Désactive (active=0, jamais une suppression -- réversible) les URLs découvertes,
    jamais visitées, classées 'other' (score le plus bas hors 'ignore'), et découvertes il y a
    plus de `days` jours. Renvoie le nombre de lignes désactivées.

    ``db_path`` : appelée depuis scrapers.scrape_actors, qui a sa PROPRE copie importée de
    ACTORS_DB -- les tests du crawler patchent celle-là (scrapers.ACTORS_DB), pas celle-ci
    (db.ACTORS_DB). Sans ce paramètre explicite, cette fonction ignorerait silencieusement ce
    patch et se connecterait au vrai chemin par défaut au lieu de la base de test isolée."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect(db_path or ACTORS_DB) as db:
        purged = db.execute(
            """UPDATE actor_sources SET active=0
               WHERE active=1 AND page_type='other' AND last_checked_at IS NULL
                 AND discovered_at IS NOT NULL AND discovered_at < ?""",
            (cutoff,),
        ).rowcount
    return purged


def _init_actors_db() -> None:
    """Base 1/3 : acteurs suivis, leurs pages sources, leur profil de crawl.

    Extrait de init_databases(), qui atteignait 974 lignes -- soit 42 % de ce module -- pour
    un contenu deja naturellement decoupe en trois blocs `with connect(...)`, un par fichier
    SQLite. Le decoupage ne change ni la logique ni l'ordre d'execution : voir init_databases()."""
    # --- Base 1/3 : ACTORS_DB (acteurs suivis, leurs pages sources, leur profil de crawl) ---
    with connect(ACTORS_DB) as db:
        db.executescript(
            """
            -- Un acteur suivi (concurrent, centre technologique, référence interne...).
            CREATE TABLE IF NOT EXISTS actors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                country TEXT NOT NULL,
                role TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1)),
                official_url TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                last_scraped_at TEXT,
                last_status TEXT NOT NULL DEFAULT 'never',
                updated_at TEXT NOT NULL
            );
            -- Une relation (partenaire/fournisseur/client) entre deux acteurs, pour /api/network.
            CREATE TABLE IF NOT EXISTS actor_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                related_actor_id INTEGER REFERENCES actors(id) ON DELETE SET NULL,
                related_name TEXT NOT NULL,
                relation_type TEXT NOT NULL CHECK(relation_type IN ('partner','supplier','client')),
                note TEXT,
                source_url TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS actor_relations_actor_idx ON actor_relations(actor_id);
            -- Découverte d'acteurs (§4.D audit veille, 30/08/2026, Lot 3 §3.2) : "actors.
            -- review_status='candidate' existe déjà en base et n'est jamais alimenté."  Un
            -- candidat n'entre JAMAIS directement dans `actors` : il s'accumule ici (occurrences,
            -- sources, score) et n'est promu en acteur réel (voir actor_discovery.promote_
            -- candidate) qu'après validation humaine -- même gouvernance que vocabulary_
            -- candidates -> custom_lexicon_entries, le modèle explicitement cité par l'audit.
            -- Alimenté par 3 sources déjà présentes dans le pipeline (voir actor_discovery.py) :
            -- CORDIS (actor_relations.related_name jamais rattaché, PUIS un passage "topic-
            -- scoped" séparé -- tout projet on-topic, avec ou sans acteur déjà suivi dedans,
            -- pas seulement les consortiums d'un acteur connu), OpenAlex (institution co-autrice
            -- récurrente sur des travaux d'un acteur suivi, PUIS une recherche globale par sujet
            -- indépendante de tout acteur déjà suivi), outbound_links (hôte externe revenant sur
            -- plusieurs sites d'acteurs -- Lot 2 §2.4). Les mentions de presse non appariées (4e
            -- source listée par l'audit) sont volontairement omises : press.py n'extrait
            -- aujourd'hui aucun nom d'organisation (seul un matching par mot-clé sur des noms
            -- déjà connus), et une heuristique de reconnaissance de nom sans vraie extraction
            -- d'entités nommées produirait plus de bruit que de signal.
            -- country/suggested_official_url : jamais obligatoires, jamais inventés -- remplis
            -- seulement quand la source elle-même les porte (organization.csv de CORDIS a un
            -- vrai champ country + organizationURL ; OpenAlex un vrai country_code par
            -- institution), pour préremplir la promotion sans jamais la décider à la place d'un
            -- humain (voir actor_discovery.promote_candidate, qui les garde éditables).
            CREATE TABLE IF NOT EXISTS actor_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                score INTEGER NOT NULL DEFAULT 0,
                country TEXT,
                suggested_official_url TEXT,
                review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','promoted','rejected')),
                promoted_actor_id INTEGER REFERENCES actors(id),
                reviewed_by TEXT,
                reviewed_at TEXT,
                reject_reason TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actor_candidate_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES actor_candidates(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL CHECK(source_type IN ('cordis','openalex','outbound_link','patent')),
                context TEXT,
                source_url TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_id, source_type, source_url)
            );
            CREATE INDEX IF NOT EXISTS actor_candidate_sources_candidate_idx ON actor_candidate_sources(candidate_id);
            -- Un fait ponctuel sourcé sur un acteur : certification obtenue ou différenciateur.
            CREATE TABLE IF NOT EXISTS actor_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                dimension TEXT NOT NULL CHECK(dimension IN ('certification','differentiator')),
                value TEXT NOT NULL,
                source_url TEXT,
                review_status TEXT NOT NULL DEFAULT 'verified' CHECK(review_status IN ('pending','verified','rejected')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS actor_facts_actor_idx ON actor_facts(actor_id);
            -- Un événement daté sourcé sur un acteur (ex: acquisition, ouverture de site).
            CREATE TABLE IF NOT EXISTS actor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                event_date TEXT,
                source_url TEXT,
                review_status TEXT NOT NULL DEFAULT 'verified' CHECK(review_status IN ('pending','verified','rejected')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS actor_events_actor_idx ON actor_events(actor_id);
            -- Socle firmographique (chantier 3/5) : ce que l'acteur EST en tant qu'entreprise
            -- (année de création, forme juridique, tranche d'effectif), par opposition à ce
            -- qu'il FAIT (marché, technologie), déjà couvert par evidence/offers. Alimenté par
            -- firmographics.collect_french_registry() -- voir ce module pour la portée exacte
            -- (France uniquement, liste d'alias SIREN vérifiés à la main, pas de matching par
            -- nom automatique) et ce qui reste volontairement NULL (revenue_eur, parent_group,
            -- sites_json, cleanroom_iso_class, laser_systems_count : aucune source branchée
            -- pour l'instant, colonnes réservées plutôt que fabriquées).
            CREATE TABLE IF NOT EXISTS actor_profile (
                actor_id INTEGER PRIMARY KEY REFERENCES actors(id) ON DELETE CASCADE,
                founded_year INTEGER,
                legal_form_code TEXT,
                headcount_bracket_code TEXT,
                revenue_eur REAL,
                parent_group TEXT,
                sites_json TEXT,
                cleanroom_iso_class TEXT,
                laser_systems_count INTEGER,
                registry_id TEXT,
                registry_name TEXT,
                registry_active INTEGER,
                source_url TEXT,
                as_of_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Enveloppe de capacités chiffrées (chantier 5), alimentée par
            -- capabilities.collect_capability_specs() : extraction déterministe (regex +
            -- lexique MATERIALS, pas d'IA) sur les pages product/equipment/capability déjà
            -- crawlées. Une ligne par acteur ; chaque champ numérique retient la MEILLEURE
            -- valeur trouvée (min pour min_feature_size_um/tolerance_um/pulse_duration_fs,
            -- max pour max_part_size_mm/throughput_units_per_h) parmi toutes ses pages --
            -- voir le docstring de capabilities.py pour le détail de chaque pattern et
            -- pourquoi batch_size_range reste la citation brute plutôt qu'une valeur reformulée.
            CREATE TABLE IF NOT EXISTS capability_spec (
                actor_id INTEGER PRIMARY KEY REFERENCES actors(id) ON DELETE CASCADE,
                min_feature_size_um REAL,
                tolerance_um REAL,
                max_part_size_mm REAL,
                throughput_units_per_h REAL,
                wavelengths_nm TEXT,
                pulse_duration_fs REAL,
                materials_qualified TEXT,
                batch_size_range TEXT,
                source_url TEXT,
                as_of_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Une page web connue pour un acteur : URL, dernier statut HTTP, hash de contenu
            -- (pour détecter les changements), type de page détecté, score de priorité de
            -- crawl... C'est la table centrale que scrapers.scrape_actors() alimente au fil du
            -- crawl (une ligne par URL découverte/visitée).
            CREATE TABLE IF NOT EXISTS actor_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                url TEXT NOT NULL UNIQUE,
                source_kind TEXT NOT NULL DEFAULT 'official',
                active INTEGER NOT NULL DEFAULT 1,
                content_hash TEXT,
                last_http_status INTEGER,
                last_checked_at TEXT,
                last_changed_at TEXT
            );
            -- Liens sortants vers un hôte hors du domaine racine de l'acteur, jamais crawlés
            -- (§8.2 audit veille, 30/08/2026, Lot 2 §2.4) : "un hôte externe qui revient sur
            -- cinq sites d'acteurs différents est un candidat acteur de très bonne qualité --
            -- bien meilleur qu'une mention presse." Alimentée au passage du crawler (voir
            -- hybrid._meaningful_links/ParsedDocument.outbound_links, scrapers.scrape_actors),
            -- sans jamais suivre ces liens. UNIQUE(source_id,target_url) : une même page revue
            -- plusieurs fois ne duplique jamais la même cible, seulement first_seen_at compte.
            CREATE TABLE IF NOT EXISTS outbound_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES actor_sources(id) ON DELETE CASCADE,
                target_url TEXT NOT NULL,
                target_host TEXT NOT NULL,
                label TEXT,
                first_seen_at TEXT NOT NULL,
                UNIQUE(source_id, target_url)
            );
            CREATE INDEX IF NOT EXISTS outbound_links_host_idx ON outbound_links(target_host);
            -- Historique des versions d'une page (chantier 6 : "le content_hash détecte déjà
            -- le changement, il suffit de conserver l'avant"). Une ligne par changement de
            -- contenu détecté -- voir scrapers.scrape_actors, juste avant que la ligne
            -- actor_sources correspondante ne soit écrasée par la nouvelle version : c'est
            -- l'ancien (content_hash,blocks_json,title) qui est archivé ici, jamais le nouveau
            -- (déjà dans actor_sources, pas besoin d'un double). Retention bornée par
            -- scrapers.PAGE_VERSIONS_RETENTION, pour que l'historique ne grossisse pas sans
            -- limite au fil des recrawls mensuels.
            CREATE TABLE IF NOT EXISTS page_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES actor_sources(id) ON DELETE CASCADE,
                content_hash TEXT,
                blocks_json TEXT,
                title TEXT,
                captured_at TEXT,
                archived_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS page_versions_source_idx ON page_versions(source_id);
            -- Diff sémantique entre deux versions d'une page (§5.E.1/§8.3 audit veille,
            -- 30/08/2026) : la majorité du corpus (pages service/application/product) n'a
            -- aucune date de publication exploitable (voir date_confidence) -- le diff entre le
            -- blocks_json archivé dans page_versions et le nouveau est la SEULE date fiable que
            -- ce crawler puisse produire pour ces pages. Transforme "quand je l'ai vu"
            -- (created_at) en "quand ils l'ont écrit" (detected_at, borné par la fréquence de
            -- crawl). Voir scrapers._diff_page_blocks. Pas de purge par rétention ici,
            -- contrairement à page_versions/source_metrics : c'est un journal d'événements
            -- datés destiné au digest et aux séries temporelles, pas un instantané à remplacer.
            CREATE TABLE IF NOT EXISTS page_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES actor_sources(id) ON DELETE CASCADE,
                change_type TEXT NOT NULL CHECK(change_type IN (
                    'block_added', 'block_removed', 'lexicon_term_appeared', 'numeric_spec_changed'
                )),
                term TEXT,
                detail TEXT,
                old_value TEXT,
                new_value TEXT,
                detected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS page_changes_source_idx ON page_changes(source_id);
            CREATE INDEX IF NOT EXISTS page_changes_detected_idx ON page_changes(detected_at);
            -- Historique des lancements de collecte (une ligne par clic sur "Lancer le crawl
            -- acteurs" -- voir app.py: start_scrape / _run_job).
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                changed INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            -- État du profil de crawl "adaptatif" d'un acteur (1 ligne par acteur) : stratégie
            -- retenue (adaptive = avec IA de secours, generic = purement déterministe), état de
            -- couverture des types de page stratégiques, dernière erreur éventuelle.
            CREATE TABLE IF NOT EXISTS site_profiles (
                actor_id INTEGER PRIMARY KEY REFERENCES actors(id) ON DELETE CASCADE,
                strategy TEXT NOT NULL CHECK(strategy IN ('adaptive','generic')),
                status TEXT NOT NULL DEFAULT 'pending',
                profile_json TEXT,
                profile_hash TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                generated_by TEXT NOT NULL DEFAULT 'bootstrap',
                version INTEGER NOT NULL DEFAULT 1,
                last_profiled_at TEXT,
                needs_reprofile INTEGER NOT NULL DEFAULT 0 CHECK(needs_reprofile IN (0,1)),
                failure_count INTEGER NOT NULL DEFAULT 0,
                health_score REAL NOT NULL DEFAULT 1,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS profile_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                generated_by TEXT,
                confidence REAL,
                message TEXT
            );
            """
        )
        _add_columns(db, "actor_facts", dict(_REVIEW_TRACE_COLUMNS))
        _add_columns(db, "actor_events", dict(_REVIEW_TRACE_COLUMNS))
        _add_columns(db, "actors", {
            # competitive_class: C1/C2 (direct/partial competitor), T1 (technology centre),
            # A1 (internal reference, e.g. HEF/IREIS), etc. -- orthogonal to "role", which
            # stays free text. is_reference actors are excluded from competitive counts/views.
            "competitive_class": "TEXT",
            "is_reference": "INTEGER NOT NULL DEFAULT 0 CHECK(is_reference IN (0,1))",
            "parent_actor": "TEXT",
            "entity_note": "TEXT",
            **_REVIEW_TRACE_COLUMNS,
            # Human-validation queue for actors whose evidence is too thin to trust yet
            # (e.g. a newly-launched site found by the audit's counter-investigation).
            # 'verified' is the default so every actor added the normal way (or already
            # in the base before this column existed) counts as a real actor immediately.
            "review_status": "TEXT NOT NULL DEFAULT 'verified' CHECK(review_status IN ('candidate','verified','rejected','monitor'))",
            # actor_type is the *nature* of the organization (orthogonal to competitive_class,
            # which is the *relation* to us) -- a centre technologique and a prestataire
            # industriel can both be C1, but they are not the same kind of actor.
            "actor_type": (
                "TEXT CHECK(actor_type IN ("
                "'groupe_industriel','prestataire_industriel','societe_developpement_procedes',"
                "'societe_technologique_specialisee','centre_technologique','institut_recherche_appliquee',"
                "'laboratoire_academique','partenaire_adjacent'))"
            ),
            # JSON array of the business lines actually demonstrated, e.g. ["equipment","service"].
            # An actor selling both must have both, but only "service"/"process"/"research"
            # lines should ever feed the competitive score -- "equipment" alone never does.
            "business_models": "TEXT",
            # Analyst synthesis and human-validation metadata that market.db cannot supply --
            # see actor_facts/actor_events for the sourced, itemized facts (certifications,
            # differentiators, M&A events) that back this summary up.
            "strategic_summary": "TEXT",
            "last_verified_at": "TEXT",
            # Flux RSS/Atom de l'acteur lui-même (§4.A audit veille, 30/08/2026, Lot 2 §2.5),
            # découvert via <link rel="alternate"> sur sa page d'accueil (voir
            # press._discover_actor_feed). rss_feed_checked_at distingue "jamais tenté" (les
            # deux colonnes NULL) de "tenté, aucun flux trouvé" (rss_feed_url NULL mais
            # rss_feed_checked_at renseigné) -- sans cette distinction, une découverte négative
            # relancerait une requête HTTP inutile à chaque collecte.
            "rss_feed_url": "TEXT",
            "rss_feed_checked_at": "TEXT",
        })
        # Un acteur peut légitimement porter DEUX identifiants de registre à la fois : un
        # identifiant national (SIREN via firmographics.py) et un LEI international (gleif.py).
        # Tant qu'ils partageaient le trio registry_id/registry_name/registry_active, le dernier
        # collecteur exécuté écrasait silencieusement l'identifiant de l'autre -- constaté en
        # base (Meliad, acteur français, ne portait plus qu'un LEI). Le LEI a donc désormais ses
        # propres colonnes, et registry_* redevient réservé au registre national.
        _add_columns(db, "actor_profile", {
            "lei": "TEXT",
            "lei_active": "INTEGER",
            "lei_source_url": "TEXT",
            "lei_as_of_date": "TEXT",
        })
        _migrate_lei_out_of_registry_columns(db)
        _add_columns(db, "actor_sources", {
            "page_type": "TEXT",
            "source_score": "INTEGER NOT NULL DEFAULT 0",
            # §9.1 audit veille (30/08/2026, Lot 2 §2.2) : "89% des URLs découvertes ne sont
            # jamais visitées [...] et rien ne purge [le reliquat]." Sans horodatage de
            # découverte, impossible de distinguer une URL découverte hier d'une découverte il
            # y a six mois -- NULL sur les lignes déjà en base avant cette colonne (leur vraie
            # date de découverte est inconnue, jamais fabriquée) ; purge_stale_backlog() n'agit
            # que sur les lignes où cette colonne est renseignée.
            "discovered_at": "TEXT",
            "discovery_depth": "INTEGER NOT NULL DEFAULT 0",
            "discovery_context": "TEXT",
            "discovery_reason": "TEXT",
            "parent_url": "TEXT",
            "extraction_mode": "TEXT",
            "structure_hash": "TEXT",
            "last_title": "TEXT",
            "last_error": "TEXT",
            "ambiguous": "INTEGER NOT NULL DEFAULT 0",
            "blocks_json": "TEXT",
            # content_hash au moment de la DERNIERE extraction marché (scrapers.scrape_market),
            # distinct de content_hash lui-même (qui reflète le dernier CRAWL, scrape_actors).
            # Une page dont content_hash==market_extracted_hash n'a pas changé depuis sa
            # dernière analyse marché et peut être sautée (voir _select_market_sources,
            # chantier 2 item 4 : "ne re-analyser que les pages dont le content_hash a changé").
            "market_extracted_hash": "TEXT",
            # Date de publication de la page (chantier 4), extraite au moment du crawl (voir
            # hybrid._extract_published_date) -- stockée ici pour que scrape_market() puisse la
            # lire sans re-télécharger/re-parser le HTML, qu'elle utilise ou non les blocs
            # mis en cache (_stored_blocks).
            "published_date": "TEXT",
            # Détection d'anomalie de source (plan d'action web-scraping, priorité #1) :
            # non NULL quand le dernier fetch a un nombre de blocs très inférieur à la médiane
            # historique de cette page (voir scrapers._detect_content_anomaly / source_metrics
            # ci-dessous) alors même que le HTTP status reste 200 -- le cas qu'aucun champ
            # existant (last_http_status, health_score) ne couvre : une refonte HTML ou un
            # site passé au rendu JS continue de répondre normalement, seul le CONTENU s'effondre.
            # Effacé (remis à NULL) dès qu'un fetch ultérieur repasse au-dessus du seuil --
            # reflète toujours l'état constaté au DERNIER crawl, jamais un historique d'alertes.
            "anomaly_detected_at": "TEXT",
            "anomaly_detail": "TEXT",
            # Diagnostic JS (priorité #4) : signale qu'une extraction quasi vide coexiste avec
            # un DOM riche en <script>, distinguant "site pauvre en contenu" de "site qui ne
            # rend rien sans exécuter de JS" -- voir hybrid._render_required_signal. Jamais
            # corrigé automatiquement (pas de Playwright ici), seulement signalé.
            "render_required": "INTEGER NOT NULL DEFAULT 0",
            # GET conditionnel (priorité #3) : ETag/Last-Modified renvoyés par le serveur au
            # dernier fetch réussi, réutilisés au prochain passage via If-None-Match/
            # If-Modified-Since -- un 304 Not Modified évite de retélécharger un corps de page
            # que content_hash aurait de toute façon jugé inchangé, gain de politesse/bande
            # passante pur (voir scrapers._fetch).
            "etag": "TEXT",
            "last_modified_header": "TEXT",
        })
        # Historique du nombre de blocs extraits à CHAQUE crawl réussi (contrairement à
        # page_versions, qui n'archive qu'au moment d'un changement de content_hash -- une page
        # stable pendant des mois n'y génère donc aucune profondeur d'historique). C'est cette
        # table qui fournit la médiane de référence pour anomaly_detected_at ci-dessus. Retention
        # bornée par scrapers.SOURCE_METRICS_RETENTION.
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES actor_sources(id) ON DELETE CASCADE,
                block_count INTEGER NOT NULL,
                text_chars INTEGER NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS source_metrics_source_idx ON source_metrics(source_id);
            """
        )
        _add_columns(db, "site_profiles", {
            "coverage_json": "TEXT NOT NULL DEFAULT '{}'",
            "coverage_ready": "INTEGER NOT NULL DEFAULT 0",
            "coverage_discovered": "INTEGER NOT NULL DEFAULT 0",
        })
        # Audit v8 §2.4/§3 priorité 4 : "une seule source_url par ligne" -- le meilleur
        # min_feature_size_um et le meilleur max_part_size_mm, par exemple, peuvent venir de
        # deux pages différentes, mais capability_spec.source_url (un seul champ partagé) ne
        # pouvait pointer que vers l'une des deux. Une colonne par champ numérique/liste, plutôt
        # qu'une deuxième table : capability_spec reste "la meilleure valeur connue par acteur",
        # pas un historique de citations multiples comme evidence/evidence_sources (voir
        # capabilities.py). L'ancienne colonne `source_url` reste en place (première page
        # analysée pour cet acteur, inchangée) pour ne rien casser en aval ; les colonnes
        # ci-dessous sont la source de vérité par champ désormais utilisée par le front.
        _add_columns(db, "capability_spec", {
            "min_feature_size_um_source_url": "TEXT",
            "tolerance_um_source_url": "TEXT",
            "max_part_size_mm_source_url": "TEXT",
            "throughput_units_per_h_source_url": "TEXT",
            "wavelengths_nm_source_url": "TEXT",
            "pulse_duration_fs_source_url": "TEXT",
            "materials_qualified_source_url": "TEXT",
            "batch_size_range_source_url": "TEXT",
            # Audit veille §9.4/§10.12 item 0.8 (30/08/2026) : le contrôle de plausibilité
            # (_KNOWN_LASER_LINES_NM/_PULSE_MIN_FS.../_FEATURE_SIZE_MIN_UM.../_PART_SIZE_MAX_MM)
            # écartait déjà les valeurs hors plage, mais SILENCIEUSEMENT -- le champ restait NULL
            # comme si rien n'avait été trouvé, sans jamais dire qu'une valeur avait existé et
            # semblé aberrante. review_status='review' quand au moins une valeur candidate a été
            # écartée pour un champ borné (voir capabilities._extract_capabilities) ; la valeur
            # écartée elle-même n'est JAMAIS écrite comme si elle était vérifiée -- seul le fait
            # qu'une anomalie a été vue devient visible, jamais le chiffre suspect lui-même.
            "review_status": "TEXT NOT NULL DEFAULT 'verified'",
            "review_note": "TEXT",
        })
        _add_columns(db, "actor_candidates", {
            "country": "TEXT",
            "suggested_official_url": "TEXT",
        })
        _widen_actor_candidate_sources_type(db)
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS actor_sources_actor_active_score_idx
                ON actor_sources(actor_id,active,source_score DESC);
            CREATE INDEX IF NOT EXISTS actor_sources_checked_idx
                ON actor_sources(last_checked_at);
            CREATE INDEX IF NOT EXISTS actors_active_priority_idx
                ON actors(active,priority DESC,name);
            """
        )
        _normalize_existing_page_types(db)
        stamp = utc_now()
        db.executemany(
            """INSERT INTO actors(name,country,role,priority,official_url,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET country=excluded.country, role=excluded.role,
               priority=excluded.priority, official_url=excluded.official_url, updated_at=excluded.updated_at""",
            [(*actor, stamp) for actor in ACTORS],
        )
        actor_ids = {row["name"]: row["id"] for row in db.execute("SELECT id,name FROM actors")}
        for name, url, source_kind in SEED_SOURCES:
            db.execute(
                "INSERT OR IGNORE INTO actor_sources(actor_id,url,source_kind) VALUES(?,?,?)",
                (actor_ids[name], url, source_kind),
            )
        # Read priority back from the actors table itself, not the static ACTORS list: this
        # loop must also cover actors added later via create_actor() (POST /api/actors), which
        # are not and never will be in ACTORS. Looking them up in ACTORS raised StopIteration
        # here and crashed every startup once a single manually-added actor existed.
        for row in db.execute("SELECT id,priority FROM actors"):
            actor_id, priority = row["id"], row["priority"]
            db.execute(
                """INSERT INTO site_profiles(actor_id,strategy,status,generated_by)
                   VALUES(?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET strategy=excluded.strategy""",
                (actor_id, "adaptive" if priority else "generic", "pending", "bootstrap"),
            )
        _reconcile_orphaned_runs(db)


def _init_market_db() -> None:
    """Base 2/3 : faits marche, offres/capacites et leurs preuves sourcees.

    Voir _init_actors_db() pour le motif du decoupage."""

    with connect(MARKET_DB) as db:
        db.executescript(
            """
            -- Un "fait marché" canonique : tel acteur adresse tel marché, avec tel composant,
            -- via telle opération laser, à tel niveau de maturité industrielle (bucket).
            -- Une ligne = un fait unique (déduplication via fact_key/application_key, voir
            -- market_fact_key/application_key plus haut) ; les citations/preuves qui le
            -- confirment sont dans evidence_sources, pas dupliquées ici.
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK(bucket IN ('existing','radar','pending','rejected')),
                market TEXT,
                component TEXT,
                operation TEXT,
                industrial_stage TEXT,
                source_url TEXT NOT NULL,
                source_title TEXT,
                source_date TEXT,
                quote TEXT NOT NULL,
                source_group TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                review_status TEXT NOT NULL DEFAULT 'accepted' CHECK(review_status IN ('accepted','review','rejected')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_group_idx ON evidence(bucket,market,component,operation);
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            """
        )
        _add_columns(db, "evidence", {
            **_REVIEW_TRACE_COLUMNS,
            "block_heading": "TEXT",
            "block_path": "TEXT",
            "extraction_mode": "TEXT",
            "field_confidence": "REAL",
            "laser_process": "TEXT",
            "material": "TEXT",
            "performance": "TEXT",
            "maturity_level": "TEXT",
            "relation_strength": "TEXT",
            "relation_evidence": "TEXT",
            "source_role": "TEXT",
            "fact_key": "TEXT",
            "evidence_kind": "TEXT NOT NULL DEFAULT 'market_application'",
            "language": "TEXT",
            "fact_status": "TEXT NOT NULL DEFAULT 'review'",
            "last_seen_at": "TEXT",
            "application_key": "TEXT",
            # Chantier 4 : architecture avait sa propre dimension dans _candidate() depuis le
            # début, mais jamais de colonne -- seulement du texte concaténé dans
            # industrial_stage ("Architecture: TGV"), donc invisible dès qu'on arrêterait cette
            # concaténation. evidence_type : voir classify_evidence_type ci-dessus.
            "architecture": "TEXT",
            "evidence_type": "TEXT",
            # Revue chronologie du 30/08/2026 : voir classify_source_date/compute_is_backfill
            # ci-dessus. date_confidence in ('published','observed_only','unknown').
            "date_confidence": "TEXT",
            "is_backfill": "INTEGER",
            # JSON {dimension: [termes]} -- les termes du lexique qui ont DÉCLENCHÉ chaque
            # dimension de ce fait (voir scrapers._candidate). Calculés depuis toujours, jetés
            # jusqu'ici : sans eux, un rejet humain n'est rattachable à aucune règle, donc rien
            # ne peut être appris des rejets. NULL sur les lignes antérieures que le backfill
            # (backfill_match_terms.py) ne sait pas reconstituer.
            "match_terms": "TEXT",
            # 1 = match_terms reconstitué après coup par backfill_match_terms.py, jamais observé
            # à l'extraction. Ces lignes ne portent QUE les 3 dimensions de cœur : elles sont
            # re-dérivées depuis relation_evidence, la fenêtre que _candidate() utilise pour le
            # cœur -- la fenêtre `section` dont dépendent process/architecture/material/
            # performance n'est stockée nulle part et ne peut donc pas être rejouée. Sans ce
            # drapeau, comparer la fréquence de déclenchement d'une règle complémentaire entre
            # lignes anciennes et récentes conclurait à tort qu'elle ne tire jamais sur les
            # anciennes -- exactement le genre de mesure fausse que match_terms doit éviter.
            "match_terms_backfilled": "INTEGER",
        })
        db.executescript(
            """
            -- Une preuve individuelle (URL + citation exacte) à l'appui d'une ligne evidence.
            -- Plusieurs preuves peuvent citer le même fait (plusieurs langues, plusieurs pages) --
            -- c'est ce qui alimente le compteur "proofs"/"languages" affiché dans l'UI.
            CREATE TABLE IF NOT EXISTS evidence_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_title TEXT,
                source_date TEXT,
                quote TEXT NOT NULL,
                language TEXT,
                block_heading TEXT,
                block_path TEXT,
                extraction_mode TEXT,
                field_confidence REAL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_sources_fact_idx ON evidence_sources(evidence_id);

            -- Une capacité/offre d'un acteur (ex: "service de micro-usinage laser") qui n'est
            -- PAS forcément rattachée à un marché ou un composant précis -- contrairement à
            -- evidence, qui exige les trois dimensions marché/composant/opération.
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                offer_type TEXT NOT NULL,
                capability TEXT NOT NULL,
                operation TEXT,
                laser_process TEXT,
                material TEXT,
                performance TEXT,
                industrial_stage TEXT,
                page_type TEXT,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                fact_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                review_status TEXT NOT NULL DEFAULT 'accepted' CHECK(review_status IN ('accepted','review','rejected')),
                field_confidence REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS offers_actor_idx ON offers(actor_name,offer_type,capability);
            CREATE TABLE IF NOT EXISTS offer_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                language TEXT,
                block_heading TEXT,
                block_path TEXT,
                extraction_mode TEXT,
                field_confidence REAL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS offer_sources_fact_idx ON offer_sources(offer_id);

            -- Historique : à quelle date un fait evidence a changé de bucket de maturité
            -- (ex: radar -> existing), pour pouvoir tracer sa progression dans le temps.
            CREATE TABLE IF NOT EXISTS evidence_bucket_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                from_bucket TEXT,
                to_bucket TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_bucket_transitions_evidence_idx
                ON evidence_bucket_transitions(evidence_id);

            -- File d'attente de relecture humaine : un libellé proposé par l'IA (marché/
            -- composant/opération) qui ne correspond à aucune entrée connue des lexiques de
            -- scrapers.py. Un analyste valide ou rejette via /api/vocabulary-candidates
            -- (voir accept_vocabulary_candidate/reject_vocabulary_candidate ci-dessous).
            CREATE TABLE IF NOT EXISTS vocabulary_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                block_heading TEXT,
                proposed_labels TEXT NOT NULL,
                resolved_labels TEXT NOT NULL DEFAULT '{}',
                review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','accepted','rejected')),
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS vocabulary_candidates_status_idx
                ON vocabulary_candidates(review_status, created_at);

            -- Libellés promus depuis vocabulary_candidates : rechargés en mémoire au démarrage
            -- de chaque collecte marché (voir scrapers._load_custom_lexicon_entries) pour
            -- enrichir les lexiques MARKETS/COMPONENTS/OPERATIONS sans redéployer le code.
            CREATE TABLE IF NOT EXISTS custom_lexicon_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL CHECK(dimension IN ('market','component','operation')),
                label TEXT NOT NULL,
                match_terms TEXT NOT NULL,
                source_vocabulary_candidate_id INTEGER REFERENCES vocabulary_candidates(id),
                created_at TEXT NOT NULL,
                UNIQUE(dimension, label)
            );

            -- Séries temporelles (audit Horizon 2 #12 : "Construire séries temporelles par
            -- acteur, marché, technologie, maturité et signal") -- voir timeseries.py pour le
            -- calcul (capture_metric_snapshot, appelée après chaque collecte, voir
            -- app._run_job) et la lecture (read_timeseries/list_timeseries_keys). Une ligne =
            -- un instantané mensuel agrégé pour une clé donnée d'une dimension ; regroupée ici
            -- (MARKET_DB) plutôt que répartie dans les 3 bases, y compris pour la dimension
            -- 'technology' dont les compteurs sources viennent de TECH_DB, pour que
            -- /api/timeseries n'ait jamais à ouvrir plus d'un fichier SQLite en lecture.
            CREATE TABLE IF NOT EXISTS metric_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL CHECK(dimension IN ('actor','market','technology','maturity','signal')),
                dimension_key TEXT NOT NULL,
                period TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                metrics_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS metric_snapshots_uq
                ON metric_snapshots(dimension, dimension_key, period);
            CREATE INDEX IF NOT EXISTS metric_snapshots_lookup_idx
                ON metric_snapshots(dimension, period);

            -- Tableau de bord de la veille (§10.11 audit veille, 30/08/2026) : "aucun des
            -- chiffres de l'audit n'est calculé par l'application, ils viennent tous de
            -- requêtes ad hoc -- tant que ce sera le cas, aucune des dégradations décrites ici
            -- ne sera détectée en production." Huit indicateurs de SANTÉ de la collecte/
            -- extraction elle-même (pas des dimensions métier comme metric_snapshots ci-dessus),
            -- alimentés par le même mécanisme (voir veille_metrics.capture_veille_metrics,
            -- appelée après chaque collecte comme capture_metric_snapshot). value est NULL
            -- quand le dénominateur est nul (rien à mesurer ce cycle), jamais fabriqué à 0.
            CREATE TABLE IF NOT EXISTS veille_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,
                indicator TEXT NOT NULL,
                value REAL,
                captured_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS veille_metrics_uq ON veille_metrics(period, indicator);

            -- Alertes (§5.F audit veille, 30/08/2026, Lot 1 §1.4) : règles explicites évaluées
            -- après chaque collecte (voir alerts.capture_alerts, appelée comme
            -- capture_metric_snapshot/capture_veille_metrics) plutôt que des requêtes ad hoc au
            -- moment de la lecture -- l'historique des alertes doit rester consultable même
            -- après que l'état sous-jacent a changé. fingerprint rend la capture idempotente
            -- (INSERT OR IGNORE), comme evidence_sources/offer_sources/vocabulary_candidates.
            -- event_at est l'horodatage de l'ÉVÉNEMENT métier (changed_at/created_at/
            -- last_profiled_at de la ligne source), jamais celui de la capture -- c'est lui que
            -- /api/digest?since= filtre, pour que le digest ne contienne que du changement.
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL CHECK(alert_type IN (
                    'bucket_transition_existing', 'new_fact_high_value_actor', 'collection_incident', 'ma_funding_event'
                )),
                actor_name TEXT,
                summary TEXT NOT NULL,
                detail TEXT,
                source_url TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                event_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alerts_event_at_idx ON alerts(event_at);
            -- Signal de demande (§4.B.3 audit veille, 30/08/2026, Lot 4 §15) : un appel d'offres
            -- public mentionnant du micro-usinage laser ultra-rapide, vu par demand_signals.py
            -- (TED pour l'UE, BOAMP pour la France -- toutes deux vérifiées en direct avant
            -- d'écrire ce module). Sort la dimension marché du miroir de l'offre : jusqu'ici elle
            -- ne documentait que ce que les acteurs suivis DISENT faire, jamais ce que le marché
            -- ACHÈTE réellement. signal_type='tender' seulement pour l'instant -- 'hiring' (offres
            -- d'emploi des donneurs d'ordre) reste hors scope, aucune source vérifiée identifiée.
            CREATE TABLE IF NOT EXISTS demand_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type TEXT NOT NULL CHECK(signal_type IN ('tender','hiring')),
                source TEXT NOT NULL,
                buyer_name TEXT,
                title TEXT NOT NULL,
                published_at TEXT,
                source_url TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE INDEX IF NOT EXISTS demand_signals_published_idx ON demand_signals(published_at);

            -- Taille de marché (§4.B.2 audit veille, 30/08/2026, Lot 4 §14) : "les chiffres de
            -- marché laser publiés mélangent allègrement machines, services et composants" --
            -- jamais moyennée entre sources, toujours affichée avec son périmètre exact. Saisie
            -- semi-manuelle depuis un rapport public/communiqué d'analyste réel : source_url est
            -- donc NOT NULL, il n'existe aucun mode "sans source" pour cette table. Rien n'est
            -- pré-rempli -- market_sizing.py n'a ni collecteur ni valeur par défaut, seulement un
            -- CRUD, exactement comme vocabulary_candidates -> custom_lexicon_entries.
            CREATE TABLE IF NOT EXISTS market_sizing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                scope TEXT NOT NULL,
                value REAL NOT NULL,
                currency TEXT NOT NULL,
                year INTEGER NOT NULL,
                cagr REAL,
                method TEXT,
                source_url TEXT NOT NULL,
                added_by TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS market_sizing_market_idx ON market_sizing(market);

            -- Matrice de référence marché x composant x opération (§4.B.1 audit veille, Lot 4
            -- §13) : "l'app ne peut afficher que ce qu'elle a trouvé ; elle ne peut pas afficher
            -- ce que personne ne fait." Une cellule déclarée ici est une AMBITION (ce qui devrait
            -- exister d'après le métier), indépendante de ce que evidence a réellement observé --
            -- reference_matrix.py calcule le taux de couverture et les zones blanches en
            -- comparant les deux, jamais en devinant une cellule à partir des faits déjà connus
            -- (ça reviendrait à ne jamais pouvoir détecter une zone blanche).
            CREATE TABLE IF NOT EXISTS market_reference_matrix (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                component TEXT NOT NULL,
                operation TEXT NOT NULL,
                rationale TEXT,
                added_by TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(market,component,operation)
            );

            -- Golden set (§5.H audit veille, Lot 4 §17) : "sur un golden set de 30 à 50 faits
            -- vérifiés à la main [...], combien le pipeline retrouve-t-il ? À rejouer à chaque
            -- évolution des lexiques -- le seul garde-fou contre une régression silencieuse."
            -- Un fait golden est saisi à la main par un humain qui a vérifié la page lui-même ;
            -- jamais copié depuis evidence (ça ferait du golden set une simple redite de ce que
            -- le pipeline croit déjà, plus un vrai test de rappel indépendant).
            CREATE TABLE IF NOT EXISTS golden_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT NOT NULL,
                market TEXT NOT NULL,
                component TEXT NOT NULL,
                operation TEXT NOT NULL,
                source_url TEXT NOT NULL,
                expected_quote TEXT NOT NULL,
                added_by TEXT,
                created_at TEXT NOT NULL
            );

            -- Journal des décisions de revue, en APPEND-ONLY : une ligne par décision prise,
            -- jamais mise à jour ni supprimée. Les colonnes reviewed_by/reviewed_at/
            -- reject_reason posées sur chaque table restent la source pour "quel est l'état
            -- actuel de cet item" ; ce journal répond à deux questions qu'elles ne peuvent pas
            -- couvrir.
            --
            -- 1. L'HISTORIQUE. Une colonne ne garde qu'un état : un fait rejeté puis rouvert et
            --    accepté ne laisse aucune trace de son premier passage, alors que c'est
            --    exactement le genre de revirement dont une analyse des rejets doit tenir compte.
            -- 2. LA SURVIE À LA DISPARITION DE LA LIGNE. _canonicalise_existing_evidence et
            --    _migrate_evidence_fact_model SUPPRIMENT des lignes d'evidence en fusionnant les
            --    doublons : la décision part avec elles, silencieusement.
            --
            -- D'où les colonnes d'instantané (actor_name/summary/source_url) : elles figent
            -- l'identité métier de l'item au moment de la décision, pour que le journal reste
            -- lisible seul quand la ligne d'origine n'existe plus. Pas de clé étrangère, à
            -- dessein : les huit files vivent dans trois bases différentes, (queue,item_id) est
            -- une référence logique et rien d'autre.
            CREATE TABLE IF NOT EXISTS review_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('accept','reject')),
                reject_reason TEXT,
                decided_by TEXT,
                decided_at TEXT NOT NULL,
                actor_name TEXT,
                summary TEXT,
                source_url TEXT
            );
            CREATE INDEX IF NOT EXISTS review_decisions_item_idx ON review_decisions(queue,item_id);
            """
        )
        _add_columns(db, "vocabulary_candidates", dict(_REVIEW_TRACE_COLUMNS))
        _add_columns(db, "offers", {
            **_REVIEW_TRACE_COLUMNS,
            "last_seen_at": "TEXT",
            # Chantier 4 (fiabiliser la preuve) : is_verbatim distingue une citation exacte
            # (le scraper garantit déjà `quote in block.text`, voir scrapers._candidate) d'une
            # reformulation -- jusqu'ici les deux cohabitaient dans `quote` sans distinction
            # visible. source_date reçoit la date de publication de la page (voir
            # hybrid._extract_published_date), à défaut la date d'observation.
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
            "source_date": "TEXT",
            "evidence_type": "TEXT",
            # Revue chronologie du 30/08/2026 : voir classify_source_date/compute_is_backfill
            # ci-dessus. date_confidence in ('published','observed_only','unknown').
            "date_confidence": "TEXT",
            "is_backfill": "INTEGER",
            "match_terms": "TEXT",
        })
        _add_columns(db, "evidence_sources", {
            "relation_strength": "TEXT",
            "relation_evidence": "TEXT",
            "source_role": "TEXT",
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
            # §5.E.2 audit veille (30/08/2026, Lot 4 §18) : "comparer le plus ancien snapshot
            # contenant un terme au premier ne le contenant pas date l'apparition d'une offre à
            # quelques mois près, rétroactivement" -- voir wayback_retrodating.py. NULL tant
            # qu'aucune correspondance n'a été retrouvée dans l'historique Wayback (jamais
            # deviné) ; distinct de source_date, qui vient de la page elle-même quand elle en
            # affiche une (souvent absent).
            "first_appeared_at": "TEXT",
            "first_appeared_snapshot_url": "TEXT",
        })
        _add_columns(db, "evidence", {
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
        })
        _add_columns(db, "offer_sources", {
            "is_verbatim": "INTEGER NOT NULL DEFAULT 1",
            "source_date": "TEXT",
        })
        _add_columns(db, "collection_runs", {
            "offers_added": "INTEGER NOT NULL DEFAULT 0",
            "sources_added": "INTEGER NOT NULL DEFAULT 0",
            "blocks_examined": "INTEGER NOT NULL DEFAULT 0",
            "laser_blocks": "INTEGER NOT NULL DEFAULT 0",
            "candidate_count": "INTEGER NOT NULL DEFAULT 0",
            "diagnostics_json": "TEXT NOT NULL DEFAULT '{}'",
            "ai_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "ai_output_tokens": "INTEGER NOT NULL DEFAULT 0",
        })
        db.execute("UPDATE evidence SET fact_status=CASE WHEN review_status='accepted' THEN 'validated' WHEN review_status='rejected' THEN 'rejected' ELSE 'review' END WHERE fact_status IS NULL OR fact_status='' OR fact_status='review'")
        db.execute("UPDATE evidence SET last_seen_at=COALESCE(last_seen_at,updated_at,created_at)")
        db.execute("UPDATE offers SET last_seen_at=COALESCE(last_seen_at,updated_at,created_at)")
        # Chantier 4 : rows written by the scraper always set extraction_mode ("block-rules",
        # "anthropic:...", "ollama:..." -- see scrapers._candidate/_offer_candidates/
        # _ai_candidates) and are always genuinely verbatim (the pipeline enforces
        # `quote in block.text`); extraction_mode IS NULL is exactly the audit's own signal for
        # a manually-entered/seeded row (the 40 "pre_batch2"/"pre_deepenrich" groups etc.),
        # whose `quote` is often a paraphrase, not an exact excerpt. One-time backfill: harmless
        # to re-run since is_verbatim only ever moves 1->0 here, never back. `offers` has no
        # extraction_mode column at all (unlike evidence/*_sources) -- every offer row has
        # always come from the scraper, so its DEFAULT 1 is already correct with no backfill.
        db.execute("UPDATE evidence SET is_verbatim=0 WHERE extraction_mode IS NULL")
        db.execute("UPDATE evidence_sources SET is_verbatim=0 WHERE extraction_mode IS NULL")
        db.execute("UPDATE offer_sources SET is_verbatim=0 WHERE extraction_mode IS NULL")
        _migrate_industrial_stage_concatenation(db)
        _migrate_industrial_stage_placeholder_values(db)
        _backfill_evidence_type(db)
        _backfill_date_confidence_market(db)
        # Both migrations run on every startup, not just once: they are idempotent (a row that
        # already carries its canonical key is only re-derived, never duplicated) and this keeps
        # the evidence table self-healing if a row is ever inserted or edited outside the normal
        # upsert path (manual fix, restored backup) without a fact_key/application_key.
        _migrate_evidence_fact_model(db)
        _migrate_application_keys(db)
        # Cleanup before the uniqueness constraint below, not after: on an existing database
        # where the constraint was never actually enforced (CREATE TABLE IF NOT EXISTS does not
        # retrofit constraints onto an already-created table), duplicate content rows can already
        # exist and would make CREATE UNIQUE INDEX fail outright at every future startup.
        _dedupe_source_rows(db, "evidence_sources", "evidence_id")
        _dedupe_source_rows(db, "offer_sources", "offer_id")
        db.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_fact_key_uq ON evidence(fact_key) WHERE fact_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_application_key_uq
                ON evidence(application_key) WHERE application_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_sources_fingerprint_uq ON evidence_sources(fingerprint);
            CREATE UNIQUE INDEX IF NOT EXISTS offer_sources_fingerprint_uq ON offer_sources(fingerprint);
            CREATE INDEX IF NOT EXISTS evidence_status_bucket_idx
                ON evidence(fact_status,evidence_kind,bucket,created_at);
            CREATE INDEX IF NOT EXISTS evidence_last_seen_idx
                ON evidence(last_seen_at);
            CREATE INDEX IF NOT EXISTS evidence_date_confidence_idx
                ON evidence(date_confidence,created_at);
            CREATE INDEX IF NOT EXISTS offers_status_created_idx
                ON offers(review_status,created_at);
            CREATE INDEX IF NOT EXISTS offers_last_seen_idx
                ON offers(last_seen_at);
            CREATE INDEX IF NOT EXISTS offers_date_confidence_idx
                ON offers(date_confidence,created_at);
            """
        )
        _reconcile_orphaned_runs(db)


def _init_tech_db() -> None:
    """Base 3/3 : documents technologiques (publications, brevets) et signaux d'industrialisation.

    Voir _init_actors_db() pour le motif du decoupage."""

    with connect(TECH_DB) as db:
        db.executescript(
            """
            -- Une publication scientifique collectée via Crossref (voir scrapers.scrape_technology).
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_name TEXT,
                document_type TEXT NOT NULL CHECK(document_type IN ('publication','patent','project','other')),
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                published_at TEXT,
                doi TEXT,
                patent_number TEXT,
                abstract TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            -- Un signal de "maturité science -> industrie" pour un axe technologique donné
            -- (ex: SLE, LIPSS...), potentiellement partagé entre plusieurs acteurs d'un même
            -- projet collaboratif -- voir db.technology_signal_key.
            CREATE TABLE IF NOT EXISTS technology_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                axis TEXT NOT NULL,
                maturity_stage TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK(bucket IN ('existing','radar')),
                project_name TEXT,
                actor_names TEXT NOT NULL DEFAULT '[]',
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                fact_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                review_status TEXT NOT NULL DEFAULT 'accepted' CHECK(review_status IN ('accepted','review','rejected')),
                field_confidence REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS technology_signals_fact_key_uq ON technology_signals(fact_key);
            -- Un projet européen et sa fiche d'identité, une ligne par projet. Séparé de
            -- technology_signals, qui porte une ligne par (axe, projet) : y mettre le montant
            -- ou la date l'aurait répété autant de fois que le projet a d'axes, et rendu tout
            -- SUM() faux. Cette table est ce qui permet de compter des projets, des euros et
            -- des années -- ce que la page ne savait pas faire avant le 10/09/2026.
            --
            -- Le contenu vient des dumps CORDIS (titre, dates, coordinateur, montant), jamais
            -- d'un document intermédiaire : seule la classification tier/category est humaine.
            CREATE TABLE IF NOT EXISTS eu_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL UNIQUE,
                acronym TEXT NOT NULL,
                title TEXT NOT NULL,
                programme TEXT,
                started_at TEXT,
                ended_at TEXT,
                coordinator TEXT,
                coordinator_country TEXT,
                participants INTEGER,
                ec_contribution_eur REAL,
                tier TEXT,
                category TEXT,
                source_url TEXT NOT NULL,
                curated_by TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS eu_projects_tier_idx ON eu_projects(tier, category);
            CREATE TABLE IF NOT EXISTS technology_signal_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL REFERENCES technology_signals(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                source_title TEXT,
                quote TEXT NOT NULL,
                language TEXT,
                field_confidence REAL,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS technology_signal_sources_signal_idx ON technology_signal_sources(signal_id);
            """
        )
        _add_columns(db, "technology_signals", dict(_REVIEW_TRACE_COLUMNS))
        # De quel vocabulaire vient `axis`. Jusqu'au 09/09/2026 la colonne ne pouvait porter
        # que des libellés de PROCESS_TECHNOLOGIES, seul lexique branché sur les documents ; la
        # page n'arrivait donc à classer que 33% du corpus alors que cinq autres vocabulaires
        # fermés (OPERATIONS, MATERIALS, MARKETS, APPLICATION_ARCHITECTURES, PERFORMANCE_TERMS)
        # existaient déjà et couvraient 41 des 61 documents restants. Les mélanger dans une
        # colonne sans les distinguer aurait recréé le défaut corrigé par l'audit §10.10 (deux
        # libellés voisins pour deux concepts différents, indiscernables au comptage) -- d'où
        # cette colonne, qui sert aussi de groupe de facettes sur le front.
        _add_columns(db, "technology_signals", {"dimension": "TEXT"})
        _normalize_technology_axes(db)
        # Après les alias (un libellé renommé n'est pas inconnu) et avant la déduction des
        # dimensions (inutile de ranger une ligne qu'on va supprimer).
        _purge_unknown_document_axes(db)
        # APRÈS _normalize_technology_axes, jamais avant : la dimension se déduit du libellé,
        # donc elle doit être calculée sur le libellé DÉFINITIF. Dans l'autre ordre, une ligne
        # renommée par un alias garde la dimension de son ancien libellé -- constaté en
        # production le 10/09/2026 sur l'alias "Soudage / assemblage de transparents" ->
        # "Soudage", où une ligne renommée restait en `process_technology` alors que "Soudage"
        # appartient à OPERATIONS.
        _reconcile_technology_signal_dimensions(db)
        _reconcile_technology_signal_maturity(db)
        _add_columns(db, "documents", {
            "last_seen_at": "TEXT",
            # Revue chronologie du 30/08/2026 : voir classify_source_date/compute_is_backfill
            # ci-dessus. date_confidence in ('published','observed_only','unknown').
            "date_confidence": "TEXT",
            "is_backfill": "INTEGER",
            # Non NULL = ligne entrée à la main (voir curated_sources.py), donc jamais retirée
            # par prune_documents(). Porte le nom de la curation plutôt qu'un booléen : la table
            # n'a aucune colonne rattachant une publication à un projet, et c'est ce qui permet
            # de retrouver l'ensemble d'une même initiative.
            "curated_by": "TEXT",
        })
        db.execute("UPDATE documents SET last_seen_at=COALESCE(last_seen_at,created_at)")
        _normalize_partial_document_dates(db)
        _backfill_date_confidence_documents(db)
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS documents_type_published_idx
                ON documents(document_type,published_at);
            CREATE INDEX IF NOT EXISTS documents_created_idx
                ON documents(created_at);
            CREATE INDEX IF NOT EXISTS documents_date_confidence_idx
                ON documents(date_confidence,created_at);
            """
        )
        _reconcile_orphaned_runs(db)


def init_databases() -> None:
    """Crée/actualise le schéma des 3 bases (appelée à chaque démarrage, voir app.py: lifespan).

    Idempotente et additive : toutes les instructions sont `CREATE TABLE IF NOT EXISTS` /
    `CREATE INDEX IF NOT EXISTS`, et les colonnes ajoutées après coup passent par
    _add_columns() qui ne fait rien si la colonne existe déjà -- donc relancer cette fonction
    sur une base qui a déjà des données ne perd jamais rien, elle ne fait que compléter le
    schéma manquant.

    Le travail est délégué à une fonction par fichier SQLite (_init_actors_db, _init_market_db,
    _init_tech_db), dans cet ordre. L'ordre compte : les deux dernières supposent que DATA_DIR
    existe déjà, et le reste du module lit ACTORS_DB en premier. Chacune crée ses tables puis
    lance ses migrations de données (normalisation, dédoublonnage, backfill de colonnes).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _init_actors_db()
    _init_market_db()
    _init_tech_db()


def _backup_dir() -> Path:
    """Resolved fresh on every call (DATA_DIR / "backups"), like ACTORS_DB/MARKET_DB/TECH_DB
    are used elsewhere in this module -- tests patch DATA_DIR to an isolated path, and a
    module-level constant computed once at import time would silently keep pointing at the
    real project's data/backups/ instead of following that patch."""
    return DATA_DIR / "backups"


def _backup_one(path: Path, stamp: str) -> Path | None:
    """Copy one SQLite file to data/backups/<stem>_<stamp>.db. No-op if it doesn't exist yet."""
    if not path.exists():
        return None
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{path.stem}_{stamp}.db"
    shutil.copy2(path, backup_path)
    return backup_path


_AUTO_BACKUP_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _prune_backups(stem: str, keep: int = BACKUP_RETENTION_COUNT) -> None:
    """Keep only the ``keep`` most recent AUTOMATIC backups for one database.

    Only ``<stem>_<stamp>.db`` files are eligible, where <stamp> is the utc_now-style stamp
    written by _backup_one -- those sort chronologically by name. Manually named checkpoints
    (``market_pre_ontology_cleanup_...``) are left alone.

    The glob used to be ``{stem}_*.db``, which swept those checkpoints in too. Sorting the
    mixed list by name put every ``<stem>_pre_...`` above every ``<stem>_<digits>...`` ('p' >
    '2'), so once a database had ``keep`` named checkpoints -- market and actors both had
    exactly 14 -- the retention window was full of them and the backup that had JUST been
    written was always the first thing deleted. backup_all_databases() then returned, and
    printed, the path of a file it had already removed: every caller relying on it for a
    rollback point (prune_off_topic_sources.py, backfill_match_terms.py, each scrape run) had
    no rollback point at all, and no way to notice.
    """
    backup_dir = _backup_dir()
    if not backup_dir.exists():
        return
    automatic = [
        path for path in backup_dir.glob(f"{stem}_*.db")
        if _AUTO_BACKUP_STAMP_RE.match(path.stem[len(stem) + 1:])
    ]
    for stale in sorted(automatic, key=lambda p: p.name, reverse=True)[keep:]:
        stale.unlink(missing_ok=True)


def backup_all_databases() -> list[Path]:
    """Snapshot the three SQLite databases to data/backups/ before a collection run.

    Best-effort and additive: a missing or unreadable database is skipped rather than aborting
    the others, and this never runs automatically at import time or app startup -- only right
    before a scrape job -- so a bad run always has a rollback point without the analyst having
    to remember to run reset_market_db.py first. Old backups beyond BACKUP_RETENTION_COUNT are
    pruned per database so data/backups/ doesn't grow unbounded.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    saved: list[Path] = []
    for path in (ACTORS_DB, MARKET_DB, TECH_DB):
        try:
            backup_path = _backup_one(path, stamp)
        except OSError:
            continue
        if backup_path:
            saved.append(backup_path)
            _prune_backups(path.stem)
    return saved


def reset_market_database(*, backup: bool = True) -> Path | None:
    """Rebuild market.db from an empty schema while preserving actors.db and technology.db.

    When ``backup`` is true the previous SQLite database is copied to ``data/backups`` with a
    timestamp before deletion. WAL/SHM sidecars are removed as well. This is intentionally an
    explicit maintenance action and is never executed automatically at application startup.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_one(MARKET_DB, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")) if backup else None
    if backup_path:
        _prune_backups(MARKET_DB.stem)
    for path in (MARKET_DB, Path(str(MARKET_DB) + "-wal"), Path(str(MARKET_DB) + "-shm")):
        if path.exists():
            path.unlink()
    init_databases()
    return backup_path


# Deux petits raccourcis génériques utilisés par app.py pour lire les bases sans ouvrir de
# connexion à la main à chaque endpoint : `rows` pour un SELECT qui renvoie plusieurs lignes
# (converties en dicts JSON-sérialisables), `scalar` pour une seule valeur (ex: un COUNT(*)).
def rows(path: Path, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect(path) as db:
        return [dict(row) for row in db.execute(query, tuple(params)).fetchall()]


def scalar(path: Path, query: str, params: Iterable[Any] = ()) -> Any:
    with connect(path) as db:
        result = db.execute(query, tuple(params)).fetchone()
        return result[0] if result else None


def create_actor(name: str, country: str, role: str, official_url: str, priority: bool = False) -> int:
    """Add a new actor outside the static ACTORS seed list.

    Growing coverage currently means editing the hardcoded ACTORS list in this module and
    redeploying; this lets an analyst add one from the UI/API instead. Bootstraps a matching
    site_profiles row so the crawler picks the actor up on its next run, exactly like a
    seeded one. Raises ValueError on a blank/duplicate name or a non-http(s) URL.
    """
    name = name.strip()
    if not name:
        raise ValueError("Actor name is required")
    official_url = official_url.strip()
    if not official_url.startswith(("http://", "https://")):
        raise ValueError("official_url must be an absolute http(s) URL")
    stamp = utc_now()
    with connect(ACTORS_DB) as db:
        if db.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone():
            raise ValueError(f"An actor named '{name}' already exists")
        actor_id = db.execute(
            "INSERT INTO actors(name,country,role,priority,official_url,updated_at) VALUES(?,?,?,?,?,?)",
            (name, country.strip(), role.strip(), int(bool(priority)), official_url, stamp),
        ).lastrowid
        assert actor_id is not None
        db.execute(
            "INSERT INTO site_profiles(actor_id,strategy,status,generated_by) VALUES(?,?,?,?)",
            (actor_id, "adaptive" if priority else "generic", "pending", "manual"),
        )
    return actor_id


def set_actor_active(actor_id: int, active: bool) -> None:
    """Pause or resume an actor without deleting its history. Raises ValueError if unknown."""
    with connect(ACTORS_DB) as db:
        updated = db.execute(
            "UPDATE actors SET active=?,updated_at=? WHERE id=?", (int(bool(active)), utc_now(), actor_id)
        ).rowcount
    if not updated:
        raise ValueError(f"Actor {actor_id} not found")


ACTOR_TYPES = {
    "groupe_industriel", "prestataire_industriel", "societe_developpement_procedes",
    "societe_technologique_specialisee", "centre_technologique", "institut_recherche_appliquee",
    "laboratoire_academique", "partenaire_adjacent",
}
BUSINESS_MODELS = {"equipment", "service", "process", "research", "internal"}


def update_actor_classification(
    actor_id: int,
    *,
    name: str | None = None,
    country: str | None = None,
    role: str | None = None,
    official_url: str | None = None,
    priority: bool | None = None,
    competitive_class: str | None = None,
    is_reference: bool | None = None,
    parent_actor: str | None = None,
    entity_note: str | None = None,
    review_status: str | None = None,
    actor_type: str | None = None,
    business_models: list[str] | None = None,
    strategic_summary: str | None = None,
) -> None:
    """Patch an actor's editable fields: the plain descriptive ones (name, country, role,
    official_url, priority) plus the analytical fields (competitive class, actor type,
    business model(s), internal-reference flag, M&A parent/note, human-review status) added
    on top of them. Only fields explicitly passed (not None) are updated, so a caller can set
    a single field without clobbering the others.
    """
    updates: dict[str, object] = {}
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Actor name is required")
        updates["name"] = name
        with connect(ACTORS_DB) as db:
            clash = db.execute("SELECT id FROM actors WHERE name=? AND id<>?", (name, actor_id)).fetchone()
        if clash:
            raise ValueError(f"An actor named '{name}' already exists")
    if country is not None:
        updates["country"] = country
    if role is not None:
        updates["role"] = role
    if official_url is not None:
        official_url = official_url.strip()
        if not official_url.startswith(("http://", "https://")):
            raise ValueError("official_url must be an absolute http(s) URL")
        updates["official_url"] = official_url
    if priority is not None:
        updates["priority"] = int(bool(priority))
    if competitive_class is not None:
        updates["competitive_class"] = competitive_class
    if is_reference is not None:
        updates["is_reference"] = int(bool(is_reference))
    if parent_actor is not None:
        updates["parent_actor"] = parent_actor
    if entity_note is not None:
        updates["entity_note"] = entity_note
    if review_status is not None:
        if review_status not in {"candidate", "verified", "rejected", "monitor"}:
            raise ValueError(f"Invalid review_status: {review_status!r}")
        updates["review_status"] = review_status
    if actor_type is not None:
        if actor_type not in ACTOR_TYPES:
            raise ValueError(f"Invalid actor_type: {actor_type!r}")
        updates["actor_type"] = actor_type
    if business_models is not None:
        invalid = set(business_models) - BUSINESS_MODELS
        if invalid:
            raise ValueError(f"Invalid business_models: {sorted(invalid)!r}")
        updates["business_models"] = json.dumps(business_models)
    if strategic_summary is not None:
        updates["strategic_summary"] = strategic_summary
    if not updates:
        return
    updates["updated_at"] = utc_now()
    assignments = ",".join(f"{field}=?" for field in updates)
    with connect(ACTORS_DB) as db:
        updated = db.execute(
            f"UPDATE actors SET {assignments} WHERE id=?", (*updates.values(), actor_id)
        ).rowcount
    if not updated:
        raise ValueError(f"Actor {actor_id} not found")


def delete_actor(actor_id: int) -> None:
    """Permanently remove an actor and everything scraped for it (sources, site profile,
    relations cascade via ON DELETE CASCADE). Irreversible -- the caller is responsible for
    confirming this with a human first. Raises ValueError if the actor doesn't exist.
    """
    with connect(ACTORS_DB) as db:
        deleted = db.execute("DELETE FROM actors WHERE id=?", (actor_id,)).rowcount
    if not deleted:
        raise ValueError(f"Actor {actor_id} not found")


def add_actor_relation(
    actor_id: int,
    relation_type: str,
    related_name: str,
    *,
    related_actor_id: int | None = None,
    note: str | None = None,
    source_url: str | None = None,
) -> int:
    """Record a partner/supplier/client relation for the network map. ``related_name`` is
    always stored (even when ``related_actor_id`` points at a tracked actor) so the edge
    still renders a label if that actor is later renamed or removed.
    """
    if relation_type not in {"partner", "supplier", "client"}:
        raise ValueError(f"Invalid relation_type: {relation_type!r}")
    related_name = related_name.strip()
    if not related_name:
        raise ValueError("related_name is required")
    with connect(ACTORS_DB) as db:
        exists = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not exists:
            raise ValueError(f"Actor {actor_id} not found")
        return db.execute(
            """INSERT INTO actor_relations(actor_id,related_actor_id,related_name,relation_type,note,source_url,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (actor_id, related_actor_id, related_name, relation_type, note, source_url, utc_now()),
        ).lastrowid


def add_actor_fact(actor_id: int, dimension: str, value: str, *, source_url: str | None = None) -> int:
    """Record one sourced, itemized fact (certification or differentiator) that market.db
    cannot supply -- see the fiches-cibles integration plan. Distinct from actors.role/
    strategic_summary, which stay free text: this is queryable per dimension.
    """
    if dimension not in {"certification", "differentiator"}:
        raise ValueError(f"Invalid dimension: {dimension!r}")
    value = value.strip()
    if not value:
        raise ValueError("value is required")
    with connect(ACTORS_DB) as db:
        exists = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not exists:
            raise ValueError(f"Actor {actor_id} not found")
        return db.execute(
            "INSERT INTO actor_facts(actor_id,dimension,value,source_url,created_at) VALUES(?,?,?,?,?)",
            (actor_id, dimension, value, source_url, utc_now()),
        ).lastrowid


def add_actor_event(
    actor_id: int, event_type: str, description: str, *, event_date: str | None = None, source_url: str | None = None
) -> int:
    """Record one dated, sourced event (e.g. an acquisition) for an actor's fiche."""
    event_type = event_type.strip()
    if not event_type:
        raise ValueError("event_type is required")
    description = description.strip()
    if not description:
        raise ValueError("description is required")
    with connect(ACTORS_DB) as db:
        exists = db.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not exists:
            raise ValueError(f"Actor {actor_id} not found")
        return db.execute(
            "INSERT INTO actor_events(actor_id,event_type,description,event_date,source_url,created_at) VALUES(?,?,?,?,?,?)",
            (actor_id, event_type, description, event_date, source_url, utc_now()),
        ).lastrowid


def upsert_actor_event(
    db: sqlite3.Connection,
    actor_id: int,
    event_type: str,
    description: str,
    *,
    source_url: str,
    event_date: str | None = None,
    review_status: str = "pending",
    dedupe_on_description: bool = False,
    refresh_event_type: bool = False,
) -> int:
    """Écrit un événement collecté dans actor_events, en dédoublonnant sur sa source.

    Contrairement à add_actor_event() (qui ouvre sa propre connexion, pour un ajout manuel
    isolé), cette fonction prend une connexion déjà ouverte : les collecteurs écrivent des
    dizaines d'événements dans une seule transaction.

    Remplace trois implémentations quasi identiques -- cordis._upsert_actor_event,
    press._upsert_press_event et scrapers._upsert_career_event -- dont les seules vraies
    différences sont portées ici par des paramètres explicites :

    - ``review_status`` : CORDIS écrit 'verified' (participation à un projet européen, sourcée
      sur une page stable), presse et recrutement écrivent 'pending' (un appariement par
      mot-clé sur un titre d'article ou une offre d'emploi est un signal faible, jamais publié
      sans relecture).
    - ``dedupe_on_description`` : une page carrières liste souvent plusieurs intitulés sous la
      même URL, la source seule ne suffit donc pas à les distinguer.
    - ``refresh_event_type`` : la presse peut reclasser un événement déjà vu (un mot-clé ajouté
      depuis la dernière collecte), sans pour autant le dupliquer.

    Renvoie 1 si une ligne a été insérée, 0 sinon -- même contrat que les trois fonctions
    remplacées, dont les appelants comptent les insertions.
    """
    if dedupe_on_description:
        existing = db.execute(
            "SELECT id,event_type FROM actor_events WHERE actor_id=? AND source_url=? AND description=?",
            (actor_id, source_url, description),
        ).fetchone()
    else:
        existing = db.execute(
            "SELECT id,event_type FROM actor_events WHERE actor_id=? AND source_url=?",
            (actor_id, source_url),
        ).fetchone()
    if existing:
        if refresh_event_type and existing["event_type"] != event_type:
            db.execute("UPDATE actor_events SET event_type=? WHERE id=?", (event_type, existing["id"]))
        return 0
    db.execute(
        """INSERT INTO actor_events(actor_id,event_type,description,event_date,source_url,review_status,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (actor_id, event_type, description, event_date, source_url, review_status, utc_now()),
    )
    return 1


def upsert_document(
    db: sqlite3.Connection,
    *,
    document_type: str,
    title: str,
    source_url: str,
    fingerprint_source: str,
    actor_name: str | None = None,
    published_at: str | None = None,
    doi: str | None = None,
    patent_number: str | None = None,
    abstract: str | None = None,
) -> tuple[int, int]:
    """Écrit un document (publication, brevet) dans technology.db, dédoublonné sur son empreinte.

    Remplace openalex._upsert_document et patent._upsert_patent_document, qui ne différaient que
    par trois choses désormais passées en paramètres : le ``document_type``, la colonne
    d'identité renseignée (``doi`` ou ``patent_number``) et la chaîne dont on dérive l'empreinte
    (le DOI ou l'URL pour une publication, le numéro de brevet pour un brevet).

    Renvoie ``(inséré, attribué)`` : le second compteur est le comportement utile hérité des deux
    fonctions d'origine -- un document déjà vu SANS acteur (trouvé par une passe thématique, ou
    par la collecte Crossref générique) se voit enfin rattacher son ``actor_name`` au lieu de
    rester orphelin, sans être dupliqué pour autant.
    """
    fingerprint = hashlib.sha256(fingerprint_source.casefold().encode()).hexdigest()
    stamp = utc_now()
    # published_at vient toujours d'un champ structuré de l'API amont, jamais d'un repli
    # fabriqué : 'published' vs 'observed_only' se décide donc sans ambiguïté dès l'écriture,
    # contrairement à evidence/offers (voir scrapers.py). is_backfill compare cette date à
    # `stamp`, qui EST le created_at de la ligne (première insertion, jamais réécrite ensuite).
    date_confidence = "published" if published_at else "observed_only"
    is_backfill = compute_is_backfill(stamp, published_at, date_confidence)
    before = db.total_changes
    db.execute(
        """INSERT OR IGNORE INTO documents(
               actor_name,document_type,title,source_url,published_at,doi,patent_number,abstract,
               date_confidence,is_backfill,fingerprint,created_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            actor_name, document_type, title, source_url, published_at, doi, patent_number, abstract,
            date_confidence, is_backfill, fingerprint, stamp, stamp,
        ),
    )
    if db.total_changes > before:
        return 1, 0
    db.execute("UPDATE documents SET last_seen_at=? WHERE fingerprint=?", (stamp, fingerprint))
    if not actor_name:
        return 0, 0
    row = db.execute("SELECT actor_name FROM documents WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row and not row["actor_name"]:
        db.execute("UPDATE documents SET actor_name=? WHERE fingerprint=?", (actor_name, fingerprint))
        return 0, 1
    return 0, 0


def upsert_eu_project(db: sqlite3.Connection, *, project_id: str, **fields: Any) -> int:
    """Écrit la fiche d'un projet européen, ou la rafraîchit si elle existe déjà.

    Renvoie 1 si la ligne est neuve, 0 sinon. Les champs absents de l'appel ne sont jamais
    écrasés par NULL : un import ultérieur qui n'apporte qu'une partie des métadonnées
    (typiquement le montant, publié plus tard par CORDIS) complète la fiche au lieu de la
    vider.
    """
    stamp = utc_now()
    allowed = {
        "acronym", "title", "programme", "started_at", "ended_at", "coordinator",
        "coordinator_country", "participants", "ec_contribution_eur", "tier", "category",
        "source_url", "curated_by",
    }
    payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
    row = db.execute("SELECT id FROM eu_projects WHERE project_id=?", (project_id,)).fetchone()
    if row:
        if payload:
            assignments = ",".join(f"{k}=?" for k in payload)
            db.execute(
                f"UPDATE eu_projects SET {assignments},last_seen_at=? WHERE id=?",
                (*payload.values(), stamp, row["id"]),
            )
        else:
            db.execute("UPDATE eu_projects SET last_seen_at=? WHERE id=?", (stamp, row["id"]))
        return 0
    columns = ["project_id", *payload, "created_at", "last_seen_at"]
    placeholders = ",".join("?" * len(columns))
    db.execute(
        f"INSERT INTO eu_projects({','.join(columns)}) VALUES({placeholders})",
        (project_id, *payload.values(), stamp, stamp),
    )
    return 1


def upsert_technology_signal(
    db: sqlite3.Connection,
    *,
    fact_key: str,
    axis: str,
    maturity_stage: str,
    bucket: str,
    actor_names: list[str],
    source_url: str,
    quote: str,
    field_confidence: float,
    project_name: str | None = None,
    source_title: str | None = None,
    dimension: str = "process_technology",
) -> tuple[int, int]:
    """Insère un signal technologique, ou fusionne ses acteurs s'il existe déjà.

    Cœur commun à cordis._upsert_technology_signal (un axe pour un projet européen) et
    scrapers.upsert_document_technology_signal (les axes détectés dans un document) : les deux
    dérivaient leur ``fact_key`` différemment -- (axe, projet) contre (axe, URL du document) --
    mais écrivaient ensuite exactement la même ligne, avec la même règle de fusion.

    Cette règle est le point important : un même axe peut être porté par plusieurs acteurs d'un
    même consortium, donc ``actor_names`` fusionne au lieu de dupliquer la ligne.

    Renvoie ``(1 si créé sinon 0, id du signal)``. L'écriture de la ligne de preuve associée
    (technology_signal_sources) reste à l'appelant : CORDIS en écrit une, la passe documentaire
    non.
    """
    stamp = utc_now()
    row = db.execute("SELECT id,actor_names FROM technology_signals WHERE fact_key=?", (fact_key,)).fetchone()
    if row:
        signal_id = int(row["id"])
        if actor_names:
            merged = sorted(set(json.loads(row["actor_names"] or "[]")) | set(actor_names))
            db.execute(
                "UPDATE technology_signals SET actor_names=?,updated_at=?,last_seen_at=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), stamp, stamp, signal_id),
            )
        return 0, signal_id
    new_id = db.execute(
        """INSERT INTO technology_signals(
               axis,maturity_stage,bucket,project_name,actor_names,source_url,source_title,quote,
               fact_key,fingerprint,review_status,field_confidence,dimension,created_at,updated_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?,?,?)""",
        (
            axis, maturity_stage, bucket, project_name,
            json.dumps(sorted(set(actor_names)), ensure_ascii=False),
            source_url, source_title, quote, fact_key,
            hashlib.sha256(fact_key.encode()).hexdigest(), field_confidence, dimension, stamp, stamp, stamp,
        ),
    ).lastrowid
    assert new_id is not None  # garanti par sqlite3 juste après un INSERT AUTOINCREMENT réussi
    return 1, int(new_id)


# Les trois tables de preuve qui partagent le même contrat : une citation sourcée rattachée à un
# fait, dédoublonnée sur son empreinte. La clé étrangère et les colonnes acceptées sont listées
# ici plutôt que déduites à l'exécution -- le nom de table entrant dans le SQL, il ne doit jamais
# venir d'ailleurs que de cette table blanche.
_FACT_SOURCE_TABLES: dict[str, tuple[str, frozenset[str]]] = {
    "evidence_sources": ("evidence_id", frozenset({
        "source_url", "source_title", "source_date", "quote", "is_verbatim", "language",
        "block_heading", "block_path", "extraction_mode", "field_confidence",
        "relation_strength", "relation_evidence", "source_role", "fingerprint", "created_at",
    })),
    "offer_sources": ("offer_id", frozenset({
        "source_url", "source_title", "source_date", "quote", "is_verbatim", "language",
        "block_heading", "block_path", "extraction_mode", "field_confidence",
        "fingerprint", "created_at",
    })),
    "technology_signal_sources": ("signal_id", frozenset({
        "source_url", "source_title", "quote", "language", "field_confidence",
        "fingerprint", "created_at",
    })),
}


def upsert_fact_source(db: sqlite3.Connection, table: str, fact_id: int, **fields: Any) -> int:
    """Écrit une citation sourcée rattachée à un fait, sans jamais la dupliquer.

    Le patron « un fait + ses preuves » est implémenté par trois tables au contrat identique
    (evidence_sources, offer_sources, technology_signal_sources), chacune avec sa propre copie
    du même INSERT OR IGNORE + comptage via total_changes. Cette fonction les remplace : les
    champs non pertinents pour une table donnée sont simplement ignorés, ce qui évite d'écrire
    trois signatures différentes pour la même opération.

    N'inclut délibérément PAS actor_candidate_sources : cette table porte (source_type, context)
    et ni citation ni empreinte -- elle décrit d'où vient un candidat, pas ce qui prouve un fait.
    L'aligner supposerait une migration de schéma, pas un simple partage de code.

    Renvoie 1 si une ligne a été insérée, 0 si l'empreinte était déjà connue.
    """
    if table not in _FACT_SOURCE_TABLES:
        raise ValueError(f"Table de preuve inconnue : {table!r} (attendues : {sorted(_FACT_SOURCE_TABLES)})")
    fact_id_column, allowed = _FACT_SOURCE_TABLES[table]
    fields.setdefault("created_at", utc_now())
    payload = {k: v for k, v in fields.items() if k in allowed}
    columns = [fact_id_column, *payload]
    placeholders = ",".join("?" for _ in columns)
    before = db.total_changes
    db.execute(
        f"INSERT OR IGNORE INTO {table}({','.join(columns)}) VALUES({placeholders})",
        (fact_id, *payload.values()),
    )
    return int(db.total_changes > before)


def _normalize_domain(url: str) -> str:
    host = urlparse(url).netloc.casefold()
    return host[4:] if host.startswith("www.") else host


def _name_tokens(name: str) -> set[str]:
    stop = {"gmbh", "ag", "srl", "ltd", "sa", "sas", "sarl", "inc", "co", "laser", "lasers", "photonics", "technology", "technologies"}
    slug = _slug(name).replace("-", " ")
    return {token for token in slug.split() if token and token not in stop}


def find_actor_duplicate_candidates() -> list[dict[str, object]]:
    """Flag actor pairs that may be the same organization: exact domain match, a shared
    parent company, or near-identical names once legal suffixes (GmbH, Ltd...) are
    stripped. Read-only -- this only reports candidates, it never merges or deletes
    anything; a human decides. No address/registry data is collected today, so that
    signal from the audit isn't checked here.
    """
    with connect(ACTORS_DB) as db:
        actors = [dict(row) for row in db.execute("SELECT id,name,official_url,parent_actor FROM actors WHERE active=1")]
    candidates: list[dict[str, object]] = []
    for i, left in enumerate(actors):
        left_domain = _normalize_domain(left["official_url"])
        left_tokens = _name_tokens(left["name"])
        for right in actors[i + 1 :]:
            reasons = []
            if left_domain and left_domain == _normalize_domain(right["official_url"]):
                reasons.append("meme domaine officiel")
            if left["parent_actor"] and left["parent_actor"] == right["name"]:
                reasons.append(f"{left['name']} rattache a {right['name']}")
            elif right["parent_actor"] and right["parent_actor"] == left["name"]:
                reasons.append(f"{right['name']} rattache a {left['name']}")
            elif left["parent_actor"] and left["parent_actor"] == right["parent_actor"]:
                reasons.append(f"meme maison mere ({left['parent_actor']})")
            right_tokens = _name_tokens(right["name"])
            if left_tokens and right_tokens and left_tokens == right_tokens:
                reasons.append("noms identiques hors forme juridique")
            if reasons:
                candidates.append({
                    "actor_a": left["name"], "actor_a_id": left["id"],
                    "actor_b": right["name"], "actor_b_id": right["id"],
                    "reasons": reasons,
                })
    return candidates


def accept_vocabulary_candidate(candidate_id: int, dimension: str) -> dict[str, str]:
    """Promote one proposed label from a vocabulary candidate into the live custom lexicon.

    ``dimension`` selects which of the candidate's (possibly several) unresolved dimensions
    to promote -- a candidate proposing both an unknown component and an unknown market
    requires one call per dimension, so each can be reviewed on its own merits. The matching
    rule created is a single-term "any_of" using the accepted label's own text, mirroring
    how most existing lexicon entries already work (e.g. "Stents": any_of=("stent",)).
    Raises ValueError if the candidate doesn't exist or has no proposal for that dimension.
    """
    with connect(MARKET_DB) as db:
        row = db.execute("SELECT proposed_labels FROM vocabulary_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise ValueError(f"Vocabulary candidate {candidate_id} not found")
        proposed = json.loads(row["proposed_labels"])
        label = proposed.get(dimension)
        if not label:
            raise ValueError(f"No proposed label for dimension '{dimension}' on candidate {candidate_id}")
        stamp = utc_now()
        db.execute(
            """INSERT INTO custom_lexicon_entries(dimension,label,match_terms,source_vocabulary_candidate_id,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(dimension,label) DO NOTHING""",
            (dimension, label, json.dumps([label], ensure_ascii=False), candidate_id, stamp),
        )
        db.execute("UPDATE vocabulary_candidates SET review_status='accepted' WHERE id=?", (candidate_id,))
    return {"dimension": dimension, "label": label}


def reject_vocabulary_candidate(candidate_id: int) -> None:
    """Mark a vocabulary candidate reviewed-and-declined. Raises ValueError if unknown."""
    with connect(MARKET_DB) as db:
        updated = db.execute(
            "UPDATE vocabulary_candidates SET review_status='rejected' WHERE id=?", (candidate_id,)
        ).rowcount
    if not updated:
        raise ValueError(f"Vocabulary candidate {candidate_id} not found")


# Même principe que accept_vocabulary_candidate/reject_vocabulary_candidate, mais pour les
# faits `evidence` en attente de relecture humaine (fact_status='review' pour un fait proposé
# par l'IA, 'partial' pour un fait à 2 dimensions sur 3 -- voir scrapers._candidate et
# scrapers._ai_candidates, chantier 2 items 2 et 3). Avant ce couple de fonctions, ces faits
# atterrissaient bien en base (jamais jetés) mais n'avaient aucun moyen d'en sortir : ni
# promotion vers la matrice marché validée, ni rejet explicite.
def accept_evidence_review(evidence_id: int) -> dict[str, Any]:
    """Promote one review/partial evidence row to 'validated' after a human check.

    Raises ValueError if the row doesn't exist or was already validated (fact_status
    'validated' facts go through the normal collection pipeline, not this manual queue).
    """
    with connect(MARKET_DB) as db:
        row = db.execute("SELECT id,fact_status FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError(f"Evidence {evidence_id} not found")
        if row["fact_status"] == "validated":
            raise ValueError(f"Evidence {evidence_id} is already validated")
        # reviewed_at est posé ICI, pas seulement par review_queue.decide_review_item : les
        # endpoints hérités /api/market/review/{id}/accept|reject (ceux que l'écran marché
        # utilise) appellent cette fonction directement. Sans ce marquage, une décision prise
        # depuis cet écran restait indiscernable d'une ligne jamais relue -- et la garde
        # "ne jamais écraser une décision humaine" de scrapers._upsert_market_candidate, qui
        # teste reviewed_at, l'aurait donc laissée se faire écraser au crawl suivant.
        # data_quality.compute_precision (WHERE reviewed_at IS NOT NULL) y gagne au passage :
        # elle comptait jusqu'ici les seules décisions passées par la file unifiée.
        db.execute(
            "UPDATE evidence SET fact_status='validated',review_status='accepted',reviewed_at=?,updated_at=? WHERE id=?",
            (stamp := utc_now(), stamp, evidence_id),
        )
    return {"id": evidence_id, "fact_status": "validated"}


def reject_evidence_review(
    evidence_id: int, *, reviewed_by: str | None = None, reject_reason: str | None = None
) -> None:
    """Mark a review/partial evidence row reviewed-and-declined. Raises ValueError if unknown.

    fact_status is left untouched (still 'review'/'partial', for an audit trail of what was
    proposed) -- review_status='rejected' alone is enough to drop it from both the validated
    market matrix (which filters on fact_status='validated') and the pending-review queue
    (which filters on review_status='review').

    ``reject_reason`` est le motif TYPÉ (review_queue.REJECT_REASONS), validé par l'appelant
    HTTP. Il est écrit ici parce que c'est cette fonction -- et non decide_review_item -- que
    l'écran « Faits marché à valider » emprunte : c'est la file la plus volumineuse (123 des 152
    items en attente), et donc celle dont les rejets ont le plus à enseigner. Les paramètres
    restent optionnels pour ne pas casser decide_review_item, qui écrit déjà sa propre trace
    juste après son appel.
    """
    with connect(MARKET_DB) as db:
        # reviewed_at : même raison que dans accept_evidence_review ci-dessus -- c'est ce
        # marquage qui rend la décision reconnaissable par la garde du crawler.
        # COALESCE sur les deux colonnes de trace : decide_review_item appelle cette fonction
        # SANS motif puis pose le sien dans la foulée -- un NULL passé ici ne doit pas pouvoir
        # écraser un motif déjà écrit si l'ordre de ces deux écritures venait à changer.
        updated = db.execute(
            """UPDATE evidence
                  SET review_status='rejected', reviewed_at=?, updated_at=?,
                      reviewed_by=COALESCE(?,reviewed_by), reject_reason=COALESCE(?,reject_reason)
                WHERE id=? AND fact_status!='validated'""",
            (stamp := utc_now(), stamp, reviewed_by, reject_reason, evidence_id),
        ).rowcount
    if not updated:
        raise ValueError(f"Evidence {evidence_id} not found or already validated")
