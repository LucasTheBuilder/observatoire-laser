const state = {
  view: "monthly",
  overview: null,
  monthly: null,
  market: null,
  offers: [],
  actors: [],
  profiles: [],
  vocabulary: [],
  network: {nodes: [], edges: []},
  duplicates: [],
  pipelineFunnel: {discovered: 0, fetched: 0, parsed: 0, evidence: 0, validated: 0},
  query: "",
  offerQuery: "",
  offerDrill: {family: null, level2: null, level3: null},
  marketDrill: null,
  actorFilters: {competitiveClass: "", actorType: "", country: "", businessModel: "", priorityOnly: false},
};

const content = document.querySelector("#content");
const dialog = document.querySelector("#proof-dialog");

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `Erreur ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;"
  })[char]);
}

function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 3500);
}

function dateLabel(value) {
  if (!value) return "Jamais";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date inconnue";
  return new Intl.DateTimeFormat("fr-FR", {dateStyle:"medium", timeStyle:"short"}).format(date);
}

function debounce(callback, delay = 180) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
}

function header(eyebrow, title, description, action="") {
  return `<header class="page-header"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p>${description}</p></div>${action}</header>`;
}

// --- Market family visual metadata (decorative icon + tint, not chart color) -----

const MARKET_STYLE = {
  "Médical": {bg: "#eaf7f1", fg: "#1f8a63", icon: '<path d="M10 3v14M3 10h14"/>'},
  "Batteries": {bg: "#fff7e0", fg: "#a8790b", icon: '<rect x="3" y="6" width="12" height="9" rx="1.5"/><rect x="15" y="9" width="2" height="3"/><path d="M7 6V4h5v2"/>'},
  "Optique": {bg: "#eef4fb", fg: "#2f6fb0", icon: '<circle cx="10" cy="10" r="6.5"/><circle cx="10" cy="10" r="2.4"/>'},
  "Semi-conducteurs": {bg: "#f3edfb", fg: "#6b4fb3", icon: '<rect x="6" y="6" width="8" height="8" rx="1"/><path d="M6 3v3M10 3v3M14 3v3M6 14v3M10 14v3M14 14v3M3 6h3M3 10h3M3 14h3M14 6h3M14 10h3M14 14h3"/>'},
  "Aéronautique": {bg: "#e9f5fb", fg: "#1f7aa8", icon: '<path d="M2 11l16-6-3 6 3 6-16-6zM9 11h9"/>'},
  "Spatial": {bg: "#efeefb", fg: "#5b4fa8", icon: '<path d="M10 2l2.5 6H15l-4 4 1.5 6-2.5-4-2.5 4L9 12 5 8h2.5z"/>'},
  "Défense": {bg: "#fdeeec", fg: "#c14a2f", icon: '<path d="M10 2l7 3v5c0 5-3 7.5-7 8-4-.5-7-3-7-8V5z"/>'},
  "Automobile": {bg: "#f1f3f5", fg: "#445566", icon: '<path d="M3 13l1.5-5A2 2 0 0 1 6.4 6.5h7.2A2 2 0 0 1 15.5 8L17 13"/><rect x="2" y="13" width="16" height="3" rx="1"/><circle cx="6" cy="16" r="1.4"/><circle cx="14" cy="16" r="1.4"/>'},
  "Luxe": {bg: "#fbeef6", fg: "#a84f86", icon: '<path d="M4 8l3-5h6l3 5-6 9z"/>'},
};
const DEFAULT_MARKET_STYLE = {bg: "#eef2f1", fg: "#687683", icon: '<circle cx="10" cy="10" r="5"/>'};

function marketStyle(label) {
  return MARKET_STYLE[label] || DEFAULT_MARKET_STYLE;
}

function marketIcon(label) {
  const s = marketStyle(label);
  return `<span class="market-icon" style="background:${s.bg};color:${s.fg}"><svg viewBox="0 0 20 20" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${s.icon}</svg></span>`;
}

// --- CSV export -------------------------------------------------------------

function toCSV(rows, columns) {
  const cell = value => {
    const str = String(value ?? "");
    return /[",;\n]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
  };
  const lines = [columns.map(c => cell(c.label)).join(",")];
  for (const row of rows) lines.push(columns.map(c => cell(row[c.key])).join(","));
  return lines.join("\r\n");
}

function downloadCSV(filename, rows, columns) {
  if (!rows.length) { toast("Rien à exporter pour le moment."); return; }
  // BOM so Excel opens accented French text as UTF-8 instead of guessing wrong.
  const blob = new Blob(["﻿" + toCSV(rows, columns)], {type: "text/csv;charset=utf-8;"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// --- Classification helpers (client-side grouping, no backend schema change) -----

function groupByMarket(rows) {
  const map = new Map();
  for (const row of rows) {
    const market = row.market || "Non classé";
    if (!map.has(market)) map.set(market, {total: 0, subthemes: new Map()});
    const bucket = map.get(market);
    bucket.total += 1;
    const sub = row.component || row.operation || "Autre";
    bucket.subthemes.set(sub, (bucket.subthemes.get(sub) || 0) + 1);
  }
  return [...map.entries()].sort((a, b) => b[1].total - a[1].total);
}

function marketFamilyCards(rows) {
  const groups = groupByMarket(rows);
  if (!groups.length) return `<div class="empty">Pas encore assez de faits pour une lecture par marché.</div>`;
  return `<div class="market-fam-grid">${groups.map(([market, bucket]) => {
    const chips = [...bucket.subthemes.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6)
      .map(([label, count]) => `<span class="subtheme-chip">${esc(label)}<b>${count}</b></span>`).join("");
    return `<button class="market-fam-card market-fam-card-link" data-drill-market="${esc(market)}"><header>${marketIcon(market)}<div><h3>${esc(market)}</h3><b>${bucket.total} faits</b></div></header><div class="subtheme-chips">${chips}</div></button>`;
  }).join("")}</div>`;
}

// Single-level equivalent of offerBreadcrumb: only ever "Tous les marchés" or "Tous les
// marchés › {market}", but reuses the same visual pattern for consistency across the two pages.
function marketBreadcrumb(selected) {
  if (!selected) return "";
  return `<nav class="drill-breadcrumb" tabindex="-1">
    <button class="drill-crumb" data-drill-market="">Tous les marchés</button><span class="drill-sep">›</span>
    <span class="drill-crumb current">${esc(selected)}</span>
  </nav>`;
}

// Capacity families: an operation/capability is matched against every keyword family
// (usinage / fonctionnalisation / structuration interne) independently -- a capability
// mentioning both an operation and a material (e.g. "découpe du saphir") belongs to every
// family it matches, not just the first one, so it must appear in each matching card.
const CAPABILITY_FAMILIES = [
  {label: "Usinage", test: t => /découpe|perçage|gravure|ablation|soudage|scribing|dicing|milling|rainurage|usinage|drilling|cutting|welding|engraving/i.test(t)},
  {label: "Fonctionnalisation", test: t => /fonctionnalisation|texturation|anti-?givre|hydrophob|hydrophile|oléophobe|olephobe|anti-?reflet|anti-?bu[ée]e|wetting|nettoyage|polissage|cleaning|polishing/i.test(t)},
  {label: "Structuration interne", test: t => /structuration interne|modification interne|guide d.onde|waveguide|debonding|volume modification/i.test(t)},
];
const CAPABILITY_FAMILY_ORDER = ["Usinage", "Fonctionnalisation", "Matériau", "Structuration interne", "Autres"];

function capabilityFamilies(row) {
  const text = [row.operation, row.capability].filter(Boolean).join(" ");
  const families = CAPABILITY_FAMILIES.filter(f => f.test(text)).map(f => f.label);
  // Additive, not a fallback: a material mention earns its own card on top of any operation
  // family already matched, instead of only appearing when no operation keyword matched.
  if (row.material) families.push("Matériau");
  return families.length ? families : ["Autres"];
}

function groupByFamily(rows, familyOf) {
  const map = new Map();
  for (const row of rows) {
    const family = familyOf(row);
    if (!map.has(family)) map.set(family, []);
    map.get(family).push(row);
  }
  return [...map.entries()].sort((a, b) => {
    const ia = CAPABILITY_FAMILY_ORDER.indexOf(a[0]), ib = CAPABILITY_FAMILY_ORDER.indexOf(b[0]);
    return (ia === -1 ? CAPABILITY_FAMILY_ORDER.length : ia) - (ib === -1 ? CAPABILITY_FAMILY_ORDER.length : ib);
  });
}

// Same idea as groupByFamily, but for a classifier that can return several families for one
// row: the row is duplicated into every matching bucket instead of only the first one matched.
function groupByFamilies(rows, familiesOf) {
  const map = new Map();
  for (const row of rows) {
    const families = familiesOf(row);
    for (const family of families) {
      if (!map.has(family)) map.set(family, []);
      map.get(family).push(row);
    }
  }
  return [...map.entries()].sort((a, b) => {
    const ia = CAPABILITY_FAMILY_ORDER.indexOf(a[0]), ib = CAPABILITY_FAMILY_ORDER.indexOf(b[0]);
    return (ia === -1 ? CAPABILITY_FAMILY_ORDER.length : ia) - (ib === -1 ? CAPABILITY_FAMILY_ORDER.length : ib);
  });
}

// --- Offers drill-down: family -> operation/property -> material, source list at the leaf ----
// A material has exactly one parent group here (unlike top-level families, which can overlap):
// grouping by material is meant to narrow down a search, not to re-surface the same duplicate-
// membership behavior already handled at the family level.
const MATERIAL_GROUP = {
  "Métal": "Métaux", "Nitinol": "Métaux", "Magnésium": "Métaux",
  "Verre": "Matériau transparent", "Saphir": "Matériau transparent",
  "Polymère": "Polymère", "Silicium": "Silicium", "Céramique": "Céramique", "Composite": "Composite",
};
// Only groups with more than one raw material inside them get a further level-3 split; a
// single-material group (Polymère, Silicium, Céramique, Composite) goes straight to sources.
const MATERIAL_GROUP_HAS_SUBLEVEL = new Set(["Métaux", "Matériau transparent"]);

// Some rows carry the raw English operation name instead of the canonical French label (older
// technology/product rows) -- normalized here so "Dicing" and "Microdécoupe" don't split into
// two separate buckets for what is the same operation.
const OPERATION_ALIAS = {"Dicing": "Microdécoupe"};

function normalizedOperation(row) {
  const op = (row.operation || "").trim();
  return OPERATION_ALIAS[op] || op || null;
}

// Fonctionnalisation is grouped by the surface property actually achieved, not by the generic
// operation label -- most source pages only say "texturation"/"fonctionnalisation de surface"
// without naming the specific property, so most rows land in the catch-all today; the buckets
// stay meaningful as sources get more precise.
const FONCTIONNALISATION_SUBFAMILIES = [
  {label: "Hydrophobie", test: t => /hydrophob|hydrophile|oléophobe|olephobe|wetting|mouillabilit/i.test(t)},
  {label: "Anti-givre", test: t => /anti-?givre|anti-?bu[ée]e|anti-?frost|icing/i.test(t)},
  {label: "Frottement", test: t => /frottement|friction|tribolog/i.test(t)},
];

function fonctionnalisationSubfamily(row) {
  const text = [row.operation, row.capability].filter(Boolean).join(" ");
  const hit = FONCTIONNALISATION_SUBFAMILIES.find(f => f.test(text));
  return hit ? hit.label : "Autre fonctionnalisation";
}

// Structuration interne (3 items total) and Autres (heterogeneous by nature) don't have a
// meaningful second level -- clicking them goes straight to the source list.
const FAMILIES_WITHOUT_LEVEL2 = new Set(["Structuration interne", "Autres"]);

function groupSimple(rows, keyOf) {
  const map = new Map();
  for (const row of rows) {
    const key = keyOf(row);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(row);
  }
  return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
}

function familyLevel2Groups(family, rows) {
  if (family === "Usinage") return groupSimple(rows, row => normalizedOperation(row) || "Autres opérations d’usinage");
  if (family === "Fonctionnalisation") return groupSimple(rows, fonctionnalisationSubfamily);
  if (family === "Matériau") return groupSimple(rows, row => MATERIAL_GROUP[row.material] || "Autres matériaux");
  return null;
}

function rowsForDrill(filtered, drill) {
  let rows = (groupByFamilies(filtered, capabilityFamilies).find(([label]) => label === drill.family) || [null, []])[1];
  if (!drill.level2) return rows;
  const level2Groups = familyLevel2Groups(drill.family, rows) || [];
  rows = (level2Groups.find(([label]) => label === drill.level2) || [null, []])[1];
  if (!drill.level3) return rows;
  const level3Groups = groupSimple(rows, row => row.material || "Non précisé");
  return (level3Groups.find(([label]) => label === drill.level3) || [null, []])[1];
}

function offerBreadcrumb(drill) {
  const segments = [{label: "Toutes les familles", drill: {family: null, level2: null, level3: null}}];
  if (drill.family) segments.push({label: drill.family, drill: {family: drill.family, level2: null, level3: null}});
  if (drill.level2) segments.push({label: drill.level2, drill: {family: drill.family, level2: drill.level2, level3: null}});
  if (drill.level3) segments.push({label: drill.level3, drill: {family: drill.family, level2: drill.level2, level3: drill.level3}});
  return `<nav class="drill-breadcrumb" tabindex="-1">${segments.map((seg, i) => {
    if (i === segments.length - 1) return `<span class="drill-crumb current">${esc(seg.label)}</span>`;
    return `<button class="drill-crumb" data-drill='${esc(JSON.stringify(seg.drill))}'>${esc(seg.label)}</button><span class="drill-sep">›</span>`;
  }).join("")}</nav>`;
}

function offerCategoryGrid(groups) {
  return `<div class="fam-grid">${groups.map(([label, rows]) => `
    <button class="fam-card fam-card-link" data-drill='${esc(JSON.stringify({family: label, level2: null, level3: null}))}'>
      <header><h3>${esc(label)}</h3><b>${rows.length}</b></header>
      <p class="fam-card-hint">${rows.length ? "Explorer →" : "Aucune capacité pour le moment"}</p>
    </button>`).join("")}</div>`;
}

// Same visual card as offerCategoryGrid, but the click target carries the full resulting drill
// state (family already fixed) instead of assuming it's always a fresh top-level family.
function offerSubCategoryGrid(drill, groups) {
  return `<div class="fam-grid">${groups.map(([label, rows]) => `
    <button class="fam-card fam-card-link" data-drill='${esc(JSON.stringify({...drill, level2: drill.level2 || label, level3: drill.level2 ? label : null}))}'>
      <header><h3>${esc(label)}</h3><b>${rows.length}</b></header>
      <p class="fam-card-hint">${rows.length ? "Explorer →" : "Aucune capacité pour le moment"}</p>
    </button>`).join("")}</div>`;
}

function offerEmptyMessage(hasQuery) {
  return `<div class="empty">${hasQuery
    ? "Aucun résultat pour cette recherche dans cette catégorie."
    : "Aucune capacité dans cette catégorie pour le moment."}</div>`;
}

function renderOfferDrillContent(filtered, hasQuery) {
  const drill = state.offerDrill;
  if (!drill.family) {
    const families = groupByFamilies(filtered, capabilityFamilies);
    return offerBreadcrumb(drill) + (families.length ? offerCategoryGrid(families) : offerEmptyMessage(hasQuery));
  }
  const familyRows = rowsForDrill(filtered, {family: drill.family, level2: null, level3: null});
  if (!drill.level2 && !FAMILIES_WITHOUT_LEVEL2.has(drill.family)) {
    const level2Groups = familyLevel2Groups(drill.family, familyRows) || [];
    return offerBreadcrumb(drill) + (level2Groups.length ? offerSubCategoryGrid(drill, level2Groups) : offerEmptyMessage(hasQuery));
  }
  if (drill.level2 && !drill.level3 && MATERIAL_GROUP_HAS_SUBLEVEL.has(drill.level2)) {
    const level2Rows = rowsForDrill(filtered, {family: drill.family, level2: drill.level2, level3: null});
    const level3Groups = groupSimple(level2Rows, row => row.material || "Non précisé");
    return offerBreadcrumb(drill) + (level3Groups.length ? offerSubCategoryGrid(drill, level3Groups) : offerEmptyMessage(hasQuery));
  }
  const rows = rowsForDrill(filtered, drill);
  const hideMaterialSuffix = drill.family === "Matériau";
  return offerBreadcrumb(drill) + (rows.length
    ? `<ul class="fam-list">${rows.map(row => offerFamilyItem(row, {hideMaterialSuffix})).join("")}</ul>`
    : offerEmptyMessage(hasQuery));
}

// Competitive class is an analyst-assigned field (see db.update_actor_classification),
// not guessed from role text -- classification lives in the data, not in a regex.
const COMPETITIVE_CLASS_LABELS = {
  C1: "Concurrence directe",
  C2: "Concurrence partielle",
  T1: "Centres technologiques / recherche",
};
const COMPETITIVE_CLASS_SHORT = {C1: "Direct", C2: "Partiel", T1: "Centre techno"};

function competitiveClassLabel(actor) {
  return COMPETITIVE_CLASS_LABELS[actor.competitive_class] || "Non classé";
}

function groupActorsByCategory(actors) {
  const order = ["C1", "C2", "T1", "Non classé"];
  const map = new Map();
  for (const actor of actors) {
    if (actor.is_reference || actor.review_status !== "verified") continue;
    const key = COMPETITIVE_CLASS_LABELS[actor.competitive_class] ? actor.competitive_class : "Non classé";
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(actor);
  }
  return order.filter(key => map.has(key)).map(key => [COMPETITIVE_CLASS_LABELS[key] || key, map.get(key)]);
}

function aiProviderName(adaptive) {
  return adaptive.ai_provider === "anthropic" ? "Claude" : "Ollama";
}

function aiCostSuffix(adaptive) {
  return typeof adaptive.ai_cost_month_usd === "number" ? ` · ~${adaptive.ai_cost_month_usd.toFixed(2)} $ ce mois-ci` : "";
}

function proofMeta(row) {
  const proofs = Number(row.proofs || 0);
  const languages = Number(row.languages || 0);
  if (languages > 1) return `${proofs} src · ${languages} lang.`;
  return String(proofs);
}

// hideMarketColumn drops the now-redundant "Marché" column once the reader has already picked
// a market via marketFamilyCards -- same idea as offers' hideMaterialSuffix: don't repeat what
// the breadcrumb already says.
function evidenceTable(rows, {hideMarketColumn = false, emptyMessage} = {}) {
  if (!rows.length) {
    return `<div class="empty">${emptyMessage || "Aucune application suffisamment documentée pour le moment."}</div>`;
  }
  const marketHead = hideMarketColumn ? "" : "<span>Marché</span>";
  return `<div class="evidence-table ${hideMarketColumn ? "no-market-col" : ""}">
    <div class="evidence-head">${marketHead}<span>Pièce / composant</span><span>Fonction / opération laser</span><span>Preuves</span></div>
    ${rows.map(row => `<div class="evidence-row">
      ${hideMarketColumn ? "" : `<div class="market-cell"><i></i>${esc(row.market)}</div>`}
      <div>${esc(row.component)}</div>
      <div class="operation">${esc(row.operation)}</div>
      <button class="proof-pill ${Number(row.languages||0)>1?'multi-source':''}" data-proof='${esc(JSON.stringify(row))}' title="Voir les sources">${esc(proofMeta(row))}</button>
    </div>`).join("")}
  </div>`;
}

function renderMarket() {
  const market = state.market || {existing:[], radar:[]};
  const allRows = [...market.existing, ...market.radar];
  const selected = state.marketDrill;
  const existingRows = selected ? market.existing.filter(row => row.market === selected) : market.existing;
  const radarRows = selected ? market.radar.filter(row => row.market === selected) : market.radar;

  // Same idea as Offres & capacités: the family-card overview is the entry point; once a market
  // is picked it steps aside for a breadcrumb, and the two fact tables below narrow to it with
  // their now-redundant "Marché" column dropped.
  const section01 = selected
    ? marketBreadcrumb(selected)
    : `<div class="section-title"><div><span>01</span><div><h2>Lecture par marché</h2><p>Chaque marché, avec ses sous-thèmes (pièce / composant) les plus documentés. Cliquer un marché filtre les deux tableaux ci-dessous.</p></div></div><b>${allRows.length} faits</b></div>${marketFamilyCards(allRows)}`;

  content.innerHTML = header(
    "Lecture marché",
    "Applications femtoseconde",
    "Uniquement les faits où marché, pièce/composant et opération laser sont explicitement reliés. Les versions linguistiques d’un même fait sont regroupées comme preuves.",
    `<div class="header-actions"><button class="export-btn" data-export="market">⬇ Exporter CSV</button><button class="primary" data-run="market">↻ Actualiser l’analyse</button></div>`
  ) +
  `<section id="market-drill-root">${section01}</section>
   <section><div class="section-title"><div><span>02</span><div><h2>Applications industrielles existantes</h2><p>Production, prestation ou qualification explicitement démontrée.</p></div></div><b>${existingRows.length} faits</b></div>${evidenceTable(existingRows, {hideMarketColumn: !!selected, emptyMessage: selected ? `Aucune application existante documentée pour ${selected}.` : undefined})}</section>
   <section><div class="section-title"><div><span>03</span><div><h2>Radar applications et besoins</h2><p>Applications documentées dont l’industrialisation reste à confirmer.</p></div></div><b>${radarRows.length} faits</b></div>${evidenceTable(radarRows, {hideMarketColumn: !!selected, emptyMessage: selected ? `Aucune application radar documentée pour ${selected}.` : undefined})}</section>`;
  wireActions();
  document.querySelectorAll("[data-drill-market]").forEach(el => el.addEventListener("click", () => {
    state.marketDrill = el.dataset.drillMarket || null;
    renderMarket();
  }));
  const exportBtn = document.querySelector('[data-export="market"]');
  if (exportBtn) exportBtn.addEventListener("click", () => downloadCSV("marche.csv", selected ? [...existingRows, ...radarRows] : allRows, [
    {key: "market", label: "Marché"}, {key: "component", label: "Composant"},
    {key: "operation", label: "Opération"}, {key: "languages", label: "Langues"},
  ]));
}


function renderMonthly() {
  const monthly = state.monthly || {
    days: 30,
    counts: {},
    changed_sources: [],
    new_market: [],
    new_offers: [],
    resurfaced_market: [],
    technology: [],
  };
  const counts = monthly.counts || {};
  const marketSignals = [...(monthly.new_market || []), ...(monthly.resurfaced_market || [])];

  const marketList = marketSignals.length
    ? marketSignalGrid(marketSignals)
    : `<div class="empty">Aucun nouveau fait marché validé sur la période.</div>`;

  const offerFamilies = groupByFamilies(monthly.new_offers || [], capabilityFamilies);
  const offerList = (monthly.new_offers || []).length
    ? offerFamilyGrid(offerFamilies)
    : `<div class="empty">Aucune nouvelle capacité concurrente détectée sur la période.</div>`;

  const techList = (monthly.technology || []).length
    ? `<div class="signal-list">${monthly.technology.map(row => `<article class="signal-item">
        <div><small>${esc(String(row.document_type || "document").toUpperCase())}${row.published_at ? ` · ${esc(row.published_at)}` : ""}</small>
        <strong>${esc(row.title)}</strong>
        <span>${esc(row.actor_name || "Acteur non attribué")}</span></div>
        <a class="signal-link" href="${esc(row.source_url)}" target="_blank" rel="noopener">source ↗</a>
      </article>`).join("")}</div>`
    : `<div class="empty">Aucun nouveau document technologique collecté sur la période.</div>`;

  content.innerHTML = header(
    "Synthèse",
    `Ce qui a changé sur les ${monthly.days || 30} derniers jours`,
    "Nouveaux faits marché, mouvements concurrents, reconfirmations et signaux technologiques depuis la dernière période.",
    `<button class="primary" data-run="monthly">↻ Actualiser toute la veille</button>`
  ) +
  `<section><div class="section-title"><div><span>01</span><div><h2>Marché & opportunités</h2><p>Nouveaux faits validés et applications déjà connues mais observées de nouveau.</p></div></div><b>${marketSignals.length} signaux</b></div>${marketList}</section>
   <section><div class="section-title"><div><span>02</span><div><h2>Mouvements concurrents</h2><p>Nouvelles offres, capacités et savoir-faire détectés chez les acteurs suivis, classés par famille.</p></div></div><b>${Number(counts.new_offers || 0)} signaux</b></div>${offerList}</section>
   <section><div class="section-title"><div><span>03</span><div><h2>Technologies futures</h2><p>Publications, brevets, projets et autres documents collectés récemment.</p></div></div><b>${Number(counts.technology || 0)} signaux</b></div>${techList}</section>`;
  wireActions();
}


function offerTypeLabel(value) {
  const labels = {
    service: "Service",
    capability: "Capacité",
    technology: "Technologie",
    product: "Produit",
  };
  return labels[value] || value || "Capacité";
}

function familyCardGrid(groups, renderItem) {
  if (!groups.length) return "";
  return `<div class="fam-grid">${groups.map(([label, rows]) => `<article class="fam-card"><header><h3>${esc(label)}</h3><b>${rows.length}</b></header><ul class="fam-list">${rows.map(row => renderItem(row)).join("")}</ul></article>`).join("")}</div>`;
}

// The offer_type badge (Service/Capacité/Technologie) and cross-family "également dans" note
// were classification bookkeeping, not something that helps identify who does what on which
// piece -- dropped so the two lines that remain (actor, then the actual capability text) carry
// all the weight. hideMaterialSuffix lets a caller already inside the Matériau branch of the
// drill-down skip repeating the material that's already implied by where the list is nested;
// everywhere else (Usinage, Fonctionnalisation...) the material is still new information.
function offerFamilyItem(row, {hideMaterialSuffix = false} = {}) {
  const materialSuffix = !hideMaterialSuffix && row.material ? ` · ${esc(row.material)}` : "";
  return `<li><button class="fam-item" data-offer-proof="${Number(row.id)}">
      <span class="fam-row-top">
        <span class="fam-actor">${esc(row.actor_name)}</span>
        <span class="fam-proof-pill" title="Voir les sources">${esc(proofMeta(row))}</span>
      </span>
      <span class="fam-cap">${esc(row.capability)}${materialSuffix}</span>
    </button></li>`;
}

function offerFamilyGrid(families) {
  return familyCardGrid(families, offerFamilyItem);
}

function marketSignalItem(row) {
  return `<li><button class="fam-item" data-proof='${esc(JSON.stringify(row))}'>
      <span class="fam-row-top"><span class="fam-actor">${esc(row.actor_name)}</span></span>
      <span class="fam-cap">${esc(row.component)} · ${esc(row.operation)}</span>
    </button></li>`;
}

function marketSignalGrid(rows) {
  const groups = groupByFamily(rows, row => row.market || "Non classé").sort((a, b) => b[1].length - a[1].length);
  return familyCardGrid(groups, marketSignalItem);
}

function renderOffers() {
  // Preserve focus/caret only if the search box itself had it -- a drill-down click also calls
  // this function, and unconditionally refocusing the search input on every render would yank
  // keyboard focus away from the category the user just clicked into.
  const hadSearchFocus = document.activeElement && document.activeElement.id === "offer-search";
  const caret = hadSearchFocus ? document.activeElement.selectionStart : null;

  const q = state.offerQuery.trim().toLowerCase();
  const filtered = state.offers.filter(row => {
    const haystack = [row.actor_name, row.offer_type, row.capability, row.operation, row.laser_process, row.material, row.performance, row.industrial_stage, row.page_type].join(" ").toLowerCase();
    return haystack.includes(q);
  });
  const actors = new Set(filtered.map(row => row.actor_name)).size;
  content.innerHTML = header(
    "Veille concurrentielle",
    "Offres & capacités",
    "Prestations, procédés et savoir-faire détectés chez les acteurs suivis. Cliquez une famille pour explorer ses sous-catégories (opération ou propriété, puis matériau si besoin) jusqu’à la liste des sources. Une capacité qui relève de plusieurs familles à la fois (ex. découpe + matériau) apparaît dans chacune d’elles. Cette vue n’invente pas de marché lorsqu’une page décrit uniquement une capacité technique.",
    `<div class="header-actions"><button class="export-btn" data-export="offers">⬇ Exporter CSV</button><button class="primary" data-run="market">↻ Actualiser les preuves</button></div>`
  ) +
  `<div class="actor-toolbar offer-toolbar"><input id="offer-search" value="${esc(state.offerQuery)}" placeholder="Rechercher un acteur, un procédé, une opération, un matériau…"><span>${filtered.length} capacités · ${actors} acteurs</span></div>
   <section><div class="section-title"><div><span>01</span><div><h2>Cartographie des offres détectées</h2><p>Famille → opération/propriété → matériau si besoin → sources.</p></div></div><b>${filtered.length} capacités</b></div>
   <div id="offer-drill-root">${renderOfferDrillContent(filtered, q.length > 0)}</div></section>`;

  const input = document.querySelector("#offer-search");
  if (input) {
    if (hadSearchFocus) {
      input.focus({preventScroll: true});
      input.setSelectionRange(caret, caret);
    }
    input.addEventListener("input", debounce(e => {
      state.offerQuery = e.target.value;
      // A fresh search should search everything, not stay pinned inside whatever category was
      // open -- otherwise a query that doesn't match the current branch silently looks like
      // "no results" for the whole app instead of "try a different category".
      state.offerDrill = {family: null, level2: null, level3: null};
      renderOffers();
    }));
  }
  wireActions();
  document.querySelectorAll("#offer-drill-root [data-drill]").forEach(el => el.addEventListener("click", () => {
    state.offerDrill = JSON.parse(el.dataset.drill);
    renderOffers();
    const root = document.querySelector("#offer-drill-root .drill-breadcrumb");
    if (root) root.focus({preventScroll: false});
  }));
  const exportBtn = document.querySelector('[data-export="offers"]');
  if (exportBtn) exportBtn.addEventListener("click", () => downloadCSV("offres-capacites.csv", filtered, [
    {key: "actor_name", label: "Acteur"}, {key: "offer_type", label: "Type"},
    {key: "capability", label: "Capacité"}, {key: "operation", label: "Opération"},
    {key: "laser_process", label: "Procédé"}, {key: "material", label: "Matériau"},
  ]));
}

const ACTOR_TYPE_LABELS = {
  groupe_industriel: "Groupe industriel",
  prestataire_industriel: "Prestataire industriel",
  societe_developpement_procedes: "Société de développement de procédés",
  societe_technologique_specialisee: "Société technologique spécialisée",
  centre_technologique: "Centre technologique",
  institut_recherche_appliquee: "Institut de recherche appliquée",
  laboratoire_academique: "Laboratoire académique",
  partenaire_adjacent: "Partenaire adjacent",
};
const BUSINESS_MODEL_LABELS = {equipment: "Équipement", service: "Service", process: "Procédé", research: "Recherche", internal: "Interne"};

function cardSummaryLine(a) {
  const text = a.strategic_summary || a.role || "";
  return text.length > 300 ? `${text.slice(0, 299)}…` : text;
}

function actorCard(a) {
  const paused = !a.active;
  const classBadge = a.competitive_class
    ? `<span class="class-badge ${esc(a.competitive_class)}">${esc(a.competitive_class)}${COMPETITIVE_CLASS_SHORT[a.competitive_class] ? ` · ${esc(COMPETITIVE_CLASS_SHORT[a.competitive_class])}` : ""}</span>`
    : "";
  const typeLabel = ACTOR_TYPE_LABELS[a.actor_type] ? `<p class="actor-type-label">${esc(ACTOR_TYPE_LABELS[a.actor_type])}</p>` : "";
  return `<article class="actor-card ${a.priority?'priority':''}" ${paused?'style="opacity:.55"':''}>
    <div class="actor-top"><div class="initial">${esc(a.name.slice(0,2))}</div><div class="actor-top-tags">${classBadge}${a.priority?'<span>★ Prioritaire</span>':''}</div></div>
    <h3 class="actor-name-link" data-actor-detail="${a.id}">${esc(a.name)}</h3>${typeLabel}<p class="actor-summary-line">${esc(cardSummaryLine(a))}</p>
    <div class="actor-card-actions">
      <button class="actor-detail-link" data-actor-detail="${a.id}">Voir la fiche →</button>
      <details class="actor-menu">
        <summary>⋯</summary>
        <div class="actor-menu-list">
          <a href="${esc(a.official_url)}" target="_blank" rel="noopener">Site officiel ↗</a>
          <button data-actor-edit="${a.id}">Modifier</button>
          <button data-actor-toggle-priority="${a.id}" data-next-priority="${a.priority?'0':'1'}">${a.priority?'Retirer la priorité':'Marquer prioritaire'}</button>
          <button data-toggle-actor="${a.id}" data-next-active="${paused?'1':'0'}">${paused?'Réactiver':'Mettre en pause'}</button>
          <button class="menu-danger" data-actor-delete="${a.id}" data-actor-name="${esc(a.name)}">Supprimer</button>
        </div>
      </details>
    </div>
  </article>`;
}

const VALUE_CHAIN_ALL_STAGES = ["R&D", "Prototype", "Pré-industrialisation", "Industrialisation", "Production"];

const VALUE_CHAIN_SHORT_LABELS = {"R&D": "Dev", "Prototype": "Proto", "Pré-industrialisation": "Pré-indus", "Industrialisation": "Indus", "Production": "Prod"};

function valueChainTrack(stages, compact = false) {
  const demonstrated = new Set(stages || []);
  return `<div class="value-chain-track ${compact ? "compact" : ""}">${VALUE_CHAIN_ALL_STAGES.map(stage =>
    `<div class="value-chain-node ${demonstrated.has(stage) ? "done" : ""}"><span class="dot"></span><small>${esc(compact ? VALUE_CHAIN_SHORT_LABELS[stage] : stage)}</small></div>`
  ).join("")}</div>`;
}

const COVERAGE_LABELS = {good: "Bonne", partial: "Partielle", weak: "Faible"};
const COVERAGE_HINTS = {
  good: "La fiche dispose déjà d'un socle documentaire suffisant.",
  partial: "Des faits existent mais la couverture est insuffisante pour considérer la fiche comme complète.",
  weak: "Le crawler ne fournit actuellement aucun socle structuré suffisant ; ne pas interpréter cette absence comme une absence de compétence.",
};

function differentiatorFacts(a) { return (a.facts || []).filter(f => f.dimension === "differentiator"); }
function certificationFacts(a) { return (a.facts || []).filter(f => f.dimension === "certification"); }

function factLine(f) {
  return `<li>${esc(f.value)}${f.source_url ? ` <a href="${esc(f.source_url)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`;
}

