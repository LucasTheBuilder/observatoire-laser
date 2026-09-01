"""Matrice de référence marché x composant x opération (§4.B.1 audit veille, 30/08/2026, Lot 4
§13) : "aujourd'hui l'app ne peut afficher que ce qu'elle a trouvé ; elle ne peut pas afficher ce
que personne ne fait. Déclarer la matrice cible permet de calculer un taux de couverture par
case et de faire ressortir les zones blanches -- c'est le livrable qui intéressera le business
development."

Une cellule de la matrice de référence est une AMBITION déclarée par un humain -- "le marché X a
un vrai besoin pour le composant Y en opération Z" -- indépendante de ce que evidence a
effectivement observé. Ne jamais générer une cellule à partir des faits déjà en base : ça
transformerait chaque zone blanche existante en case couverte par construction, rendant la
fonctionnalité inutile (voir docstring de la table dans db.py).

Couverture : une cellule est "couverte" si au moins un fait evidence validé (bucket
existing/radar) partage exactement son triplet marché/composant/opération. Zone blanche = cellule
de référence sans aucun fait correspondant.
"""

from __future__ import annotations

from db import MARKET_DB, connect, utc_now


def list_reference_matrix() -> list[dict]:
    """Chaque cellule de référence, avec son statut de couverture calculé contre evidence."""
    with connect(MARKET_DB) as db:
        cells = [dict(row) for row in db.execute(
            "SELECT * FROM market_reference_matrix ORDER BY market,component,operation"
        ).fetchall()]
        observed = {
            (row["market"], row["component"], row["operation"]): row["bucket"]
            for row in db.execute(
                """SELECT DISTINCT market,component,operation,bucket FROM evidence
                   WHERE fact_status='validated' AND bucket IN ('existing','radar')"""
            ).fetchall()
        }
    for cell in cells:
        key = (cell["market"], cell["component"], cell["operation"])
        cell["covered"] = key in observed
        cell["covered_bucket"] = observed.get(key)
    return cells


def coverage_summary() -> dict:
    cells = list_reference_matrix()
    covered = sum(1 for cell in cells if cell["covered"])
    return {
        "total_cells": len(cells),
        "covered_cells": covered,
        "white_space_cells": len(cells) - covered,
        "coverage_rate": round(covered / len(cells), 4) if cells else None,
        "white_space": [cell for cell in cells if not cell["covered"]],
    }


def add_reference_cell(*, market: str, component: str, operation: str, rationale: str | None = None, added_by: str | None = None) -> int:
    market, component, operation = market.strip(), component.strip(), operation.strip()
    if not market or not component or not operation:
        raise ValueError("market, component and operation are all required")
    with connect(MARKET_DB) as db:
        existing = db.execute(
            "SELECT id FROM market_reference_matrix WHERE market=? AND component=? AND operation=?",
            (market, component, operation),
        ).fetchone()
        if existing:
            raise ValueError("This market/component/operation cell is already declared")
        row_id = db.execute(
            """INSERT INTO market_reference_matrix(market,component,operation,rationale,added_by,created_at)
               VALUES(?,?,?,?,?,?)""",
            (market, component, operation, rationale, added_by, utc_now()),
        ).lastrowid
    assert row_id is not None
    return row_id


def delete_reference_cell(cell_id: int) -> None:
    with connect(MARKET_DB) as db:
        deleted = db.execute("DELETE FROM market_reference_matrix WHERE id=?", (cell_id,)).rowcount
    if not deleted:
        raise ValueError(f"Reference cell {cell_id} not found")
