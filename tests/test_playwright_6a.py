from __future__ import annotations

import pathlib
import sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browser_fetcher import BrowserFetcher
from browser_service import _validate_url


def test_url_validation_accepts_http_https():
    assert _validate_url("https://www.alphanov.com/en/application-sectors/lasers").startswith("https://")
    assert _validate_url("http://example.com/").startswith("http://")


def test_url_validation_rejects_non_http():
    with pytest.raises(ValueError):
        _validate_url("file:///etc/passwd")


def test_browser_fetcher_decodes_renderer_response(monkeypatch):
    payload = {
        "requested_url": "https://example.com",
        "final_url": "https://example.com/",
        "status": 200,
        "title": "Example",
        "html": "<html><body>Hello</body></html>",
        "text_length": 5,
        "link_count": 1,
        "elapsed_ms": 123,
        "rendered_by": "playwright-chromium",
        "warnings": [],
    }

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, *args, **kwargs):
            assert kwargs["json"]["url"] == "https://example.com"
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    page = BrowserFetcher("http://browser:8780").fetch_rendered_page("https://example.com")
    assert page.status == 200
    assert page.title == "Example"
    assert page.text_length == 5
    assert page.rendered_by == "playwright-chromium"