function eventLine(e) {
  return `<li>${e.event_date ? `<b>${esc(e.event_date)}</b> — ` : ""}${esc(e.description)}${e.source_url ? ` <a href="${esc(e.source_url)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`;
}

function actorDetailContent(a) {
  const offers = state.offers.filter(o => o.actor_name === a.name);
  const marketRows = [...(state.market?.existing || []), ...(state.market?.radar || [])].filter(r => r.actor_name === a.name);
  const capabilities = [...new Set(offers.map(o => o.operation).filter(Boolean))];
  const markets = [...new Set(marketRows.map(r => r.market).filter(Boolean))];
  const proofsTotal = offers.length + marketRows.length;
  const classLabel = COMPETITIVE_CLASS_LABELS[a.competitive_class] || "Non classé";

  return `<p class="eyebrow">${esc(a.country)}</p>
    <h2>${esc(a.name)}</h2>
    <p class="dialog-operation">${a.competitive_class ? `${esc(a.competitive_class)} · ${esc(classLabel)}` : classLabel}${a.priority ? " · ★ Prioritaire" : ""}</p>

    <div class="detail-block">
      <h4>Positionnement</h4>
      ${a.strategic_summary ? `<p>${esc(a.strategic_summary)}</p>` : ""}
      <p>${esc(ACTOR_TYPE_LABELS[a.actor_type] || a.role)}</p>
      ${(a.business_models || []).length ? `<div class="business-model-tags">${a.business_models.map(m => `<span class="business-tag ${esc(m)}">${esc(BUSINESS_MODEL_LABELS[m] || m)}</span>`).join("")}</div>` : ""}
    </div>

    ${(a.value_chain_stages || []).length ? `<div class="detail-block"><h4>Chaîne de valeur</h4>${valueChainTrack(a.value_chain_stages)}</div>` : ""}

    ${capabilities.length ? `<div class="detail-block"><h4>Capacités démontrées</h4><div class="subtheme-chips">${capabilities.map(c => `<span class="subtheme-chip">${esc(c)}</span>`).join("")}</div></div>` : ""}

    ${markets.length ? `<div class="detail-block"><h4>Marchés</h4><div class="subtheme-chips">${markets.map(m => `<span class="subtheme-chip">${esc(m)}</span>`).join("")}</div></div>` : ""}

    ${differentiatorFacts(a).length ? `<div class="detail-block"><h4>Différenciateurs</h4><ul class="fact-list">${differentiatorFacts(a).map(f => factLine(f)).join("")}</ul></div>` : ""}

    ${certificationFacts(a).length ? `<div class="detail-block"><h4>Certifications</h4><ul class="fact-list">${certificationFacts(a).map(f => factLine(f)).join("")}</ul></div>` : ""}

    ${(a.events || []).length ? `<div class="detail-block"><h4>Événements</h4><ul class="fact-list">${a.events.map(eventLine).join("")}</ul></div>` : ""}

    ${a.parent_actor ? `<div class="detail-block"><h4>Mouvement capitalistique</h4><p class="actor-entity-note">Racheté par <b>${esc(a.parent_actor)}</b>${a.entity_note ? ` — ${esc(a.entity_note)}` : ""}</p></div>` : ""}

    <div class="detail-block">
      <h4>Preuves</h4>
      <p>${proofsTotal} preuve(s) issues de nos collectes${a.evidence_confirmed === false ? ' — <span class="evidence-warning">⚠ non confirmé par nos preuves</span>' : ""}</p>
      <p class="coverage-note">Couverture documentaire : <b>${COVERAGE_LABELS[a.coverage_level] || "Non documenté dans la base"}</b>. ${COVERAGE_HINTS[a.coverage_level] || ""}</p>
    </div>

    <div class="detail-block admin-block">
      <h4>Administration technique</h4>
      <div class="profile-line"><span class="profile-badge ${a.needs_reprofile ? "warning" : a.strategy}">${a.needs_reprofile ? "À recalibrer" : a.strategy === "adaptive" ? "Adaptatif" : "Générique"}</span><small>${a.profile_status === "ready" ? "Profil prêt" : a.profile_status === "partial" ? "Profil partiel" : a.profile_status === "degraded" ? "Mode dégradé" : "À cartographier"}</small></div>
      <a href="${esc(a.official_url)}" target="_blank" rel="noopener" class="signal-link">Site officiel ↗</a>
    </div>`;
}

