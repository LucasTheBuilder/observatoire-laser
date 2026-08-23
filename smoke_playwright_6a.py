from __future__ import annotations

import argparse
import json
import os
import time

import httpx

from browser_fetcher import BrowserFetcher
from hybrid import parse_document
from site_profiles import get_site_profile

DEFAULT_URLS = [
    "https://www.alphanov.com/en/application-sectors/lasers",
    "https://www.alphanov.com/en/products-and-services/laser-machining-and-micro-machining",
    "https://www.alphanov.com/produits-et-services/procedes-laser",
]


def httpx_snapshot(url: str) -> dict:
    started = time.monotonic()
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "ObservatoireLaser/3.3.2-6A"},
            timeout=httpx.Timeout(18.0, connect=8.0),
            follow_redirects=True,
        )
        response.raise_for_status()
        profile = get_site_profile(url=url)
        doc = parse_document(response.text, str(response.url), profile=profile)
        return {
            "ok": True,
            "status": response.status_code,
            "html_chars": len(response.text),
            "blocks": len(doc.blocks),
            "links": len(doc.links),
            "title": doc.title,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def playwright_snapshot(fetcher: BrowserFetcher, url: str) -> dict:
    try:
        rendered = fetcher.fetch_rendered_page(url)
        profile = get_site_profile(url=url)
        doc = parse_document(rendered.html, rendered.final_url, profile=profile)
        return {
            "ok": True,
            "status": rendered.status,
            "html_chars": len(rendered.html),
            "text_chars": rendered.text_length,
            "browser_links": rendered.link_count,
            "blocks": len(doc.blocks),
            "links": len(doc.links),
            "title": doc.title,
            "elapsed_ms": rendered.elapsed_ms,
            "extraction_method": doc.extraction_method,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS)
    parser.add_argument("--renderer", default=os.getenv("BROWSER_RENDERER_URL", "http://localhost:8780"))
    args = parser.parse_args()
    fetcher = BrowserFetcher(args.renderer, timeout=35)
    print("Renderer:", fetcher.health())
    results = []
    for url in args.urls:
        result = {
            "url": url,
            "httpx": httpx_snapshot(url),
            "playwright": playwright_snapshot(fetcher, url),
        }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\nRésumé 6A :")
    for item in results:
        h = item["httpx"]
        p = item["playwright"]
        print(
            f"- {item['url']}\n"
            f"  httpx      : blocks={h.get('blocks', 'ERR')} links={h.get('links', 'ERR')} html={h.get('html_chars', 'ERR')}\n"
            f"  playwright : blocks={p.get('blocks', 'ERR')} links={p.get('links', 'ERR')} html={p.get('html_chars', 'ERR')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
