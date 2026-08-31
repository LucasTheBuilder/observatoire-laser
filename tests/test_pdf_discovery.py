"""Tests pour la découverte des PDF (Lot 2 §2.1, audit veille §8.1/§9.3, 30/08/2026) :
le parseur PDF fonctionne déjà (27 PDF parsés en production) mais captait quasiment jamais un
vrai datasheet -- ceux qui passaient étaient des catalogues/rapports annuels/CGV trouvés par
accident. La correction porte sur la DÉCOUVERTE (discovery terms + .pdf intrinsèquement digne
d'intérêt) et le TYPAGE (classify_source), pas sur l'extraction.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hybrid import SECTION_SCORES, classify_source, parse_document


class ClassifySourceDatasheetTests(unittest.TestCase):
    def test_pdf_with_datasheet_term_in_label_is_classified_as_datasheet(self):
        page_type, score = classify_source(
            "https://example.test/wp-content/uploads/2024/xyz-product.pdf", "Download datasheet (PDF)",
        )
        self.assertEqual("datasheet", page_type)
        self.assertEqual(SECTION_SCORES["datasheet"], score)

    def test_pdf_with_fiche_technique_in_url_path_is_classified_as_datasheet(self):
        page_type, _ = classify_source("https://example.test/fiche-technique-laser-2024.pdf")
        self.assertEqual("datasheet", page_type)

    def test_pdf_with_brochure_term_is_classified_as_datasheet(self):
        page_type, _ = classify_source("https://example.test/downloads/brochure-2024.pdf", "Company brochure")
        self.assertEqual("datasheet", page_type)

    def test_pdf_without_any_datasheet_term_falls_through_to_normal_rules(self):
        # §9.3: the PDFs actually captured in production were a company catalog, annual
        # reports, terms and conditions, a job posting -- none of these should be forced into
        # 'datasheet' just because the URL ends in .pdf.
        page_type, _ = classify_source("https://example.test/downloads/terms-and-conditions.pdf", "Terms and Conditions")
        self.assertNotEqual("datasheet", page_type)

    def test_pdf_matching_careers_is_typed_careers_not_datasheet(self):
        # Lot 2 §2.3 (implemented after this test was first written) made 'careers' its own
        # page_type rather than 'ignore' -- the datasheet rule must not steal a careers PDF
        # away from that classification just because it also ends in .pdf.
        page_type, _ = classify_source("https://example.test/careers/job-opening.pdf", "Open position PDF")
        self.assertEqual("careers", page_type)

    def test_non_pdf_page_with_datasheet_wording_is_not_forced_to_datasheet(self):
        # The rule is deliberately gated on the .pdf suffix (§8.1: "le suffixe .pdf croisé au
        # libellé du lien") -- an ordinary HTML "specifications" page keeps its existing
        # classification rather than being redefined by this change.
        page_type, _ = classify_source("https://example.test/specifications/", "Specifications")
        self.assertNotEqual("datasheet", page_type)


class MeaningfulLinksPdfTests(unittest.TestCase):
    def test_pdf_link_with_no_discovery_term_is_kept(self):
        html = """<html><head><title>Products</title></head><body><main>
          <p><a href="/files/xyz.pdf">More info</a></p>
        </main></body></html>"""
        document = parse_document(html, "https://example.test/products/")
        urls = [link["url"] for link in document.links]
        self.assertIn("https://example.test/files/xyz.pdf", urls)

    def test_non_pdf_link_with_no_discovery_term_is_still_dropped(self):
        html = """<html><head><title>Products</title></head><body><main>
          <p><a href="/files/random-page">More info</a></p>
        </main></body></html>"""
        document = parse_document(html, "https://example.test/products/")
        urls = [link["url"] for link in document.links]
        self.assertNotIn("https://example.test/files/random-page", urls)

    def test_pdf_link_reason_is_recorded_as_pdf(self):
        html = """<html><head><title>Products</title></head><body><main>
          <p><a href="/files/xyz.pdf">More info</a></p>
        </main></body></html>"""
        document = parse_document(html, "https://example.test/products/")
        link = next(link for link in document.links if link["url"].endswith("xyz.pdf"))
        self.assertEqual("pdf", link["reason"])


if __name__ == "__main__":
    unittest.main()