function showActorDetail(actorId) {
  const actor = state.actors.find(a => a.id === actorId);
  if (!actor) return;
  document.querySelector("#proof-content").innerHTML = actorDetailContent(actor);
  dialog.classList.add("wide");
  dialog.showModal();
}

function actorFormFields(a) {
  const classOptions = ["", "C1", "C2", "T1"]
    .map(v => `<option value="${v}" ${(a.competitive_class || "") === v ? "selected" : ""}>${v ? `${v} · ${esc(COMPETITIVE_CLASS_SHORT[v])}` : "Non classé"}</option>`)
    .join("");
  const typeOptions = Object.entries(ACTOR_TYPE_LABELS)
    .map(([v, label]) => `<option value="${v}" ${a.actor_type === v ? "selected" : ""}>${esc(label)}</option>`)
    .join("");
  const modelCheckboxes = Object.entries(BUSINESS_MODEL_LABELS)
    .map(([v, label]) => `<label class="checkbox-row"><input type="checkbox" name="business_models" value="${v}" ${(a.business_models || []).includes(v) ? "checked" : ""}> ${esc(label)}</label>`)
    .join("");
  return `<label>Nom<input type="text" name="name" value="${esc(a.name || "")}" required></label>
    <label>Pays<input type="text" name="country" value="${esc(a.country || "")}" required></label>
    <label>Rôle<input type="text" name="role" value="${esc(a.role || "")}" required></label>
    <label>Site officiel<input type="url" name="official_url" value="${esc(a.official_url || "")}" required></label>
    <label class="checkbox-row"><input type="checkbox" name="priority" ${a.priority ? "checked" : ""}> Prioritaire</label>
    <label>Classe concurrentielle<select name="competitive_class">${classOptions}</select></label>
    <label>Type d'acteur<select name="actor_type"><option value="">Non classé</option>${typeOptions}</select></label>
    <fieldset><legend>Business models</legend>${modelCheckboxes}</fieldset>
    <label>Résumé stratégique<textarea name="strategic_summary" rows="3">${esc(a.strategic_summary || "")}</textarea></label>`;
}

