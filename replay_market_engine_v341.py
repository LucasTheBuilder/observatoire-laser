from __future__ import annotations

import json
from collections import Counter

from db import ACTORS_DB, connect
from hybrid import ContentBlock
from scrapers import _candidate, _dedupe_candidates, _structured_neighbors


def _block(item: dict) -> ContentBlock:
    return ContentBlock(
        heading=item.get("heading", ""), text=item.get("text", ""), path=item.get("path", ""),
        media_context=item.get("media_context", ""), h1=item.get("h1", ""),
        h2=item.get("h2", ""), h3=item.get("h3", ""),
    )


def _load_blocks(raw: object, diagnostics: Counter[str]) -> list[ContentBlock]:
    """Parse one stored payload without letting a malformed source abort the replay."""
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        diagnostics["sources_invalid_json"] += 1
        return []

    if not isinstance(payload, list):
        diagnostics["sources_invalid_payload"] += 1
        return []

    blocks: list[ContentBlock] = []
    for item in payload:
        if not isinstance(item, dict):
            diagnostics["invalid_block_items"] += 1
            continue
        blocks.append(_block(item))
    return blocks


def main() -> None:
    with connect(ACTORS_DB) as db:
        sources = [dict(row) for row in db.execute(
            """SELECT a.name,s.url,s.last_title,s.blocks_json
               FROM actor_sources s JOIN actors a ON a.id=s.actor_id
               WHERE s.blocks_json IS NOT NULL AND TRIM(s.blocks_json)!=''"""
        )]

    diagnostics: Counter[str] = Counter()
    raw_candidates: list[dict] = []
    for source in sources:
        blocks = _load_blocks(source["blocks_json"], diagnostics)
        if not blocks:
            diagnostics["sources_without_usable_blocks"] += 1
            continue
        diagnostics["sources_parsed"] += 1
        for index, block in enumerate(blocks):
            local: dict[str, int] = {}
            candidate = _candidate(
                source["name"], source["url"], source.get("last_title") or "", block,
                structured_blocks=_structured_neighbors(blocks, index), diagnostics=local,
            )
            diagnostics.update(local)
            if candidate:
                raw_candidates.append(candidate)

    candidates = _dedupe_candidates(raw_candidates)
    print(f"Sources disponibles   : {len(sources)}")
    print(f"Sources rejouées      : {diagnostics['sources_parsed']}")
    print(f"Sources invalides     : {diagnostics['sources_invalid_json'] + diagnostics['sources_invalid_payload']}")
    print(f"Blocs invalides       : {diagnostics['invalid_block_items']}")
    print(f"Blocs examinés        : {diagnostics['blocks_examined']}")
    print(f"Blocs laser           : {diagnostics['laser_blocks']}")
    print(f"Candidats bruts       : {len(raw_candidates)}")
    print(f"Faits uniques         : {len(candidates)}")
    print(f"Multi-context rejetés : {diagnostics['multi_context_rejected']}")
    print("\nDiagnostics :")
    for key, value in diagnostics.most_common():
        print(f"  {key:26s} {value}")
    print("\nFaits uniques :")
    for item in candidates:
        print(
            f"- {item['actor']} | {item['bucket']} | {item['market']} | {item['component']} | "
            f"{item['operation']} | {item['relation_strength']} | {item['maturity']}"
        )
        print(f"  {item['url']}")


if __name__ == "__main__":
    main()
