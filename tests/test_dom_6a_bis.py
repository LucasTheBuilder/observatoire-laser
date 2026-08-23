from hybrid import diagnose_document, parse_document
from site_profiles import get_site_profile


def test_heading_segmentation_replaces_coarse_single_container():
    html = """
    <html><head><title>Laser processes</title></head><body><main>
      <div class='item'>
        <h1>Laser processes</h1>
        <h2>Micromachining</h2><p>Femtosecond laser micromachining enables precise cutting and drilling for industrial components.</p>
        <h2>Surface engineering</h2><p>Ultrashort pulse laser surface texturing and functionalisation are available for industrial studies.</p>
        <h2>Transparent materials</h2><p>Femtosecond laser processing of glass supports selective modification and microfabrication applications.</p>
        <h2>Industrial services</h2><p>Process development, feasibility studies and manufacturing support are offered to customers.</p>
      </div>
    </main></body></html>
    """
    profile = get_site_profile(url="https://www.alphanov.com/en/products-and-services/laser-processes")
    doc = parse_document(html, "https://www.alphanov.com/en/products-and-services/laser-processes", profile=profile)
    assert doc.extraction_method == "heading-segments"
    assert len(doc.blocks) == 4
    assert [b.heading for b in doc.blocks] == ["Micromachining", "Surface engineering", "Transparent materials", "Industrial services"]


def test_no_segmentation_when_semantic_extraction_is_already_granular():
    html = """
    <html><body><main>
      <section><h2>A</h2><p>Femtosecond laser processing content sufficiently long for a useful independent industrial section.</p></section>
      <section><h2>B</h2><p>Femtosecond laser processing content sufficiently long for a useful independent industrial section.</p></section>
      <section><h2>C</h2><p>Femtosecond laser processing content sufficiently long for a useful independent industrial section.</p></section>
      <section><h2>D</h2><p>Femtosecond laser processing content sufficiently long for a useful independent industrial section.</p></section>
    </main></body></html>
    """
    profile = get_site_profile(url="https://example.com/applications")
    doc = parse_document(html, "https://example.com/applications", profile=profile)
    assert doc.extraction_method == "semantic"
    assert len(doc.blocks) == 4


def test_diagnostics_explain_parent_rejection_and_final_method():
    html = """
    <html><body><main><div class='item'>
      <h2>One</h2><p>Long enough industrial femtosecond laser content for deterministic extraction and diagnostics.</p>
      <h2>Two</h2><p>Long enough industrial femtosecond laser content for deterministic extraction and diagnostics.</p>
      <h2>Three</h2><p>Long enough industrial femtosecond laser content for deterministic extraction and diagnostics.</p>
      <h2>Four</h2><p>Long enough industrial femtosecond laser content for deterministic extraction and diagnostics.</p>
    </div></main></body></html>
    """
    profile = get_site_profile(url="https://www.alphanov.com/en/products-and-services/x")
    diag = diagnose_document(html, "https://www.alphanov.com/en/products-and-services/x", profile=profile)
    assert diag["raw_dom"]["h2"] == 4
    assert diag["heading_segments"] == 4
    assert diag["final_blocks"] == 4
    assert diag["extraction_method"] == "heading-segments"
