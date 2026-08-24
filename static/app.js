const state = {
  view: "monthly",
  overview: null,
  monthly: null,
  market: null,
  offers: [],
  actors: [],
  profiles: [],
  query: "",
  offerQuery: "",
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

function evidenceTable(rows) {
  if (!rows.length) return `<div class="empty">Aucune application suffisamment documentée pour le moment.</div>`;
  return `<div class="evidence-table">
    <div class="evidence-head"><span>Marché</span><span>Pièce / composant</span><span>Fonction / opération laser</span><span>Preuves</span></div>
    ${rows.map(row => `<div class="evidence-row">
      <div class="market-cell"><i></i>${esc(row.market)}</div>
      <div>${esc(row.component)}</div>
      <div class="operation">${esc(row.operation)}</div>
      <button class="proof-pill ${Number(row.languages||0)>1?'multi-source':''}" data-proof='${esc(JSON.stringify(row))}' title="Voir les sources">${esc(proofMeta(row))}</button>
    </div>`).join("")}
  </div>`;
}

function renderMarket() {
  const market = state.market || {existing:[], radar:[]};
  content.innerHTML = header(
    "Lecture marché",
    "Applications femtoseconde",
    "Uniquement les faits où marché, pièce/composant et opération laser sont explicitement reliés. Les versions linguistiques d’un même fait sont regroupées comme preuves.",
    `<button class="primary" data-run="market">↻ Actualiser l’analyse</button>`
  ) +
  `<section><div class="section-title"><div><span>01</span><div><h2>Applications industrielles existantes</h2><p>Production, prestation ou qualification explicitement démontrée.</p></div></div><b>${market.existing.length} faits</b></div>${evidenceTable(market.existing)}</section>
   <section><div class="section-title"><div><span>02</span><div><h2>Radar applications et besoins</h2><p>Applications documentées dont l’industrialisation reste à confirmer.</p></div></div><b>${market.radar.length} faits</b></div>${evidenceTable(market.radar)}</section>`;
  wireActions();
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
    ? `<div class="signal-list">${marketSignals.map(row => `<article class="signal-item">
        <div><small>${row.created_at && row.created_at >= monthly.cutoff ? "NOUVEAU" : "RECONFIRMÉ"} · ${esc(row.actor_name)}</small>
        <strong>${esc(row.market)} · ${esc(row.component)}</strong>
        <span>${esc(row.operation)}</span></div>
        <button class="proof-pill" data-proof='${esc(JSON.stringify(row))}'>preuve</button>
      </article>`).join("")}</div>`
    : `<div class="empty">Aucun nouveau fait marché validé sur la période.</div>`;

  const offerList = (monthly.new_offers || []).length
    ? `<div class="signal-list">${monthly.new_offers.map(row => `<article class="signal-item">
        <div><small>NOUVELLE CAPACITÉ · ${esc(row.actor_name)}</small>
        <strong>${esc(row.capability)}</strong>
        <span>${esc([row.operation, row.laser_process, row.material].filter(Boolean).join(" · "))}</span></div>
        <button class="proof-pill" data-offer-proof="${Number(row.id)}">preuve</button>
      </article>`).join("")}</div>`
    : `<div class="empty">Aucune nouvelle capacité concurrente détectée sur la période.</div>`;

  const techList = (monthly.technology || []).length
    ? `<div class="signal-list">${monthly.technology.map(row => `<article class="signal-item">
        <div><small>${esc(String(row.document_type || "document").toUpperCase())}${row.published_at ? ` · ${esc(row.published_at)}` : ""}</small>
        <strong>${esc(row.title)}</strong>
        <span>${esc(row.actor_name || "Acteur non attribué")}</span></div>
        <a class="signal-link" href="${esc(row.source_url)}" target="_blank" rel="noopener">source ↗</a>
      </article>`).join("")}</div>`
    : `<div class="empty">Aucun nouveau document technologique collecté sur la période.</div>`;

  const sourceList = (monthly.changed_sources || []).length
    ? `<div class="signal-list compact">${monthly.changed_sources.slice(0, 20).map(row => `<article class="signal-item">
        <div><small>${esc(row.actor_name)} · ${esc(row.page_type || "page")}</small>
        <strong>${esc(row.last_title || row.url)}</strong>
        <span>${esc(dateLabel(row.last_changed_at))}</span></div>
        <a class="signal-link" href="${esc(row.url)}" target="_blank" rel="noopener">ouvrir ↗</a>
      </article>`).join("")}</div>`
    : `<div class="empty">Aucune page source modifiée détectée sur la période.</div>`;

  content.innerHTML = header(
    "Revue mensuelle",
    `Ce qui a changé sur les ${monthly.days || 30} derniers jours`,
    "Nouveaux faits marché, mouvements concurrents, reconfirmations et signaux technologiques depuis la dernière période.",
    `<button class="primary" data-run="monthly">↻ Actualiser toute la veille</button>`
  ) +
  `<div class="signal-grid">
      <article><small>Applications</small><strong>${Number(counts.new_market || 0)}</strong><span>nouveaux faits validés</span></article>
      <article><small>Concurrence</small><strong>${Number(counts.new_offers || 0)}</strong><span>nouvelles capacités</span></article>
      <article><small>Technologie</small><strong>${Number(counts.technology || 0)}</strong><span>nouveaux documents</span></article>
      <article><small>Sources</small><strong>${Number(counts.changed_sources || 0)}</strong><span>pages modifiées</span></article>
   </div>
   <section><div class="section-title"><div><span>01</span><div><h2>Marché & opportunités</h2><p>Nouveaux faits validés et applications déjà connues mais observées de nouveau.</p></div></div><b>${marketSignals.length} signaux</b></div>${marketList}</section>
   <section><div class="section-title"><div><span>02</span><div><h2>Mouvements concurrents</h2><p>Nouvelles offres, capacités et savoir-faire détectés chez les acteurs suivis.</p></div></div><b>${Number(counts.new_offers || 0)} signaux</b></div>${offerList}</section>
   <section><div class="section-title"><div><span>03</span><div><h2>Technologies futures</h2><p>Publications, brevets, projets et autres documents collectés récemment.</p></div></div><b>${Number(counts.technology || 0)} signaux</b></div>${techList}</section>
   <section><div class="section-title"><div><span>04</span><div><h2>Pages qui ont changé</h2><p>Modifications détectées dans les sources officielles surveillées.</p></div></div><b>${Number(counts.changed_sources || 0)} pages</b></div>${sourceList}</section>`;
  wireActions();
}


function offerTypeLabel(value) {
  const labels = {
    service: "Service",
    capability: "Capacité",
    technology: "Technologie",
    product: "Produit / offre",
  };
  return labels[value] || value || "Capacité";
}

function offerDetails(row) {
  return [
    row.operation && `Opération : ${row.operation}`,
    row.laser_process && `Procédé : ${row.laser_process}`,
    row.material && `Matériau : ${row.material}`,
    row.performance && `Performance : ${row.performance}`,
  ].filter(Boolean);
}

function offersTable(rows) {
  if (!rows.length) return `<div class="empty">Aucune offre ou capacité concurrente documentée pour le moment.</div>`;
  return `<div class="offer-table">
    <div class="offer-head"><span>Acteur</span><span>Type</span><span>Capacité / savoir-faire</span><span>Contexte technique</span><span>Preuves</span></div>
    ${rows.map(row => {
      const details = offerDetails(row);
      return `<div class="offer-row">
        <div class="offer-actor">${esc(row.actor_name)}</div>
        <div><span class="offer-type ${esc(row.offer_type)}">${esc(offerTypeLabel(row.offer_type))}</span></div>
        <div><strong>${esc(row.capability)}</strong>${row.industrial_stage?`<small>${esc(row.industrial_stage)}</small>`:""}</div>
        <div class="offer-context">${details.length ? details.map(item=>`<span>${esc(item)}</span>`).join("") : '<span class="muted">Contexte non précisé</span>'}</div>
        <button class="proof-pill ${Number(row.languages||0)>1?'multi-source':''}" data-offer-proof="${Number(row.id)}" title="Voir les sources">${esc(proofMeta(row))}</button>
      </div>`;
    }).join("")}
  </div>`;
}

function renderOffers() {
  const q = state.offerQuery.trim().toLowerCase();
  const filtered = state.offers.filter(row => {
    const haystack = [row.actor_name, row.offer_type, row.capability, row.operation, row.laser_process, row.material, row.performance, row.industrial_stage, row.page_type].join(" ").toLowerCase();
    return haystack.includes(q);
  });
  const actors = new Set(filtered.map(row => row.actor_name)).size;
  content.innerHTML = header(
    "Veille concurrentielle",
    "Offres & capacités",
    "Prestations, procédés et savoir-faire détectés chez les acteurs suivis. Cette vue n’invente pas de marché lorsqu’une page décrit uniquement une capacité technique.",
    `<button class="primary" data-run="market">↻ Actualiser les preuves</button>`
  ) +
  `<div class="actor-toolbar offer-toolbar"><input id="offer-search" value="${esc(state.offerQuery)}" placeholder="Rechercher un acteur, un procédé, une opération, un matériau…"><span>${filtered.length} capacités · ${actors} acteurs</span></div>
   <section><div class="section-title"><div><span>01</span><div><h2>Cartographie des offres détectées</h2><p>Une capacité peut avoir plusieurs sources et plusieurs langues sans créer de doublon métier.</p></div></div><b>${filtered.length} capacités</b></div>${offersTable(filtered)}</section>`;
  const input = document.querySelector("#offer-search");
  if (input) {
    input.focus({preventScroll:true});
    input.setSelectionRange(input.value.length, input.value.length);
    input.addEventListener("input", debounce(e => {
      state.offerQuery = e.target.value;
      renderOffers();
    }));
  }
  wireActions();
}

function renderActors() {
  const q = state.query.toLowerCase();
  const filtered = state.actors.filter(a => `${a.name} ${a.country} ${a.role}`.toLowerCase().includes(q));
  content.innerHTML = header("Écosystème suivi","20 acteurs","Rôles, sources officielles et pertinence des contenus suivis.",`<button class="primary" data-run="actors">↻ Mettre à jour</button>`)+
  `<div class="actor-toolbar"><input id="actor-search" value="${esc(state.query)}" placeholder="Rechercher un acteur, un pays ou un rôle…"><span>${filtered.length} résultats</span></div><div class="actor-grid">${filtered.map(a=>`<article class="actor-card ${a.priority?'priority':''}"><div class="actor-top"><div class="initial">${esc(a.name.slice(0,2))}</div>${a.priority?'<span>Prioritaire</span>':''}</div><h3>${esc(a.name)}</h3><p>${esc(a.role)}</p><div class="profile-line"><span class="profile-badge ${a.needs_reprofile?'warning':a.strategy}">${a.needs_reprofile?'À recalibrer':a.strategy==='adaptive'?'Adaptatif':'Générique'}</span><small>${a.profile_status==='ready'?'Profil prêt':a.profile_status==='partial'?'Profil partiel':a.profile_status==='degraded'?'Mode dégradé':'À cartographier'}</small></div><footer><span>${esc(a.country)}</span><a href="${esc(a.official_url)}" target="_blank" rel="noopener">Site officiel ↗</a></footer></article>`).join("")}</div>`;
  const input = document.querySelector("#actor-search");
  if (input) {
    input.focus({preventScroll:true});
    input.setSelectionRange(input.value.length, input.value.length);
    input.addEventListener("input", debounce(e => {
      state.query = e.target.value;
      renderActors();
    }));
  }
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
   <div class="rule-note"><strong>Règle de séparation</strong><p>Le crawler collecte les pages et reconstruit leurs blocs. La vue Marché exige marché + composant + opération explicitement reliés. Les pages de service, technologie ou savoir-faire qui ne portent pas de marché explicite sont conservées séparément dans « Offres & capacités ».</p></div>`;
  wireActions();
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
  if(state.view==="collections") renderCollections();
  if(state.view==="settings") renderSettings();
}

async function showProofs(row) {
  const qs=new URLSearchParams({bucket:row.bucket,market:row.market,component:row.component,operation:row.operation});
  const proofs=await api(`/api/market/proofs?${qs}`);
  document.querySelector("#proof-content").innerHTML=`<p class="eyebrow">${esc(row.market)}</p><h2>${esc(row.component)}</h2><p class="dialog-operation">${esc(row.operation)}</p>${proofs.map(p=>`<article class="proof"><div><strong>${esc(p.actor_name)}</strong><span>${esc(p.industrial_stage)}</span></div>${p.language?`<small class="source-language">${esc(String(p.language).toUpperCase())}</small>`:''}${p.block_heading?`<small class="block-label">Bloc : ${esc(p.block_heading)}</small>`:''}${[p.laser_process,p.material,p.performance].filter(Boolean).length?`<small class="block-label">${[p.laser_process,p.material,p.performance].filter(Boolean).map(esc).join(' · ')}</small>`:''}${p.relation_strength?`<small class="block-label">Relation : ${esc(p.relation_strength==='direct'?'directe':'contextuelle')}${p.source_role?` · source : ${esc(p.source_role)}`:''}</small>`:''}<blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
  dialog.showModal();
}

async function showOfferProofs(offerId) {
  const proofs = await api(`/api/offers/${offerId}/proofs`);
  if (!proofs.length) {
    document.querySelector("#proof-content").innerHTML = `<div class="empty">Aucune source disponible.</div>`;
    dialog.showModal();
    return;
  }
  const first = proofs[0];
  document.querySelector("#proof-content").innerHTML=`<p class="eyebrow">${esc(first.actor_name)}</p><h2>${esc(first.capability)}</h2><p class="dialog-operation">${esc(offerTypeLabel(first.offer_type))}</p>${proofs.map(p=>`<article class="proof"><div><strong>${esc(p.operation || p.laser_process || p.capability)}</strong><span>${esc(p.industrial_stage || '')}</span></div>${p.language?`<small class="source-language">${esc(String(p.language).toUpperCase())}</small>`:''}${p.block_heading?`<small class="block-label">Bloc : ${esc(p.block_heading)}</small>`:''}${[p.laser_process,p.material,p.performance].filter(Boolean).length?`<small class="block-label">${[p.laser_process,p.material,p.performance].filter(Boolean).map(esc).join(' · ')}</small>`:''}${p.relation_strength?`<small class="block-label">Relation : ${esc(p.relation_strength==='direct'?'directe':'contextuelle')}${p.source_role?` · source : ${esc(p.source_role)}`:''}</small>`:''}<blockquote>${esc(p.quote)}</blockquote><a href="${esc(p.source_url)}" target="_blank" rel="noopener">Ouvrir la source ↗</a></article>`).join("")}`;
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
  [state.overview,state.monthly,state.market,state.offers,state.actors,state.profiles]=await Promise.all([
    api("/api/overview"),
    api("/api/monthly?days=30"),
    api("/api/market"),
    api("/api/offers"),
    api("/api/actors"),
    api("/api/profiles"),
  ]);
  render();
}

refresh().catch(error=>{
  content.innerHTML=`<div class="empty">Impossible de charger l’application : ${esc(error.message)}</div>`;
});
