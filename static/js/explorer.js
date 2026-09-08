// Le moteur des pages "explorateur" : recherche plein texte + facettes cumulatives.
//
// Partagé par "Technologie laser" et "Offres & capacités". Les deux affichent un corpus de
// quelques centaines de lignes au plus, entièrement chargé côté client : tout le filtrage se
// fait ici, sans aller-retour serveur.
//
// La règle de composition est celle qu'on attend d'une recherche à facettes : plusieurs valeurs
// cochées DANS un même groupe s'unissent (OU), et les groupes se croisent ENTRE eux (ET).

import { esc } from "./ui.js";

/**
 * Filtre `rows` et calcule, pour chaque groupe de facettes, ses options et leurs compteurs.
 *
 * @param rows      le corpus complet
 * @param query     la recherche plein texte (trimée et mise en minuscules ici)
 * @param haystackOf (row) => string -- le texte dans lequel la recherche cherche
 * @param groups    [{key, valuesOf(row) => string[]}] -- une ligne sans valeur doit renvoyer
 *                  son libellé de repli ("Non précisé"), jamais [] : sinon elle devient
 *                  invisible dès qu'on touche à ce groupe, sans que rien ne l'explique.
 * @param active    {key: [valeurs cochées]}
 * @param prefilter (row) => bool -- filtre en amont (onglet de type), appliqué aussi aux
 *                  compteurs de facettes : un onglet restreint le corpus, il n'est pas une
 *                  facette parmi d'autres.
 * @returns {filtered, options} où options[key] = [[valeur, compteur], ...]
 */
export function explore(rows, {query = "", haystackOf, groups, active, prefilter = null}) {
  const needle = query.trim().toLowerCase();

  // `skip` laisse un groupe hors du filtre pour pouvoir compter ses propres options sur le
  // résultat des AUTRES groupes. Sans ça, un compteur annoncerait des lignes que le clic ne
  // ramènerait jamais -- l'écueil classique des facettes cumulatives.
  const matches = (row, skip) => {
    if (prefilter && !prefilter(row)) return false;
    if (needle && !haystackOf(row).toLowerCase().includes(needle)) return false;
    for (const group of groups) {
      if (group.key === skip) continue;
      const selected = active[group.key] || [];
      if (selected.length && !group.valuesOf(row).some(value => selected.includes(value))) return false;
    }
    return true;
  };

  const options = {};
  for (const group of groups) {
    const counts = new Map();
    for (const row of rows) {
      if (!matches(row, group.key)) continue;
      for (const value of group.valuesOf(row)) counts.set(value, (counts.get(value) || 0) + 1);
    }
    // Une option reste listée à 0 tant qu'elle existe dans le corpus complet : une facette qui
    // disparaît quand on clique ailleurs empêche de comprendre le filtre qu'on vient de poser.
    const all = new Set();
    for (const row of rows) for (const value of group.valuesOf(row)) all.add(value);
    options[group.key] = [...all]
      .map(value => [value, counts.get(value) || 0])
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "fr"));
  }

  return {filtered: rows.filter(row => matches(row, null)), options};
}

/** Bascule une valeur dans la sélection d'un groupe, sans muter l'objet d'origine. */
export function toggleFacet(active, key, value) {
  const current = active[key] || [];
  return {...active, [key]: current.includes(value) ? current.filter(v => v !== value) : [...current, value]};
}

export function activeFacetCount(active) {
  return Object.values(active).reduce((total, values) => total + values.length, 0);
}

// Au-delà de ce nombre d'options, un groupe est replié : la facette ACTEUR d'Offres & capacités
// compte une trentaine d'entrées, qui repousseraient les groupes suivants hors de l'écran.
const COLLAPSED_OPTIONS = 8;

/**
 * Rend un groupe de facettes. `expanded` porte les clés de groupes dépliés par l'utilisateur.
 *
 * Une option à 0 sur le résultat courant est désactivée -- elle ne mènerait qu'à une liste
 * vide -- SAUF si elle est déjà cochée, auquel cas il faut pouvoir la décocher.
 */
export function facetGroupHtml(title, key, options, activeValues, {expanded = false} = {}) {
  if (!options.length) return "";
  const hidden = Math.max(0, options.length - COLLAPSED_OPTIONS);
  const visible = expanded || !hidden ? options : options.slice(0, COLLAPSED_OPTIONS);
  const rows = visible.map(([value, count]) => {
    const on = activeValues.includes(value);
    const dead = !count && !on;
    return `<button type="button" class="ex-facet${on ? " is-active" : ""}${count ? "" : " is-empty"}" data-ex-facet="${esc(key)}" data-ex-value="${esc(value)}" aria-pressed="${on}"${dead ? " disabled" : ""}><span>${esc(value)}</span><span>${count}</span></button>`;
  }).join("");
  const toggle = hidden
    ? `<button type="button" class="ex-facet-toggle" data-ex-expand="${esc(key)}">${expanded ? "Réduire" : `+ ${hidden} autre${hidden > 1 ? "s" : ""}`}</button>`
    : "";
  return `<div class="ex-facet-group"><div class="ex-facet-title">${esc(title)}</div>${rows}${toggle}</div>`;
}