function showActorEdit(actorId) {
  const actor = state.actors.find(a => a.id === actorId);
  if (!actor) return;
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">Modifier l'acteur</p>
    <h2>${esc(actor.name)}</h2>
    <form id="actor-edit-form" class="detail-form">
      ${actorFormFields(actor)}
      <div class="detail-form-actions"><button type="button" data-close-dialog>Annuler</button><button type="submit" class="primary">Enregistrer</button></div>
    </form>`;
  dialog.classList.add("wide");
  dialog.showModal();
  document.querySelector("#actor-edit-form").addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(e.target);
    try {
      await api(`/api/actors/${actorId}`, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: data.get("name"), country: data.get("country"), role: data.get("role"),
          official_url: data.get("official_url"), priority: data.get("priority") === "on",
          competitive_class: data.get("competitive_class") || null,
          actor_type: data.get("actor_type") || null,
          business_models: data.getAll("business_models"),
          strategic_summary: data.get("strategic_summary") || null,
        }),
      });
      toast("Acteur mis à jour.");
      dialog.close();
      state.actors = await api("/api/actors");
      renderActors();
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
}

async function toggleActorPriority(actorId, nextPriority) {
  try {
    await api(`/api/actors/${actorId}`, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({priority: nextPriority})});
    toast(nextPriority ? "Acteur marqué prioritaire." : "Priorité retirée.");
    state.actors = await api("/api/actors");
    renderActors();
  } catch (error) { toast(error.message); }
}

async function deleteActorWithConfirm(actorId, actorName) {
  if (!confirm(`Supprimer définitivement « ${actorName} » ? Cette action supprime aussi tout son historique de collecte et ne peut pas être annulée.`)) return;
  try {
    await api(`/api/actors/${actorId}`, {method: "DELETE"});
    toast("Acteur supprimé.");
    state.actors = await api("/api/actors");
    renderActors();
  } catch (error) { toast(error.message); }
}

function acquisitionsSection(actors) {
  const acquired = actors.filter(a => a.parent_actor);
  if (!acquired.length) return "";
  return `<section><div class="section-title"><div><span>⇄</span><div><h2>Mouvements capitalistiques</h2><p>Acquisitions et changements de maison mère détectés sur les acteurs suivis.</p></div></div><b>${acquired.length}</b></div>
    <div class="acquisition-list">${acquired.map(a => `<article class="acquisition-row">
      <div class="acquisition-names"><strong>${esc(a.name)}</strong><span class="acquisition-arrow">racheté par</span><strong>${esc(a.parent_actor)}</strong></div>
      ${a.entity_note ? `<p>${esc(a.entity_note)}</p>` : ""}
      <a class="signal-link" href="${esc(a.official_url)}" target="_blank" rel="noopener">Voir la source ↗</a>
    </article>`).join("")}</div>
  </section>`;
}

function reviewActorCard(a) {
  const label = a.review_status === "monitor" ? "SOUS SURVEILLANCE" : "À VALIDER";
  return `<article class="review-card">
    <header><small>${esc(label)} · ${esc(a.country)}</small><strong>${esc(a.name)}</strong><span>${esc(a.role)}</span></header>
    ${a.entity_note ? `<blockquote>${esc(a.entity_note)}</blockquote>` : ""}
    <div class="review-actions">
      <button class="vocab-accept" data-review-actor="${a.id}" data-review-status="verified">✓ Valider</button>
      ${a.review_status !== "monitor" ? `<button class="review-monitor" data-review-actor="${a.id}" data-review-status="monitor">◷ Surveiller</button>` : ""}
      <button class="vocab-reject" data-review-actor="${a.id}" data-review-status="rejected">✕ Rejeter</button>
    </div>
    <a class="signal-link" href="${esc(a.official_url)}" target="_blank" rel="noopener">Voir le site ↗</a>
  </article>`;
}

async function decideActorReview(actorId, reviewStatus) {
  try {
    await api(`/api/actors/${actorId}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({review_status: reviewStatus}),
    });
    toast(reviewStatus === "verified" ? "Acteur validé." : reviewStatus === "monitor" ? "Acteur mis sous surveillance." : "Acteur rejeté.");
    [state.actors, state.network] = await Promise.all([api("/api/actors"), api("/api/network")]);
    renderActors();
  } catch (error) { toast(error.message); }
}

