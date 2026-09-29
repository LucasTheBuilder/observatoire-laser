"""Rejuge le review_status de toutes les offres non tranchées par un humain avec la règle
ACTUELLE (scrapers._offer_review_reasons), dans les deux sens : accepted -> review ET
review -> accepted.

Pourquoi : review_status n'est recalculé qu'à la ré-observation d'une offre par
scrape_market(), qui ne ré-analyse une page que si son content_hash a changé. Une évolution
de la règle ne touche donc jamais les offres déjà en base -- mesuré le 29/09/2026 : 19 offres
ne déclenchaient plus aucun motif mais restaient en `review`, et l'assouplissement "page
d'offre du domaine officiel" (_is_first_party_offer_page) n'aurait rien débloqué sans ce script.

Réutilise exactement scrapers._offer_review_reasons(), jamais une règle réinventée pour
l'occasion -- même principe que prune_off_topic_sources.py. Les offres déjà tranchées par un
humain (reviewed_at non NULL) sont hors d'atteinte, même garde que _upsert_offer_candidate.

Sauvegarde les 3 bases avant toute écriture (voir db.backup_all_databases()).
Usage : py rejudge_offers.py [--dry-run]
"""

from __future__ import annotations

import sys
from collections import Counter

from db import ACTORS_DB, MARKET_DB, backup_all_databases, connect
from scrapers import _offer_review_reasons


def rejudge_offers(*, dry_run: bool = False) -> dict:
    with connect(ACTORS_DB) as db:
        official_urls = {row["name"]: row["official_url"] for row in db.execute("SELECT name,official_url FROM actors")}

    with connect(MARKET_DB) as db:
        offers = db.execute(
            "SELECT id,actor_name,operation,laser_process,page_type,quote,source_url,review_status "
            "FROM offers WHERE reviewed_at IS NULL"
        ).fetchall()
        source_counts = dict(db.execute(
            "SELECT offer_id,COUNT(DISTINCT source_url) FROM offer_sources GROUP BY offer_id"
        ).fetchall())

        transitions: Counter[str] = Counter()
        still_blocking: Counter[str] = Counter()
        changes: list[tuple[str, int]] = []
        for row in offers:
            candidate = {
                "quote": row["quote"] or "",
                "page_type": row["page_type"],
                "url": row["source_url"] or "",
                "operation": row["operation"],
                "process": row["laser_process"],
                "official_url": official_urls.get(row["actor_name"]),
            }
            reasons = _offer_review_reasons(candidate, max(int(source_counts.get(row["id"], 0)), 1))
            still_blocking.update(reasons)
            status = "review" if reasons else "accepted"
            if status != row["review_status"]:
                transitions[f"{row['review_status']}->{status}"] += 1
                changes.append((status, int(row["id"])))

        if not dry_run:
            db.executemany(
                "UPDATE offers SET review_status=? WHERE id=? AND reviewed_at IS NULL", changes
            )

    return {
        "checked": len(offers),
        "transitions": dict(transitions),
        "still_blocking": dict(still_blocking.most_common()),
    }


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if not dry_run:
        backups = backup_all_databases()
        print(f"Sauvegarde préalable : {len(backups)} fichier(s)")
    report = rejudge_offers(dry_run=dry_run)
    print(f"{'[simulation] ' if dry_run else ''}Offres réexaminées : {report['checked']}")
    print("Transitions :", report["transitions"])
    print("Motifs restants :", report["still_blocking"])
