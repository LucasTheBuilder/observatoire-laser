"""Copie datée des bases de production (data/*.db) hors du disque du projet.

Lancé chaque jour par une tâche planifiée Windows ; par défaut la destination est
%OneDrive%\\observatoire-backups, synchronisée hors de la machine. Usage :

    py backup_databases.py [--dest DOSSIER] [--keep-days N]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from db import DATA_DIR

DEFAULT_KEEP_DAYS = 30


def default_destination() -> Path:
    onedrive = os.environ.get("OneDrive")
    if not onedrive:
        raise SystemExit("OneDrive introuvable : passez --dest explicitement.")
    return Path(onedrive) / "observatoire-backups"


def backup_all(source_dir: Path, dest_root: Path, today: date) -> list[Path]:
    target_dir = dest_root / today.isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for source in sorted(source_dir.glob("*.db")):
        target = target_dir / source.name
        # API de sauvegarde plutôt que copie de fichier : les bases sont en WAL, une copie brute
        # du .db pourrait manquer les écritures encore dans le -wal.
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            status = dst.execute("PRAGMA quick_check").fetchone()[0]
        if status != "ok":
            raise RuntimeError(f"{target} : quick_check a renvoyé {status!r}")
        written.append(target)
    return written


def prune(dest_root: Path, today: date, keep_days: int) -> list[Path]:
    cutoff = today - timedelta(days=keep_days)
    removed = []
    for child in dest_root.iterdir():
        try:
            day = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if child.is_dir() and day < cutoff:
            shutil.rmtree(child)
            removed.append(child)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=None)
    parser.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    args = parser.parse_args(argv)

    dest_root = args.dest or default_destination()
    today = date.today()
    written = backup_all(DATA_DIR, dest_root, today)
    removed = prune(dest_root, today, args.keep_days)
    for path in written:
        print(f"sauvegardé : {path} ({path.stat().st_size // 1024} Ko)")
    for path in removed:
        print(f"supprimé (> {args.keep_days} j) : {path}")
    if not written:
        print(f"Aucune base trouvée dans {DATA_DIR}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