const NETWORK_NODE_COLOR = {market: "var(--chart-coral)", technology: "#b9aee0"};
const NETWORK_CLASS_COLOR = {C1: "#c14a2f", C2: "#a8790b", T1: "#6b4fb3"};

function networkGraphSvg(nodes, edges) {
  if (!nodes.length) return `<div class="empty">Pas encore assez de preuves marché/technologie pour construire la cartographie.</div>`;
  const width = 780, height = 780, cx = width / 2, cy = height / 2;
  const radiusByType = {market: 90, technology: 190, actor: 320};
  const byType = {market: [], technology: [], actor: []};
  for (const node of nodes) (byType[node.type] || byType.actor).push(node);
  const positioned = new Map();
  for (const type of ["market", "technology", "actor"]) {
    const list = byType[type];
    list.forEach((node, i) => {
      const angle = (i / Math.max(list.length, 1)) * 2 * Math.PI - Math.PI / 2;
      positioned.set(node.id, {...node, x: cx + radiusByType[type] * Math.cos(angle), y: cy + radiusByType[type] * Math.sin(angle), angle});
    });
  }
  const colorFor = node => node.type === "actor" ? (NETWORK_CLASS_COLOR[node.class] || "var(--teal)") : NETWORK_NODE_COLOR[node.type];
  const edgeLines = edges.map(edge => {
    const source = positioned.get(edge.source), target = positioned.get(edge.target);
    if (!source || !target) return "";
    return `<line x1="${source.x.toFixed(1)}" y1="${source.y.toFixed(1)}" x2="${target.x.toFixed(1)}" y2="${target.y.toFixed(1)}" class="network-edge"><title>${esc(source.label)} → ${esc(target.label)}${edge.relation ? ` (${esc(edge.relation)})` : ""}</title></line>`;
  }).join("");
  const nodeMarks = [...positioned.values()].map(node => {
    const r = node.type === "actor" ? 7 : 5;
    const cos = Math.cos(node.angle);
    const anchor = cos > 0.15 ? "start" : cos < -0.15 ? "end" : "middle";
    const dx = anchor === "start" ? 10 : anchor === "end" ? -10 : 0;
    const sin = Math.sin(node.angle);
    const dy = sin > 0.85 ? 15 : sin < -0.85 ? -10 : 4;
    return `<g>
      <circle cx="${node.x.toFixed(1)}" cy="${node.y.toFixed(1)}" r="${r}" fill="${colorFor(node)}"><title>${esc(node.label)}</title></circle>
      <text x="${(node.x + dx).toFixed(1)}" y="${(node.y + dy).toFixed(1)}" text-anchor="${anchor}" class="network-label ${node.type}">${esc(node.label)}</text>
    </g>`;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" class="network-graph" role="img" aria-label="Cartographie réseau">${edgeLines}${nodeMarks}</svg>`;
}

function networkSection() {
  const legend = [
    {label: "Marché", color: NETWORK_NODE_COLOR.market},
    {label: "Technologie", color: NETWORK_NODE_COLOR.technology},
    {label: "Acteur C1", color: NETWORK_CLASS_COLOR.C1},
    {label: "Acteur C2", color: NETWORK_CLASS_COLOR.C2},
    {label: "Acteur T1", color: NETWORK_CLASS_COLOR.T1},
  ];
  return `<section><div class="section-title"><div><span>02</span><div><h2>Cartographie réseau</h2><p>Acteur ↔ marché ↔ technologie, reconstruite à partir des preuves déjà collectées. Un acteur sans preuve n'apparaît pas encore.</p></div></div></div>
    <div class="chart-legend">${legend.map(item => `<span><i style="background:${item.color}"></i>${item.label}</span>`).join("")}</div>
    ${networkGraphSvg(state.network.nodes, state.network.edges)}
  </section>`;
}

function actorFilterBar() {
  const f = state.actorFilters;
  const countries = [...new Set(state.actors.map(a => a.country))].sort();
  const types = [...new Set(state.actors.map(a => a.actor_type).filter(Boolean))];
  const classChip = (value, label) => `<button type="button" class="filter-chip ${f.competitiveClass===value?'active':''}" data-filter="competitiveClass" data-value="${value}">${esc(label)}</button>`;
  const modelChip = (value, label) => `<button type="button" class="filter-chip ${f.businessModel===value?'active':''}" data-filter="businessModel" data-value="${value}">${esc(label)}</button>`;
  return `<div class="actor-filters">
    <div class="filter-group"><label>Classe</label><div class="filter-chips">${classChip("","Tous")}${classChip("C1","C1")}${classChip("C2","C2")}${classChip("T1","T1")}</div></div>
    <div class="filter-group"><label>Type</label><select id="filter-actor-type"><option value="">Tous</option>${types.map(t => `<option value="${esc(t)}" ${f.actorType===t?'selected':''}>${esc(ACTOR_TYPE_LABELS[t]||t)}</option>`).join("")}</select></div>
    <div class="filter-group"><label>Pays</label><select id="filter-country"><option value="">Tous</option>${countries.map(c => `<option value="${esc(c)}" ${f.country===c?'selected':''}>${esc(c)}</option>`).join("")}</select></div>
    <div class="filter-group"><label>Modèle</label><div class="filter-chips">${modelChip("","Tous")}${modelChip("service","Service")}${modelChip("equipment","Équipement")}${modelChip("research","Recherche")}</div></div>
    <label class="filter-priority"><input type="checkbox" id="filter-priority-only" ${f.priorityOnly?'checked':''}> ★ Prioritaires uniquement</label>
  </div>`;
}

function actorMatchesFilters(a) {
  const f = state.actorFilters;
  if (f.competitiveClass && a.competitive_class !== f.competitiveClass) return false;
  if (f.actorType && a.actor_type !== f.actorType) return false;
  if (f.country && a.country !== f.country) return false;
  if (f.businessModel && !(a.business_models || []).includes(f.businessModel)) return false;
  if (f.priorityOnly && !a.priority) return false;
  return true;
}

function renderActors() {
  const q = state.query.toLowerCase();
  const filtered = state.actors
    .filter(a => `${a.name} ${a.country} ${a.role}`.toLowerCase().includes(q))
    .filter(actorMatchesFilters);
  const nonReference = state.actors.filter(a => !a.is_reference);
  const countFor = cls => nonReference.filter(a => a.competitive_class === cls && a.review_status === "verified").length;
  const toVerifyCount = nonReference.filter(a => a.review_status === "candidate" || a.review_status === "monitor").length;
  const priorityCount = nonReference.filter(a => a.priority).length;
  const summaryLine = `${countFor("C1")} C1 directs · ${countFor("C2")} C2 significatifs · ${countFor("T1")} T1 centres / recherche · ${toVerifyCount} à vérifier · ${priorityCount} prioritaires`;
  const references = filtered.filter(a => a.is_reference);
  const pendingReview = filtered.filter(a => a.review_status === "candidate" || a.review_status === "monitor");
  const categories = groupActorsByCategory(filtered);
  const referenceSection = references.length
    ? `<div class="actor-category reference"><p>Références internes<small>${references.length}</small></p><div class="actor-grid">${references.map(actorCard).join("")}</div></div>`
    : "";
  const categorySections = categories.map(([category, list]) =>
    `<div class="actor-category"><p>${esc(category)}<small>${list.length}</small></p><div class="actor-grid">${list.map(actorCard).join("")}</div></div>`
  ).join("");
  const pendingSection = pendingReview.length
    ? `<section><div class="section-title"><div><span>—</span><div><h2>En attente de validation</h2><p>Signaux émergents dont la preuve est encore insuffisante pour compter comme concurrent.</p></div></div><b>${pendingReview.length}</b></div><div class="review-grid">${pendingReview.map(reviewActorCard).join("")}</div></section>`
    : "";
  content.innerHTML = header("Écosystème suivi",`${nonReference.length} acteurs suivis`,"HEF et IREIS restent hors benchmark comme références internes.",`<div class="header-actions"><button class="export-btn" data-open-actor-add>+ Ajouter un acteur</button><button class="primary" data-run="actors">↻ Mettre à jour</button></div>`)+
  `<p class="actor-summary-counts">${esc(summaryLine)}</p>
   ${pendingSection}
   ${acquisitionsSection(filtered)}
   <div class="actor-toolbar"><input id="actor-search" value="${esc(state.query)}" placeholder="Rechercher un acteur, un pays ou un rôle…"><span>${filtered.length} résultats</span></div>
   ${actorFilterBar()}
   <section><div class="section-title"><div><span>01</span><div><h2>Répartition par classe concurrentielle</h2></div></div></div>${categorySections || '<div class="empty">Aucun acteur ne correspond à cette recherche.</div>'}${referenceSection}</section>
   ${networkSection()}`;
  const input = document.querySelector("#actor-search");
  if (input) {
    input.focus({preventScroll:true});
    input.setSelectionRange(input.value.length, input.value.length);
    input.addEventListener("input", debounce(e => {
      state.query = e.target.value;
      renderActors();
    }));
  }
  document.querySelectorAll(".actor-filters [data-filter]").forEach(btn => btn.addEventListener("click", () => {
    state.actorFilters[btn.dataset.filter] = btn.dataset.value;
    renderActors();
  }));
  const typeSelect = document.querySelector("#filter-actor-type");
  if (typeSelect) typeSelect.addEventListener("change", e => { state.actorFilters.actorType = e.target.value; renderActors(); });
  const countrySelect = document.querySelector("#filter-country");
  if (countrySelect) countrySelect.addEventListener("change", e => { state.actorFilters.country = e.target.value; renderActors(); });
  const priorityCheckbox = document.querySelector("#filter-priority-only");
  if (priorityCheckbox) priorityCheckbox.addEventListener("change", e => { state.actorFilters.priorityOnly = e.target.checked; renderActors(); });
  document.querySelector("[data-open-actor-add]")?.addEventListener("click", showActorAdd);
  wireActions();
}

function showActorAdd() {
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">Nouvel acteur</p>
    <h2>Ajouter un acteur</h2>
    <form id="actor-add-form" class="detail-form">
      <label>Nom<input type="text" name="name" placeholder="Nom de l'acteur" required></label>
      <label>Pays<input type="text" name="country" placeholder="Pays" required></label>
      <label>Rôle<input type="text" name="role" placeholder="Rôle" required></label>
      <label>Site officiel<input type="url" name="official_url" placeholder="https://site-officiel.example" required></label>
      <label class="checkbox-row"><input type="checkbox" name="priority"> Prioritaire</label>
      <div class="detail-form-actions"><button type="button" data-close-dialog>Annuler</button><button type="submit" class="primary">Ajouter</button></div>
    </form>`;
  dialog.classList.remove("wide");
  dialog.showModal();
  document.querySelector("#actor-add-form").addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(e.target);
    try {
      await api("/api/actors", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: data.get("name"), country: data.get("country"), role: data.get("role"),
          official_url: data.get("official_url"), priority: data.get("priority") === "on",
        }),
      });
      toast("Acteur ajouté.");
      dialog.close();
      state.actors = await api("/api/actors");
      renderActors();
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
}

function dimensionLabel(dimension) {
  return {market: "Marché", component: "Composant", operation: "Opération"}[dimension] || dimension;
}

function vocabCard(item) {
  const dims = Object.entries(item.proposed_labels || {});
  const context = Object.entries(item.resolved_labels || {}).map(([k, v]) => `${dimensionLabel(k)} : ${v}`).join(" · ");
  return `<article class="vocab-card">
    <header><span>${esc(item.actor_name)} · ${dateLabel(item.last_seen_at)}</span>${context ? `<span>${esc(context)}</span>` : ""}</header>
    <blockquote>${esc(item.quote)}</blockquote>
    <div class="vocab-dims">
      ${dims.map(([dimension, label]) => `<div class="vocab-dim"><small>${esc(dimensionLabel(dimension))}</small><b>${esc(label)}</b><button class="vocab-accept" data-accept-vocab="${item.id}" data-dimension="${esc(dimension)}">Accepter</button></div>`).join("")}
      <button class="vocab-reject" data-reject-vocab="${item.id}">Rejeter</button>
    </div>
    <a class="signal-link" href="${esc(item.source_url)}" target="_blank" rel="noopener">Voir la source ↗</a>
  </article>`;
}

function renderVocabulary() {
  const items = state.vocabulary || [];
  content.innerHTML = header(
    "Validation",
    "Détections IA à valider",
    "Libellés marché/composant/opération proposés par le modèle mais absents du lexique connu. Accepter un libellé l’ajoute au lexique vivant, utilisable dès la prochaine collecte marché — sans déploiement de code."
  ) +
  (items.length
    ? `<div class="vocab-list">${items.map(vocabCard).join("")}</div>`
    : `<div class="empty">Aucune proposition en attente de revue.</div>`);
  wireActions();
}

function dbCard(kind,label,file,count,detail,paused=false) {
  const last=state.overview?.[kind]?.last_run;
  const job=state.overview?.jobs?.[kind];
  return `<article class="db-card ${kind}"><div class="db-icon">▤</div><div class="db-title"><div><small>${file}</small><h2>${label}</h2></div><span class="status ${paused?'paused':'active'}">${paused?'En pause':'Active'}</span></div><strong>${count}</strong><p>${detail}</p><div class="db-meta"><span>Dernière collecte</span><b>${dateLabel(last?.finished_at)}</b></div><button data-run="${kind}" ${job?.status==='running'?'disabled':''}>${job?.status==='running'?'<i class="spinner"></i>Collecte en cours…':'↻ Lancer la collecte'}</button></article>`;
}

function renderCollections() {
  const o=state.overview;
  const priorityProfiles=state.profiles.filter(p=>p.priority);
  const sections=[
    ['service','Services'],['capability','Capacités'],['technology','Technologies'],
    ['application','Applications'],['market','Marchés'],['project','Projets'],['news','Actualités']
  ];
  const sectionStates=profile=>`<div class="section-states">${sections.map(([key,label])=>{const item=profile.coverage?.[key];const status=item?.status||'missing';const symbol=status==='ready'?'✓':status==='error'?'!':status==='pending'?'◷':'—';return `<span class="section-state ${status}" title="${item?.ready||item?.usable||0} page(s) exploitable(s) sur ${item?.discovered||0} découverte(s)">${symbol} ${label}</span>`}).join('')}</div>`;
  content.innerHTML = header("Pilotage des données","Bases & collectes","Le crawler couvre d’abord les familles stratégiques du site, puis approfondit les meilleures sources.")+
  `<div class="db-grid">${dbCard("actors","Acteurs","actors.db",`${o.actors.count} acteurs`,`${o.actors.priority} prioritaires · ${o.actors.count-o.actors.priority} suivis`)}${dbCard("market","Marché & offres","market.db",`${o.market.existing+o.market.radar} applications`,`${o.market.offers||0} offres/capacités · ${o.market.proof_sources||0} sources de preuve`)}${dbCard("technology","Technologie","technology.db",`${o.technology.documents} documents`,`Planification en pause`,true)}</div>
   <section class="adaptive-panel"><div class="adaptive-heading"><div><p class="eyebrow">Couverture déterministe</p><h2>Profils des acteurs prioritaires</h2></div><span class="ollama ${o.adaptive.ollama_available?'online':'offline'}">${o.adaptive.ollama_available?`${aiProviderName(o.adaptive)} · ${esc(o.adaptive.model)}${aiCostSuffix(o.adaptive)}`:`${aiProviderName(o.adaptive)} indisponible · crawler autonome`}</span></div><div class="profile-grid">${priorityProfiles.map(p=>`<article><div><strong>${esc(p.name)}</strong><span class="profile-badge ${p.needs_reprofile?'warning':p.strategy}">${p.needs_reprofile?'À recalibrer':p.strategy==='adaptive'?'Adaptatif':'Générique'}</span></div><p>${p.status==='ready'?'Rubriques stratégiques exploitables':p.status==='partial'?'Couverture partielle':p.status==='degraded'?'Aucune rubrique stratégique exploitable':'Cartographie au prochain lancement'}</p>${sectionStates(p)}<small>${p.last_profiled_at?`Dernière analyse : ${dateLabel(p.last_profiled_at)}`:'Pas encore analysé'}</small></article>`).join("")}</div></section>
   <div class="rule-note"><strong>Règle de séparation</strong><p>Le crawler collecte les pages et reconstruit leurs blocs. La vue Marché exige marché + composant + opération explicitement reliés. Les pages de service, technologie ou savoir-faire qui ne portent pas de marché explicite sont conservées séparément dans « Offres & capacités ».</p></div>
   ${pipelineFunnelPanel()}
   ${duplicatesPanel()}`;
  wireActions();
}

function pipelineFunnelPanel() {
  const funnel = state.pipelineFunnel || {};
  const stages = [
    {key: "discovered", label: "Découvertes"},
    {key: "fetched", label: "Récupérées"},
    {key: "parsed", label: "Analysées"},
    {key: "evidence", label: "Preuves"},
    {key: "validated", label: "Validées"},
  ];
  const total = funnel.discovered || 1;
  const cells = stages.map(stage => {
    const value = Number(funnel[stage.key] || 0);
    return `<div class="funnel-stage"><strong>${value}</strong><span>${stage.label}</span><small>${Math.round(100 * value / total)}%</small></div>`;
  }).join(`<div class="funnel-arrow">→</div>`);
  return `<section><div class="section-title"><div><span>▤</span><div><h2>Pipeline de preuve</h2><p>Une page découverte n'est pas encore une preuve : voici combien deviennent réellement un fait exploitable, jusqu'à validation.</p></div></div></div>
    <div class="funnel-row">${cells}</div>
  </section>`;
}

function duplicatesPanel() {
  const pairs = state.duplicates || [];
  if (!pairs.length) return "";
  return `<section><div class="section-title"><div><span>!</span><div><h2>Doublons potentiels</h2><p>Même domaine officiel, même maison mère ou noms identiques une fois la forme juridique retirée. Aucune fusion automatique — à vérifier manuellement.</p></div></div><b>${pairs.length}</b></div>
    <div class="dup-list">${pairs.map(pair => `<article class="dup-row"><div><strong>${esc(pair.actor_a)}</strong> ↔ <strong>${esc(pair.actor_b)}</strong></div><span>${pair.reasons.map(esc).join(" · ")}</span></article>`).join("")}</div>
  </section>`;
}

function renderSettings() {
  const adaptive=state.overview.adaptive;
  content.innerHTML=header("Configuration","Paramètres","Règles de collecte et de publication de l’observatoire.")+`<div class="settings">
    <article><div><h3>Crawler déterministe</h3><p>Découverte coverage-first, scoring des URLs, profils génériques et overrides spécifiques.</p></div><b class="ok">Actif</b></article>
    <article><div><h3>Analyse sémantique ${esc(aiProviderName(adaptive))}</h3><p>Modèle ${esc(adaptive.model)}${esc(aiCostSuffix(adaptive))} · réservé à l’interprétation sémantique des blocs ambiguës, pas au pilotage principal du crawl.</p></div><b class="${adaptive.ollama_available?'ok':'warn'}">${adaptive.ollama_available?'Disponible':'Secours absent'}</b></article>
    <article><div><h3>Recalibrage des profils</h3><p>Déclenché lorsqu’une structure devient inexploitable ou produit des erreurs répétées.</p></div><b>${adaptive.needs_reprofile} à recalibrer</b></article>
    <article><div><h3>Condition d’affichage Marché</h3><p>Marché + pièce/composant + opération laser explicitement reliés, avec source vérifiable.</p></div><b>Strict</b></article>
    <article><div><h3>Offres & capacités</h3><p>Les procédés et savoir-faire sont conservés sans forcer un marché ou un composant absent du contenu.</p></div><b>Separé</b></article>
    <article><div><h3>Déduplication multilingue</h3><p>FR, EN ou DE peuvent documenter le même fait ; elles sont stockées comme sources d’un fait canonique unique.</p></div><b>Active</b></article>
  </div>`;
}

function render(){
  if(state.view==="monthly") renderMonthly();
  if(state.view==="market") renderMarket();
  if(state.view==="offers") renderOffers();
  if(state.view==="actors") renderActors();
  if(state.view==="vocabulary") renderVocabulary();
  if(state.view==="collections") renderCollections();
  if(state.view==="settings") renderSettings();
}

async function toggleActorActive(id, nextActive) {
  try {
    await api(`/api/actors/${id}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({active: nextActive}),
    });
    toast(nextActive ? "Acteur réactivé." : "Acteur mis en pause.");
    state.actors = await api("/api/actors");
    renderActors();
  } catch (error) { toast(error.message); }
}

