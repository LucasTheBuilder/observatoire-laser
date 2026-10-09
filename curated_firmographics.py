"""Années de création relevées à la main, pour les acteurs hors du registre français.

``firmographics.py`` ne sait interroger que le registre INSEE (France, par SIREN vérifié). Pour
les autres pays, aucune source gratuite et fiable n'est branchée -- et le module précise
explicitement ne rien fabriquer. Ce module comble le trou avec la seule source disponible :
l'acteur lui-même, ou l'organisme qui l'a fondé (ex. la chronique officielle de Fraunhofer).

Chaque entrée porte l'URL exacte et la phrase VERBATIM qui énonce l'année : rien n'est déduit,
et ``registry_name`` dit en clair que la valeur est une déclaration et non une donnée de
registre. Une année déclarée par un site est une origine revendiquée ("our story began in
1987"), pas une immatriculation -- c'est pourquoi elle n'écrase JAMAIS une ligne issue d'un
registre (founded_year déjà renseigné -> l'entrée est ignorée).

Même règle que curated_sources.py : jamais appliqué au démarrage (``init_databases()`` ne
l'appelle pas), on l'invoque à la main. Une année vérifiée pendant la rédaction est datée dans
``VERIFIED_ON`` ; à revérifier si une page change.
"""

from __future__ import annotations

from typing import Any

from db import ACTORS_DB, backup_all_databases, connect, utc_now

DECLARED_LABEL = "Déclaration publique de l'acteur (année d'origine, pas un registre)"
VERIFIED_ON = "2026-10-07"

CURATED_FOUNDING_YEARS: dict[str, dict[str, Any]] = {
    "Clark-MXR": {
        "founded_year": 1987,
        "source_url": "https://cmxr.com/laser-products/",
        "quote": "Our story began in 1987, when Clark introduced the world’s first commercial femtosecond laser system",
    },
    "Femtika": {
        "founded_year": 2013,
        "source_url": "https://femtika.com/about-us/",
        "quote": "Our journey began in 2013, when a team of brilliant scientists and engineers came together",
    },
    "Fraunhofer ILT": {
        "founded_year": 1984,
        # Chronique officielle de Fraunhofer, rubrique 1984 : "Foundation Fraunhofer Institute".
        "source_url": "https://www.fraunhofer.de/en/about-fraunhofer/profile-structure/chronicles/fraunhofer-chronicle/1983-1989.html",
        "quote": "the Fraunhofer Institute for Laser Technology ILT is founded in Aachen",
    },
    "Oxford Lasers": {
        "founded_year": 1977,
        "source_url": "https://oxfordlasers.com/",
        "quote": "Founded in 1977 as a spin-out of Oxford University",
    },
    "LLT Applikation": {
        "founded_year": 1997,
        "source_url": "https://llt-applikation.de/en/",
        "quote": "wurde am 27. Februar 1997 von Dr. Siegfried Pause gegründet",
    },
    "Laser Micromachining Ltd": {
        "founded_year": 2005,
        "source_url": "https://www.lasermicromachining.com/industrial-applications-sectors/",
        "quote": "LML has been providing manufacturing services to a very diverse range of applications sectors since 2005",
    },
    "LouwersHanique": {
        # Date de la FUSION de Louwers Glastechniek (1961) et Pulles & Hanique (1950), pas de leurs
        # fondations : c'est l'entité suivie qui naît en 2012.
        "founded_year": 2012,
        "source_url": "https://www.louwershanique.com/about-us/history",
        "quote": "LouwersHanique was formed by the merger of two specialized companies back in 2012",
    },
    "Pulsar Photonics": {
        "founded_year": 2013,
        "source_url": "https://www.pulsar-photonics.de/en/about-us/",
        "quote": "Our statistics Establishment date 2013",
    },
    "KMLT": {
        "founded_year": 1991,
        "source_url": "https://kmlt.de/en/",
        "quote": "Since 1991 KMLT ® has been your reliable partner for industrial contract manufacturing",
    },
    "Laser Zentrum Hannover": {
        "founded_year": 1986,
        "source_url": "https://www.lzh.de/en/about-us/profile",
        "quote": "The LZH was founded in 1986 to conduct interdisciplinary research and development in laser technology",
    },
    "Workshop of Photonics": {
        # La frise de la page dissocie années et événements dans le HTML : l'année (2007) est
        # portée par l'étiquette qui suit l'événement, lue par position. Les deux étiquettes
        # voisines connues la valident (1996 = ALTECHNA, 2003 = "Our journey started in 2003").
        "founded_year": 2007,
        "source_url": "https://wophotonics.com/about-us/",
        "quote": "Workshop of Photonics was founded – laser micromachining business part spun off from Altechna",
    },
    "FEMTOprint": {
        "founded_year": 2013,
        "source_url": "https://www.femtoprint.ch/media/femtoprint-sa-and-the-nmi-natural-and-medical-sciences-institute-join-forces-within-the-framework-of-the-eurostars/",
        "quote": "Founded in 2013 in Muzzano (Switzerland), FEMTOPRINT SA is a high-tech Contract Development and Manufacturing Organization",
    },
}


def apply_curated_founding_years(entries: dict[str, dict[str, Any]] | None = None) -> dict:
    """Écrit founded_year + provenance pour chaque acteur listé. Ignore un acteur inconnu ou
    déjà doté d'une année (registre ou saisie précédente) : un registre prime toujours."""
    entries = CURATED_FOUNDING_YEARS if entries is None else entries
    stamp = utc_now()
    written: list[str] = []
    skipped: dict[str, str] = {}
    with connect(ACTORS_DB) as db:
        for name, entry in entries.items():
            actor = db.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone()
            if not actor:
                skipped[name] = "acteur absent"
                continue
            row = db.execute("SELECT founded_year FROM actor_profile WHERE actor_id=?", (actor["id"],)).fetchone()
            if row and row["founded_year"] is not None:
                skipped[name] = "année déjà renseignée"
                continue
            if row:
                db.execute(
                    "UPDATE actor_profile SET founded_year=?,registry_name=?,source_url=?,as_of_date=?,updated_at=? WHERE actor_id=?",
                    (entry["founded_year"], DECLARED_LABEL, entry["source_url"], VERIFIED_ON, stamp, actor["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO actor_profile(actor_id,founded_year,registry_name,source_url,as_of_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (actor["id"], entry["founded_year"], DECLARED_LABEL, entry["source_url"], VERIFIED_ON, stamp, stamp),
                )
            written.append(name)
    return {"written": written, "skipped": skipped}


if __name__ == "__main__":
    backups = backup_all_databases()
    print(f"Sauvegarde préalable : {len(backups)} fichier(s)")
    print(apply_curated_founding_years())
