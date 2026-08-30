"""Tests pour le chantier 1 de l'audit collecte : découverte via sitemap.xml (au lieu de
compter uniquement sur les liens trouvés en explorant le site) et ingestion des PDF
(brochures/datasheets/rapports annuels), qui étaient auparavant des sources totalement
ignorées par le crawler (voir scrapers._discover_sitemap_urls, hybrid.parse_pdf_document)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scrapers
from hybrid import is_pdf_response, parse_pdf_document
from site_profiles import DEFAULT_SITE_PROFILE


def _build_minimal_pdf(text: str) -> bytes:
    """Assemble un PDF valide minimal (un objet Catalog/Pages/Page/Font/Content-stream, avec
    une table xref correcte) contenant `text` comme unique ligne de texte -- juste assez pour
    que pdfplumber puisse le relire, sans dépendre d'une librairie d'écriture de PDF tierce."""
    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]

    def add_object(n: int, body: bytes) -> None:
        offsets.append(len(buf))
        buf.extend(f"{n} 0 obj\n".encode("latin-1"))
        buf.extend(body)
        buf.extend(b"\nendobj\n")

    add_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add_object(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add_object(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    add_object(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    stream_content = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode("latin-1")
    stream_body = (
        ("<< /Length %d >>\nstream\n" % len(stream_content)).encode("latin-1")
        + stream_content
        + b"\nendstream"
    )
    add_object(5, stream_body)

    xref_offset = len(buf)
    buf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        buf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    buf.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\n")
    buf.extend(f"startxref\n{xref_offset}\n%%EOF".encode("latin-1"))
    return bytes(buf)


class PdfIngestionTests(unittest.TestCase):
    def test_is_pdf_response_detects_content_type_and_extension(self):
        self.assertTrue(is_pdf_response("application/pdf", "https://example.test/doc"))
        self.assertTrue(is_pdf_response("application/pdf; charset=binary", "https://example.test/doc"))
        self.assertTrue(is_pdf_response("", "https://example.test/brochure.PDF"))
        self.assertFalse(is_pdf_response("text/html", "https://example.test/page"))

    def test_parse_pdf_document_extracts_a_content_block_from_real_text(self):
        text = "Femtosecond laser micromachining supports medical stent production at industrial scale."
        pdf_bytes = _build_minimal_pdf(text)
        document = parse_pdf_document(pdf_bytes, "https://example.test/downloads/stent-datasheet.pdf")

        self.assertEqual(document.extraction_method, "pdf-text")
        self.assertEqual(document.links, [])
        self.assertEqual(document.h1, "")
        self.assertEqual(len(document.blocks), 1)
        self.assertIn(text, document.blocks[0].text)
        # No PDF metadata Title was set, so the fallback is the decoded file name.
        self.assertEqual(document.title, "stent-datasheet.pdf")

    def test_parse_pdf_document_drops_paragraphs_under_the_noise_floor(self):
        # A single short line ("Page 1") is below the 55-char floor shared with the HTML
        # pipeline's _content_block, so it must not become a spurious ContentBlock.
        pdf_bytes = _build_minimal_pdf("Page 1")
        document = parse_pdf_document(pdf_bytes, "https://example.test/downloads/empty.pdf")
        self.assertEqual(document.blocks, [])

    def test_parse_pdf_document_respects_configured_max_pages(self):
        text = "Femtosecond laser micromachining supports medical stent production at industrial scale."
        pdf_bytes = _build_minimal_pdf(text)
        profile = {**DEFAULT_SITE_PROFILE, "pdf": {"max_pages": 0}}
        document = parse_pdf_document(pdf_bytes, "https://example.test/downloads/stent-datasheet.pdf", profile=profile)
        self.assertEqual(document.blocks, [])


SITEMAP_INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.test/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://example.test/sitemap-news.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGES_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/applications/medical/</loc></url>
  <url><loc>https://example.test/services/</loc></url>
  <url><loc>https://another-domain.test/off-site/</loc></url>
</urlset>"""

