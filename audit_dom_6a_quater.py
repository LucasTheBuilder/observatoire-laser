from __future__ import annotations

import argparse
import json
import os
import time

import httpx

from browser_fetcher import BrowserFetcher
from hybrid import diagnose_document
from site_profiles import get_site_profile

DEFAULT_URLS = [
    "https://www.alphanov.com/en/application-sectors/lasers",
    "https://www.alphanov.com/en/products-and-services/laser-machining-and-micro-machining",
    "https://www.alphanov.com/produits-et-services/procedes-laser",
]


def get_httpx(url: str) -> tuple[str, str, int]:
    started = time.monotonic()
    r = httpx.get(url, headers={"User-Agent": "ObservatoireLaser/3.3.2-6A-quater"},
                  timeout=httpx.Timeout(18.0, connect=8.0), follow_redirects=True)
    r.raise_for_status()
    return r.text, str(r.url), int((time.monotonic() - started) * 1000)


def summarize(label: str, diag: dict, elapsed_ms: int) -> None:
    dom = diag["raw_dom"]
    print(f"\n[{label}] {diag['url']}")
    print(f"  html={diag['html_chars']} chars · elapsed={elapsed_ms} ms · root={diag['root_selector']}")
    print(f"  DOM: H1={dom['h1']} H2={dom['h2']} H3={dom['h3']} H4={dom['h4']} sections={dom['section']} articles={dom['article']} anchors={dom['anchors']}")
    print(f"  candidats={diag['candidate_selector_matches']} utilisables={diag['candidate_usable']} feuilles={diag['candidate_leaf_selected']}")
    print(f"  rejetés courts={diag['candidate_rejected_short_or_empty']} parents={diag['candidate_rejected_parent_has_usable_descendant']}")
    print(f"  blocs sémantiques={diag['semantic_blocks']} segments titres={diag['heading_segments']} unités éditoriales={diag.get('editorial_units',0)} parents sauvés={diag.get('rescued_parent_units',0)}")
    print(f"  bruit retiré={diag.get('removed_noise_zones',0)} zones · oversized={diag.get('oversized_blocks',0)}")
    print(f"  couverture texte={diag.get('text_coverage',0):.1%} · utile={diag.get('useful_coverage',0):.1%} · bruit={diag.get('noise_ratio',0):.1%}")
    print(f"  QUALITÉ={diag.get('quality_score',0):.1f}/100 · FINAL={diag['final_blocks']} ({diag['extraction_method']})")
    print(f"  liens métier={diag['meaningful_links']}")
    for i, block in enumerate(diag["blocks"][:20], 1):
        hierarchy = " > ".join(x for x in (block['h1'], block['h2'], block['h3']) if x)
        print(f"    {i:02d}. {block['chars']:4d}c · {hierarchy or block['heading']} · {block['heading']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit DOM / granularité 6A-quater")
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS)
    parser.add_argument("--renderer", default=os.getenv("BROWSER_RENDERER_URL", "http://browser:8780"))
    parser.add_argument("--json", action="store_true", help="Afficher le diagnostic JSON complet")
    args = parser.parse_args()
    fetcher = BrowserFetcher(args.renderer, timeout=35)
    print("Renderer:", fetcher.health())
    for url in args.urls:
        profile = get_site_profile(url=url)
        html, final_url, h_ms = get_httpx(url)
        h_diag = diagnose_document(html, final_url, profile=profile)
        rendered = fetcher.fetch_rendered_page(url)
        p_diag = diagnose_document(rendered.html, rendered.final_url, profile=profile)
        summarize("HTTPX", h_diag, h_ms)
        summarize("PLAYWRIGHT", p_diag, rendered.elapsed_ms)
        if args.json:
            print(json.dumps({"url": url, "httpx": h_diag, "playwright": p_diag}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
