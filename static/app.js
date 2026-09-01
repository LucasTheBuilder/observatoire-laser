const state = {
  view: "monthly",
  overview: null,
  monthly: null,
  market: null,
  offers: [],
  technologySignals: [],
  documents: [],
  actors: [],
  profiles: [],
  vocabulary: [],
  marketReview: [],
  actorDiscovery: [],
  reviewOffers: [],
  reviewEvents: [],
  collectionHealth: null,
  schedulerStatus: null,
  veilleMetrics: null,
  digest: null,
  demandSignals: [],
  marketSizing: [],
  referenceMatrix: [],
  dataQuality: null,
  goldenFacts: [],
  network: {nodes: [], edges: []},
  duplicates: [],
  pipelineFunnel: {discovered: 0, fetched: 0, parsed: 0, evidence: 0, validated: 0},
  // Score d'attractivité par marché (audit Horizon 2 #14, voir scoring.py) -- chargé en bloc
  // avec le reste (petite liste, un par marché connu), contrairement aux séries temporelles.
  marketScores: [],
  query: "",
  offerQuery: "",
  offerDrill: {family: null, level2: null, level3: null},
  marketDrill: null,
  actorFilters: {competitiveClass: "", actorType: "", country: "", businessModel: "", priorityOnly: false},
  // Séries temporelles (audit Horizon 2 #12) : chargées à la demande (pas dans refresh()) --
  // 85+ clés possibles (acteurs+marchés+axes), un fetch par clé au clic évite un chargement
  // initial disproportionné. keys/points restent en cache tant que la dimension/clé ne change
  // pas, et sont resynchronisés après toute collecte terminée (voir poll()).
  trends: {dimension: "signal", key: "__global__", keys: [], points: [], loading: false},
};

const content = document.querySelector("#content");
const dialog = document.querySelector("#proof-dialog");
// Calling showModal() on an already-open <dialog> is spec-invalid (throws InvalidStateError) --
// a real path here is clicking a second proof pill before closing the first. Guarding once at
// the source protects every call site instead of repeating the check at each of them.
const nativeDialogShowModal = dialog.showModal.bind(dialog);
dialog.showModal = () => { if (!dialog.open) nativeDialogShowModal(); };

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

// Score d'intensité concurrentielle par marché (audit Horizon 2 #14, voir scoring.py:
// compute_competitive_intensity_scores ; renommé depuis "attractivité" le 30/08/2026, audit
// veille §10.4 -- ce score ne mesure que l'offre concurrente déjà présente, un marché encombré
// y ressort "haut", ce qui est l'inverse d'une attractivité business). Absent (pas de badge)
// pour un marché sans faits validés -- jamais un score fabriqué à 0 pour "pas encore de données".
function intensityLevel(score) {
  if (score >= 55) return "good";
  if (score >= 25) return "partial";
  return "weak";
}

function intensityBadge(market) {
  const entry = (state.marketScores || []).find(m => m.market === market);
  if (!entry) return "";
  const level = intensityLevel(entry.intensity_score);
  const title = `Intensité concurrentielle : ${entry.existing} fait(s) existant(s), ${entry.radar} radar, ${entry.actors_count} acteur(s) actif(s), ${Math.round(entry.production_share * 100)}% en stade Production/Industrialisation. Mesure l'offre déjà présente, pas la demande.`;
  // §10.4/§0.5 audit veille (30/08/2026) : "afficher les composantes" -- un agrégat seul masque
  // ce qu'il mesure réellement (l'offre déjà présente, pas la demande). Visible en texte, pas
  // seulement au survol (title reste en complément, pour le détail complet).
  return `<span class="attractiveness-badge lvl-${level}" title="${esc(title)}">${Math.round(entry.intensity_score)} intensité</span>
    <small class="block-label">${entry.existing} existant · ${entry.radar} radar · ${entry.actors_count} acteur${entry.actors_count > 1 ? "s" : ""} · ${Math.round(entry.production_share * 100)}% industrialisé</small>`;
}

function marketFamilyCards(rows) {
  const groups = groupByMarket(rows);
  if (!groups.length) return `<div class="empty">Pas encore assez de faits pour une lecture par marché.</div>`;
  return `<div class="market-fam-grid">${groups.map(([market, bucket]) => {
    const chips = [...bucket.subthemes.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6)
      .map(([label, count]) => `<span class="subtheme-chip">${esc(label)}<b>${count}</b></span>`).join("");
    return `<button class="market-fam-card market-fam-card-link" data-drill-market="${esc(market)}"><header>${marketIcon(market)}<div><h3>${esc(market)}</h3><b>${bucket.total} faits</b></div></header>${intensityBadge(market)}<div class="subtheme-chips">${chips}</div></button>`;
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

// §4.B.3 audit veille (30/08/2026, Lot 4 §15) : "un cahier des charges mentionnant du
// micro-usinage femtoseconde est un signal d'achat, pas un signal de discours" -- appels
// d'offres publics (TED/BOAMP) trouvés par demand_signals.py, le seul endroit de market.db qui
// documente ce que le marché ACHÈTE plutôt que ce que les acteurs suivis DISENT faire.
function demandSignalCard(item) {
  return `<article class="vocab-card">
    <header><span>${esc(item.buyer_name || "Acheteur non précisé")} · ${dateLabel(item.published_at)}</span><span>${esc(item.source)}</span></header>
    <p class="dialog-operation">${esc(item.title)}</p>
    <a class="signal-link" href="${esc(item.source_url)}" target="_blank" rel="noopener">Voir l'avis ↗</a>
  </article>`;
}

function demandSignalsPanel() {
  const items = state.demandSignals || [];
  return `<section><div class="section-title"><div><span>04</span><div><h2>Signaux de demande</h2><p>Appels d'offres publics (TED, BOAMP) mentionnant du laser ultra-rapide -- un signal d'achat réel, pas une déclaration d'un acteur suivi.</p></div></div><b>${items.length}</b></div>
    ${items.length ? `<div class="vocab-list">${items.map(demandSignalCard).join("")}</div>` : `<div class="empty">Aucun signal de demande sur la fenêtre couverte.</div>`}
  </section>`;
}

// §4.B.2 audit veille (30/08/2026, Lot 4 §14) : "les chiffres de marché laser publiés mélangent
// allègrement machines, services et composants -- jamais moyennée entre sources, toujours
// affichée avec son périmètre." CRUD sourcé, jamais de collecteur (market_sizing.py) -- vide
// tant qu'un humain n'a pas saisi une vraie figure depuis un rapport/communiqué réel.
function marketSizingCard(item) {
  return `<article class="vocab-card">
    <header><span>${esc(item.market)} · ${item.year}</span><span>${esc(item.currency)} ${item.value.toLocaleString('fr-FR')}${item.cagr != null ? ` · CAGR ${(item.cagr * 100).toFixed(1)}%` : ""}</span></header>
    <p class="dialog-operation">${esc(item.scope)}</p>
    ${item.method ? `<small class="block-label">Méthode : ${esc(item.method)}</small>` : ""}
    <div class="vocab-dims" style="margin-top:12px"><button class="vocab-reject" data-delete-market-sizing="${item.id}">✕ Retirer</button></div>
    <a class="signal-link" href="${esc(item.source_url)}" target="_blank" rel="noopener">Voir la source ↗</a>
  </article>`;
}

function marketSizingPanel() {
  const items = state.marketSizing || [];
  return `<section><div class="section-title"><div><span>05</span><div><h2>Taille de marché</h2><p>Saisie sourcée depuis des rapports publics/communiqués d'analystes -- jamais moyennée entre sources, toujours affichée avec son périmètre exact.</p></div></div><b>${items.length}</b></div>
    <button class="export-btn" data-open-market-sizing-add>+ Ajouter une taille de marché</button>
    ${items.length ? `<div class="vocab-list" style="margin-top:14px">${items.map(marketSizingCard).join("")}</div>` : `<div class="empty">Aucune taille de marché saisie.</div>`}
  </section>`;
}

function showMarketSizingAdd() {
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">Nouvelle entrée</p>
    <h2>Ajouter une taille de marché</h2>
    <form id="market-sizing-add-form" class="detail-form">
      <label>Marché<input type="text" name="market" placeholder="ex: Médical" required></label>
      <label>Périmètre exact<input type="text" name="scope" placeholder="ex: machines uniquement, hors services" required></label>
      <label>Valeur<input type="number" step="any" name="value" required></label>
      <label>Devise<input type="text" name="currency" value="EUR" required></label>
      <label>Année<input type="number" name="year" required></label>
      <label>CAGR (optionnel, ex: 0.08 pour 8%)<input type="number" step="any" name="cagr"></label>
      <label>Méthode (optionnel)<input type="text" name="method"></label>
      <label>URL source<input type="url" name="source_url" placeholder="https://..." required></label>
      <div class="detail-form-actions"><button type="button" data-close-dialog>Annuler</button><button type="submit" class="primary">Ajouter</button></div>
    </form>`;
  dialog.classList.remove("wide");
  dialog.showModal();
  document.querySelector("#market-sizing-add-form").addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(e.target);
    try {
      await api("/api/market-sizing", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          market: data.get("market"), scope: data.get("scope"), value: Number(data.get("value")),
          currency: data.get("currency"), year: Number(data.get("year")),
          cagr: data.get("cagr") ? Number(data.get("cagr")) : null,
          method: data.get("method") || null, source_url: data.get("source_url"),
        }),
      });
      toast("Taille de marché ajoutée.");
      dialog.close();
      state.marketSizing = await api("/api/market-sizing");
      renderMarket();
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
}

async function deleteMarketSizing(entryId) {
  try {
    await api(`/api/market-sizing/${entryId}`, {method: "DELETE"});
    toast("Entrée retirée.");
    state.marketSizing = await api("/api/market-sizing");
    renderMarket();
  } catch (error) { toast(error.message); }
}

// §4.B.1 audit veille (Lot 4 §13) : une cellule déclarée ici est une AMBITION humaine
// (marché x composant x opération qui DEVRAIT exister), indépendante de ce qu'evidence a
// réellement observé -- reference_matrix.py calcule la couverture en comparant les deux.
function referenceCellRow(cell) {
  return `<article class="dup-row"><div><strong>${esc(cell.market)}</strong> · ${esc(cell.component)} · ${esc(cell.operation)}</div><span>${cell.covered ? `✓ Couverte (${cell.covered_bucket === "existing" ? "existant" : "radar"})` : "○ Zone blanche"}${cell.rationale ? ` · ${esc(cell.rationale)}` : ""} <button class="vocab-reject" data-delete-reference-cell="${cell.id}" style="margin-left:8px">✕</button></span></article>`;
}

function referenceMatrixPanel() {
  const cells = state.referenceMatrix || [];
  const covered = cells.filter(c => c.covered).length;
  const whiteSpace = cells.filter(c => !c.covered);
  return `<section><div class="section-title"><div><span>06</span><div><h2>Matrice de référence & zones blanches</h2><p>Cellules marché x composant x opération déclarées comme pertinentes par le métier -- indépendant de ce qui a déjà été observé.</p></div></div><b>${cells.length ? `${covered}/${cells.length}` : 0}</b></div>
    <button class="export-btn" data-open-reference-cell-add>+ Déclarer une cellule</button>
    ${cells.length ? `<div class="dup-list" style="margin-top:14px">${cells.map(referenceCellRow).join("")}</div>` : `<div class="empty">Aucune cellule de référence déclarée.</div>`}
    ${whiteSpace.length ? `<p class="actor-summary-counts" style="margin-top:10px">${whiteSpace.length} zone${whiteSpace.length > 1 ? "s" : ""} blanche${whiteSpace.length > 1 ? "s" : ""} -- déclarée${whiteSpace.length > 1 ? "s" : ""} mais sans fait observé.</p>` : ""}
  </section>`;
}