async function decideVocabulary(id, action, dimension) {
  try {
    if (action === "accept") {
      await api(`/api/vocabulary-candidates/${id}/accept`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({dimension}),
      });
      toast("Libellé ajouté au lexique.");
    } else {
      await api(`/api/vocabulary-candidates/${id}/reject`, {method: "POST"});
      toast("Proposition rejetée.");
    }
    state.vocabulary = await api("/api/vocabulary-candidates");
    renderVocabulary();
  } catch (error) { toast(error.message); }
}

async function showProofs(row) {
  const qs=new URLSearchParams({bucket:row.bucket,market:row.market,component:row.component,operation:row.operation});
  const proofs=await api(`/api/market/proofs?${qs}`);
  document.querySelector("#proof-content").innerHTML=`<p class="eyebrow">${esc(row.market)}</p><h2>${esc(row.component)}</h2><p class="dialog-operation">${esc(row.operation)}</p>${proofs.map(p=>`<article class="proof"><div><strong>${esc(p.actor_name)}</strong><span>${esc(p.industrial_stage)}</span></div>${p.language?`<small class="source-language">${esc(String(p.language).toUpperCase())}</small>`:''}${p.block_heading?`<small class="block-label">Bloc : ${esc(p.block_heading)}</small>`:''}${[p.laser_process,p.material,p.performance].filter(Boolean).length?`<small class="block-label">${[p.laser_process,p.material,p.performance].filter(Boolean).map(esc).join(' · ')}</small>`:''}${p.relation_strength?`<small class="block-label">Relation : ${esc(p.relation_strength==='direct'?'directe':'contextuelle')}${p.source_role?` · source : ${esc(p.source_role)}`:''}</small>`:''}<blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
  dialog.classList.remove("wide");
  dialog.showModal();
}

