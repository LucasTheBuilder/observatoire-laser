"""Tests pour le diff sémantique de page_versions -> page_changes (Lot 1 §1.6, audit veille
§5.E.1/§8.3, 30/08/2026) : pour la majorité du corpus (pages service/application/product, sans
date de publication exploitable), ce diff est la seule date fiable que le crawler puisse
produire -- transforme "quand je l'ai vu" en "quand ils l'ont écrit".
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from hybrid import ContentBlock, block_payload
from scrapers import _diff_page_blocks


def _blocks_json(*blocks: ContentBlock) -> str:
    return json.dumps([block_payload(b) for b in blocks], ensure_ascii=False)


class DiffPageBlocksTests(unittest.TestCase):
    def test_both_empty_produces_no_changes(self):
        self.assertEqual([], _diff_page_blocks(None, None))
        self.assertEqual([], _diff_page_blocks("[]", "[]"))

    def test_new_block_is_reported_as_added(self):
        old = _blocks_json(ContentBlock(heading="Intro", text="Company overview.", path="main > p"))
        new = _blocks_json(
            ContentBlock(heading="Intro", text="Company overview.", path="main > p"),
            ContentBlock(heading="Battery", text="Femtosecond laser structuring of battery electrodes.", path="main > section"),
        )
        changes = _diff_page_blocks(old, new)
        added = [c for c in changes if c["change_type"] == "block_added"]
        self.assertEqual(1, len(added))
        self.assertIn("battery electrodes", added[0]["detail"].lower())

    def test_removed_block_is_reported_as_removed(self):
        old = _blocks_json(
            ContentBlock(heading="Intro", text="Company overview.", path="main > p"),
            ContentBlock(heading="Battery", text="Femtosecond laser structuring of battery electrodes.", path="main > section"),
        )
        new = _blocks_json(ContentBlock(heading="Intro", text="Company overview.", path="main > p"))
        changes = _diff_page_blocks(old, new)
        removed = [c for c in changes if c["change_type"] == "block_removed"]
        self.assertEqual(1, len(removed))
        self.assertIn("battery electrodes", removed[0]["detail"].lower())

    def test_newly_appeared_lexicon_term_is_reported(self):
        old = _blocks_json(ContentBlock(heading="Intro", text="Company overview of our activities.", path="main > p"))
        new = _blocks_json(ContentBlock(
            heading="Intro",
            text="Femtosecond laser dicing is used for semiconductor wafers in advanced packaging.",
            path="main > p",
        ))
        changes = _diff_page_blocks(old, new)
        terms = {c["term"] for c in changes if c["change_type"] == "lexicon_term_appeared"}
        self.assertIn("Wafers", terms)
        self.assertIn("Dicing", terms)

    def test_unchanged_content_produces_no_lexicon_or_block_events(self):
        text = "Femtosecond laser dicing is used for semiconductor wafers in advanced packaging."
        old = _blocks_json(ContentBlock(heading="Intro", text=text, path="main > p"))
        new = _blocks_json(ContentBlock(heading="Intro", text=text, path="main > p"))
        self.assertEqual([], _diff_page_blocks(old, new))

    def test_numeric_spec_change_in_a_persisting_block_is_reported(self):
        old = _blocks_json(ContentBlock(
            heading="Performance", text="We drill 300 holes per second on titanium.", path="main > section.spec",
        ))
        new = _blocks_json(ContentBlock(
            heading="Performance", text="We drill 500 holes per second on titanium.", path="main > section.spec",
        ))
        changes = _diff_page_blocks(old, new)
        spec_changes = [c for c in changes if c["change_type"] == "numeric_spec_changed"]
        self.assertEqual(1, len(spec_changes))
        self.assertIn("300 holes", spec_changes[0]["old_value"])
        self.assertIn("500 holes", spec_changes[0]["new_value"])

    def test_a_spec_appearing_only_in_a_new_block_is_not_double_reported_as_a_change(self):
        # Covered by block_added instead -- numeric_spec_changed is only for a block that
        # persisted across both versions (same path) with different text.
        old = _blocks_json(ContentBlock(heading="Intro", text="Company overview.", path="main > p"))
        new = _blocks_json(
            ContentBlock(heading="Intro", text="Company overview.", path="main > p"),
            ContentBlock(heading="Performance", text="We drill 300 holes per second on titanium.", path="main > section.spec"),
        )
        changes = _diff_page_blocks(old, new)
        self.assertEqual([], [c for c in changes if c["change_type"] == "numeric_spec_changed"])
        self.assertEqual(1, len([c for c in changes if c["change_type"] == "block_added"]))

    def test_malformed_json_degrades_to_empty_rather_than_raising(self):
        self.assertEqual([], _diff_page_blocks("not json", "also not json"))


class PageChangesSchemaTests(unittest.TestCase):
    def test_page_changes_table_exists_and_accepts_a_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors_db = Path(tmp) / "actors.db"
            with (
                patch.object(dbmod, "ACTORS_DB", actors_db),
                patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
                patch.object(dbmod, "TECH_DB", Path(tmp) / "technology.db"),
            ):
                dbmod.init_databases()
                actor_id = dbmod.create_actor("Example", "France", "Test", "https://example.test/", priority=False)
            with dbmod.connect(actors_db) as db:
                source_id = db.execute(
                    "INSERT INTO actor_sources(actor_id,url,active) VALUES(?,?,1)", (actor_id, "https://example.test/services"),
                ).lastrowid
                db.execute(
                    """INSERT INTO page_changes(source_id,change_type,term,detail,old_value,new_value,detected_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (source_id, "lexicon_term_appeared", "Dicing", None, None, None, dbmod.utc_now()),
                )
                row = db.execute("SELECT change_type,term FROM page_changes WHERE source_id=?", (source_id,)).fetchone()
            self.assertEqual("lexicon_term_appeared", row["change_type"])
            self.assertEqual("Dicing", row["term"])


if __name__ == "__main__":
    unittest.main()
