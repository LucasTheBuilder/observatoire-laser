from hybrid import diagnose_document, parse_document
from site_profiles import get_site_profile


def test_alphanov_like_cards_are_split_into_editorial_units():
    html = '''
    <html><body><main>
      <section class="laser-processes">
        <div class="cards">
          <div class="card"><a href="/surface-texturing"><span class="card-title">Surface texturing</span><p>Femtosecond laser texturing creates functional surfaces with controlled topography.</p></a></div>
          <div class="card"><a href="/cutting"><span class="card-title">Cutting</span><p>Precision cutting and micro-cutting of thin industrial components with ultrashort pulses.</p></a></div>
          <div class="card"><a href="/drilling"><span class="card-title">Drilling</span><p>High aspect ratio drilling and micro-drilling for demanding industrial parts.</p></a></div>
          <div class="card"><a href="/ablation"><span class="card-title">Selective ablation</span><p>Selective ablation removes a surface layer without damaging the underlying substrate.</p></a></div>
        </div>
        <div class="item"><h2>Brochures</h2><p>Download technical brochures for laser processes and services.</p></div>
      </section>
    </main></body></html>
    '''
    profile = get_site_profile(url="https://www.alphanov.com/produits-et-services/procedes-laser")
    doc = parse_document(html, "https://www.alphanov.com/produits-et-services/procedes-laser", profile=profile)
    headings = {b.heading for b in doc.blocks}
    assert "Surface texturing" in headings
    assert "Cutting" in headings
    assert "Drilling" in headings
    assert "Selective ablation" in headings
    assert len(doc.blocks) >= 4


def test_alphanov_like_pseudo_headings_are_split():
    html = '''
    <html><body><main><section>
      <h1>Laser machining and micro-machining</h1>
      <div class="process-copy">
        <p><strong>Laser turning</strong></p>
        <p>Femtosecond laser turning enables precision machining of cylindrical micro-parts.</p>
        <p><strong>Cutting and micro-cutting</strong></p>
        <p>Ultrashort pulse cutting produces narrow kerfs and low thermal impact on industrial materials.</p>
        <p><strong>Drilling and micro-drilling</strong></p>
        <p>Micro-drilling creates precise holes with controlled geometry and high aspect ratio.</p>
        <p><strong>Controlled engraving</strong></p>
        <p>Controlled engraving enables deterministic material removal and fine surface features.</p>
        <p><strong>Selective ablation</strong></p>
        <p>Selective ablation removes coatings while preserving the substrate underneath.</p>
      </div>
    </section></main></body></html>
    '''
    profile = get_site_profile(url="https://www.alphanov.com/en/products-and-services/laser-machining-and-micro-machining")
    doc = parse_document(html, "https://www.alphanov.com/en/products-and-services/laser-machining-and-micro-machining", profile=profile)
    headings = {b.heading for b in doc.blocks}
    assert "Laser turning" in headings
    assert "Cutting and micro-cutting" in headings
    assert "Drilling and micro-drilling" in headings
    assert "Controlled engraving" in headings
    assert "Selective ablation" in headings


def test_large_publications_block_is_refined_when_entries_are_repeated():
    html = '''
    <html><body><main>
      <section><h2>Applications</h2><p>Femtosecond laser applications and services for industrial partners.</p></section>
      <section class="publications"><h2>Publications</h2><ul>
        <li><a href="/p1"><strong>Mass production of laser nanostructures</strong></a><p>Study of high-throughput femtosecond surface processing.</p></li>
        <li><a href="/p2"><strong>LIPSS for functional surfaces</strong></a><p>Experimental work on laser-induced periodic surface structures.</p></li>
        <li><a href="/p3"><strong>Femtosecond surface texturing</strong></a><p>Influence of beam intensity profiles on texturing quality.</p></li>
        <li><a href="/p4"><strong>Ultrafast laser ablation</strong></a><p>Selective material removal with ultrashort pulses.</p></li>
      </ul></section>
    </main></body></html>
    '''
    profile = get_site_profile(url="https://www.alphanov.com/en/application-sectors/lasers")
    doc = parse_document(html, "https://www.alphanov.com/en/application-sectors/lasers", profile=profile)
    assert not any(b.heading == "Publications" and len(b.text) > 900 for b in doc.blocks)
    assert any("LIPSS" in b.heading for b in doc.blocks)
    assert any("Femtosecond surface texturing" in b.heading for b in doc.blocks)


def test_diagnostics_expose_editorial_units_and_coverage():
    html = '''
    <html><body><main><section><div class="cards">
      <div class="card"><a href="/a"><span class="title">Surface texturing</span><p>Long useful femtosecond laser texturing description for an industrial process.</p></a></div>
      <div class="card"><a href="/b"><span class="title">Micro-drilling</span><p>Long useful femtosecond laser drilling description for industrial parts.</p></a></div>
      <div class="card"><a href="/c"><span class="title">Selective ablation</span><p>Long useful femtosecond laser ablation description for coated substrates.</p></a></div>
    </div></section></main></body></html>
    '''
    profile = get_site_profile(url="https://www.alphanov.com/produits-et-services/procedes-laser")
    diag = diagnose_document(html, "https://www.alphanov.com/produits-et-services/procedes-laser", profile=profile)
    assert diag["editorial_units"] >= 3
    assert 0 <= diag["coverage_ratio"] <= 1
    assert diag["final_blocks"] >= 3