async function showOfferProofs(offerId) {
  const proofs = await api(`/api/offers/${offerId}/proofs`);
  if (!proofs.length) {
    document.querySelector("#proof-content").innerHTML = `<div class="empty">Aucune source disponible.</div>`;
    dialog.classList.remove("wide");
    dialog.showModal();
    return;
  }
  const first = proofs[0];
  document.querySelector("#proof-content").innerHTML=`<p class="eyebrow">${esc(first.actor_name)}</p><h2>${esc(first.capability)}</h2><p class="dialog-operation">${esc(offerTypeLabel(first.offer_type))}</p>${proofs.map(p=>`<article class="proof"><div><strong>${esc(p.operation || p.laser_process || p.capability)}</strong><span>${esc(p.industrial_stage || '')}</span></div>${p.language?`<small class="source-language">${esc(String(p.language).toUpperCase())}</small>`:''}${p.block_heading?`<small class="block-label">Bloc : ${esc(p.block_heading)}</small>`:''}${[p.laser_process,p.material,p.performance].filter(Boolean).length?`<small class="block-label">${[p.laser_process,p.material,p.performance].filter(Boolean).map(esc).join(' · ')}</small>`:''}${p.relation_strength?`<small class="block-label">Relation : ${esc(p.relation_strength==='direct'?'directe':'contextuelle')}${p.source_role?` · source : ${esc(p.source_role)}`:''}</small>`:''}<blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
  dialog.classList.remove("wide");
  dialog.showModal();
}

