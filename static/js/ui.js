// Utilitaires de présentation partagés par toutes les vues.
//
// Extraits de app.js (2 599 lignes en portée globale unique) : ce sont les fonctions les plus
// appelées du front -- esc() par 65 fonctions, toast() par 23, header() par 14, dateLabel() par
// 11 -- et les seules qui n'aient aucune dépendance vers le reste de l'application. Elles
// forment donc la couche la plus basse, celle qu'on peut isoler sans démêler quoi que ce soit.

// Échappement HTML. Chaque interpolation de donnée non fiable (contenu scrapé : citations, URLs,
// titres, noms d'acteurs) DOIT passer par ici avant d'atteindre innerHTML.
export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

export function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 3500);
}

export function dateLabel(value) {
  if (!value) return "Jamais";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date inconnue";
  return new Intl.DateTimeFormat("fr-FR", {dateStyle: "medium", timeStyle: "short"}).format(date);
}

export function debounce(callback, delay = 180) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
}

export function header(eyebrow, title, description, action = "") {
  return `<header class="page-header"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p>${description}</p></div>${action}</header>`;
}
