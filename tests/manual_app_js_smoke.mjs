// Not part of the Python test suite (pytest ignores non-Python files) -- a manual smoke test
// for the front-end code added by the séries temporelles (audit Horizon 2 #12) and scores
// séparés / signaux structurés (Horizon 2 #13-14) chantiers in static/app.js, run with plain
// Node (`node tests/manual_app_js_smoke.mjs`). Stubs just enough of the DOM/fetch surface for
// app.js's top-level code to execute without throwing, then calls the new pure rendering
// functions directly with sample payloads shaped like the real backend output (timeseries.py,
// scoring.py, press.py), to catch runtime errors that `node --check` (syntax only) cannot.
import fs from "node:fs";
import vm from "node:vm";

function fakeElement() {
  const el = {
    dataset: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false}},
    addEventListener(){}, appendChild(){}, querySelector(){return null}, querySelectorAll(){return []},
    style: {}, children: [], open: false, showModal(){this.open = true;}, close(){this.open = false;},
  };
  Object.defineProperty(el, "innerHTML", {get(){return this._html || "";}, set(v){this._html = v;}});
  return el;
}

const fakeDocument = {
  querySelector: () => fakeElement(),
  querySelectorAll: () => [],
  createElement: () => fakeElement(),
};

const sandbox = {
  document: fakeDocument,
  window: {setTimeout, addEventListener(){}},
  fetch: async () => ({ok: true, json: async () => ({})}),
  URLSearchParams: URLSearchParams,
  Intl,
  Date,
  Math,
  console,
  JSON,
  Number,
  String,
  Object,
  Array,
  encodeURIComponent,
  setTimeout,
  clearTimeout,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const code = fs.readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
// refresh() at the bottom awaits Promise.all(fetch stubs) which resolve fine with our stub.
vm.runInContext(code, sandbox, {filename: "app.js"});

function assert(cond, msg) {
  if (!cond) throw new Error("FAIL: " + msg);
  console.log("ok:", msg);
}

// --- periodLabel ---
assert(sandbox.periodLabel("2026-08").includes("26"), "periodLabel renders a short year");

// --- svgSeriesChart: 1 point (dots only, no path) ---
const onePoint = [{period: "2026-08", evidence_existing: 3, evidence_radar: 1}];
const series = [
  {key: "evidence_existing", label: "Existants", color: "#048f83"},
  {key: "evidence_radar", label: "Radar", color: "#d85f3d"},
];
const svg1 = sandbox.svgSeriesChart(onePoint, series);
assert(svg1.includes("<svg"), "svgSeriesChart (1 point) returns an <svg>");
assert(!svg1.includes("<path"), "svgSeriesChart (1 point) has no connecting path");
assert(svg1.includes("<circle"), "svgSeriesChart (1 point) still draws dots");

// --- svgSeriesChart: 3 points (path present) ---
const threePoints = [
  {period: "2026-06", evidence_existing: 1, evidence_radar: 0},
  {period: "2026-07", evidence_existing: 2, evidence_radar: 1},
  {period: "2026-08", evidence_existing: 3, evidence_radar: 1},
];
const svg3 = sandbox.svgSeriesChart(threePoints, series);
assert(svg3.includes("<path"), "svgSeriesChart (3 points) draws a connecting path");

// --- svgStackedBarChart: stage distribution ---
const maturityPoints = [{period: "2026-08", stage_distribution: {Production: 4, "R&D": 2}}];
const order = ["R&D", "Prototype", "Pré-industrialisation", "Industrialisation", "Production", "Maturité industrielle non déterminée"];
const colors = {"R&D": "#8b79c9", "Production": "#d85f3d"};
const bars = sandbox.svgStackedBarChart(maturityPoints, "stage_distribution", order, colors);
assert(bars.includes("<svg"), "svgStackedBarChart returns an <svg>");
assert((bars.match(/<rect/g) || []).length === 2, "svgStackedBarChart draws one segment per present stage (2)");

// --- tsSparseNote ---
assert(sandbox.tsSparseNote([{}]).includes("Historique en construction"), "tsSparseNote warns below 2 points");
assert(sandbox.tsSparseNote([{}, {}]) === "", "tsSparseNote is silent at 2+ points");

// --- renderTrendsBody for each dimension, using shapes exactly matching timeseries.py's output ---
const samples = {
  actor: [{period: "2026-08", competitive_class: "C1", evidence_existing: 2, evidence_radar: 1, offers_count: 5, sources_active: 12}],
  market: [{period: "2026-08", total: 3, existing: 1, radar: 2, actors_count: 2, stage_distribution: {Production: 1, "R&D": 1}}],
  technology: [{period: "2026-08", signals_existing: 1, signals_radar: 2}],
  maturity: [{period: "2026-08", stage_distribution: {Production: 4}, bucket_distribution: {existing: 3, radar: 1}, total: 4}],
  signal: [{period: "2026-08", new_evidence: 7, new_offers: 3, new_documents: 2, new_technology_signals: 1}],
};
// `const state = {...}` at app.js's top level lives in the vm context's shared top-level
// lexical environment, not as a `sandbox.state` own-property (a vm/realm quirk: only `var`
// and function declarations attach to the global object) -- but that lexical environment IS
// shared across separate runInContext() calls in the same context, so a second small script
// can still reach and mutate it directly.
for (const [dimension, points] of Object.entries(samples)) {
  const key = dimension === "technology" ? "SLE" : "__global__";
  vm.runInContext(`state.trends.dimension=${JSON.stringify(dimension)}; state.trends.key=${JSON.stringify(key)};`, sandbox);
  const html = sandbox.renderTrendsBody(points);
  assert(typeof html === "string" && html.length > 0, `renderTrendsBody(${dimension}) returns non-empty HTML`);
}
// technology '__global__' branch (documents_by_type stacked bar)
vm.runInContext(`state.trends.dimension="technology"; state.trends.key="__global__";`, sandbox);
const techGlobal = sandbox.renderTrendsBody([{period: "2026-08", documents_total: 5, documents_by_type: {publication: 3, patent: 2}}]);
assert(techGlobal.includes("<svg"), "renderTrendsBody(technology,__global__) renders the documents stacked bar");

// --- full renderTrends() + async setTrendsDimension()/setTrendsKey() wiring, against a
// fetch stub that mimics /api/timeseries/keys and /api/timeseries realistically ---
sandbox.fetch = async (path) => {
  const url = new URL(path, "http://local");
  if (url.pathname === "/api/timeseries/keys") {
    const dimension = url.searchParams.get("dimension");
    const byDim = {actor: ["ACunity", "HAILTEC"], market: ["Medtech", "__global__"], technology: ["SLE", "__global__"], maturity: ["__global__", "Medtech"], signal: ["__global__"]};
    return {ok: true, json: async () => ({dimension, keys: byDim[dimension] || []})};
  }
  if (url.pathname === "/api/timeseries") {
    const dimension = url.searchParams.get("dimension"), key = url.searchParams.get("key");
    return {ok: true, json: async () => ({dimension, key, points: [{period: "2026-08", evidence_existing: 1, evidence_radar: 0, offers_count: 2, sources_active: 3, competitive_class: "C1", total: 1, existing: 1, radar: 0, actors_count: 1, stage_distribution: {Production: 1}, bucket_distribution: {existing: 1}, signals_existing: 1, signals_radar: 0, documents_total: 2, documents_by_type: {publication: 2}, new_evidence: 1, new_offers: 1, new_documents: 1, new_technology_signals: 0}]})};
  }
  return {ok: true, json: async () => ({})};
};

vm.runInContext(`state.trends = {dimension: "actor", key: "", keys: [], points: [], loading: false};`, sandbox);
await sandbox.setTrendsDimension("actor");
assert(vm.runInContext("content.innerHTML", sandbox).includes("<svg"), "renderTrends() after setTrendsDimension('actor') renders a chart, not just the empty state");
assert(vm.runInContext("content.innerHTML", sandbox).includes("ACunity"), "renderTrends() key selector lists the fetched keys");

await sandbox.setTrendsKey("HAILTEC");
assert(vm.runInContext("content.innerHTML", sandbox).includes("HAILTEC"), "setTrendsKey() switches the selected key and re-renders");

for (const dimension of ["actor", "market", "technology", "maturity", "signal"]) {
  await sandbox.setTrendsDimension(dimension);
  assert(!vm.runInContext("content.innerHTML", sandbox).includes("undefined"), `renderTrends() for dimension=${dimension} contains no stray "undefined"`);
}

// --- scores séparés (audit Horizon 2 #14) : confidenceBadge/threatBadge/attractivenessBadge/
// eventTypeBadge -- pure functions, exercised directly against realistic payload shapes.
assert(sandbox.confidenceBadge({confidence_score: null}) === "", "confidenceBadge is empty when no data exists yet (not a fabricated 0)");
assert(sandbox.confidenceBadge({confidence_score: 87.3}).includes("87 confiance"), "confidenceBadge renders a rounded score");
assert(sandbox.threatBadge({is_reference: 1, threat_score: 90}) === "", "threatBadge is always empty for a reference actor, even with a high raw score");
assert(sandbox.threatBadge({is_reference: 0, threat_score: 62}).includes("62 menace"), "threatBadge renders a rounded score for a non-reference actor");

vm.runInContext(`state.marketScores = [{market: "Medtech", attractiveness_score: 71.2, existing: 4, radar: 1, actors_count: 3, production_share: 0.5}];`, sandbox);
const badge = sandbox.attractivenessBadge("Medtech");
assert(badge.includes("71 attractivité"), "attractivenessBadge looks up the right market and rounds the score");
assert(sandbox.attractivenessBadge("Unknown Market") === "", "attractivenessBadge is empty for a market with no computed score");

assert(sandbox.eventTypeBadge("patent").includes("Brevet"), "eventTypeBadge labels a patent signal");
assert(sandbox.eventTypeBadge("press_mention") === "", "eventTypeBadge stays silent for a plain press mention (not a structured signal)");
const eventHtml = sandbox.eventLine({event_type: "investment", event_date: "2026-08-24", description: "Investissement (Laser Focus World) : raises funding", source_url: "https://x"});
assert(eventHtml.includes("Investissement") && eventHtml.includes("2026-08-24"), "eventLine combines the structured-type badge with the existing date/description/link");

// --- full card/fiche rendering, badges included ---
const sampleActor = {
  id: 1, name: "ACunity", country: "Allemagne", role: "Test", active: 1, priority: 1,
  competitive_class: "C2", actor_type: "societe_technologique_specialisee", is_reference: 0,
  confidence_score: 88.5, threat_score: 42.1, completeness_score: 0.6, completeness_present: 4,
  completeness_total: 7, completeness_missing: ["Publications / brevets / projets"],
  business_models: [], events: [{event_type: "patent", event_date: "2026-08-20", description: "Brevet (X) : test", source_url: "https://x"}],
  facts: [], value_chain_stages: [], official_url: "https://acunity.de",
};
const cardHtml = sandbox.actorCard(sampleActor);
assert(cardHtml.includes("89 confiance") || cardHtml.includes("88 confiance"), "actorCard renders the confidence badge");
assert(cardHtml.includes("42 menace"), "actorCard renders the threat badge");

vm.runInContext(`state.offers = []; state.market = {existing: [], radar: []};`, sandbox);
const ficheHtml = sandbox.actorDetailContent(sampleActor);
assert(ficheHtml.includes("Scores séparés"), "actorDetailContent renders the new Scores séparés block");
assert(ficheHtml.includes("Brevet"), "actorDetailContent's event list shows the structured-signal badge");

const referenceActor = {...sampleActor, is_reference: 1, threat_score: 90};
const referenceFiche = sandbox.actorDetailContent(referenceActor);
assert(!referenceFiche.includes("Menace concurrentielle"), "actorDetailContent hides the threat line for a reference actor");

console.log("\nAll app.js timeseries smoke checks passed.");
