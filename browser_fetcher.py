from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class RenderedPage:
    requested_url: str
    final_url: str
    status: int | None
    title: str
    html: str
    text_length: int
    link_count: int
    elapsed_ms: int
    rendered_by: str
    warnings: tuple[str, ...] = ()


class BrowserFetcher:
    """Client du renderer Playwright 6A.

    Ce client n'est volontairement pas branché automatiquement au crawler.
    Le fallback automatique sera introduit seulement en phase 6B.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = str(base_url or os.getenv("BROWSER_RENDERER_URL", "http://browser:8780")).rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=min(self.timeout, 5.0)) as client:
            response = client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    def fetch_rendered_page(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        extra_wait_ms: int = 500,
        scroll_once: bool = True,
    ) -> RenderedPage:
        payload = {
            "url": url,
            "wait_until": wait_until,
            "extra_wait_ms": extra_wait_ms,
            "scroll_once": scroll_once,
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/render", json=payload)
            response.raise_for_status()
            data = response.json()
        return RenderedPage(
            requested_url=data["requested_url"],
            final_url=data["final_url"],
            status=data.get("status"),
            title=data.get("title", ""),
            html=data.get("html", ""),
            text_length=int(data.get("text_length", 0)),
            link_count=int(data.get("link_count", 0)),
            elapsed_ms=int(data.get("elapsed_ms", 0)),
            rendered_by=data.get("rendered_by", "playwright-chromium"),
            warnings=tuple(data.get("warnings") or ()),
        )


def fetch_rendered_page(url: str, **kwargs: Any) -> RenderedPage:
    return BrowserFetcher().fetch_rendered_page(url, **kwargs)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Test manuel du renderer Playwright de l'Observatoire Laser")
    parser.add_argument("url")
    parser.add_argument("--renderer", default=os.getenv("BROWSER_RENDERER_URL", "http://localhost:8780"))
    args = parser.parse_args()
    fetcher = BrowserFetcher(args.renderer)
    print("Health:", fetcher.health())
    page = fetcher.fetch_rendered_page(args.url)
    print(f"HTTP       : {page.status}")
    print(f"Final URL  : {page.final_url}")
    print(f"Titre      : {page.title}")
    print(f"Texte      : {page.text_length} caractères")
    print(f"Liens      : {page.link_count}")
    print(f"Rendu      : {page.elapsed_ms} ms")
    if page.warnings:
        print("Warnings   :", "; ".join(page.warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