function collectionSummary(kind, result) {
  if (!result) return "Collecte terminée.";
  if (kind === "monthly") {
    const market = result.market || {};
    const technology = result.technology || {};
    return `Veille actualisée · ${Number(market.market_added || 0)} faits marché · ${Number(market.offers_added || 0)} capacités · ${Number(technology.added || 0)} documents tech.`;
  }
  if (kind === "market") {
    return `Marché actualisé · ${Number(result.market_added || 0)} faits · ${Number(result.offers_added || 0)} capacités · ${Number(result.errors || 0)} erreur(s).`;
  }
  if (kind === "actors") {
    return `Acteurs actualisés · ${Number(result.scanned || 0)} page(s) analysée(s) · ${Number(result.errors || 0)} erreur(s).`;
  }
  if (kind === "technology") {
    return `Technologie actualisée · ${Number(result.added || 0)} nouveau(x) document(s) · ${Number(result.errors || 0)} erreur(s).`;
  }
  return "Collecte terminée.";
}

async function run(kind) {
  try {
    await api(`/api/scrape/${kind}`,{method:"POST"});
    toast(`Collecte ${kind} lancée.`);
    state.view="collections";
    document.querySelectorAll(".nav").forEach(n => n.classList.toggle("active", n.dataset.view === "collections"));
    await refresh();
    poll(kind);
  } catch(error) { toast(error.message); }
}

async function poll(kind) {
  const tick = async () => {
    try {
      const job = await api(`/api/scrape/${kind}`);
      if (state.overview?.jobs) state.overview.jobs[kind] = job;
      if (state.view === "collections") renderCollections();

      if (job.status === "running") {
        window.setTimeout(tick, 1800);
        return;
      }

      await refresh();
      if (kind === "monthly" && job.status === "completed") {
        state.view = "monthly";
        document.querySelectorAll(".nav").forEach(n => n.classList.toggle("active", n.dataset.view === "monthly"));
        render();
      }
      toast(job.status === "completed"
        ? collectionSummary(kind, job.result)
        : `Échec : ${job.error || "erreur inconnue"}`);
    } catch (error) {
      toast(`Suivi de collecte impossible : ${error.message}`);
      window.setTimeout(tick, 4000);
    }
  };
  window.setTimeout(tick, 900);
}

function wireActions(){
  document.querySelectorAll("[data-run]").forEach(button=>button.addEventListener("click",()=>run(button.dataset.run)));
  document.querySelectorAll("[data-proof]").forEach(button=>button.addEventListener("click",()=>showProofs(JSON.parse(button.dataset.proof))));
  document.querySelectorAll("[data-offer-proof]").forEach(button=>button.addEventListener("click",()=>showOfferProofs(Number(button.dataset.offerProof))));
  document.querySelectorAll("[data-toggle-actor]").forEach(button=>button.addEventListener("click",()=>toggleActorActive(Number(button.dataset.toggleActor), button.dataset.nextActive==="1")));
  document.querySelectorAll("[data-accept-vocab]").forEach(button=>button.addEventListener("click",()=>decideVocabulary(Number(button.dataset.acceptVocab),"accept",button.dataset.dimension)));
  document.querySelectorAll("[data-reject-vocab]").forEach(button=>button.addEventListener("click",()=>decideVocabulary(Number(button.dataset.rejectVocab),"reject")));
  document.querySelectorAll("[data-review-actor]").forEach(button=>button.addEventListener("click",()=>decideActorReview(Number(button.dataset.reviewActor), button.dataset.reviewStatus)));
  document.querySelectorAll("[data-actor-detail]").forEach(el=>el.addEventListener("click",()=>showActorDetail(Number(el.dataset.actorDetail))));
  document.querySelectorAll("[data-actor-edit]").forEach(el=>el.addEventListener("click",()=>showActorEdit(Number(el.dataset.actorEdit))));
  document.querySelectorAll("[data-actor-toggle-priority]").forEach(el=>el.addEventListener("click",()=>toggleActorPriority(Number(el.dataset.actorTogglePriority), el.dataset.nextPriority==="1")));
  document.querySelectorAll("[data-actor-delete]").forEach(el=>el.addEventListener("click",()=>deleteActorWithConfirm(Number(el.dataset.actorDelete), el.dataset.actorName)));
}

document.querySelectorAll(".nav").forEach(button=>button.addEventListener("click",()=>{
  document.querySelectorAll(".nav").forEach(n=>n.classList.remove("active"));
  button.classList.add("active");
  state.view=button.dataset.view;
  render();
}));

document.querySelector(".dialog-close").addEventListener("click",()=>dialog.close());
dialog.addEventListener("click",e=>{if(e.target===dialog)dialog.close()});

async function refresh(){
  [state.overview,state.monthly,state.market,state.offers,state.actors,state.profiles,state.vocabulary,state.network,state.duplicates,state.pipelineFunnel]=await Promise.all([
    api("/api/overview"),
    api("/api/monthly?days=30"),
    api("/api/market"),
    api("/api/offers"),
    api("/api/actors"),
    api("/api/profiles"),
    api("/api/vocabulary-candidates"),
    api("/api/network"),
    api("/api/actors/duplicates"),
    api("/api/pipeline-funnel"),
  ]);
  render();
}

refresh().catch(error=>{
  content.innerHTML=`<div class="empty">Impossible de charger l’application : ${esc(error.message)}</div>`;
});
