"""Sites faussement "degraded" (29/09/2026) : deux causes mesurées en direct, aucune n'était
du JavaScript.

1. _remove_noise_zones supprimait <html> quand le thème y pose une classe d'état cookie/consent
   (Enfold : photonicfab.de, 3d-micromac.com), et tous les composants Adobe AEM `cmp-*`
   (coherent.com) -- 0 bloc extrait, render_required=1 à tort.
2. Un site dont toutes les pages revisitées répondent 304 comptait "0 document" -> anomalie.
"""

from __future__ import annotations

import unittest

from hybrid import parse_document
from scrapers import adaptive_decision

BODY = """<main><h1>Ultrafast laser services</h1>
<section><h2>Micromachining</h2><p>We provide femtosecond laser micromachining of glass, ceramics
and metals for medical and electronics customers, from prototypes to series production.</p></section>
<section><h2>Drilling</h2><p>Our ultrashort pulse lasers drill micro holes down to 10 micrometres in
thin foils and technical ceramics with no heat affected zone and no burr.</p></section></main>"""


class NoiseZoneTests(unittest.TestCase):
    def test_consent_state_class_on_html_does_not_wipe_the_page(self):
        html = f"""<html class="responsive av-cookies-consent-show-message-bar av-cookies-needs-opt-in">
<body>{BODY}<div class="avia-cookie-consent-wrap"><p>Diese Website verwendet Cookies. Datenschutz
Einstellungen akzeptieren oder ablehnen, weitere Informationen finden Sie hier.</p></div></body></html>"""
        doc = parse_document(html, "https://photonicfab.test/")
        text = " ".join(block.text for block in doc.blocks)
        self.assertIn("femtosecond laser micromachining", text)
        self.assertNotIn("Cookies", text)
        self.assertFalse(doc.render_required)

    def test_consent_class_on_body_does_not_wipe_the_page(self):
        html = f"""<html><body class="cookie-consent-pending">{BODY}</body></html>"""
        doc = parse_document(html, "https://example.test/")
        self.assertIn("micro holes", " ".join(block.text for block in doc.blocks))

    def test_aem_cmp_components_are_content_not_consent_banners(self):
        html = f"""<html><body><div class="cmp-container"><div class="cmp-text">{BODY}</div></div>
<div id="cmpbox" class="cmpbox"><p>We use cookies. Accept all or manage your consent preferences
for this website and our partners here.</p></div></body></html>"""
        doc = parse_document(html, "https://coherent.test/industrial")
        text = " ".join(block.text for block in doc.blocks)
        self.assertIn("femtosecond laser micromachining", text)
        self.assertNotIn("consent preferences", text)


class UnchangedPagesTests(unittest.TestCase):
    def test_all_pages_unchanged_is_not_an_anomaly(self):
        self.assertEqual(("generic", False), adaptive_decision(False, 0, 0, 0, 6, unchanged_count=6))

    def test_nothing_fetched_and_nothing_unchanged_is_still_an_anomaly(self):
        self.assertEqual(("adaptive", True), adaptive_decision(False, 0, 0, 0, 6, unchanged_count=0))

    def test_fresh_pages_with_zero_blocks_and_no_unchanged_page_is_still_an_anomaly(self):
        self.assertEqual(("adaptive", True), adaptive_decision(False, 3, 0, 0, 3))


if __name__ == "__main__":
    unittest.main()
