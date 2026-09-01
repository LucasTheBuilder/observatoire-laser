"""Taille de marché (§4.B.2 audit veille, 30/08/2026, Lot 4 §14) : "les chiffres de marché laser
publiés mélangent allègrement machines, services et composants. Sans cette table, la dimension
'marché' restera un synonyme de 'application'."

Pas de collecteur : aucune source ouverte fiable ne publie une taille de marché laser
micro-usinage ultra-rapide directement exploitable par API -- ce sont des rapports d'analystes et
des communiqués, à lire et saisir par un humain. Ce module n'est donc qu'un CRUD sourcé, jamais
une moyenne entre sources (le §4.B.2 est explicite : "jamais moyennée entre sources, toujours
affichée avec son périmètre") et jamais une valeur inventée -- ``source_url`` est obligatoire,
il n'existe aucun chemin d'écriture sans lui.
"""

from __future__ import annotations

from db import MARKET_DB, connect, utc_now


def list_market_sizing(market: str | None = None) -> list[dict]:
    with connect(MARKET_DB) as db:
        if market:
            rows = db.execute(
                "SELECT * FROM market_sizing WHERE market=? ORDER BY market,year DESC", (market,)
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM market_sizing ORDER BY market,year DESC").fetchall()
        return [dict(row) for row in rows]


def add_market_sizing(
    *, market: str, scope: str, value: float, currency: str, year: int,
    source_url: str, cagr: float | None = None, method: str | None = None, added_by: str | None = None,
) -> int:
    """Ajoute une entrée de taille de marché. Chaque champ vient d'une source réelle que
    l'appelant a lue lui-même -- rien ici n'est calculé ni deviné. Raises ValueError si un champ
    obligatoire est vide ou si source_url n'est pas une URL http(s) absolue."""
    market = market.strip()
    scope = scope.strip()
    currency = currency.strip()
    source_url = source_url.strip()
    if not market:
        raise ValueError("market is required")
    if not scope:
        raise ValueError("scope is required -- le périmètre exact (machines/services/composants...)")
    if not currency:
        raise ValueError("currency is required")
    if not source_url.startswith(("http://", "https://")):
        raise ValueError("source_url must be an absolute http(s) URL")
    with connect(MARKET_DB) as db:
        row_id = db.execute(
            """INSERT INTO market_sizing(market,scope,value,currency,year,cagr,method,source_url,added_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (market, scope, value, currency, year, cagr, method, source_url, added_by, utc_now()),
        ).lastrowid
    assert row_id is not None
    return row_id


def delete_market_sizing(entry_id: int) -> None:
    with connect(MARKET_DB) as db:
        deleted = db.execute("DELETE FROM market_sizing WHERE id=?", (entry_id,)).rowcount
    if not deleted:
        raise ValueError(f"market_sizing entry {entry_id} not found")
