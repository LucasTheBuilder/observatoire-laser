import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from backup_databases import backup_all, prune


class BackupDatabasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "data"
        self.dest = self.root / "backups"
        self.source.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_copies_rows_still_in_the_wal(self) -> None:
        writer = sqlite3.connect(self.source / "actors.db")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE actors (name TEXT)")
        writer.execute("INSERT INTO actors VALUES ('ALPHANOV')")
        writer.commit()

        written = backup_all(self.source, self.dest, date(2026, 9, 29))
        writer.close()

        self.assertEqual([self.dest / "2026-09-29" / "actors.db"], written)
        with closing(sqlite3.connect(written[0])) as copy:
            self.assertEqual([("ALPHANOV",)], copy.execute("SELECT name FROM actors").fetchall())

    def test_prune_removes_only_dated_folders_older_than_the_window(self) -> None:
        for name in ("2026-08-01", "2026-09-20", "notes"):
            (self.dest / name).mkdir(parents=True)

        removed = prune(self.dest, date(2026, 9, 29), keep_days=30)

        self.assertEqual([self.dest / "2026-08-01"], removed)
        self.assertEqual({"2026-09-20", "notes"}, {p.name for p in self.dest.iterdir()})


if __name__ == "__main__":
    unittest.main()
