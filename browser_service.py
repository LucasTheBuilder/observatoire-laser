from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import Browser, Playwright, async_playwright


DEFAULT_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "18000"))
NAVIGATION_TIMEOUT_MS = int(os.getenv("BROWSER_NAVIGATION_TIMEOUT_MS", "15000"))
MAX_HTML_CHARS = int(os.getenv("BROWSER_MAX_HTML_CHARS", "3000000"))
MAX_CONCURRENCY = max(1, int(os.getenv("BROWSER_MAX_CONCURRENCY", "2")))
USER_AGENT = os.getenv(
    "BROWSER_USER_AGENT",
    "ObservatoireLaser/3.3.2-6A (+Playwright renderer)",
)
ALLOWED_DOMAINS = tuple(
    item.strip().lower().removeprefix("www.")
    for item in os.getenv("BROWSER_ALLOWED_DOMAINS", "").split(",")
    if item.strip()
)


class RenderRequest(BaseModel):
    url: str
    wait_until: str = Field(default="domcontentloaded", pattern="^(commit|domcontentloaded|load|networkidle)$")
    extra_wait_ms: int = Field(default=500, ge=0, le=5000)
    scroll_once: bool = True


class RenderResponse(BaseModel):
    requested_url: str
    final_url: str
    status: int | None
    title: str
    html: str
    text_length: int
    link_count: int
    elapsed_ms: int
    rendered_by: str = "playwright-chromium"
    warnings: list[str]


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Seules les URL http(s) absolues sont autorisées.")
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    if ALLOWED_DOMAINS and not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_DOMAINS):
        raise ValueError(f"Domaine non autorisé par BROWSER_ALLOWED_DOMAINS : {host}")
    return url


class BrowserRenderer:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def start(self) -> None:
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )

    async def stop(self) -> None:
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def render(self, request: RenderRequest) -> RenderResponse:
        _validate_url(request.url)
        if not self.browser:
            raise RuntimeError("Chromium n'est pas initialisé.")

        started = time.monotonic()
        warnings: list[str] = []
        async with self.semaphore:
            context = await self.browser.new_context(
                user_agent=USER_AGENT,
                java_script_enabled=True,
                ignore_https_errors=False,
                viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            response = None
            try:
                response = await page.goto(request.url, wait_until=request.wait_until)
                if request.extra_wait_ms:
                    await page.wait_for_timeout(request.extra_wait_ms)
                if request.scroll_once:
                    await page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight, 2500))")
                    await page.wait_for_timeout(min(request.extra_wait_ms, 500))
                    await page.evaluate("window.scrollTo(0, 0)")

                title = await page.title()
                html = await page.content()
                if len(html) > MAX_HTML_CHARS:
                    warnings.append(f"HTML tronqué à {MAX_HTML_CHARS} caractères")
                    html = html[:MAX_HTML_CHARS]
                text_length = await page.locator("body").inner_text(timeout=DEFAULT_TIMEOUT_MS)
                link_count = await page.locator("a[href]").count()
                return RenderResponse(
                    requested_url=request.url,
                    final_url=page.url,
                    status=response.status if response else None,
                    title=title.strip(),
                    html=html,
                    text_length=len(text_length.strip()),
                    link_count=link_count,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    warnings=warnings,
                )
            finally:
                await context.close()


renderer = BrowserRenderer()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await renderer.start()
    try:
        yield
    finally:
        await renderer.stop()


app = FastAPI(title="Observatoire Laser Browser Renderer", version="6A", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if renderer.browser else "starting",
        "renderer": "playwright-chromium",
        "max_concurrency": MAX_CONCURRENCY,
        "allowed_domains": list(ALLOWED_DOMAINS),
    }


@app.post("/render", response_model=RenderResponse)
async def render_page(request: RenderRequest) -> RenderResponse:
    try:
        return await renderer.render(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Échec du rendu Playwright : {str(exc)[:500]}") from exc