function showReferenceCellAdd() {
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">Nouvelle cellule</p>
    <h2>Déclarer une cellule de référence</h2>
    <form id="reference-cell-add-form" class="detail-form">
      <label>Marché<input type="text" name="market" placeholder="ex: Médical" required></label>
      <label>Composant<input type="text" name="component" placeholder="ex: Stents" required></label>
      <label>Opération<input type="text" name="operation" placeholder="ex: Microperçage" required></label>
      <label>Justification (optionnel)<textarea name="rationale" rows="3"></textarea></label>
      <div class="detail-form-actions"><button type="button" data-close-dialog>Annuler</button><button type="submit" class="primary">Déclarer</button></div>
    </form>`;
  dialog.classList.remove("wide");
  dialog.showModal();
  document.querySelector("#reference-cell-add-form").addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(e.target);
    try {
      await api("/api/reference-matrix", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          market: data.get("market"), component: data.get("component"), operation: data.get("operation"),
          rationale: data.get("rationale") || null,
        }),
      });
      toast("Cellule déclarée.");
      dialog.close();
      state.referenceMatrix = await api("/api/reference-matrix");
      renderMarket();
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
}

async function deleteReferenceCell(cellId) {
  try {
    await api(`/api/reference-matrix/${cellId}`, {method: "DELETE"});
    toast("Cellule retirée.");
    state.referenceMatrix = await api("/api/reference-matrix");
    renderMarket();
  } catch (error) { toast(error.message); }
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
   <section><div class="section-title"><div><span>03</span><div><h2>Radar applications et besoins</h2><p>Applications documentées dont l’industrialisation reste à confirmer.</p></div></div><b>${radarRows.length} faits</b></div>${evidenceTable(radarRows, {hideMarketColumn: !!selected, emptyMessage: selected ? `Aucune application radar documentée pour ${selected}.` : undefined})}</section>
   ${demandSignalsPanel()}
   ${marketSizingPanel()}
   ${referenceMatrixPanel()}`;
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


// --- Synthèse decision-support helpers (UX audit item 11) -----------------------------------
// Each of these answers one of the audit's "10-minute questions" using data already in the
// system -- no invented score, no fabricated recommendation. Two of the audit's questions
// ("quel marché monte" needs real history, not one snapshot; "qui prospecter" turned out to mean
// end-customer companies, an entity type that doesn't exist yet) are intentionally left out of
// this pass rather than faked.

function nextBestActions() {
  const actions = [];
  const weakPriority = (state.actors || [])
    .filter(a => a.priority && !a.is_reference && a.review_status === "verified" && a.coverage_level !== "good")
    .sort((a, b) => (a.coverage_level === "weak" ? 0 : 1) - (b.coverage_level === "weak" ? 0 : 1))
    .slice(0, 3);
  for (const actor of weakPriority) {
    actions.push({
      label: `Enrichir la fiche de ${actor.name}`,
      detail: `Acteur prioritaire, couverture ${actor.coverage_level === "weak" ? "faible" : "partielle"}.`,
      view: "actors",
    });
  }
  const pendingVocab = (state.vocabulary || []).filter(v => v.review_status === "pending").length;
  if (pendingVocab > 0) {
    actions.push({
      label: `Valider ${pendingVocab} terme${pendingVocab > 1 ? "s" : ""} en attente`,
      detail: "File de validation du vocabulaire.",
      view: "vocabulary",
    });
  }
  const pendingCandidates = (state.actorDiscovery || []).length;
  if (pendingCandidates > 0) {
    actions.push({
      label: `Trier ${pendingCandidates} acteur${pendingCandidates > 1 ? "s" : ""} candidat${pendingCandidates > 1 ? "s" : ""}`,
      detail: "Organisations repérées automatiquement, à promouvoir ou rejeter.",
      view: "actor-discovery",
    });
  }
  const pendingMarketReview = (state.marketReview || []).length;
  if (pendingMarketReview > 0) {
    actions.push({
      label: `Trier ${pendingMarketReview} fait${pendingMarketReview > 1 ? "s" : ""} marché en attente`,
      detail: "Faits partiels ou proposés par l'IA, à valider ou rejeter.",
      view: "market-review",
    });
  }
  const pendingReviewQueues = (state.reviewOffers || []).length + (state.reviewEvents || []).length;
  if (pendingReviewQueues > 0) {
    actions.push({
      label: `Trier ${pendingReviewQueues} capacité(s)/événement(s) en attente`,
      detail: "Offres et événements détectés (M&A, financement...), jamais visibles ailleurs tant qu'ils restent ici.",
      view: "market-review",
    });
  }
  const concerningHealth = (state.collectionHealth?.actors || []).filter(a =>
    (a.health_score != null && a.health_score < 70) || a.error_rate > 0.1 || a.needs_reprofile
  ).length;
  if (concerningHealth > 0) {
    actions.push({
      label: `${concerningHealth} acteur${concerningHealth > 1 ? "s" : ""} en difficulté de collecte`,
      detail: "Erreurs répétées ou score de santé faible, potentiellement silencieux jusqu'ici.",
      view: "collections",
    });
  }
  const marketRun = state.overview?.market?.last_run;
  if (marketRun?.finished_at) {
    const days = Math.floor((Date.now() - new Date(marketRun.finished_at).getTime()) / 86400000);
    if (days >= 30) {
      actions.push({
        label: "Relancer la collecte marché",
        detail: `Dernière collecte il y a ${days} jours.`,
        view: "collections",
      });
    }
  }
  return actions;
}

function actionChecklist(actions) {
  if (!actions.length) return `<div class="empty">Rien d’urgent : couverture, validation et collectes sont à jour.</div>`;
  return `<ul class="fam-list">${actions.map(a => `<li><button class="fam-item" data-goto-view="${esc(a.view)}">
      <span class="fam-row-top"><span class="fam-actor">${esc(a.label)}</span></span>
      <span class="fam-cap">${esc(a.detail)}</span>
    </button></li>`).join("")}</ul>`;
}

// "Qui devient plus dangereux et pourquoi": an objective capability-trajectory proxy (count of
// new capacités per actor this period), not a fabricated threat score. T1 actors (centres
// technologiques/instituts) are excluded entirely -- they aren't commercial competitors, so
// counting their activity on the same scale as a C1/C2 competitor would compare incomparable
// things (an audit finding confirmed against this exact ranking). Hybrid C1/C2 actors (both an
// equipment vendor and a service provider) stay in, but tagged with their business model(s) so
// the reader isn't silently comparing a machine-maker's activity to a job-shop's.
function actorActivityRanking(newOffers, actors) {
  const byName = new Map((actors || []).map(a => [a.name, a]));
  const counts = new Map();
  for (const row of newOffers) {
    if (byName.get(row.actor_name)?.competitive_class === "T1") continue;
    counts.set(row.actor_name, (counts.get(row.actor_name) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, count]) => [name, count, byName.get(name)?.business_models || []])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
}

function activityRankingList(ranking) {
  if (!ranking.length) return `<div class="empty">Aucun mouvement de capacité chez les concurrents directs/partiels sur la période.</div>`;
  return `<ol class="rank-list">${ranking.map(([actor, count, models]) => `<li><span class="fam-actor">${esc(actor)}${models.length ? ` <span class="rank-model-tags">(${models.map(m => esc(BUSINESS_MODEL_LABELS[m] || m)).join(", ")})</span>` : ""}</span><span class="fam-proof-pill">${count} nouvelle${count > 1 ? "s" : ""} capacité${count > 1 ? "s" : ""}</span></li>`).join("")}</ol>`;
}

// "Quelles opportunités commerciales": a radar-bucket fact where only one tracked actor is
// active in that market+component pairing -- a competitive whitespace signal, not a confirmed
// opportunity (could just mean under-documented, said explicitly in the section copy).
function marketOpportunities(market) {
  const all = [...(market.existing || []), ...(market.radar || [])];
  const density = new Map();
  for (const row of all) {
    const key = `${row.market}||${row.component}`;
    if (!density.has(key)) density.set(key, new Set());
    density.get(key).add(row.actor_name);
  }
  return (market.radar || []).filter(row => (density.get(`${row.market}||${row.component}`) || new Set()).size === 1);
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

  const actions = nextBestActions();
  const ranking = actorActivityRanking(monthly.new_offers || [], state.actors);
  const opportunities = marketOpportunities(state.market || {existing: [], radar: []});
  const inProgressTech = (state.technologySignals || []).filter(s => s.bucket === "radar").slice(0, 5);
  const techSignalList = inProgressTech.length
    ? `<ul class="fam-list">${inProgressTech.map(row => `<li><button class="fam-item" data-tech-signal-proof="${Number(row.id)}">
        <span class="fam-row-top"><span class="fam-actor">${esc(row.axis)}</span><span class="fam-proof-pill">${esc(row.maturity_stage)}</span></span>
        <span class="fam-cap">${esc(row.project_name || "—")}${row.actor_names.length ? ` · ${row.actor_names.map(esc).join(", ")}` : ""}</span>
      </button></li>`).join("")}</ul>`
    : `<div class="empty">Aucun signal technologique en cours d’industrialisation pour le moment.</div>`;

  content.innerHTML = header(
    "Synthèse",
    `Ce qui a changé sur les ${monthly.days || 30} derniers jours`,
    "Nouveaux faits marché, mouvements concurrents, reconfirmations et signaux technologiques depuis la dernière période.",
    `<button class="primary" data-run="monthly">↻ Actualiser toute la veille</button>`
  ) +
  `<section><div class="section-title"><div><span>00</span><div><h2>Que faire maintenant</h2><p>Actions concrètes disponibles dans l’outil, dérivées de l’état réel de la base — pas une suggestion générique.</p></div></div><b>${actions.length} action${actions.length>1?"s":""}</b></div>${actionChecklist(actions)}</section>
   <section><div class="section-title"><div><span>01</span><div><h2>Qui devient plus dangereux</h2><p>Concurrents directs/partiels (C1/C2) avec le plus de nouvelles capacités sur la période — un indicateur de rythme, pas un score de menace. Centres technologiques/instituts exclus : ce ne sont pas des concurrents commerciaux.</p></div></div><b>${ranking.length} acteur${ranking.length>1?"s":""}</b></div>${activityRankingList(ranking)}</section>
   <section><div class="section-title"><div><span>02</span><div><h2>Marché & opportunités</h2><p>Nouveaux faits validés et applications déjà connues mais observées de nouveau.</p></div></div><b>${marketSignals.length} signaux</b></div>${marketList}</section>
   <section><div class="section-title"><div><span>03</span><div><h2>Blancs concurrentiels</h2><p>Applications radar où un seul acteur suivi est actif sur ce couple marché/composant — signal de blanc, pas une opportunité confirmée (peut aussi juste refléter une couverture incomplète).</p></div></div><b>${opportunities.length} signaux</b></div>${opportunities.length ? evidenceTable(opportunities) : `<div class="empty">Aucun blanc concurrentiel identifié pour le moment.</div>`}</section>
   <section><div class="section-title"><div><span>04</span><div><h2>Mouvements concurrents</h2><p>Nouvelles offres, capacités et savoir-faire détectés chez les acteurs suivis, classés par famille.</p></div></div><b>${Number(counts.new_offers || 0)} signaux</b></div>${offerList}</section>
   <section><div class="section-title"><div><span>05</span><div><h2>Technologie qui approche l’industrie</h2><p>Axes technologiques en pré-industrialisation ou industrialisation, avec projet/acteurs sourcés.</p></div></div><b>${inProgressTech.length} signaux</b></div>${techSignalList}</section>
   <section><div class="section-title"><div><span>06</span><div><h2>Technologies futures</h2><p>Publications, brevets, projets et autres documents collectés récemment.</p></div></div><b>${Number(counts.technology || 0)} signaux</b></div>${techList}</section>`;
  wireActions();
  document.querySelectorAll("[data-goto-view]").forEach(el => el.addEventListener("click", () => {
    const view = el.dataset.gotoView;
    document.querySelectorAll(".nav").forEach(n => n.classList.toggle("active", n.dataset.view === view));
    state.view = view;
    render();
  }));
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

// --- Intelligence techno: science -> industry readiness signals ---------------------------

function technologySignalTable(rows) {
  if (!rows.length) return `<div class="empty">Aucun signal documenté pour le moment.</div>`;
  return `<div class="evidence-table no-market-col">
    <div class="evidence-head"><span>Axe technologique</span><span>Projet / acteurs</span><span>Preuves</span></div>
    ${rows.map(row => `<div class="evidence-row">
      <div><strong>${esc(row.axis)}</strong><br><span class="operation">${esc(row.maturity_stage)}</span></div>
      <div>${esc(row.project_name || "—")}${row.actor_names.length ? `<br><span class="operation">${row.actor_names.map(esc).join(", ")}</span>` : ""}</div>
      <button class="proof-pill ${Number(row.languages||0)>1?'multi-source':''}" data-tech-signal-proof="${Number(row.id)}" title="Voir les sources">${esc(proofMeta(row))}</button>
    </div>`).join("")}
  </div>`;
}

function renderTechIntel() {
  const signals = state.technologySignals || [];
  const existing = signals.filter(s => s.bucket === "existing");
  const radar = signals.filter(s => s.bucket === "radar");
  content.innerHTML = header(
    "Intelligence",
    "Intelligence techno",
    "Signaux de passage science → industrie par axe technologique (haute puissance, parallélisation, beam shaping, TGV, LIPSS/DLIP…). Recherchés et sourcés manuellement, comme les fiches acteurs — jamais déduits automatiquement.",
  ) +
  `<section><div class="section-title"><div><span>01</span><div><h2>Déjà industrialisé</h2><p>Axe en production ou en industrialisation avancée, avec preuve documentée.</p></div></div><b>${existing.length} signaux</b></div>${technologySignalTable(existing)}</section>
   <section><div class="section-title"><div><span>02</span><div><h2>En cours d’industrialisation</h2><p>Prototype, pré-industrialisation ou R&D avec une trajectoire vers la production.</p></div></div><b>${radar.length} signaux</b></div>${technologySignalTable(radar)}</section>`;
  wireActions();
}

// --- Technologies futures: recently collected documents (publications, patents, projects) --

const DOC_TYPE_LABELS = {publication: "Publication", patent: "Brevet", project: "Projet", other: "Autre"};

function docDateLabel(value) {
  if (!value) return "Date inconnue";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date inconnue";
  return new Intl.DateTimeFormat("fr-FR", {dateStyle: "medium"}).format(date);
}

function documentRow(d) {
  const typeLabel = DOC_TYPE_LABELS[d.document_type] || d.document_type;
  const abstract = d.abstract ? `${d.abstract.slice(0, 220)}${d.abstract.length > 220 ? "…" : ""}` : "";
  return `<div class="doc-row">
    <span class="doc-type-badge ${esc(d.document_type)}">${esc(typeLabel)}</span>
    <div class="doc-main">
      <a href="${esc(d.source_url)}" target="_blank" rel="noopener"><strong>${esc(d.title)}</strong></a>
      ${abstract ? `<p class="doc-abstract">${esc(abstract)}</p>` : ""}
      ${d.doi ? `<span class="operation">DOI : ${esc(d.doi)}</span>` : ""}
    </div>
    <span>${esc(d.actor_name || "Non attribué")}</span>
    <span>${esc(docDateLabel(d.published_at || d.created_at))}</span>
  </div>`;
}

function renderFutureTech() {
  const docs = state.documents || [];
  content.innerHTML = header(
    "Intelligence",
    "Technologies futures",
    "Publications, brevets, projets et autres documents collectés récemment.",
  ) + (docs.length
    ? `<div class="doc-table">
        <div class="doc-head"><span>Type</span><span>Document</span><span>Acteur</span><span>Date</span></div>
        ${docs.map(documentRow).join("")}
      </div>`
    : `<div class="empty">Aucun document collecté pour le moment.</div>`);
  wireActions();
}

async function showTechnologySignalProofs(signalId) {
  const proofs = await api(`/api/technology-signals/${signalId}/proofs`);
  if (!proofs.length) {
    document.querySelector("#proof-content").innerHTML = `<div class="empty">Aucune source disponible.</div>`;
    dialog.classList.remove("wide");
    dialog.showModal();
    return;
  }
  const first = proofs[0];
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">${esc(first.maturity_stage)}</p><h2>${esc(first.axis)}</h2><p class="dialog-operation">${esc(first.project_name || "")}${first.actor_names.length ? ` · ${first.actor_names.map(esc).join(", ")}` : ""}</p>${proofs.map(p => `<article class="proof"><div><strong>Source</strong>${p.language ? `<span>${esc(String(p.language).toUpperCase())}</span>` : ""}</div><blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
  dialog.classList.remove("wide");
  dialog.showModal();
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
const RELATION_TYPE_LABELS = {partner: "Partenaire", supplier: "Fournisseur", client: "Client"};

function cardSummaryLine(a) {
  const text = a.strategic_summary || a.role || "";
  return text.length > 300 ? `${text.slice(0, 299)}…` : text;
}

// Chantier 6 : "score de complétude par acteur ... c'est ce qui pilote l'effort de collecte,
// aujourd'hui rien ne signale qu'un acteur priority avec des dizaines d'URLs découvertes a
// zéro fait marché" -- ce badge est délibérément sur la CARTE (pas seulement dans la fiche),
// pour être visible d'un coup d'oeil sur la liste complète des acteurs.
function completenessLevel(score) {
  if (score >= 0.6) return "good";
  if (score >= 0.3) return "partial";
  return "weak";
}

// Scores séparés confiance/menace (audit Horizon 2 #14, voir scoring.py) : distincts de la
// complétude ci-dessus (qui ne mesure que la PRÉSENCE de données). confidence_score est None
// tant qu'aucun fait n'a été collecté sur l'acteur -- pas de badge dans ce cas plutôt qu'un 0
// qui dirait à tort "confiance nulle" au lieu de "rien à évaluer encore".
function confidenceBadge(a) {
  if (a.confidence_score == null) return "";
  const level = completenessLevel(a.confidence_score / 100);
  return `<span class="completeness-badge lvl-${level}" title="Fiabilité des données collectées : faits validés, citations verbatim, dates de publication confirmées.">${Math.round(a.confidence_score)} confiance</span>`;
}

function threatLevel(score) {
  if (score >= 55) return "weak"; // reuses completeness-badge's red tone for "high threat"
  if (score >= 25) return "partial";
  return "good"; // reuses the green tone for "low threat" -- inverted semantics vs confidence
}

function threatBadge(a) {
  if (a.is_reference) return "";
  const level = threatLevel(a.threat_score || 0);
  return `<span class="completeness-badge lvl-${level}" title="Classe concurrentielle, ampleur des faits démontrés, vélocité récente (60 derniers jours).">${Math.round(a.threat_score || 0)} menace</span>`;
}

function completenessBadge(a) {
  const pct = Math.round((a.completeness_score || 0) * 100);
  const level = completenessLevel(a.completeness_score || 0);
  const title = (a.completeness_missing || []).length
    ? `Manque : ${a.completeness_missing.join(", ")}`
    : "Complet sur toutes les dimensions suivies";
  return `<span class="completeness-badge lvl-${level}" title="${esc(title)}">${pct}% complet</span>`;
}

function actorCard(a) {
  const paused = !a.active;
  const classBadge = a.competitive_class
    ? `<span class="class-badge ${esc(a.competitive_class)}">${esc(a.competitive_class)}${COMPETITIVE_CLASS_SHORT[a.competitive_class] ? ` · ${esc(COMPETITIVE_CLASS_SHORT[a.competitive_class])}` : ""}</span>`
    : "";
  const typeLabel = ACTOR_TYPE_LABELS[a.actor_type] ? `<p class="actor-type-label">${esc(ACTOR_TYPE_LABELS[a.actor_type])}</p>` : "";
  return `<article class="actor-card ${a.priority?'priority':''}" ${paused?'style="opacity:.55"':''}>
    <div class="actor-top"><div class="initial">${esc(a.name.slice(0,2))}</div><div class="actor-top-tags">${classBadge}${a.priority?'<span>★ Prioritaire</span>':''}${completenessBadge(a)}${confidenceBadge(a)}${threatBadge(a)}</div></div>
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

// Chantier 5 : firmographics.py (founded_year/legal_form_code/headcount_bracket_code) et
// capabilities.py (le reste) -- deux sources déterministes, jamais fabriquées, donc chaque
// ligne n'apparaît que si le champ correspondant est réellement renseigné en base.
function firmographicsRows(a) {
  const rows = [];
  if (a.founded_year) rows.push(["Création", String(a.founded_year)]);
  if (a.legal_form_code) rows.push(["Forme juridique (code INSEE)", a.legal_form_code]);
  if (a.headcount_bracket_code) rows.push(["Effectif (tranche INSEE)", a.headcount_bracket_code]);
  // Le LEI vit dans ses propres colonnes depuis la séparation registre national / GLEIF : un
  // acteur peut porter les deux identifiants à la fois, ils ne s'écrasent plus (voir gleif.py).
  if (a.lei) rows.push(["LEI (GLEIF)", a.lei]);
  return rows;
}

// Audit v8 §2.4/priorité 4 : chaque champ chiffré a désormais SA PROPRE source_url (voir
// capabilities._extract_capabilities) -- affichée en 3e élément de chaque ligne, plutôt que le
// seul lien générique `capability_source_url` d'avant, qui pouvait pointer vers une page sans
// rapport avec la valeur affichée à côté.
function capabilitySpecRows(a) {
  const rows = [];
  if (a.min_feature_size_um != null) rows.push(["Finesse min. démontrée", `${a.min_feature_size_um} µm`, a.min_feature_size_um_source_url]);
  if (a.tolerance_um != null) rows.push(["Tolérance", `± ${a.tolerance_um} µm`, a.tolerance_um_source_url]);
  if (a.max_part_size_mm != null) rows.push(["Taille de pièce max.", `${a.max_part_size_mm} mm`, a.max_part_size_mm_source_url]);
  if (a.throughput_units_per_h != null) rows.push(["Cadence", `${a.throughput_units_per_h} pièces/h`, a.throughput_units_per_h_source_url]);
  if (a.pulse_duration_fs != null) rows.push(["Durée d'impulsion min.", `${a.pulse_duration_fs} fs`, a.pulse_duration_fs_source_url]);
  if ((a.wavelengths_nm || []).length) rows.push(["Longueurs d'onde", a.wavelengths_nm.map(w => `${w} nm`).join(", "), a.wavelengths_nm_source_url]);
  if (a.batch_size_range) rows.push(["Taille de série", a.batch_size_range, a.batch_size_range_source_url]);
  return rows;
}

function factLine(f) {
  return `<li>${esc(f.value)}${f.source_url ? ` <a href="${esc(f.source_url)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`;
}

// revenue_eur/parent_group (actor_profile) restent NULL tant qu'aucune source n'est branchée
// (voir firmographics.py) -- jamais fabriqués, juste affichés dès qu'ils existent. Les
// investissements viennent des événements déjà classés event_type='investment' (press.py) :
// pas une nouvelle collecte, seulement une vue dédiée pour ne plus les noyer dans la liste
// chronologique générale des événements.
function investmentEvents(a) { return (a.events || []).filter(e => e.event_type === "investment"); }

function financialRows(a) {
  const rows = [];
  if (a.revenue_eur != null) rows.push(["Chiffre d'affaires", `${a.revenue_eur.toLocaleString("fr-FR")} €`]);
  if (a.parent_group) rows.push(["Groupe", a.parent_group]);
  return rows;
}

// Relations (partner/supplier/client, voir actor_relations) : jamais affichées sur la fiche
// avant (seulement dans le graphe global /api/network, qui omet les acteurs pausés/candidats
// et ne porte pas note/source_url par arête) -- ici groupées par type pour rester lisibles.
function relationsByType(a) {
  const groups = {};
  for (const r of a.relations || []) (groups[r.relation_type] ||= []).push(r);
  return groups;
}

function relationLine(r) {
  return `<li>${esc(r.related_name)}${r.note ? ` — ${esc(r.note)}` : ""}${r.source_url ? ` <a href="${esc(r.source_url)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`;
}

// Brevets/publications/projets (documents.db, voir patent.py/openalex.py) : déjà collectés et
// rattachés à l'acteur (documents.actor_name) mais jusqu'ici seulement visibles agrégés, sans
// filtre par acteur, sur la page globale "Technologies futures" -- jamais sur la fiche elle-même.
function documentsFor(a) { return (state.documents || []).filter(d => d.actor_name === a.name); }

function documentLine(d) {
  const extra = d.document_type === "patent" && d.patent_number ? ` (${esc(d.patent_number)})` : "";
  return `<li>${d.published_at ? `<b>${esc(d.published_at)}</b> — ` : ""}${esc(d.title)}${extra}${d.source_url ? ` <a href="${esc(d.source_url)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`;
}

// Signaux structurés (audit Horizon 2 #13 : "Ajouter brevets, recrutements et investissements
// comme signaux structurés") -- voir press.classify_press_event côté serveur. press_mention
// (mention générique, sans mot-clé structurant détecté) n'a pas de badge : c'est le
// comportement par défaut, pas un signal typé à mettre en avant.
const EVENT_TYPE_BADGE_LABELS = {patent: "Brevet", investment: "Investissement", recruitment: "Recrutement", acquisition: "M&A"};
const EVENT_TYPE_BADGE_COLORS = {patent: "#a8790b", investment: "#048f83", recruitment: "#6b4fb3", acquisition: "#c14a2f"};

function eventTypeBadge(type) {
  const label = EVENT_TYPE_BADGE_LABELS[type];
  if (!label) return "";
  const color = EVENT_TYPE_BADGE_COLORS[type] || "#5c7986";
  return `<span class="event-type-badge" style="background:${color}1a;color:${color}">${esc(label)}</span> `;
}

function eventLine(e) {
  return `<li>${eventTypeBadge(e.event_type)}${e.event_date ? `<b>${esc(e.event_date)}</b> — ` : ""}${esc(e.description)}${e.source_url ? ` <a href="${esc(e.source_url)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`;
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

    ${firmographicsRows(a).length ? `<div class="detail-block"><h4>Identité entreprise</h4><ul class="fact-list">${firmographicsRows(a).map(([label, val]) => `<li><b>${esc(label)}</b> : ${esc(val)}</li>`).join("")}</ul>${a.registry_source_url ? `<a href="${esc(a.registry_source_url)}" target="_blank" rel="noopener" class="signal-link">Source registre ↗</a>` : ""}</div>` : ""}

    <div class="detail-block">
      <h4>Financier</h4>
      ${financialRows(a).length ? `<ul class="fact-list">${financialRows(a).map(([label, val]) => `<li><b>${esc(label)}</b> : ${esc(val)}</li>`).join("")}</ul>` : ""}
      ${investmentEvents(a).length ? `<ul class="fact-list">${investmentEvents(a).map(eventLine).join("")}</ul>` : ""}
      ${!financialRows(a).length && !investmentEvents(a).length ? `<p class="coverage-note">Aucune donnée financière collectée pour cet acteur (chiffre d'affaires, levées de fonds) — aucune source n'est aujourd'hui branchée pour le chiffre d'affaires ; seules des mentions presse de levées de fonds peuvent alimenter cette section.</p>` : ""}
    </div>

    ${capabilitySpecRows(a).length || (a.materials_qualified || []).length ? `<div class="detail-block"><h4>Capacités chiffrées</h4>${a.capability_review_status === "review" ? `<p class="coverage-note" title="Audit veille §9.4/§10.12 item 0.8 : contrôle de plausibilité (raies laser connues, durées d'impulsion, tailles de motif/pièce)">⚠ ${esc(a.capability_review_note || "Valeur(s) écartée(s) par le contrôle de plausibilité, à vérifier.")}</p>` : ""}${capabilitySpecRows(a).length ? `<ul class="fact-list">${capabilitySpecRows(a).map(([label, val, sourceUrl]) => `<li><b>${esc(label)}</b> : ${esc(val)}${sourceUrl ? ` <a href="${esc(sourceUrl)}" target="_blank" rel="noopener" class="fact-source">↗</a>` : ""}</li>`).join("")}</ul>` : ""}${(a.materials_qualified || []).length ? `<div class="subtheme-chips">${a.materials_qualified.map(m => `<span class="subtheme-chip">${esc(m)}</span>`).join("")}</div>${a.materials_qualified_source_url ? `<a href="${esc(a.materials_qualified_source_url)}" target="_blank" rel="noopener" class="signal-link">Source matériaux ↗</a>` : ""}` : ""}</div>` : ""}

    ${differentiatorFacts(a).length ? `<div class="detail-block"><h4>Différenciateurs</h4><ul class="fact-list">${differentiatorFacts(a).map(f => factLine(f)).join("")}</ul></div>` : ""}

    ${certificationFacts(a).length ? `<div class="detail-block"><h4>Certifications</h4><ul class="fact-list">${certificationFacts(a).map(f => factLine(f)).join("")}</ul></div>` : ""}

    ${(() => {
      const docs = documentsFor(a);
      if (!docs.length) return "";
      const byType = {};
      for (const d of docs) (byType[d.document_type] ||= []).push(d);
      return `<div class="detail-block"><h4>Brevets &amp; publications</h4>${DOC_TYPE_ORDER.filter(t => byType[t]?.length).map(t =>
        `<p class="coverage-note"><b>${esc(DOC_TYPE_LABELS[t] || t)}</b> (${byType[t].length})</p><ul class="fact-list">${byType[t].slice(0, 10).map(documentLine).join("")}</ul>`
      ).join("")}</div>`;
    })()}

    ${(a.events || []).length ? `<div class="detail-block"><h4>Événements</h4><ul class="fact-list">${a.events.map(eventLine).join("")}</ul></div>` : ""}

    ${a.parent_actor ? `<div class="detail-block"><h4>Mouvement capitalistique</h4><p class="actor-entity-note">Racheté par <b>${esc(a.parent_actor)}</b>${a.entity_note ? ` — ${esc(a.entity_note)}` : ""}</p></div>` : ""}

    ${(() => {
      const groups = relationsByType(a);
      const types = Object.keys(groups);
      if (!types.length) return "";
      return `<div class="detail-block"><h4>Relations</h4>${types.map(t =>
        `<p class="coverage-note"><b>${esc(RELATION_TYPE_LABELS[t] || t)}</b></p><ul class="fact-list">${groups[t].map(relationLine).join("")}</ul>`
      ).join("")}</div>`;
    })()}

    <div class="detail-block">
      <h4>Preuves</h4>
      <p>${proofsTotal} preuve(s) issues de nos collectes${a.evidence_confirmed === false ? ' — <span class="evidence-warning">⚠ non confirmé par nos preuves</span>' : ""}</p>
      <p class="coverage-note">Couverture documentaire : <b>${COVERAGE_LABELS[a.coverage_level] || "Non documenté dans la base"}</b>. ${COVERAGE_HINTS[a.coverage_level] || ""}</p>
      <p class="coverage-note">Complétude de la fiche : <b>${Math.round((a.completeness_score || 0) * 100)}%</b> (${a.completeness_present}/${a.completeness_total} dimensions, pondéré par la fraîcheur du dernier crawl).${(a.completeness_missing || []).length ? ` Manque : ${a.completeness_missing.map(esc).join(", ")}.` : " Toutes les dimensions suivies sont renseignées."}</p>
    </div>

    <div class="detail-block">
      <h4>Scores séparés</h4>
      <p class="coverage-note">Confiance dans les données : ${a.confidence_score == null ? "<b>Pas encore de fait collecté</b>" : `<b>${Math.round(a.confidence_score)}/100</b>`} — fiabilité des données déjà collectées (faits validés, citations verbatim, dates de publication confirmées), distincte de la complétude ci-dessus qui ne mesure que leur présence.</p>
      ${!a.is_reference ? `<p class="coverage-note">Menace concurrentielle : <b>${Math.round(a.threat_score || 0)}/100</b> — classe concurrentielle, ampleur des faits démontrés, vélocité sur les 60 derniers jours.</p>` : ""}
    </div>

    <div class="detail-block admin-block">
      <h4>Administration technique</h4>
      <div class="profile-line"><span class="profile-badge ${a.needs_reprofile ? "warning" : a.strategy}">${a.needs_reprofile ? "À recalibrer" : a.strategy === "adaptive" ? "Adaptatif" : "Générique"}</span><small>${a.profile_status === "ready" ? "Profil prêt" : a.profile_status === "partial" ? "Profil partiel" : a.profile_status === "degraded" ? "Mode dégradé" : "À cartographier"}</small></div>
      ${a.generated_by ? `<p class="coverage-note">Profil de collecte construit par : <b>${a.generated_by === "deterministic" ? "règles déterministes (sans IA)" : `IA de secours (${esc(a.generated_by)})`}</b>${a.confidence != null ? `, confiance ${Math.round(a.confidence * 100)}%` : ""} — détermine quelles pages de son site sont considérées pertinentes pour l'extraction.</p>` : ""}
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

// --- Faits marché en attente de revue humaine (fact_status='partial'/'review') --------------
// Contrairement à /api/market (qui exige les 3 dimensions market+component+operation reliées
// ET bucket in existing/radar), ces faits n'apparaissent nulle part ailleurs dans l'app tant
// qu'ils ne sont pas traités ici -- voir app.py: /api/market/review.

const MARKET_REVIEW_STATUS_LABELS = {partial: "Partiel · 2 dimensions sur 3", review: "Proposé par l'IA"};

function marketReviewStatusLabel(item) {
  return MARKET_REVIEW_STATUS_LABELS[item.fact_status] || item.fact_status;
}

function marketReviewDims(item) {
  return [["Marché", item.market], ["Composant", item.component], ["Opération", item.operation]]
    .map(([label, value]) => `<div class="vocab-dim"><small>${esc(label)}</small><b>${value ? esc(value) : "—"}</b></div>`)
    .join("");
}

// mode/extraction_mode carries either "block-rules" (deterministic lexicon) or a provider tag
// like "anthropic:claude-..."/"ollama:..." -- surfaced so a reviewer knows at a glance whether
// they're checking a rules-based partial match or an AI proposal the lexicon couldn't confirm.
function marketReviewMeta(item) {
  const parts = [];
  if (item.relation_strength) parts.push(`Relation : ${item.relation_strength === "partial" ? "partielle" : esc(item.relation_strength)}`);
  if (item.extraction_mode && item.extraction_mode !== "block-rules") parts.push(`IA : ${esc(item.extraction_mode)}`);
  if (typeof item.field_confidence === "number") parts.push(`Confiance : ${Math.round(item.field_confidence * 100)}%`);
  // A handful of existing rows store the literal string "None" instead of a real NULL --
  // pre-existing backend data quirk, filtered here rather than shown as confusing noise.
  if (item.industrial_stage && item.industrial_stage !== "None") parts.push(esc(item.industrial_stage));
  return parts.length ? `<small class="block-label">${parts.join(" · ")}</small>` : "";
}

function marketReviewCard(item) {
  return `<article class="vocab-card">
    <header><span>${esc(item.actor_name)} · ${dateLabel(item.last_seen_at)}</span><span>${esc(marketReviewStatusLabel(item))}</span></header>
    <blockquote>${esc(item.quote)}</blockquote>
    <div class="vocab-dims">${marketReviewDims(item)}</div>
    ${marketReviewMeta(item)}
    <div class="vocab-dims" style="margin-top:12px">
      <button class="vocab-accept" data-accept-market-review="${item.id}">✓ Valider</button>
      <button class="vocab-reject" data-reject-market-review="${item.id}">✕ Rejeter</button>
    </div>
    ${item.source_url ? `<a class="signal-link" href="${esc(item.source_url)}" target="_blank" rel="noopener">Voir la source ↗</a>` : ""}
  </article>`;
}

// §5.G audit veille (30/08/2026, Lot 1 §1.1) : GET /api/review couvre 7 files derrière un seul
// contrat {id,queue,actor_name,summary,detail,priority,...} -- avant cet endpoint, offers/
// tech_signals/actor_events/actor_facts n'avaient AUCUN moyen d'être vus en dehors d'un accès
// direct à la base. evidence/vocabulary ont déjà leur propre page dédiée (au-dessus) ; cette
// section couvre les deux files réellement chargées en production au 31/08/2026 (offers : 29,
// events : 174, dont une vraie acquisition jamais vue nulle part dans l'app avant ce jour).
const REJECT_REASON_LABELS = {
  off_topic: "Hors sujet",
  wrong_actor: "Mauvais acteur",
  wrong_dimension: "Mauvaise dimension",
  unconvincing_citation: "Citation non probante",
  duplicate: "Doublon",
};

function reviewQueueCard(item) {
  const detail = item.detail || {};
  const key = `${item.queue}:${item.id}`;
  const reasonOptions = Object.entries(REJECT_REASON_LABELS)
    .map(([value, label]) => `<option value="${value}">${esc(label)}</option>`).join("");
  return `<article class="vocab-card">
    <header><span>${esc(item.actor_name || "—")} · ${dateLabel(item.created_at)}</span><span>Priorité ${Number(item.priority || 0).toFixed(1)}</span></header>
    <p class="dialog-operation">${esc(item.summary)}</p>
    ${detail.quote ? `<blockquote>${esc(detail.quote)}</blockquote>` : ""}
    ${detail.laser_process ? `<small class="block-label">${esc(detail.laser_process)}</small>` : ""}
    <div class="vocab-dims" style="margin-top:12px">
      <button class="vocab-accept" data-accept-review="${key}">✓ Valider</button>
      <select class="reject-reason-select" data-reject-reason-for="${key}">
        <option value="">Motif de rejet…</option>
        ${reasonOptions}
      </select>
      <button class="vocab-reject" data-reject-review="${key}">✕ Rejeter</button>
    </div>
    ${detail.source_url ? `<a class="signal-link" href="${esc(detail.source_url)}" target="_blank" rel="noopener">Voir la source ↗</a>` : ""}
  </article>`;
}

function reviewQueueSection(symbol, title, description, items) {
  if (!items.length) return "";
  return `<section><div class="section-title"><div><span>${esc(symbol)}</span><div><h2>${esc(title)}</h2><p>${esc(description)}</p></div></div><b>${items.length}</b></div>
    <div class="vocab-list">${items.map(reviewQueueCard).join("")}</div>
  </section>`;
}

function renderMarketReview() {
  const items = state.marketReview || [];
  const partialCount = items.filter(i => i.fact_status === "partial").length;
  const aiCount = items.filter(i => i.fact_status === "review").length;
  content.innerHTML = header(
    "Administration",
    "Faits marché à valider",
    "Faits où seules 2 des 3 dimensions marché/composant/opération sont reliées, ou proposés par l'IA sur un bloc que le lexique déterministe avait rejeté. Valider marque le fait comme retenu et le retire de cette file — un fait partiel reste toutefois incomplet et n'apparaîtra dans la matrice Marché que si les trois dimensions finissent par y être explicitement reliées."
  ) +
  (items.length
    ? `<p class="actor-summary-counts">${partialCount} partiel${partialCount > 1 ? "s" : ""} · ${aiCount} proposé${aiCount > 1 ? "s" : ""} par l'IA</p><div class="vocab-list">${items.map(marketReviewCard).join("")}</div>`
    : `<div class="empty">Aucun fait marché en attente de revue.</div>`)
  + reviewQueueSection("◈", "Capacités à valider", "Offres/capacités extraites mais pas encore confirmées comme fait retenu.", state.reviewOffers || [])
  + reviewQueueSection("⚑", "Événements à valider", "Événements datés (M&A, financement, mentions presse) détectés mais pas encore vérifiés — jamais visibles ailleurs dans l'app tant qu'ils restent ici.", state.reviewEvents || []);
  wireActions();
}

async function decideReviewItem(queue, itemId, decision, rejectReason) {
  try {
    const body = {decision};
    if (decision === "reject") body.reject_reason = rejectReason;
    await api(`/api/review/${queue}/${itemId}/decide`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    toast(decision === "accept" ? "Validé." : "Rejeté.");
    if (queue === "offers") state.reviewOffers = (await api("/api/review?queue=offers")).items;
    if (queue === "events") state.reviewEvents = (await api("/api/review?queue=events")).items;
    renderMarketReview();
  } catch (error) { toast(error.message); }
}

// --- Découverte d'acteurs (Lot 3 §3.2/§3.4) : rubrique unique regroupant les candidats
// accumulés par toutes les sources -- CORDIS, OpenAlex, brevets EPO OPS, liens sortants
// récurrents (voir actor_discovery.py/cordis.py/openalex.py/patent.py) -- avant qu'un humain
// ne décide de les promouvoir. Un candidat promu devient un acteur réel review_status='candidate'
// et rejoint alors la file « En attente de validation » déjà présente sur la page Acteurs
// (reviewActorCard plus haut) pour la décision finale -- deux étapes distinctes, jamais
// fusionnées : un nom candidat n'est pas encore un acteur.
const CANDIDATE_SOURCE_LABELS = {cordis: "CORDIS", openalex: "OpenAlex", outbound_link: "Lien sortant", patent: "Brevet EPO OPS"};

const CANDIDATE_MAX_VISIBLE_OCCURRENCES = 4;

function actorCandidateCard(item) {
  const sources = item.sources || [];
  const distinctTypes = [...new Set(sources.map(s => s.source_type))];
  const sourceLine = distinctTypes.map(t => CANDIDATE_SOURCE_LABELS[t] || t).join(" · ");
  const visible = sources.slice(0, CANDIDATE_MAX_VISIBLE_OCCURRENCES);
  const hiddenCount = sources.length - visible.length;
  return `<article class="vocab-card">
    <header><span>${esc(item.name)}${item.country ? ` · ${esc(item.country)}` : ""}</span><span>${item.score} source${item.score > 1 ? "s" : ""} indépendante${item.score > 1 ? "s" : ""}</span></header>
    ${sourceLine ? `<small class="block-label">${esc(sourceLine)}</small>` : ""}
    ${visible.map(s => `<blockquote>${esc(s.context || "Occurrence sans contexte capturé.")}${s.source_url ? ` <a class="signal-link" href="${esc(s.source_url)}" target="_blank" rel="noopener">↗</a>` : ""}</blockquote>`).join("")}
    ${hiddenCount > 0 ? `<small class="block-label">+ ${hiddenCount} autre${hiddenCount > 1 ? "s" : ""} occurrence${hiddenCount > 1 ? "s" : ""}</small>` : ""}
    <div class="vocab-dims" style="margin-top:12px">
      <button class="vocab-accept" data-promote-candidate="${item.id}">✓ Promouvoir</button>
      <button class="vocab-reject" data-reject-candidate="${item.id}">✕ Rejeter</button>
    </div>
  </article>`;
}

function renderActorDiscovery() {
  const items = state.actorDiscovery || [];
  content.innerHTML = header(
    "Administration",
    "Découverte d'acteurs",
    "Organisations repérées automatiquement -- consortiums CORDIS, co-auteurs OpenAlex, déposants de brevets EPO OPS, liens sortants revenant sur plusieurs sites d'acteurs -- mais jamais ajoutées comme acteur tant qu'un humain ne l'a pas validé. Le score compte les sources indépendantes qui citent le même nom. Promouvoir crée un acteur réel, qui rejoint ensuite la file « En attente de validation » de la page Acteurs pour la décision finale."
  ) +
  (items.length
    ? `<div class="vocab-list">${items.map(actorCandidateCard).join("")}</div>`
    : `<div class="empty">Aucun candidat en attente de revue.</div>`);
  wireActions();
}

function showCandidatePromote(candidateId) {
  const item = (state.actorDiscovery || []).find(c => c.id === candidateId);
  if (!item) return;
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">Promouvoir en acteur</p>
    <h2>${esc(item.name)}</h2>
    <form id="candidate-promote-form" class="detail-form">
      <label>Rôle<input type="text" name="role" placeholder="Rôle" required></label>
      <label>Pays<input type="text" name="country" value="${esc(item.country || "")}" placeholder="Pays" required></label>
      <label>Site officiel<input type="url" name="official_url" value="${esc(item.suggested_official_url || "")}" placeholder="https://site-officiel.example" required></label>
      <div class="detail-form-actions"><button type="button" data-close-dialog>Annuler</button><button type="submit" class="primary">Promouvoir</button></div>
    </form>`;
  dialog.classList.remove("wide");
  dialog.showModal();
  document.querySelector("#candidate-promote-form").addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(e.target);
    try {
      await api(`/api/actor-candidates/${candidateId}/promote`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({role: data.get("role"), country: data.get("country"), official_url: data.get("official_url")}),
      });
      toast("Candidat promu en acteur.");
      dialog.close();
      [state.actorDiscovery, state.actors] = await Promise.all([api("/api/actor-candidates"), api("/api/actors")]);
      renderActorDiscovery();
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
}

async function rejectCandidate(candidateId) {
  try {
    await api(`/api/actor-candidates/${candidateId}/reject`, {method: "POST"});
    toast("Candidat rejeté.");
    state.actorDiscovery = await api("/api/actor-candidates");
    renderActorDiscovery();
  } catch (error) { toast(error.message); }
}

// --- Digest (§5.F audit veille, 30/08/2026) : "ce digest ne doit contenir QUE du changement
// depuis `since`, jamais un état" -- 4 règles évaluées après chaque collecte (voir alerts.py) :
// transition radar->existing, nouveau fait chez un acteur C1/C2, incident de collecte, signal
// M&A/financement. GET /api/digest existait déjà, mais rien dans l'app ne l'appelait -- 60
// alertes réelles accumulées en silence au 31/08/2026, dont une vraie acquisition (Blueacre
// Technology).
const ALERT_TYPE_LABELS = {
  bucket_transition_existing: "Passage radar → existant",
  new_fact_high_value_actor: "Nouveau fait chez un acteur prioritaire",
  collection_incident: "Incident de collecte",
  ma_funding_event: "M&A / financement",
};

function digestAlertCard(item) {
  return `<article class="vocab-card">
    <header><span>${esc(item.actor_name || "—")} · ${dateLabel(item.event_at)}</span></header>
    <p class="dialog-operation">${esc(item.summary)}</p>
    ${item.detail ? `<blockquote>${esc(item.detail)}</blockquote>` : ""}
    ${item.source_url ? `<a class="signal-link" href="${esc(item.source_url)}" target="_blank" rel="noopener">Voir la source ↗</a>` : ""}
  </article>`;
}

function renderDigest() {
  const digest = state.digest || {since: null, total: 0, by_type: {}};
  const sections = Object.entries(ALERT_TYPE_LABELS).map(([type, label]) => {
    const items = digest.by_type[type] || [];
    if (!items.length) return "";
    return `<section><div class="section-title"><div><span>⚑</span><div><h2>${esc(label)}</h2></div></div><b>${items.length}</b></div>
      <div class="vocab-list">${items.map(digestAlertCard).join("")}</div>
    </section>`;
  }).join("");
  content.innerHTML = header(
    "Intelligence",
    "Digest",
    "Ce qui a changé depuis la fenêtre choisie -- transition radar → existant, nouveau fait chez un acteur prioritaire, incident de collecte, ou signal M&A/financement. Jamais un état, seulement du changement.",
    `<div class="header-actions">
      <button class="export-btn" data-digest-window="7">7 j</button>
      <button class="export-btn" data-digest-window="30">30 j</button>
      <button class="export-btn" data-digest-window="90">90 j</button>
    </div>`
  ) +
  (digest.total
    ? `<p class="actor-summary-counts">${digest.total} alerte${digest.total > 1 ? "s" : ""} depuis ${dateLabel(digest.since)}</p>${sections}`
    : `<div class="empty">Aucune alerte sur cette fenêtre.</div>`);
  wireActions();
}

async function setDigestWindow(days) {
  try {
    const since = new Date(Date.now() - days * 86400000).toISOString();
    state.digest = await api(`/api/digest?since=${encodeURIComponent(since)}`);
    renderDigest();
  } catch (error) { toast(error.message); }
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
   ${veilleMetricsPanel()}
   ${collectionHealthPanel()}
   ${duplicatesPanel()}`;
  wireActions();
}

// §10.11 audit veille (30/08/2026, Lot 1 §1.7) : "aucun des chiffres de ce document n'est
// calculé par l'application" -- veille_metrics.py calcule bien les 8 indicateurs, mais rien ne
// les affichait avant ce jour.
const VEILLE_METRIC_LABELS = {
  collection_yield: "Rendement de collecte",
  unvisited_discovery_rate: "Découvertes jamais visitées",
  crawl_error_rate: "Taux d'erreur de crawl",
  non_verbatim_share: "Citations non verbatim",
  unreviewed_accepted_share: "Faits acceptés sans revue humaine",
  published_date_reliability: "Fiabilité des dates de publication",
  detection_latency_days: "Latence de détection",
  source_concentration_top10pct: "Concentration des sources (top 10%)",
};
const VEILLE_METRIC_RAW_DAYS = new Set(["detection_latency_days"]);

function veilleMetricValue(indicator) {
  if (indicator.value == null) return "—";
  if (VEILLE_METRIC_RAW_DAYS.has(indicator.indicator)) return `${Math.round(indicator.value)} j`;
  return `${Math.round(indicator.value * 100)}%`;
}

function veilleMetricsPanel() {
  const vm = state.veilleMetrics;
  if (!vm) return "";
  const items = vm.indicators.map(ind => {
    const label = VEILLE_METRIC_LABELS[ind.indicator] || ind.indicator;
    const status = ind.value == null ? "missing" : (ind.alert ? "error" : "ready");
    const symbol = status === "error" ? "!" : status === "missing" ? "—" : "✓";
    return `<span class="section-state ${status}" title="${esc(label)}">${symbol} ${esc(label)} : ${veilleMetricValue(ind)}</span>`;
  }).join("");
  return `<section><div class="section-title"><div><span>⚕</span><div><h2>Santé de la veille</h2><p>8 indicateurs de qualité du pipeline pour ${esc(vm.period)} — pas une dimension métier, la fiabilité de ce que l'observatoire produit lui-même.</p></div></div></div>
    <div class="section-states">${items}</div>
  </section>`;
}

// §9.2 audit veille (30/08/2026, Lot 1 §1.5) : health_score/failure_count existaient déjà
// (site_profiles) mais rien ne les exposait au-delà de nombres bruts sur la fiche acteur. Cas
// trouvé en production : Workshop of Photonics, le 2e acteur le mieux documenté, échouait aussi
// le plus (72 échecs 403 Forbidden) sans que rien ne le signale.
function collectionHealthPanel() {
  const health = state.collectionHealth;
  if (!health) return "";
  const concerning = (health.actors || []).filter(a =>
    (a.health_score != null && a.health_score < 70) || a.error_rate > 0.1 || a.needs_reprofile
  );
  const actorRows = concerning.length
    ? concerning.slice(0, 15).map(a => `<article class="dup-row"><div><strong>${esc(a.name)}</strong>${a.priority ? " (prioritaire)" : ""}</div><span>Santé ${a.health_score ?? "—"}/100 · ${a.sources_failed}/${a.sources_total} source(s) en erreur (${Math.round(a.error_rate * 100)}%)${a.top_error_status ? ` · le plus fréquent : HTTP ${a.top_error_status} (${a.top_error_count}×)` : ""}${a.last_error ? ` · ${esc(a.last_error)}` : ""}</span></article>`).join("")
    : `<div class="empty">Aucun acteur en difficulté détectée.</div>`;
  const runRows = (health.recent_runs || []).slice(0, 8).map(r => `<article class="dup-row"><div><strong>${esc(r.source)}</strong></div><span>${dateLabel(r.started_at)} · ${esc(r.status)} · ${r.scanned} scanné(s) · ${r.errors} erreur(s) (${Math.round(r.error_rate * 100)}%)${r.message ? ` · ${esc(r.message)}` : ""}</span></article>`).join("");
  return `<section><div class="section-title"><div><span>⚕</span><div><h2>Santé de collecte</h2><p>Acteurs dont la collecte échoue ou se dégrade silencieusement, triés du pire au meilleur.</p></div></div><b>${concerning.length}</b></div>
    <div class="dup-list">${actorRows}</div>
    <h3 style="margin-top:20px">Runs récents</h3>
    <div class="dup-list">${runRows}</div>
  </section>`;
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

function schedulerStatusLine() {
  const s = state.schedulerStatus;
  if (!s) return '<b class="warn">Inconnu</b>';
  if (!s.enabled) return '<b class="warn">Désactivée</b>';
  return `<b class="ok">Active · ${esc(s.cron)}${s.next_run_at ? ` · prochain run ${dateLabel(s.next_run_at)}` : ""}</b>`;
}

function renderSettings() {
  const adaptive=state.overview.adaptive;
  content.innerHTML=header("Configuration","Paramètres","Règles de collecte et de publication de l’observatoire.")+`<div class="settings">
    <article><div><h3>Planification automatique</h3><p>Lancement mensuel du pipeline complet (SCHEDULER_ENABLED) sans intervention manuelle.</p></div>${schedulerStatusLine()}</article>
    <article><div><h3>Crawler déterministe</h3><p>Découverte coverage-first, scoring des URLs, profils génériques et overrides spécifiques.</p></div><b class="ok">Actif</b></article>
    <article><div><h3>Analyse sémantique ${esc(aiProviderName(adaptive))}</h3><p>Modèle ${esc(adaptive.model)}${esc(aiCostSuffix(adaptive))} · réservé à l’interprétation sémantique des blocs ambiguës, pas au pilotage principal du crawl.</p></div><b class="${adaptive.ollama_available?'ok':'warn'}">${adaptive.ollama_available?'Disponible':'Secours absent'}</b></article>
    <article><div><h3>Recalibrage des profils</h3><p>Déclenché lorsqu’une structure devient inexploitable ou produit des erreurs répétées.</p></div><b>${adaptive.needs_reprofile} à recalibrer</b></article>
    <article><div><h3>Condition d’affichage Marché</h3><p>Marché + pièce/composant + opération laser explicitement reliés, avec source vérifiable.</p></div><b>Strict</b></article>
    <article><div><h3>Offres & capacités</h3><p>Les procédés et savoir-faire sont conservés sans forcer un marché ou un composant absent du contenu.</p></div><b>Separé</b></article>
    <article><div><h3>Déduplication multilingue</h3><p>FR, EN ou DE peuvent documenter le même fait ; elles sont stockées comme sources d’un fait canonique unique.</p></div><b>Active</b></article>
  </div>`;
}

// --- Qualité de la veille (§5.H audit veille, 30/08/2026, Lot 4 §17) : "_completeness et
// compute_confidence_scores évaluent les ACTEURS. Rien n'évalue la VEILLE." Quatre indicateurs :
// rappel (golden set, saisi à la main), précision (faits rejetés en revue par extraction_mode),
// latence de détection médiane, santé de couverture -- voir data_quality.py pour le calcul de
// chacun et pourquoi seul le rappel a besoin d'une saisie humaine.
function goldenFactRow(fact) {
  return `<article class="dup-row"><div><strong>${esc(fact.actor_name)}</strong> · ${esc(fact.market)} / ${esc(fact.component)} / ${esc(fact.operation)}</div><span>${esc(fact.expected_quote.slice(0, 140))}${fact.expected_quote.length > 140 ? "…" : ""} <a class="signal-link" href="${esc(fact.source_url)}" target="_blank" rel="noopener">↗</a> <button class="vocab-reject" data-delete-golden-fact="${fact.id}" style="margin-left:8px">✕</button></span></article>`;
}

function renderDataQuality() {
  const dq = state.dataQuality || {recall: {}, precision: {by_extraction_mode: []}, detection_latency: {}, coverage_health: {}};
  const recall = dq.recall || {};
  const precisionRows = (dq.precision?.by_extraction_mode || [])
    .map(row => `<article class="dup-row"><div><strong>${esc(row.extraction_mode)}</strong></div><span>${row.rejected}/${row.total} rejeté${row.rejected > 1 ? "s" : ""} en revue (${Math.round((row.rejection_rate || 0) * 100)}%)</span></article>`)
    .join("");
  const latency = dq.detection_latency || {};
  const health = dq.coverage_health || {};
  const goldenFacts = state.goldenFacts || [];

  content.innerHTML = header(
    "Pilotage des données",
    "Qualité de la veille",
    "Quatre indicateurs qui évaluent le DISPOSITIF de veille, pas les fiches acteurs : est-ce qu'il retrouve ce qu'on lui demande, à quel point on lui fait confiance sans relire, avec quel retard, et sur quelle part du périmètre il tourne vraiment."
  ) +
  `<section><div class="section-title"><div><span>01</span><div><h2>Rappel (golden set)</h2><p>Sur les faits vérifiés à la main ci-dessous, combien le pipeline en connaît-il actuellement ? À rejouer après toute évolution des lexiques.</p></div></div><b>${recall.recall != null ? `${Math.round(recall.recall * 100)}%` : "—"}</b></div>
    <p class="actor-summary-counts">${recall.golden_facts ? `${recall.matched}/${recall.golden_facts} faits golden retrouvés` : "Aucun fait golden saisi -- le rappel reste incalculable tant qu'il n'y en a pas."}</p>
    <button class="export-btn" data-open-golden-fact-add>+ Ajouter un fait golden</button>
    ${goldenFacts.length ? `<div class="dup-list" style="margin-top:14px">${goldenFacts.map(goldenFactRow).join("")}</div>` : ""}
  </section>
  <section><div class="section-title"><div><span>02</span><div><h2>Précision</h2><p>Part des faits rejetés parmi ceux réellement passés par une revue humaine, par mode d'extraction.</p></div></div><b>${dq.precision?.reviewed_total ?? 0}</b></div>
    ${precisionRows ? `<div class="dup-list">${precisionRows}</div>` : `<div class="empty">Aucun fait n'est encore passé par une revue humaine tracée.</div>`}
  </section>
  <section><div class="section-title"><div><span>03</span><div><h2>Latence de détection</h2><p>Délai médian entre la date de publication réelle d'un fait et le moment où l'observatoire l'a détecté.</p></div></div><b>${latency.median_days != null ? `${latency.median_days} j` : "—"}</b></div>
    <p class="actor-summary-counts">${latency.sample_size ? `Calculé sur ${latency.sample_size} fait(s) à date de publication connue.` : "Aucun fait avec date de publication connue."}</p>
  </section>
  <section><div class="section-title"><div><span>04</span><div><h2>Santé de couverture</h2><p>Acteurs sans crawl réussi récent, et catégories de pages stratégiques marquées manquantes.</p></div></div><b>${health.stale_actors ?? "—"}</b></div>
    <p class="actor-summary-counts">${health.stale_actors ?? 0}/${health.active_actors ?? 0} acteur(s) sans source HTTP 200 depuis ${health.stale_days_threshold ?? "?"} jours · ${health.missing_strategic_pages_total ?? 0} catégorie(s) de page stratégique manquante(s) au total.</p>
  </section>`;
  wireActions();
}

function showGoldenFactAdd() {
  document.querySelector("#proof-content").innerHTML = `<p class="eyebrow">Nouveau fait golden</p>
    <h2>Ajouter un fait vérifié à la main</h2>
    <form id="golden-fact-add-form" class="detail-form">
      <label>Acteur<input type="text" name="actor_name" placeholder="Nom exact de l'acteur" required></label>
      <label>Marché<input type="text" name="market" required></label>
      <label>Composant<input type="text" name="component" required></label>
      <label>Opération<input type="text" name="operation" required></label>
      <label>URL source (vérifiée par vous)<input type="url" name="source_url" placeholder="https://..." required></label>
      <label>Citation attendue<textarea name="expected_quote" rows="3" placeholder="La citation exacte que le pipeline devrait retrouver" required></textarea></label>
      <div class="detail-form-actions"><button type="button" data-close-dialog>Annuler</button><button type="submit" class="primary">Ajouter</button></div>
    </form>`;
  dialog.classList.remove("wide");
  dialog.showModal();
  document.querySelector("#golden-fact-add-form").addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(e.target);
    try {
      await api("/api/golden-facts", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          actor_name: data.get("actor_name"), market: data.get("market"), component: data.get("component"),
          operation: data.get("operation"), source_url: data.get("source_url"), expected_quote: data.get("expected_quote"),
        }),
      });
      toast("Fait golden ajouté.");
      dialog.close();
      [state.goldenFacts, state.dataQuality] = await Promise.all([api("/api/golden-facts"), api("/api/data-quality")]);
      renderDataQuality();
    } catch (error) { toast(error.message); }
  });
  document.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
}

async function deleteGoldenFact(factId) {
  try {
    await api(`/api/golden-facts/${factId}`, {method: "DELETE"});
    toast("Fait golden retiré.");
    [state.goldenFacts, state.dataQuality] = await Promise.all([api("/api/golden-facts"), api("/api/data-quality")]);
    renderDataQuality();
  } catch (error) { toast(error.message); }
}

// --- Séries temporelles (audit Horizon 2 #12 : "Construire séries temporelles par acteur,
// marché, technologie, maturité et signal") -- voir timeseries.py côté serveur pour le calcul.
// Chaque instantané reflète l'état constaté au moment de la capture (jamais un point
// rétroactif fabriqué) : avec un seul mois de recul pour l'instant, les graphiques ci-dessous
// sont volontairement conçus pour rester lisibles à 1 point (voir tsSparseNote/svgSeriesChart)
// plutôt que de paraître cassés en attendant les prochains cycles de collecte.
const TIMESERIES_DIMENSION_LABELS = {actor: "Acteur", market: "Marché", technology: "Technologie", maturity: "Maturité", signal: "Signal"};
const TIMESERIES_DIMENSION_HINTS = {
  actor: "Trajectoire documentaire d’un acteur : faits marché validés par bucket, offres, sources actives.",
  market: "Distribution par bucket (existant/radar) et par stade industriel d’un marché, et nombre d’acteurs actifs dessus.",
  technology: "Signaux technologiques par axe (existant/radar) ; « Toutes / global » regroupe les publications/brevets/projets.",
  maturity: "Distribution des stades industriels (R&D → Production) et des buckets, globale ou par marché — la mesure « passage prototype → production » que l’audit réclamait.",
  signal: "Vélocité : faits marché validés, offres, documents et signaux technologiques nouveaux chaque mois — la mesure la plus proche d’un « marché qui monte ».",
};
const STAGE_ORDER = ["R&D", "Prototype", "Pré-industrialisation", "Industrialisation", "Production", "Maturité industrielle non déterminée"];
const STAGE_COLORS = {
  "R&D": "#8b79c9", "Prototype": "#d8a13d", "Pré-industrialisation": "#4f8fd8",
  "Industrialisation": "var(--chart-teal)", "Production": "var(--chart-coral)",
  "Maturité industrielle non déterminée": "#b7c0c4",
};
const DOC_TYPE_ORDER = ["publication", "patent", "project", "other"];
const DOC_TYPE_COLORS = {publication: "var(--chart-teal)", patent: "#a8790b", project: "#6b4fb3", other: "#5c7986"};
// DOC_TYPE_LABELS (Publication/Brevet/Projet/Autre) est déjà déclaré plus haut pour la page
// "Technologies futures" -- réutilisé tel quel ici, pas de doublon.
const BUCKET_ORDER = ["existing", "radar"];
const BUCKET_COLORS = {existing: "var(--chart-teal)", radar: "var(--chart-coral)"};
const BUCKET_LABELS = {existing: "Existant", radar: "Radar"};

function periodLabel(period) {
  const [y, m] = String(period || "").split("-").map(Number);
  if (!y || !m) return esc(period);
  return new Intl.DateTimeFormat("fr-FR", {month: "short", year: "2-digit"}).format(new Date(y, m - 1, 1));
}

function tsLegend(series) {
  if (!series.length) return "";
  return `<div class="chart-legend">${series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>`;
}

// Une ligne + points par série ; en-dessous de 2 points le tracé disparaît (rien à relier) mais
// les points restent affichés avec leur valeur en infobulle -- jamais un graphique vide alors
// que la donnée existe.
function svgSeriesChart(points, series) {
  const width = 760, height = 220, padL = 44, padR = 16, padT = 16, padB = 30;
  const innerW = width - padL - padR, innerH = height - padT - padB;
  const maxVal = Math.max(1, ...points.flatMap(p => series.map(s => Number(p[s.key]) || 0)));
  const x = i => points.length > 1 ? padL + (innerW * i) / (points.length - 1) : padL + innerW / 2;
  const y = v => padT + innerH - (innerH * v) / maxVal;
  const gridLines = [0, 0.5, 1].map(f => `<line x1="${padL}" y1="${(padT + innerH * (1 - f)).toFixed(1)}" x2="${width - padR}" y2="${(padT + innerH * (1 - f)).toFixed(1)}" class="ts-grid"/><text x="${padL - 8}" y="${(padT + innerH * (1 - f) + 3).toFixed(1)}" text-anchor="end" class="ts-axis-label">${Math.round(maxVal * f)}</text>`).join("");
  const xLabels = points.map((p, i) => `<text x="${x(i).toFixed(1)}" y="${height - 8}" text-anchor="middle" class="ts-axis-label">${esc(periodLabel(p.period))}</text>`).join("");
  const lines = series.map(s => {
    const path = points.length > 1 ? points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(Number(p[s.key]) || 0).toFixed(1)}`).join(" ") : "";
    const dots = points.map((p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(Number(p[s.key]) || 0).toFixed(1)}" r="3.5" fill="${s.color}"><title>${esc(s.label)} · ${esc(periodLabel(p.period))} : ${esc(String(p[s.key] ?? 0))}</title></circle>`).join("");
    return (path ? `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2"/>` : "") + dots;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" class="ts-chart" role="img" aria-label="Évolution dans le temps">${gridLines}${lines}${xLabels}</svg>`;
}

// Une barre empilée par période pour une distribution imbriquée (stage_distribution,
// bucket_distribution, documents_by_type) -- fonctionne dès 1 seule période, contrairement à
// svgSeriesChart qui a besoin d'un tracé.
function svgStackedBarChart(points, field, order, colors) {
  const width = 760, height = 220, padL = 16, padR = 16, padT = 16, padB = 30;
  const innerW = width - padL - padR, innerH = height - padT - padB;
  const totals = points.map(p => order.reduce((sum, k) => sum + (Number((p[field] || {})[k]) || 0), 0));
  const maxTotal = Math.max(1, ...totals);
  const barW = Math.min(56, (innerW / points.length) * 0.55);
  const bars = points.map((p, i) => {
    const cx = padL + (innerW * (i + 0.5)) / points.length;
    let yCursor = padT + innerH;
    const dist = p[field] || {};
    const segments = order.filter(key => dist[key]).map(key => {
      const value = Number(dist[key]) || 0;
      const segH = maxTotal ? (innerH * value) / maxTotal : 0;
      yCursor -= segH;
      return `<rect x="${(cx - barW / 2).toFixed(1)}" y="${yCursor.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, segH).toFixed(1)}" fill="${colors[key] || "#ccd6d9"}"><title>${esc(key)} · ${esc(periodLabel(p.period))} : ${value}</title></rect>`;
    }).join("");
    const label = `<text x="${cx.toFixed(1)}" y="${height - 8}" text-anchor="middle" class="ts-axis-label">${esc(periodLabel(p.period))}</text>`;
    return segments + label;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" class="ts-chart" role="img" aria-label="Distribution par période">${bars}</svg>`;
}

function tsKpiGrid(entries) {
  return `<div class="ts-kpi-grid">${entries.map(([label, value]) => `<div class="ts-kpi"><strong>${esc(String(value))}</strong><span>${esc(label)}</span></div>`).join("")}</div>`;
}

function tsSparseNote(points) {
  if (points.length >= 2) return "";
  return `<p class="ts-note">Historique en construction (${points.length} point${points.length > 1 ? "s" : ""} pour l’instant) — un point réel s’ajoute à chaque cycle de collecte, jamais reconstruit rétroactivement.</p>`;
}

function renderTrendsBody(points) {
  const dimension = state.trends.dimension, key = state.trends.key;
  const latest = points[points.length - 1] || {};
  if (dimension === "actor") {
    const series = [
      {key: "evidence_existing", label: "Faits existants", color: BUCKET_COLORS.existing},
      {key: "evidence_radar", label: "Faits radar", color: BUCKET_COLORS.radar},
      {key: "offers_count", label: "Offres", color: "#6b4fb3"},
      {key: "sources_active", label: "Sources actives", color: "var(--lime)"},
    ];
    return tsSparseNote(points) + tsLegend(series) + svgSeriesChart(points, series) +
      tsKpiGrid([
        ["Classe concurrentielle", latest.competitive_class || "Non classé"],
        ["Faits existants", latest.evidence_existing ?? 0], ["Faits radar", latest.evidence_radar ?? 0],
        ["Offres", latest.offers_count ?? 0], ["Sources actives", latest.sources_active ?? 0],
      ]);
  }
  if (dimension === "market") {
    const series = [
      {key: "existing", label: "Faits existants", color: BUCKET_COLORS.existing},
      {key: "radar", label: "Faits radar", color: BUCKET_COLORS.radar},
    ];
    const stageOrder = STAGE_ORDER.filter(s => points.some(p => (p.stage_distribution || {})[s]));
    return tsSparseNote(points) + tsLegend(series) + svgSeriesChart(points, series) +
      tsKpiGrid([["Total faits validés", latest.total ?? 0], ["Acteurs actifs sur ce marché", latest.actors_count ?? 0]]) +
      (stageOrder.length ? `<h3 class="ts-subheading">Stade industriel</h3>${tsLegend(stageOrder.map(s => ({label: s, color: STAGE_COLORS[s]})))}${svgStackedBarChart(points, "stage_distribution", stageOrder, STAGE_COLORS)}` : "");
  }
  if (dimension === "technology" && key === "__global__") {
    const typeOrder = DOC_TYPE_ORDER.filter(t => points.some(p => (p.documents_by_type || {})[t]));
    return tsSparseNote(points) +
      tsKpiGrid([["Documents (tous types)", latest.documents_total ?? 0]]) +
      (typeOrder.length ? `${tsLegend(typeOrder.map(t => ({label: DOC_TYPE_LABELS[t] || t, color: DOC_TYPE_COLORS[t] || "#ccd6d9"})))}${svgStackedBarChart(points, "documents_by_type", typeOrder, DOC_TYPE_COLORS)}` : `<div class="empty">Aucun document collecté pour l’instant.</div>`);
  }
  if (dimension === "technology") {
    const series = [
      {key: "signals_existing", label: "Signaux existants", color: BUCKET_COLORS.existing},
      {key: "signals_radar", label: "Signaux radar", color: BUCKET_COLORS.radar},
    ];
    return tsSparseNote(points) + tsLegend(series) + svgSeriesChart(points, series) +
      tsKpiGrid([["Signaux existants", latest.signals_existing ?? 0], ["Signaux radar", latest.signals_radar ?? 0]]);
  }
  if (dimension === "maturity") {
    const stageOrder = STAGE_ORDER.filter(s => points.some(p => (p.stage_distribution || {})[s]));
    return tsSparseNote(points) +
      tsKpiGrid([["Total faits validés", latest.total ?? 0]]) +
      (stageOrder.length ? `<h3 class="ts-subheading">Stade industriel (R&D → Production)</h3>${tsLegend(stageOrder.map(s => ({label: s, color: STAGE_COLORS[s]})))}${svgStackedBarChart(points, "stage_distribution", stageOrder, STAGE_COLORS)}` : "") +
      `<h3 class="ts-subheading">Bucket (existant / radar)</h3>${tsLegend(BUCKET_ORDER.map(b => ({label: BUCKET_LABELS[b], color: BUCKET_COLORS[b]})))}${svgStackedBarChart(points, "bucket_distribution", BUCKET_ORDER, BUCKET_COLORS)}`;
  }
  // signal
  const series = [
    {key: "new_evidence", label: "Nouveaux faits marché", color: BUCKET_COLORS.existing},
    {key: "new_offers", label: "Nouvelles offres", color: BUCKET_COLORS.radar},
    {key: "new_documents", label: "Nouveaux documents", color: "#6b4fb3"},
    {key: "new_technology_signals", label: "Nouveaux signaux techno", color: "var(--lime)"},
  ];
  return tsSparseNote(points) + tsLegend(series) + svgSeriesChart(points, series) +
    tsKpiGrid([
      ["Nouveaux faits marché ce mois", latest.new_evidence ?? 0], ["Nouvelles offres", latest.new_offers ?? 0],
      ["Nouveaux documents", latest.new_documents ?? 0], ["Nouveaux signaux techno", latest.new_technology_signals ?? 0],
    ]);
}

async function loadTrendsKeys(dimension) {
  const {keys} = await api(`/api/timeseries/keys?dimension=${encodeURIComponent(dimension)}`);
  return keys;
}

async function loadTrendsPoints(dimension, key) {
  if (!key) return [];
  try {
    const data = await api(`/api/timeseries?dimension=${encodeURIComponent(dimension)}&key=${encodeURIComponent(key)}`);
    return data.points;
  } catch (_) {
    return [];
  }
}

async function setTrendsDimension(dimension) {
  state.trends.dimension = dimension;
  state.trends.loading = true;
  renderTrends();
  const keys = await loadTrendsKeys(dimension);
  const preferred = keys.includes("__global__") ? "__global__" : keys[0];
  state.trends.keys = keys;
  state.trends.key = preferred || "";
  state.trends.points = await loadTrendsPoints(dimension, preferred);
  state.trends.loading = false;
  renderTrends();
}

async function setTrendsKey(key) {
  state.trends.key = key;
  state.trends.loading = true;
  renderTrends();
  state.trends.points = await loadTrendsPoints(state.trends.dimension, key);
  state.trends.loading = false;
  renderTrends();
}

function renderTrends() {
  const t = state.trends;
  const dimensionOptions = Object.entries(TIMESERIES_DIMENSION_LABELS).map(([value, label]) => `<option value="${value}" ${t.dimension === value ? "selected" : ""}>${esc(label)}</option>`).join("");
  const keyOptions = t.keys.map(k => `<option value="${esc(k)}" ${t.key === k ? "selected" : ""}>${k === "__global__" ? "Toutes / global" : esc(k)}</option>`).join("");
  const body = t.loading
    ? `<div class="empty"><span class="spinner"></span>Chargement…</div>`
    : (!t.keys.length
        ? `<div class="empty">Aucun instantané pour l’instant — lancez une collecte (Bases & collectes) ou cliquez « Capturer un instantané » pour en générer un.</div>`
        : renderTrendsBody(t.points));
  content.innerHTML = header(
    "Historique",
    "Séries temporelles",
    "Un instantané réel par cycle de collecte — acteur, marché, technologie, maturité, signal. Jamais de point rétroactif fabriqué : l’historique se construit mois après mois.",
    `<div class="header-actions"><button class="primary" data-run-trends-capture>↻ Capturer un instantané</button></div>`
  ) +
  `<section><div class="section-title"><div><span>∿</span><div><h2>${esc(TIMESERIES_DIMENSION_LABELS[t.dimension])}</h2><p>${esc(TIMESERIES_DIMENSION_HINTS[t.dimension])}</p></div></div></div>
    <div class="ts-controls">
      <div class="filter-group"><label>Dimension</label><select id="ts-dimension">${dimensionOptions}</select></div>
      <div class="filter-group"><label>Clé</label><select id="ts-key" ${!t.keys.length ? "disabled" : ""}>${keyOptions}</select></div>
    </div>
    ${body}
  </section>`;
  document.querySelector("#ts-dimension")?.addEventListener("change", e => setTrendsDimension(e.target.value));
  document.querySelector("#ts-key")?.addEventListener("change", e => setTrendsKey(e.target.value));
  document.querySelector("[data-run-trends-capture]")?.addEventListener("click", async () => {
    try {
      await api("/api/timeseries/capture", {method: "POST"});
      toast("Instantané capturé.");
      await setTrendsDimension(state.trends.dimension);
    } catch (error) { toast(error.message); }
  });
}

function render(){
  if(state.view==="monthly") renderMonthly();
  if(state.view==="market") renderMarket();
  if(state.view==="offers") renderOffers();
  if(state.view==="techintel") renderTechIntel();
  if(state.view==="futuretech") renderFutureTech();
  if(state.view==="actors") renderActors();
  if(state.view==="trends") renderTrends();
  if(state.view==="vocabulary") renderVocabulary();
  if(state.view==="market-review") renderMarketReview();
  if(state.view==="actor-discovery") renderActorDiscovery();
  if(state.view==="digest") renderDigest();
  if(state.view==="data-quality") renderDataQuality();
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

async function decideMarketReview(id, action) {
  try {
    await api(`/api/market/review/${id}/${action}`, {method: "POST"});
    toast(action === "accept" ? "Fait validé." : "Fait rejeté.");
    state.marketReview = await api("/api/market/review");
    renderMarketReview();
  } catch (error) { toast(error.message); }
}

const EVIDENCE_TYPE_LABELS = {proof: "Preuve", claim: "Déclaratif", third_party: "Source tierce"};

function evidenceTypeBadge(type) {
  if (!type) return "";
  // "et-" prefix keeps this out of the way of the unrelated top-level ".proof" article class.
  return `<span class="evidence-type-badge et-${esc(type)}">${esc(EVIDENCE_TYPE_LABELS[type] || type)}</span>`;
}

// Audit veille §10.2 (30/08/2026) : is_verbatim était déjà renvoyé par l'API mais jamais
// affiché -- 40 des 71 faits marché "validés" étaient en réalité des notes de lecture saisies
// à la main (SEED_EVIDENCE, aujourd'hui vidé dans db.py), indistinguables à l'écran d'une
// citation extraite et vérifiée caractère par caractère. Silencieux dans le cas normal
// (is_verbatim=1, l'immense majorité) pour ne pas ajouter de bruit visuel à chaque preuve --
// seul le cas exceptionnel est signalé, même logique que evidence_confirmed===false ailleurs.
function verbatimBadge(isVerbatim) {
  if (isVerbatim === 0 || isVerbatim === false) {
    return `<span class="evidence-type-badge et-manual" title="Note de lecture saisie à la main, pas une citation extraite du site par le pipeline.">Saisie manuelle</span>`;
  }
  return "";
}

async function showProofs(row) {
  const qs=new URLSearchParams({bucket:row.bucket,market:row.market,component:row.component,operation:row.operation});
  const proofs=await api(`/api/market/proofs?${qs}`);
  document.querySelector("#proof-content").innerHTML=`<p class="eyebrow">${esc(row.market)}</p><h2>${esc(row.component)}</h2><p class="dialog-operation">${esc(row.operation)}</p>${proofs.map(p=>`<article class="proof"><div><strong>${esc(p.actor_name)}</strong><span>${esc(p.industrial_stage)}</span>${evidenceTypeBadge(p.evidence_type)}${verbatimBadge(p.is_verbatim)}</div>${p.language?`<small class="source-language">${esc(String(p.language).toUpperCase())}</small>`:''}${p.block_heading?`<small class="block-label">Bloc : ${esc(p.block_heading)}</small>`:''}${[p.laser_process,p.material,p.performance].filter(Boolean).length?`<small class="block-label">${[p.laser_process,p.material,p.performance].filter(Boolean).map(esc).join(' · ')}</small>`:''}${p.relation_strength?`<small class="block-label">Relation : ${esc(p.relation_strength==='direct'?'directe':'contextuelle')}${p.source_role?` · source : ${esc(p.source_role)}`:''}</small>`:''}${p.first_appeared_at?`<small class="block-label" title="Rétro-daté via Wayback Machine : plus ancien snapshot archivé où cette citation apparaît déjà">Vu pour la première fois le ${dateLabel(p.first_appeared_at)} (Wayback)</small>`:''}<blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
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
  document.querySelector("#proof-content").innerHTML=`<p class="eyebrow">${esc(first.actor_name)}</p><h2>${esc(first.capability)}</h2><p class="dialog-operation">${esc(offerTypeLabel(first.offer_type))}</p>${proofs.map(p=>`<article class="proof"><div><strong>${esc(p.operation || p.laser_process || p.capability)}</strong><span>${esc(p.industrial_stage || '')}</span>${evidenceTypeBadge(p.evidence_type)}${verbatimBadge(p.is_verbatim)}</div>${p.language?`<small class="source-language">${esc(String(p.language).toUpperCase())}</small>`:''}${p.block_heading?`<small class="block-label">Bloc : ${esc(p.block_heading)}</small>`:''}${[p.laser_process,p.material,p.performance].filter(Boolean).length?`<small class="block-label">${[p.laser_process,p.material,p.performance].filter(Boolean).map(esc).join(' · ')}</small>`:''}${p.relation_strength?`<small class="block-label">Relation : ${esc(p.relation_strength==='direct'?'directe':'contextuelle')}${p.source_role?` · source : ${esc(p.source_role)}`:''}</small>`:''}<blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
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
      // Un instantané réel a été capturé côté serveur à la fin de cette collecte (voir
      // app._run_job) -- si la vue séries temporelles a déjà été visitée, la resynchroniser
      // silencieusement plutôt que de laisser son cache devenir périmé jusqu'au prochain clic.
      if (job.status === "completed" && state.trends.keys.length) await setTrendsDimension(state.trends.dimension);
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
  document.querySelectorAll("[data-tech-signal-proof]").forEach(button=>button.addEventListener("click",()=>showTechnologySignalProofs(Number(button.dataset.techSignalProof))));
  document.querySelectorAll("[data-toggle-actor]").forEach(button=>button.addEventListener("click",()=>toggleActorActive(Number(button.dataset.toggleActor), button.dataset.nextActive==="1")));
  document.querySelectorAll("[data-accept-vocab]").forEach(button=>button.addEventListener("click",()=>decideVocabulary(Number(button.dataset.acceptVocab),"accept",button.dataset.dimension)));
  document.querySelectorAll("[data-reject-vocab]").forEach(button=>button.addEventListener("click",()=>decideVocabulary(Number(button.dataset.rejectVocab),"reject")));
  document.querySelectorAll("[data-accept-market-review]").forEach(button=>button.addEventListener("click",()=>decideMarketReview(Number(button.dataset.acceptMarketReview),"accept")));
  document.querySelectorAll("[data-reject-market-review]").forEach(button=>button.addEventListener("click",()=>decideMarketReview(Number(button.dataset.rejectMarketReview),"reject")));
  document.querySelectorAll("[data-accept-review]").forEach(button=>button.addEventListener("click",()=>{
    const [queue, id] = button.dataset.acceptReview.split(":");
    decideReviewItem(queue, Number(id), "accept");
  }));
  document.querySelectorAll("[data-reject-review]").forEach(button=>button.addEventListener("click",()=>{
    const [queue, id] = button.dataset.rejectReview.split(":");
    const select = button.closest("article").querySelector(".reject-reason-select");
    if (!select.value) { toast("Choisis un motif de rejet d'abord."); return; }
    decideReviewItem(queue, Number(id), "reject", select.value);
  }));
  document.querySelectorAll("[data-review-actor]").forEach(button=>button.addEventListener("click",()=>decideActorReview(Number(button.dataset.reviewActor), button.dataset.reviewStatus)));
  document.querySelectorAll("[data-promote-candidate]").forEach(button=>button.addEventListener("click",()=>showCandidatePromote(Number(button.dataset.promoteCandidate))));
  document.querySelectorAll("[data-reject-candidate]").forEach(button=>button.addEventListener("click",()=>rejectCandidate(Number(button.dataset.rejectCandidate))));
  document.querySelectorAll("[data-actor-detail]").forEach(el=>el.addEventListener("click",()=>showActorDetail(Number(el.dataset.actorDetail))));
  document.querySelectorAll("[data-actor-edit]").forEach(el=>el.addEventListener("click",()=>showActorEdit(Number(el.dataset.actorEdit))));
  document.querySelectorAll("[data-actor-toggle-priority]").forEach(el=>el.addEventListener("click",()=>toggleActorPriority(Number(el.dataset.actorTogglePriority), el.dataset.nextPriority==="1")));
  document.querySelectorAll("[data-actor-delete]").forEach(el=>el.addEventListener("click",()=>deleteActorWithConfirm(Number(el.dataset.actorDelete), el.dataset.actorName)));
  document.querySelectorAll("[data-digest-window]").forEach(button=>button.addEventListener("click",()=>setDigestWindow(Number(button.dataset.digestWindow))));
  document.querySelector("[data-open-market-sizing-add]")?.addEventListener("click", showMarketSizingAdd);
  document.querySelectorAll("[data-delete-market-sizing]").forEach(button=>button.addEventListener("click",()=>deleteMarketSizing(Number(button.dataset.deleteMarketSizing))));
  document.querySelector("[data-open-reference-cell-add]")?.addEventListener("click", showReferenceCellAdd);
  document.querySelectorAll("[data-delete-reference-cell]").forEach(button=>button.addEventListener("click",()=>deleteReferenceCell(Number(button.dataset.deleteReferenceCell))));
  document.querySelector("[data-open-golden-fact-add]")?.addEventListener("click", showGoldenFactAdd);
  document.querySelectorAll("[data-delete-golden-fact]").forEach(button=>button.addEventListener("click",()=>deleteGoldenFact(Number(button.dataset.deleteGoldenFact))));
}

document.querySelectorAll(".nav").forEach(button=>button.addEventListener("click",()=>{
  document.querySelectorAll(".nav").forEach(n=>n.classList.remove("active"));
  button.classList.add("active");
  state.view=button.dataset.view;
  // Séries temporelles : chargées à la demande (voir déclaration de state.trends), donc le
  // premier passage sur cette vue déclenche le fetch au lieu d'un simple render() sur un
  // cache encore vide -- les visites suivantes réutilisent ce qui est déjà chargé.
  if(state.view==="trends" && !state.trends.keys.length && !state.trends.loading){
    setTrendsDimension(state.trends.dimension);
  } else {
    render();
  }
}));

document.querySelector(".dialog-close").addEventListener("click",()=>dialog.close());
dialog.addEventListener("click",e=>{if(e.target===dialog)dialog.close()});

// veille_metrics n'a pas encore d'instantané sur une base toute neuve (404) -- ne doit jamais
// faire échouer tout refresh() pour autant, contrairement aux autres endpoints ci-dessous qui
// renvoient toujours 200 (éventuellement avec des listes vides).
async function apiOrNull(path) {
  try { return await api(path); } catch (_) { return null; }
}

async function refresh(){
  [
    state.overview,state.monthly,state.market,state.offers,state.technologySignals,state.documents,
    state.actors,state.profiles,state.vocabulary,state.marketReview,state.network,state.duplicates,
    state.pipelineFunnel,state.marketScores,state.actorDiscovery,
    state.reviewOffers,state.reviewEvents,state.collectionHealth,state.schedulerStatus,
    state.veilleMetrics,state.digest,state.demandSignals,
    state.marketSizing,state.referenceMatrix,state.dataQuality,state.goldenFacts,
  ]=await Promise.all([
    api("/api/overview"),
    api("/api/monthly?days=30"),
    api("/api/market"),
    api("/api/offers"),
    api("/api/technology-signals"),
    api("/api/documents?limit=500"),
    api("/api/actors"),
    api("/api/profiles"),
    api("/api/vocabulary-candidates"),
    api("/api/market/review"),
    api("/api/network"),
    api("/api/actors/duplicates"),
    api("/api/pipeline-funnel"),
    api("/api/market-scores"),
    api("/api/actor-candidates"),
    api("/api/review?queue=offers").then(r => r.items),
    api("/api/review?queue=events").then(r => r.items),
    api("/api/collection-health"),
    api("/api/scheduler"),
    apiOrNull("/api/veille-metrics"),
    api("/api/digest"),
    api("/api/demand-signals"),
    api("/api/market-sizing"),
    api("/api/reference-matrix"),
    api("/api/data-quality"),
    api("/api/golden-facts"),
  ]);
  render();
}

refresh().catch(error=>{
  content.innerHTML=`<div class="empty">Impossible de charger l’application : ${esc(error.message)}</div>`;
});
