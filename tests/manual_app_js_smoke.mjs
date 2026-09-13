// Not part of the Python test suite (pytest ignores non-Python files) -- a manual smoke test
// for the scores séparés / signaux structurés (audit Horizon 2 #13-14) front-end code in
// static/app.js, run with plain Node (`node tests/manual_app_js_smoke.mjs`). Stubs just enough
// of the DOM/fetch surface for app.js's top-level code to execute without throwing, then calls
// the pure rendering functions directly with sample payloads shaped like the real backend
// output (scoring.py, press.py), to catch runtime errors that `node --check` (syntax only)
// cannot. Les vérifications des séries temporelles ont été retirées le 13/09/2026 avec la page
// qu'elles couvraient.
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

console.log("\nAll app.js smoke checks passed.");