SITEMAP_NEWS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/news/laser-award/</loc></url>
</urlset>"""


class SitemapParsingTests(unittest.TestCase):
    def test_parse_sitemap_xml_reads_urlset(self):
        locs, children = scrapers._parse_sitemap_xml(SITEMAP_PAGES_XML)
        self.assertEqual(children, [])
        self.assertIn("https://example.test/applications/medical/", locs)
        self.assertIn("https://another-domain.test/off-site/", locs)

    def test_parse_sitemap_xml_reads_sitemapindex(self):
        locs, children = scrapers._parse_sitemap_xml(SITEMAP_INDEX_XML)
        self.assertEqual(locs, [])
        self.assertEqual(
            children,
            ["https://example.test/sitemap-pages.xml", "https://example.test/sitemap-news.xml"],
        )

    def test_parse_sitemap_xml_decompresses_gzip(self):
        import gzip

        compressed = gzip.compress(SITEMAP_PAGES_XML)
        locs, children = scrapers._parse_sitemap_xml(compressed)
        self.assertIn("https://example.test/services/", locs)
        self.assertEqual(children, [])

    def test_parse_sitemap_xml_tolerates_garbage(self):
        self.assertEqual(scrapers._parse_sitemap_xml(b"not xml at all"), ([], []))


class SitemapDiscoveryTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, content: bytes, status_code: int = 200):
            self.content = content
            self.text = content.decode("utf-8", "ignore")
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class FakeClient:
        # robots.txt declares the sitemap index, which fans out to two leaf sitemaps -- the
        # nested-index case that a plain "GET /sitemap.xml" fallback would miss.
        pages = {
            "https://example.test/robots.txt": b"User-agent: *\nAllow: /\nSitemap: https://example.test/sitemap-index.xml\n",
            "https://example.test/sitemap-index.xml": SITEMAP_INDEX_XML,
            "https://example.test/sitemap-pages.xml": SITEMAP_PAGES_XML,
            "https://example.test/sitemap-news.xml": SITEMAP_NEWS_XML,
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str, timeout: float | None = None, headers=None):
            if url not in self.pages:
                return SitemapDiscoveryTests.FakeResponse(b"", status_code=404)
            return SitemapDiscoveryTests.FakeResponse(self.pages[url])

    def test_discover_sitemap_urls_follows_index_and_filters_off_domain(self):
        scrapers._robots_cache.clear()
        with self.FakeClient() as client:
            urls = scrapers._discover_sitemap_urls(client, "https://example.test/", DEFAULT_SITE_PROFILE)
        self.assertIn("https://example.test/applications/medical/", urls)
        self.assertIn("https://example.test/services/", urls)
        self.assertIn("https://example.test/news/laser-award/", urls)
        # The off-domain URL from the sitemap must never be seeded as a crawl source.
        self.assertNotIn("https://another-domain.test/off-site/", urls)

    def test_discover_sitemap_urls_disabled_returns_nothing(self):
        scrapers._robots_cache.clear()
        profile = {**DEFAULT_SITE_PROFILE, "sitemap": {"enabled": False}}
        with self.FakeClient() as client:
            urls = scrapers._discover_sitemap_urls(client, "https://example.test/", profile)
        self.assertEqual(urls, [])

    def test_discover_sitemap_urls_respects_max_sitemaps_cap(self):
        scrapers._robots_cache.clear()
        profile = {**DEFAULT_SITE_PROFILE, "sitemap": {"enabled": True, "max_sitemaps": 1, "max_urls": 800}}
        with self.FakeClient() as client:
            urls = scrapers._discover_sitemap_urls(client, "https://example.test/", profile)
        # Only the index itself is fetched (1 sitemap budget); no leaf pages are reached.
        self.assertEqual(urls, [])

    def test_discover_sitemap_urls_survives_unreachable_sitemap(self):
        scrapers._robots_cache.clear()

        class BrokenClient(self.FakeClient):
            pages = {"https://example.test/robots.txt": b"User-agent: *\nAllow: /\n"}

        with BrokenClient() as client:
            urls = scrapers._discover_sitemap_urls(client, "https://example.test/", DEFAULT_SITE_PROFILE)
        self.assertEqual(urls, [])


if __name__ == "__main__":
    unittest.main()
