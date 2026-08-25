# Observatoire Laser — optimisations 3.4.1

## Objectif

Faire évoluer le prototype vers un outil de revue mensuelle : nouveaux faits marché,
mouvements concurrents, reconfirmations, changements de sources et signaux technologiques.

## Changements implémentés

### Veille temporelle
- Ajout de `last_seen_at` aux faits marché, offres et documents technologiques.
- Index SQLite dédiés aux requêtes de statut, de date et de dernière observation.
- Nouvelle API `GET /api/monthly?days=30`.
- Nouvelle vue d'accueil **30 derniers jours** avec :
  - nouveaux faits marché ;
  - faits reconfirmés ;
  - nouvelles capacités concurrentes ;
  - nouveaux documents technologiques ;
  - pages sources modifiées.
- Nouvelle collecte composite `monthly` : acteurs -> marché -> technologie.

### Performance
- Les motifs lexicaux et variantes morphologiques sont mis en cache.
- Les textes déjà normalisés ne sont plus normalisés à nouveau pour chaque terme.
- La sélection des sources marché utilise des buckets pré-indexés au lieu de rescanner
  et retrier les mêmes listes.
- `scrape_market()` réutilise les blocs stockés par le crawl acteurs lorsqu'ils datent
  de moins de 24 h ; repli réseau sinon.
- `/api/overview` utilise une connexion par base au lieu d'une connexion SQLite par compteur.
- Le front-end ne recharge plus toutes les APIs toutes les 1,8 s pendant une collecte.
- Les recherches Acteurs et Offres sont débouncées.

Mesure indicative sur les données fournies, même replay (390 sources / 3713 blocs) :
- version d'origine : ~58,8 s ;
- version optimisée : ~31,5 s.
Le gain observé est d'environ 46 %, à considérer comme un benchmark local et non une garantie.

### Robustesse
- Le replay ignore proprement les payloads JSON invalides et les comptabilise dans les diagnostics.
- Les snapshots d'état des jobs sont protégés par le verrou.
- Migration FastAPI de `on_event("startup")` vers `lifespan`.
- Validation des dates côté front-end.
- Messages de fin de collecte courts, y compris pour la collecte mensuelle.

### Technologies futures
- Le collecteur Crossref n'utilise plus une seule requête générique.
- Six requêtes ciblées couvrent microusinage, fabrication, texturation, SLE, verre et semi-conducteurs.
- Déduplication par DOI/URL, filtre temporel et validation explicite du contexte femtoseconde/ultrarapide.
- Premier passage : backfill sur 730 jours ; passages suivants : 60 jours par défaut.

Important : cette brique couvre les **publications**. Les brevets et projets financés nécessitent
encore des connecteurs dédiés et ne sont pas simulés.

## Validation

- 48 tests passent.
- 5 subtests passent.
- `node --check static/app.js` passe.
- Les nombres de lignes des bases fournies sont inchangés après migration :
  - 20 acteurs ;
  - 2115 sources acteurs ;
  - 9 faits marché ;
  - 77 offres ;
  - 0 document technologique avant nouvelle collecte.

## Priorités produit restantes

1. **Couverture mondiale**
   Le référentiel de 20 acteurs est statique. Il faut un pipeline de découverte/qualification
   de nouveaux acteurs, plus une mesure de couverture par pays et segment.

2. **Rappel du moteur marché**
   La précision est volontairement forte mais le rappel reste faible. Conserver les faits
   validés stricts, mais ajouter une file de signaux "à valider" plutôt que supprimer les
   quasi-candidats trop tôt.

3. **Identité métier stable**
   `market_fact_key` contient actuellement le bucket (`radar` / `existing`). Une application
   qui passe du radar à l'industrialisation peut donc devenir deux faits. Introduire une
   `application_key` indépendante de la maturité et historiser les transitions.

4. **Brevets et projets**
   Ajouter des connecteurs dédiés (offices brevets / registres projets) avec une provenance
   explicite et une déduplication propre.

5. **Historique complet des changements**
   `last_seen_at` permet la revue mensuelle, mais il faut ensuite un journal d'événements
   (créé, modifié, confirmé, disparu, réapparu) pour analyser les mouvements dans le temps.

6. **Fallback navigateur**
   Le renderer Playwright existe mais n'est pas encore branché automatiquement au crawler.
   Les sites fortement JavaScript peuvent donc rester sous-couverts.

7. **Planification automatique**
   La collecte mensuelle est maintenant en un clic, mais il manque encore un scheduler
   persistant (cron/Task Scheduler/service) et des alertes de collecte échouée.
