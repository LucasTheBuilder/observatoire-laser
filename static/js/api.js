// Accès HTTP à l'API de l'observatoire. Seule couche du front qui parle au réseau.

// Remonte le `detail` renvoyé par FastAPI plutôt qu'un code HTTP nu : c'est ce message que les
// appelants affichent dans un toast, donc il doit rester lisible pour l'utilisateur.
export async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `Erreur ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* corps non JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

// veille_metrics n'a pas encore d'instantané sur une base toute neuve (404) : cette variante
// évite qu'un endpoint légitimement absent fasse échouer le chargement d'un onglet entier.
export async function apiOrNull(path) {
  try { return await api(path); } catch (_) { return null; }
}
