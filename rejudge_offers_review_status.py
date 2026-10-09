"""Rejuge les offres déjà `accepted` avec la règle structurelle actuelle (voir
scrapers._offer_review_reasons, §10.7 audit veille, 30/08/2026).

Contexte : cette règle a été introduite le 30/08/2026 au soir, juste après la dernière collecte
marché complète (30/08 13:40 UTC). Une offre écrite avant ce commit n'a jamais été rejugée --
son review_status='accepted' vient de l'ancienne règle ("accepted" d'office à l'insertion), pas
de la règle actuelle. Ce script applique la règle d'AUJOURD'HUI à ce qui est déjà en base, sans
attendre qu'une future collecte retouche chaque fact_key un par un.

Réutilise exactement scrapers._offer_review_reasons(), jamais une règle réinventée pour
l'occasion -- même principe que prune_off_topic_sources.py. Les offres déjà tranchées par un
humain (reviewed_at non NULL) sont hors d'atteinte, même garde que les sites d'upsert de
scrapers.py et que prune_technology_signals().

Script de maintenance ponctuel : à relancer si _offer_review_reasons évolue et que d'anciennes
lignes `accepted` doivent être réauditées sans attendre une recollecte complète.

Sauvegarde les 3 bases avant toute écriture (voir db.backup_all_databases()).
"""

from __future__ import annotations

from db import MARKET_DB, backup_all_databases, connect
from scrapers import _offer_review_reasons


def rejudge_accepted_offers() -> dict:
    with connect(MARKET_DB) as db:
        offers = db.execute(
            "SELECT id,actor_name,operation,page_type,quote,source_url "
            "FROM offers WHERE review_status='accepted' AND reviewed_at IS NULL"
        ).fetchall()

        checked = 0
        routed_to_review: list[dict] = []
        reason_tally: dict[str, int] = {}

        for row in offers:
            checked += 1
            source_count = db.execute(
                "SELECT COUNT(DISTINCT source_url) FROM offer_sources WHERE offer_id=?", (row["id"],)
            ).fetchone()[0]
            candidate = {
                "quote": row["quote"] or "",
                "page_type": row["page_type"],
                "url": row["source_url"] or "",
                "operation": row["operation"],
            }
            reasons = _offer_review_reasons(candidate, max(source_count, 1))
            if not reasons:
                continue
            for reason in reasons:
                reason_tally[reason] = reason_tally.get(reason, 0) + 1
            routed_to_review.append({"id": row["id"], "actor_name": row["actor_name"], "reasons": reasons})

        for item in routed_to_review:
            db.execute(
                "UPDATE offers SET review_status='review' WHERE id=? AND reviewed_at IS NULL",
                (item["id"],),
            )

    return {
        "checked": checked,
        "routed_to_review": len(routed_to_review),
        "reason_tally": reason_tally,
        "by_actor": routed_to_review,
    }


if __name__ == "__main__":
    backups = backup_all_databases()
    print(f"Sauvegarde préalable : {len(backups)} fichier(s)")
    for path in backups:
        print(f"  - {path}")

    report = rejudge_accepted_offers()
    print(
        f"Offres réexaminées : {report['checked']}, "
        f"routées vers la revue : {report['routed_to_review']}"
    )
    print("Répartition des motifs :", report["reason_tally"])
