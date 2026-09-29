# Audit de la partie veille — Observatoire Laser Femtoseconde

Date : 30/08/2026 · Version auditée : 3.4.1-optimized

> **Note de correction (chiffres du périmètre).** Les sections 1 à 8 ont d'abord été écrites à
> partir du seul code source, où la liste `ACTORS` de `db.py` compte **26 entrées**. La base
> réelle en contient **55** (54 actifs) : 29 acteurs ont été ajoutés à la main via l'API et
> n'apparaissent donc pas dans le code. Les mentions de périmètre ont été corrigées dans tout
> le document ; **le §9 est la référence pour tout chiffre**, puisqu'il est mesuré sur les bases
> elles-mêmes. Cet écart entre le code et la base est lui-même un symptôme, traité au §9.8.

## 0. Périmètre de l'audit

Lu intégralement : `app.py`, `db.py`, `scrapers.py`, `openalex.py`, `cordis.py`, `press.py`,
`scoring.py`, `capabilities.py`, `timeseries.py`, `site_profiles.py`, `prune_off_topic_sources.py`.

Ajoutés en cours d'audit et traités dans l'**addendum §8** : `hybrid.py` (parsing HTML/PDF,
client IA, classification des pages) et `firmographics.py` (registre FR). L'addendum corrige
et précise plusieurs points des sections 1 à 7 — le lire avant d'attaquer la feuille de route.

**Non disponible** (donc hors audit) : le front `static/`.

---

## 1. Ce qui est solide (et qu'il ne faut pas casser)

Le socle est meilleur que la moyenne des outils de veille que l'on croise. Quatre choses en
particulier :

**La traçabilité.** Chaque fait porte une URL, une citation verbatim vérifiée (`quote in
block.text`), un fingerprint de déduplication, un `date_confidence` (`published` /
`observed_only` / `unknown`) et un `is_backfill`. La séparation `evidence` (fait canonique) /
`evidence_sources` (preuves multiples) est le bon modèle : elle permet de compter les preuves
indépendantes sans dupliquer le fait.

**Le refus explicite de deviner.** L'exclusion de Fraunhofer-Gesellschaft du matching CORDIS,
la vérification par domaine dans OpenAlex, la liste d'alias SIREN vérifiée à la main, le refus
d'inventer une URL de flux RSS pour Optics.org : ce sont des décisions coûteuses et correctes.
Une veille qui sur-attribue est pire qu'une veille incomplète, parce qu'elle est indétectable.

**Le déterministe d'abord, l'IA en secours.** Lexiques `MARKETS`/`COMPONENTS`/`OPERATIONS` avec
`requires_any`/`exclude`, `NEGATION_CUES`, `_relation_window_is_ambiguous`, et surtout
`vocabulary_candidates` : quand l'IA propose un libellé inconnu, il ne rentre pas en base, il
entre dans une file de revue et n'enrichit le lexique qu'après validation humaine. C'est le
bon design, et c'est le modèle à généraliser (voir §5.G).

**La détection de changement.** `content_hash` + `market_extracted_hash` + `page_versions`
donnent un recrawl incrémental propre et un historique du "avant".

---

## 2. Le diagnostic central : le biais du miroir

Une seule critique de fond, dont découlent la plupart des autres.

**Aujourd'hui, l'observatoire ne mesure pas le marché du femtoseconde. Il mesure ce que
un périmètre d'acteurs entièrement choisi à la main écrivent sur leur propre site web.**

Trois conséquences, dans l'ordre de gravité :

1. **Aucun mécanisme de découverte.** `ACTORS` est une liste statique de 26 entrées dans
   `db.py` ; la base en contient 55, donc 29 ont été ajoutés un par un via `POST /api/actors`,
   c'est-à-dire par un humain qui savait déjà. **La totalité de la croissance du périmètre est
   manuelle** — ce qui est précisément le problème, et ce que confirme le §9.8 : la file
   `actors.review_status='candidate'` n'a jamais reçu une seule ligne. Une
   veille concurrentielle incapable de détecter le nouvel entrant est aveugle précisément là
   où l'information a le plus de valeur. Or les matériaux pour le faire sont **déjà en base** :
   les consortiums CORDIS remplissent `actor_relations.related_name` avec des organisations
   non rattachées, `press.py` jette les articles qui ne matchent aucun acteur connu, OpenAlex
   voit des institutions co-signataires. Ces trois flux passent devant des candidats et les
   laissent filer.

2. **La base marché est un agrégat de discours commercial.** `evidence` dit « tel acteur dit
   adresser tel marché ». Il n'y a nulle part une donnée de taille de marché, de croissance,
   de prix, ni le moindre signal venant de la **demande** (donneurs d'ordre, appels d'offres,
   normes). Un marché émergent dont personne ne parle encore sur son site est structurellement
   invisible ; un marché sur-communiqué est sur-représenté. Le dispositif ne peut pas non plus
   montrer les **cases vides** de la matrice marché × composant × opération — or « personne
   n'adresse ce couple » est une information business au moins aussi forte que l'inverse.

3. **La chronologie mesure surtout la date d'observation.** `date_confidence` est un très bon
   garde-fou, mais il ne crée pas de date là où le web n'en a pas. Les pages produit/service —
   la majorité du corpus — sont sans date fiable. Résultat : un pic dans `metric_snapshots` est
   d'abord un pic de crawl. Le seul horodatage vraiment fiable qu'un crawler puisse produire
   est le **diff** : « cette page a ajouté la mention batterie entre le 12/03 et le 14/04 ».
   `page_versions` stocke déjà le `blocks_json` d'avant… et rien ne le compare (aucun `difflib`
   dans le code, l'endpoint `/api/page-versions/{id}` ne renvoie même pas les blocs).

---

## 3. Anomalies fonctionnelles à corriger en priorité

Ce sont des bugs, pas des orientations.

### 3.1 `press.py` écrit dans une file de revue morte — et le contenu non validé s'affiche quand même

`press.py` crée ses `actor_events` avec `review_status='pending'`, en documentant explicitement
pourquoi (matching par mot-clé sur un titre d'article = signal faible). Or :

- **aucun endpoint n'expose `actor_events` à la revue** — le `pending` ne peut jamais devenir
  `verified` ;
- **`/api/actors` lit `actor_events` et `actor_facts` sans filtrer `review_status`**
  (`app.py:589` et `app.py:592`) — donc ces événements non validés s'affichent dans la fiche
  acteur exactement comme les événements sourcés par CORDIS.

Le garde-fou existe en base, il est contourné à l'affichage. C'est la faille la plus directe
entre l'intention du code et son comportement réel.

### 3.2 Cinq files de validation sur sept ne sont pas exposées

| File | Statuts en base | Endpoint de revue |
|---|---|---|
| `evidence` (faits marché) | accepted/review/rejected | ✅ `/api/market/review` |
| `vocabulary_candidates` | pending/accepted/rejected | ✅ `/api/vocabulary-candidates` |
| `offers` | accepted/review/rejected | ❌ aucun |
| `technology_signals` | accepted/review/rejected | ❌ aucun |
| `actor_events` | pending/verified/rejected | ❌ aucun |
| `actor_facts` | pending/verified/rejected | ❌ aucun |
| `actors` (`review_status='candidate'`) | candidate/verified/rejected/monitor | ❌ aucun (jamais alimenté non plus) |

Le schéma anticipe une gouvernance de la donnée que l'API ne permet pas d'exercer.

### 3.3 Pas d'ordonnancement, pas de notification

`POST /api/scrape/monthly` exige un clic humain, et rien ne signale ce qui a changé. Une veille
qu'il faut penser à lancer, puis penser à consulter, retombe à zéro dès la première semaine
chargée. Tous les ingrédients d'un digest sont pourtant déjà là : `evidence_bucket_transitions`,
`page_versions`, `metric_snapshots`, `created_at`.

### 3.4 Sites inaccessibles sans repli

laserKRAFTwerk répond 403 à votre User-Agent : la couverture de cet acteur est réduite à sa page
d'accueil. C'est signalé en commentaire mais aucun mécanisme ne le remonte comme incident, et
aucun repli n'est tenté (Wayback Machine, par exemple, sert des snapshots de pages qui vous
bloquent). Combien d'autres acteurs sont dans ce cas sans qu'on le sache ? Il n'existe aucune
vue « santé de la collecte par acteur » (`site_profiles.health_score` et `failure_count`
existent, mais rien ne les expose).

---

## 4. Améliorations par dimension

### 4.A — Dimension ACTEUR

**Diagnostic** : le « quoi » (offres, capacités, faits marché) est bien couvert. Le « combien »,
le « qui » et le « depuis quand » ne le sont pas. `actor_profile` a des colonnes
`revenue_eur`, `parent_group`, `sites_json`, `cleanroom_iso_class`, `laser_systems_count`…
toutes NULL, et n'est alimenté que pour la France, via une liste SIREN saisie à la main.

**Propositions, par rapport coût/valeur décroissant :**

1. **Les pages carrières, déjà croisées et volontairement jetées.** `site_profiles.py` met
   `/careers`, `/career`, `/jobs` dans `ignore_paths`. C'est cohérent pour l'extraction de faits
   marché, mais c'est jeter le meilleur proxy public de la trajectoire d'une entreprise. Un
   acteur qui recrute trois « process engineer – ultrafast laser » annonce sa direction
   technique 12 mois avant sa page produit. Coût : un `page_type='careers'` exclu du pipeline
   marché mais crawlé, plus une extraction d'intitulés. Le crawler passe déjà devant.

2. **Registres d'entreprises européens.** La moitié de vos acteurs sont en DE, LT, UK, NL, BE,
   CH — aucun n'est couvert. Par ordre d'accessibilité : Companies House (UK, API ouverte),
   OpenCorporates (multi-pays), GLEIF/LEI (identifiants + relations de groupe, gratuit et
   propre pour résoudre `parent_group`), KVK (NL), Registrų centras (LT), Zefix (CH),
   Unternehmensregister (DE, le plus pénible). Le LEI mérite d'être fait en premier : il
   résout à lui seul les rattachements de groupe, qui sont votre angle mort sur les
   consolidations.

3. **Historiser `actor_profile`.** La table est écrasée à chaque run (`updated_at`). Un
   changement d'effectif, de dirigeant ou de rattachement de groupe est perdu au moment même
   où il devient intéressant. Même traitement que `page_versions` : une table
   `actor_profile_versions`.

4. **Signaux M&A et financement.** `press.py` détecte déjà `investment`/`patent`/`recruitment`
   dans les titres, mais sur deux flux RSS seulement. Ajouter les **flux RSS des acteurs
   eux-mêmes** (beaucoup en ont, découvrables via `<link rel="alternate">` comme vous l'avez
   fait pour Laser Focus World) : c'est du contenu **daté**, ce qui règle en partie le problème
   de chronologie du §2.3, et c'est une source primaire, pas une mention de tiers.

### 4.B — Dimension MARCHÉ

**Diagnostic** : c'est la base la plus riche techniquement et la plus pauvre stratégiquement.
Elle répond très bien à « qui dit faire quoi » et pas du tout à « combien, pour qui, et à
quel prix ».

**Propositions :**

1. **Une matrice de référence marché × composant × opération**, indépendante des faits
   observés. Aujourd'hui l'app ne peut afficher que ce qu'elle a trouvé ; elle ne peut pas
   afficher ce que personne ne fait. Déclarer la matrice cible permet de calculer un taux de
   couverture par case et de faire ressortir les zones blanches — c'est le livrable qui
   intéressera le business development.

2. **Une table `market_sizing`** (source, périmètre exact, valeur, devise, année, CAGR,
   méthode, URL). Alimentée semi-manuellement depuis les rapports publics et les communiqués
   d'analystes, jamais moyennée entre sources, toujours affichée avec son périmètre — les
   chiffres de marché laser publiés mélangent allègrement machines, services et composants.
   Sans cette table, la dimension « marché » restera un synonyme de « application ».

3. **Des signaux de demande**, pour sortir du miroir de l'offre :
   - **appels d'offres publics** : TED (UE) et BOAMP (FR) ont des API ; un cahier des charges
     mentionnant du micro-usinage femtoseconde est un signal d'achat, pas un signal de discours ;
   - **offres d'emploi des donneurs d'ordre** (pas seulement des concurrents) : un équipementier
     médical qui recrute un ingénieur procédé laser internalise — c'est un marché qui se ferme ;
   - **normes et réglementation** : ISO/ASTM sur la texturation de surface, MDR pour le médical.
     Une norme qui bouge redistribue un marché entier.

4. **Les listes d'exposants de salons** (LASER World of Photonics, Photonics West, EPHJ, MD&M)
   sont des annuaires structurés, publics, mis à jour annuellement, et segmentés par domaine.
   C'est simultanément la meilleure source de découverte d'acteurs (§4.D) et un indicateur
   d'investissement commercial par acteur et par marché.

### 4.C — Dimension TECHNOLOGIE

**Diagnostic** : c'est la dimension la plus faible des trois. `scrape_technology()` fait six
requêtes Crossref figées, ne rattache **jamais** un document à un acteur (`documents.actor_name`
reste NULL), ne classe rien sur un axe technologique et ne produit aucun `technology_signal` —
cette table n'est alimentée que par CORDIS.

**Propositions :**

1. **Les brevets — le manque le plus criant.** `documents.document_type` accepte `'patent'`,
   rien ne le remplit. Pour du procédé laser, le brevet est le signal avancé par excellence :
   18 mois d'avance sur le produit, déposant identifié, revendications techniques exploitables.
   Deux voies : **EPO OPS** (API officielle, gratuite dans des quotas confortables, recherche
   CQL par déposant et par CPC) ou **Lens.org** (publications et brevets liés, plus simple à
   démarrer). Requêtes à croiser : par déposant (vos 55 acteurs), et par classe CPC —
   B23K26/xx couvre le travail au laser, avec des sous-classes fines pour le perçage, la
   découpe, la texturation. Les métriques qui en sortent (dépôts/an/acteur, juridictions
   couvertes, âge médian du portefeuille) sont directement comparables entre concurrents,
   ce qu'aucune de vos données actuelles ne permet.

2. **Rattacher les publications à un axe et à une maturité.** Le lexique
   `PROCESS_TECHNOLOGIES` et `_detect_maturity()` existent et sont déjà appliqués aux projets
   CORDIS. Les appliquer aussi aux documents Crossref/OpenAlex ferait passer
   `technology_signals` de « ce que finance la Commission » à « où en est chaque axe, de la
   publication au produit ». C'est un branchement, pas un développement.

3. **Les actes de conférence plutôt que les revues.** En procédés laser, l'industrialisation se
   raconte à LPM, LiM, LASE (Photonics West), pas dans les revues à comité de lecture. Crossref
   les indexe mal. OpenAlex et OpenAIRE les couvrent mieux ; les programmes de conférence sont
   souvent des PDF publics — et votre pipeline sait déjà parser du PDF (`parse_pdf_document`).

4. **Suivre les fabricants de sources laser.** Amplitude, Light Conversion, Trumpf, Coherent,
   Class 5 Photonics ne sont pas vos concurrents, mais leurs specs (puissance moyenne, cadence,
   burst, durée d'impulsion) définissent la frontière du possible pour tout le marché aval. Un
   saut de génération chez un fournisseur de sources précède de 12 à 24 mois les nouvelles
   applications chez vos concurrents. Ils devraient exister en base avec
   `actor_type='partenaire_adjacent'` et `is_reference=1` (donc hors score de menace), suivis
   uniquement pour leurs `capability_spec`.

5. **Fiabiliser `capability_spec` par les fiches techniques.** L'extraction déterministe par
   regex est le bon choix, mais les valeurs chiffrées réelles (µm, fs, kHz, mm/s) sont dans les
   PDF de datasheets, pas dans les pages HTML marketing. Le parsing PDF existe ; ce qui manque
   est la **découverte** de ces PDF — un `page_type='datasheet'` avec un boost de crawl sur les
   liens `.pdf` issus de pages produit/equipment. C'est probablement le meilleur rapport
   effort/valeur de tout ce document pour la comparaison technique.

### 4.D — Découverte d'acteurs (dimension manquante)

Nouvelle brique, transverse. `actors.review_status='candidate'` existe déjà en base et n'est
jamais alimenté : le schéma l'attend.

**Sources de candidats, par fiabilité décroissante :**

| Source | Signal | Déjà en base ? |
|---|---|---|
| Consortiums CORDIS | `actor_relations.related_name` non rattaché à un acteur connu | ✅ oui, ignoré |
| Affiliations/co-auteurs OpenAlex | institution récurrente sur des travaux on-topic | ✅ oui, ignoré |
| Mentions presse non appariées | organisation citée dans un article on-topic | ✅ oui, jeté |
| Concurrents cités sur les sites crawlés | « unlike X », « compared to Y » | ✅ pages en base, non exploitées |
| Listes d'exposants de salons | segmentation par domaine, annuelle | ❌ à ajouter |
| Annuaires de filières | Photonics France, EPIC, SPECTARIS, Lithuanian Laser Association | ❌ à ajouter |
| Déposants de brevets CPC B23K26 | déposant récurrent absent de la base | ❌ dépend du §4.C.1 |

Un candidat n'entre jamais en base comme acteur : il entre dans `actor_candidates` avec ses
occurrences, ses sources, un score (nombre de sources indépendantes × on-topic du contexte), et
attend une validation humaine. Les trois premières lignes du tableau ne demandent aucune source
nouvelle — uniquement de ne plus jeter ce qui traverse déjà le pipeline.

---

## 5. Améliorations transverses

### 5.E — Chronologie et profondeur temporelle

1. **Diff sémantique des `page_versions`.** Comparer les `blocks_json` avant/après pour produire
   des événements datés : bloc ajouté, terme du lexique apparu, spec chiffrée modifiée. C'est la
   seule date que vous maîtrisez à 100 %, et elle transforme
   `evidence.created_at` (« quand je l'ai vu ») en `first_appeared_at` (« quand ils l'ont
   écrit »). À stocker dans une table `page_changes` qui alimente à la fois le digest (§5.F)
   et les séries temporelles.

2. **Wayback Machine (API CDX) pour la rétro-datation.** Pour chaque page à forte valeur, la CDX
   donne la liste des snapshots ; comparer le plus ancien snapshot contenant un terme au premier
   ne le contenant pas date l'apparition d'une offre à quelques mois près, **rétroactivement**.
   C'est le seul moyen d'avoir deux ans de profondeur historique sans attendre deux ans. Sert
   aussi de repli pour les sites qui vous bloquent (§3.4).

### 5.F — Alerting et restitution

Un scheduler (APScheduler suffit, en process, avec un verrou puisque `start_scrape` en impose
déjà un) et une table `alerts` alimentée par des règles explicites :

- transition `radar → existing` sur un marché suivi ;
- nouveau fait `existing` chez un acteur C1/C2 ;
- nouvelle page `application`/`case_study` chez un C1 ;
- `capability_spec` franchissant un seuil de référence (ex. `min_feature_size_um` passant
  sous la vôtre) ;
- événement M&A / levée détecté ;
- nouveau candidat acteur au-dessus d'un score ;
- **incident de collecte** : acteur en échec, `health_score` en baisse, page stratégique
  disparue (un 404 sur une page service est un signal business, pas seulement technique).

Restitution : `GET /api/digest?since=` renvoyant le delta, plus un export Markdown ou e-mail
hebdomadaire. La règle à tenir : le digest ne doit contenir **que du changement**, jamais un
état. Un digest qui répète l'existant n'est plus lu au bout de trois semaines.

### 5.G — Validation : unifier, prioriser, apprendre

1. **Une file unique** `GET /api/review?queue=evidence|offers|tech_signals|events|facts|actors|vocabulary`,
   avec le même contrat pour toutes. Cela règle §3.2 d'un coup et rend la revue tenable —
   sept écrans séparés ne seront jamais tous consultés.
2. **Priorisation** par (classe concurrentielle de l'acteur × impact du fait × incertitude
   d'extraction). Valider trois faits sur un C1 vaut mieux que quarante sur un T1. Le
   `field_confidence` existe déjà et n'est utilisé nulle part pour trier.
3. **Traçabilité de la décision** : `reviewed_by`, `reviewed_at`, `reject_reason` (typé :
   hors sujet / mauvais acteur / mauvaise dimension / citation non probante / doublon). Sans
   motif typé, on ne peut rien apprendre des rejets.
4. **Boucle d'apprentissage.** `vocabulary_candidates` fait déjà remonter les acceptations vers
   le lexique — c'est exactement le bon modèle. Le généraliser : les motifs de rejet récurrents
   doivent alimenter les `exclude` des règles de lexique et un taux de précision par
   `extraction_mode`, pour savoir si c'est la règle déterministe ou l'IA qui produit le bruit.

### 5.H — Mesurer la qualité du dispositif, pas seulement des fiches

`_completeness` et `compute_confidence_scores` évaluent **les acteurs**. Rien n'évalue **la
veille**. Quatre indicateurs à construire :

- **Rappel** : sur un *golden set* de 30 à 50 faits vérifiés à la main (et d'une dizaine
  d'acteurs volontairement retirés de `ACTORS`), combien le pipeline retrouve-t-il ? À rejouer
  à chaque évolution des lexiques — c'est le seul garde-fou contre une régression silencieuse
  quand vous ajouterez des règles.
- **Précision** : part de faits rejetés en revue, ventilée par `extraction_mode` et par source.
- **Latence de détection** : médiane `published_date → created_at`. C'est votre indicateur
  d'avance concurrentielle réelle.
- **Santé de couverture** : acteurs sans crawl réussi depuis N jours, pages stratégiques
  manquantes par acteur (`site_profiles.coverage_json` le sait déjà et ne le dit à personne).

---

## 6. Feuille de route proposée

Ordonnée par (impact × faisabilité). Les estimations supposent que vous travaillez seul sur
la base de code existante.

### Lot 1 — Réparer et fermer les boucles (≈ 1 semaine)
Rien de nouveau, uniquement rendre exploitable ce qui existe.

1. Filtrer `review_status` dans `/api/actors` pour `actor_facts` et `actor_events` (§3.1) — 1 h.
2. File de revue unifiée couvrant les 7 files, avec priorisation et motif de rejet (§3.2, §5.G) — 2 j.
3. Scheduler + table `alerts` + `/api/digest` (§3.3, §5.F) — 2 j.
4. Vue « santé de collecte » exposant `health_score`, `failure_count`, `coverage_json` (§3.4) — 0,5 j.

### Lot 2 — Élargir les capteurs à coût marginal (≈ 1 à 2 semaines)
Sources nouvelles, mais que le crawler existant atteint déjà ou presque.

5. `page_type='careers'` : crawlé, exclu du pipeline marché, extrait en signaux (§4.A.1) — 1 j.
6. `page_type='datasheet'` : suivre les `.pdf` depuis les pages produit, alimenter
   `capability_spec` (§4.C.5) — 2 j. **Le meilleur rapport effort/valeur du document.**
7. Flux RSS des acteurs eux-mêmes, découverts via `<link rel="alternate">` (§4.A.4) — 1 j.
8. Diff des `page_versions` → table `page_changes` → digest et séries temporelles (§5.E.1) — 2 j.

### Lot 3 — Ouvrir le périmètre (≈ 2 à 3 semaines)

9. `actor_candidates` alimenté par CORDIS / OpenAlex / presse non appariés (§4.D) — 3 j.
10. Connecteur brevets EPO OPS ou Lens, par déposant et par CPC (§4.C.1) — 4 j.
11. Classification axe + maturité des documents Crossref/OpenAlex → `technology_signals`
    (§4.C.2) — 1 j (branchement de lexiques existants).
12. GLEIF/LEI puis Companies House pour `parent_group` et l'identité groupe (§4.A.2) — 3 j.

### Lot 4 — Sortir du miroir de l'offre (≈ 3 semaines)

13. Matrice de référence marché × composant × opération + vue des zones blanches (§4.B.1) — 3 j.
14. Table `market_sizing` + saisie sourcée (§4.B.2) — 2 j.
15. Signaux de demande : TED/BOAMP, emploi côté donneurs d'ordre (§4.B.3) — 4 j.
16. Fabricants de sources laser en `partenaire_adjacent` (§4.C.4) — 1 j.
17. Golden set + métriques de qualité du dispositif (§5.H) — 3 j.
18. Rétro-datation Wayback CDX (§5.E.2) — 3 j.

---

## 7. Trois écueils à éviter en chemin

**Ne pas laisser le volume de sources dégrader la précision.** La force actuelle de l'app est
son taux de faux positifs bas. Chaque nouvelle source (presse, salons, emploi) est plus bruyante
que le site officiel d'un acteur. La règle à tenir : toute source dont la fiabilité d'attribution
est inférieure à celle d'un site officiel entre en `pending`, jamais en `accepted` — c'est déjà
ce que fait `press.py`, et c'était la bonne décision.

**Ne pas confondre plus de données et plus de discernement.** L'app produit déjà plus de faits
qu'un humain n'en valide. Avant d'ajouter des capteurs (lots 3-4), le lot 1 doit rendre la
validation tenable, sinon la file de revue devient un cimetière et les scores de confiance
s'effondrent mécaniquement.

**Garder les scores lisibles.** `confidence_score`, `threat_score`,
`market_attractiveness_score` et `completeness_score` sont des choix éditoriaux documentés, ce
qui est bien. Mais quatre scores agrégés sur une même fiche, c'est déjà à la limite de ce qu'un
lecteur peut interpréter. Chaque score devrait pouvoir être « déplié » sur ses composantes dans
l'UI, sinon il sera lu comme une note absolue et personne ne saura pourquoi un acteur passe de
62 à 71.

---

## 8. Addendum — après lecture de `hybrid.py` et `firmographics.py`

Ces deux modules confirment l'essentiel du diagnostic, mais ils **déplacent trois priorités** et
révèlent un chemin mort que je n'avais pas pu voir.

### 8.1 `parse_pdf_document()` est, en pratique, du code mort

C'est la découverte la plus importante de cette seconde passe. Le parseur PDF est écrit,
soigné (`pdfplumber`, découpage en paragraphes, date depuis `/CreationDate`, même contrat
`ParsedDocument` que le HTML) — et le crawler ne peut quasiment jamais lui donner un PDF à
manger. Trois filtres se ferment successivement :

1. **`_meaningful_links` (hybrid.py:372)** ne retient un lien de contenu que s'il porte un
   `DISCOVERY_TERM` ou un `priority_path`. Or `DISCOVERY_TERMS` ne contient ni *datasheet*, ni
   *fiche technique*, ni *brochure*, ni *technische daten*, ni *download*, ni *spécifications*.
   Un lien « Download datasheet (PDF) » posé dans le corps d'une page produit est jeté avant
   même d'être classé.
2. **`classify_source`** appliqué à une URL de PDF (typiquement
   `/wp-content/uploads/2024/xyz-datasheet.pdf`) ne matche aucune règle → `other`, score de
   base 15, le plus bas hors `ignore`. Même s'il passait le filtre 1, il resterait au fond de
   la file de priorité et le budget de pages serait épuisé avant.
3. **Aucun type `datasheet`/`pdf`** n'existe dans `SECTION_SCORES` ni dans `page_type_boosts`,
   donc aucun profil de site ne peut le remonter.

L'ironie est que `firmographics.py` documente lui-même le manque : *« capability_spec […]
nécessiterait une extraction numérique depuis des datasheets PDF, un chantier à part
entière »*. Le chantier est déjà fait à 90 %. Ce qui manque, c'est **la découverte**, pas
l'extraction — soit une vingtaine de lignes :

- ajouter à `DISCOVERY_TERMS` : `datasheet`, `data sheet`, `fiche technique`, `technische daten`,
  `brochure`, `specification`, `spécifications`, `download`, `téléchargement`, `catalog`,
  `catalogue`, `white paper` ;
- dans `_meaningful_links`, traiter tout `href` finissant par `.pdf` comme intrinsèquement
  digne d'intérêt (comme un lien de navigation), sans exiger de terme de découverte ;
- ajouter un type `datasheet` à `SECTION_SCORES` (≈ 90, au niveau de `news`) et une règle dans
  `classify_source` sur le suffixe `.pdf` croisé au libellé du lien.

**Bénéfice double** : c'est aussi une réponse au problème de chronologie (§8.3), puisque
`_pdf_metadata_date()` sait déjà lire `/CreationDate` — un datasheet PDF est souvent daté là où
une page produit HTML ne l'est jamais. Ce point passe **en tête du lot 2**.

### 8.2 Les liens sortants sont détruits, pas seulement ignorés

`_meaningful_links` filtre sur `parsed.netloc != base_host` : tout lien externe est écarté avant
d'atteindre la base. Deux conséquences que je sous-estimais :

- **Le canal de découverte d'acteurs « concurrents et partenaires cités sur les sites
  crawlés » (§4.D) n'est pas inexploité, il est impossible** — la donnée n'existe nulle part.
  C'est aussi pourquoi `actor_relations` ne se remplit que par CORDIS.
- Les **sous-domaines** tombent avec (`shop.`, `docs.`, `en.`, `produkte.`) : un acteur dont le
  catalogue vit sur un sous-domaine est partiellement invisible, sans que rien ne le signale.

Correctif proposé, de coût quasi nul : une table `outbound_links` (page source, URL cible, hôte,
libellé, date de première vue) alimentée au passage du crawler, **sans crawler ces liens**. Un
hôte externe qui revient sur cinq sites d'acteurs différents est un candidat acteur de très
bonne qualité — bien meilleur qu'une mention presse. Et les sous-domaines du même domaine
racine devraient être admis dans le crawl, pas traités comme externes.

### 8.3 La chronologie est plus fragile que je ne l'écrivais — le diff devient prioritaire

`_extract_published_date` est bien fait, mais ses trois voies (`meta article:published_time`,
JSON-LD `datePublished`, motif `/YYYY/MM/DD/` dans l'URL) sont **toutes des conventions de CMS
d'actualités**. Une page service, application, capability ou product n'en a aucune. Autrement
dit `date_confidence='published'` ne pourra jamais couvrir que la famille `news`, qui est une
fraction minoritaire du corpus.

Ce n'est donc pas que la chronologie est imparfaite : elle est **structurellement absente sur
le cœur du corpus**, et aucune amélioration de l'extraction de date n'y changera quoi que ce
soit. Le diff de `page_versions` (§5.E.1) n'est pas une option parmi d'autres, c'est la seule
source de date fiable possible pour les pages qui portent vos faits marché. **Je le remonte du
lot 2 au lot 1.**

### 8.4 `careers` est bloqué deux fois, dont une non contournable

Ma proposition §4.A.1 (exploiter les pages carrières) demande plus qu'un retrait des
`ignore_paths`. `classify_source` (hybrid.py:316) contient une **regex en dur**
(`careers?|recrutement|jobs?|...`) qui renvoie `ignore` quel que soit le profil de site. Un
profil ne peut donc pas rouvrir ce que cette ligne ferme.

Au-delà du cas carrières, c'est un défaut de conception à corriger : la liste d'exclusion
existe en double, une fois configurable (`ignore_paths`) et une fois en dur. Le contenu de la
regex devrait descendre dans `DEFAULT_SITE_PROFILE`, ce qui rendrait l'exclusion surchargeable
par acteur — utile bien au-delà des carrières.

### 8.5 `firmographics` couvre 3 acteurs sur 55, pas « la France »

`FRENCH_REGISTRY_ALIASES` contient exactement trois SIREN : ALPHANOV, MANUTECH USD, IREPA
LASER. Sur les 55 acteurs réellement en base (voir §9.8), la couverture firmographique est donc
de **5,5 %**, pas « France uniquement ». Et le module documente précisément pourquoi il ne peut pas grandir tout seul : le
matching par nom a produit des faux positifs vérifiés (MANUTECH → société de logistique dans les
Landes, IREPA LASER → son propre CSE).

C'est un argument décisif en faveur du **LEI/GLEIF** que je proposais en §4.A.2, et il faut le
faire avant les registres nationaux : le LEI est un identifiant univoque, gratuit, multi-pays,
qui porte en plus les relations de maison mère. Il résout exactement le problème que ce module
constate — et une fois le LEI obtenu, chaque registre national devient interrogeable par
identifiant plutôt que par nom, c'est-à-dire sans le risque d'attribution qui a bloqué le
chantier.

### 8.6 Le malus sur les pages `equipment` devrait dépendre du type d'acteur

`SECTION_SCORES["equipment"] = 40`, plus un boost de `-5` : les pages machines sont
délibérément enfouies. C'est le bon arbitrage pour un prestataire, dont le catalogue est du
bruit marketing. Mais c'est exactement l'inverse pour les deux usages que je propose :

- les **specs chiffrées** qui alimentent `capability_spec` vivent sur ces pages ;
- pour un **fournisseur de sources laser** suivi en `partenaire_adjacent` (§4.C.4), la page
  équipement *est* l'information — c'est même la seule qui compte.

`page_type_boosts` étant déjà défini par profil de site, il suffit de le moduler selon
`actor_type` : `-5` pour un prestataire, fortement positif pour un partenaire adjacent. Aucun
changement de structure.

### 8.7 Aucun plafond de coût IA — à traiter avant d'automatiser

`estimate_anthropic_cost_usd()` mesure la dépense *a posteriori* et `/api/overview` l'expose,
mais rien ne la **borne** : pas de budget maximum par run, pas de coupe-circuit, pas de
plafond de tokens. Il existe un budget de pages (`crawl_budget`), pas de budget d'appels IA.

Tant que la collecte est déclenchée à la main, le risque reste visible. Dès que le scheduler
du §5.F tourne tout seul chaque mois sur 55 acteurs, il devient invisible. **Un plafond par run
avec arrêt propre du repli IA (retour au déterministe seul) est un prérequis de
l'automatisation**, pas une amélioration ultérieure. À ajouter au lot 1, à côté du scheduler.

Note annexe : `ANTHROPIC_PRICING_PER_MTOK` est un instantané maintenu à la main, comme son
commentaire l'indique — à revérifier avant d'en faire une base de budget réelle.

### 8.8 Feuille de route révisée

Les changements par rapport au §6 :

| Élément | Avant | Après | Raison |
|---|---|---|---|
| Diff `page_versions` | Lot 2 | **Lot 1** | Seule source de date possible sur le cœur du corpus (§8.3) |
| Découverte des PDF | Lot 2 (2 j) | **Lot 1 (0,5 j)** | Le parseur existe déjà ; il ne manque que ~20 lignes (§8.1) |
| Plafond de coût IA | absent | **Lot 1 (0,5 j)** | Prérequis du scheduler (§8.7) |
| Table `outbound_links` | implicite dans le lot 3 | **Lot 2 (0,5 j)** | Coût quasi nul, débloque la découverte d'acteurs (§8.2) |
| GLEIF/LEI | Lot 3, après les registres | **Lot 3, en premier** | Débloque tous les registres nationaux (§8.5) |
| Pages carrières | 1 j | 1,5 j | Double blocage, dont une regex en dur à rendre configurable (§8.4) |

Le lot 1 reste tenable en une semaine et devient nettement plus rentable : à sa sortie, vous
avez une chronologie réelle, les specs chiffrées des datasheets, une validation exerçable et
une automatisation dont le coût est borné.

---

## 9. Addendum — confrontation aux données réelles (`actors.db`, `market.db`, `technology.db`)

État au 30/08/2026 : 55 acteurs, 16 118 sources découvertes, 152 faits marché, 380 offres,
22 documents scientifiques, 10 signaux technologiques.

Les chiffres confirment le diagnostic, **corrigent un point du §8**, et font apparaître quatre
problèmes que la lecture du code ne pouvait pas révéler.

### 9.1 Le crawl découvre dix fois ce qu'il visite — et le reliquat s'accumule sans fin

| | |
|---|---|
| URLs découvertes | 16 118 |
| **Jamais visitées** | **14 410 (89,4 %)** |
| Visitées | 1 708 |
| dont HTTP 200 | 1 514 |

`actor_sources` ressemble à une mesure de couverture ; c'est en réalité à 89 % une file
d'attente. Laser Zentrum Hannover : 1 822 URLs découvertes, **48 visitées (2,6 %)**. Tekniker :
968 / 32. TWI : 1 046 / 30. Le budget de crawl (`crawl_budget`, ~25-30 pages/acteur) est
consommé pendant que la file grossit de plusieurs centaines d'URLs par run.

Aggravant : **9 507 des 16 118 URLs (59 %) sont classées `other`** (score de base 15, le plus
bas hors `ignore`). Elles ne remonteront jamais en tête de file, et rien ne les purge. La base
grossit d'un backlog qui ne sera jamais traité et qui, chaque fois qu'on relance
`_select_market_sources` (dont le filtre `last_http_status IS NULL OR BETWEEN 200 AND 399`
laisse passer les pages jamais visitées), entre en concurrence avec les pages réellement
informatives.

**Deux correctifs, indépendants :**
- **Purger le backlog** : une URL découverte, non visitée, classée `other`, vue depuis plus de
  N runs sans jamais remonter, doit être supprimée ou marquée `active=0`. Sinon la base ne fait
  que croître.
- **Améliorer le typage plutôt que le budget** : 59 % d'`other` signifie que `classify_source`
  échoue sur la majorité des URLs. Augmenter le budget de crawl ne servirait qu'à visiter plus
  de pages non typées. C'est le classifieur qu'il faut travailler — probablement en exploitant
  le libellé du lien, aujourd'hui utilisé mais noyé dans `words` avec le chemin d'URL.

### 9.2 Quatre acteurs sont bloqués, pas un seul — et rien ne le signale

Je n'avais identifié que laserKRAFTwerk. La réalité est plus large : **194 sources en échec
réseau**, et un run à **129 erreurs sur 286 pages (45 %)** (run n°20 du 30/08).

| Acteur | Échecs | Cause |
|---|---|---|
| Workshop of Photonics | 72 | 403 Forbidden |
| Micreon | 53 | 403 / 404 |
| Laser Micromachining Ltd | 24 | **401 Unauthorized** |
| AJS Production SA | 18 | 403 Forbidden |

Sept `site_profiles` sont en statut `degraded` avec un `health_score` moyen de **0,3**. Rien de
tout cela n'est exposé : `/api/overview` ne remonte pas le taux d'erreur par run, et il n'existe
aucune vue de santé par acteur.

Le cas le plus gênant est **Workshop of Photonics** : c'est le 2ᵉ acteur le mieux documenté de
la base (8 faits acceptés) *et* celui qui échoue le plus. Les données dont vous disposez sur lui
viennent de crawls partiels, sans que rien ne le dise à la lecture de sa fiche.

**Note annexe** : le run n°16 est resté à `status='running'` depuis le 30/08 13:03. Si le
process meurt en cours de collecte, `_run_job` met à jour le dictionnaire `jobs` en mémoire mais
la ligne `collection_runs` reste orpheline pour toujours. Une réconciliation au démarrage
(`UPDATE collection_runs SET status='interrupted' WHERE status='running'`) coûte trois lignes.

### 9.3 Correction du §8.1 : les PDF ne sont pas ignorés, ils sont mal ciblés

J'avais écrit que `parse_pdf_document()` était « du code mort ». **C'est trop fort** : 114 URLs
`.pdf` sont en base, 47 ont été visitées, **27 ont bien été parsées** en `pdf-text`. Le chemin
fonctionne.

Mais le résultat confirme le fond du problème : sur ces 27 PDF, on trouve un catalogue
d'entreprise Pulsar, des rapports annuels Fraunhofer, des conditions générales de vente
Femtika, et **une offre d'emploi** (`lightmotif_software_engineer.pdf`). **Zéro datasheet.** Et
surtout :

- **0** ligne de `capability_spec` a une source PDF ;
- 2 faits marché et 6 offres seulement viennent d'un PDF, sur 532.

Autrement dit, les PDF qui passent sont ceux qui passent par accident (liens de navigation,
libellés contenant `publication`/`paper`), pas ceux qui portent les specs. La correction du
§8.1 reste la bonne — ajouter les termes datasheet/fiche technique/brochure/download aux
`DISCOVERY_TERMS` et traiter `.pdf` comme intrinsèquement digne d'intérêt — mais l'énoncé exact
est : **le taux de captation des PDF utiles est proche de zéro**, pas le mécanisme.

### 9.4 Problème nouveau : `capability_spec` contient des valeurs physiquement invraisemblables

C'est la découverte la plus préoccupante de cette passe, parce qu'elle est **silencieuse**.
`capability_spec` a 35 lignes, mais :

| Champ | Rempli |
|---|---|
| `materials_qualified` | 35/35 |
| `wavelengths_nm` | 10/35 |
| `tolerance_um` | 9/35 |
| `min_feature_size_um` | 6/35 |
| `max_part_size_mm` | 6/35 |
| `pulse_duration_fs` | 4/35 |
| `throughput_units_per_h` | 1/35 |
| `batch_size_range` | 0/35 |

L'« enveloppe de capacités chiffrées » est donc, à 90 %, une liste de matériaux. Et les rares
nombres présents ne tiennent pas l'examen physique :

- **Longueurs d'onde** : un acteur porte `[200, 206, 250, 257, 258, 300, 330, 343, 515, 1030,
  1064, 2000]`. 206, 258 et 330 nm ne sont des raies laser d'aucune source industrielle
  courante. Un autre en porte 14, dont 1532, 1950, 2090, 2095 et 2100 nm — c'est un catalogue
  de fournisseur, pas la capacité d'un atelier.
- **Taille de pièce** : 2 680 mm et 983 mm. Une pièce de 2,7 m en micro-usinage femtoseconde
  n'existe pas ; le nombre vient d'ailleurs sur la page.
- **Taille de motif minimale** : de 0,1 µm à 50 µm selon les acteurs, sans que rien ne
  distingue une valeur crédible d'un chiffre attrapé au vol.

Le module fait de l'extraction par regex — c'est le bon choix — mais **sans aucun contrôle de
plausibilité de domaine**. Un nombre suivi de « nm » ou « mm » est accepté tel quel. Et comme
`capability_spec` n'a **pas de colonne `review_status`**, aucune file de validation ne peut
rattraper l'erreur : ces valeurs sont publiées directement.

**Correctif** : un filtre de plage par champ, avant écriture. Longueurs d'onde restreintes à un
ensemble de raies connues (±5 nm autour de 257/343/355/515/532/1030/1064…), taille de motif
0,5–200 µm, taille de pièce ≤ 600 mm, durée d'impulsion 100–1500 fs. Tout ce qui sort de la
plage n'est pas écrit — ou est écrit avec un `review_status='review'`, ce qui suppose d'ajouter
la colonne. C'est une demi-journée et cela transforme un tableau trompeur en tableau exploitable.

### 9.5 Problème nouveau : la garantie de validation est inversée

| Table | Exigence d'extraction | En revue | Acceptés |
|---|---|---|---|
| `evidence` | marché **+** composant **+** opération | **81 (53 %)** | 71 |
| `offers` | aucune dimension obligatoire | **0** | **380 (100 %)** |
| `technology_signals` | — | **0** | **10 (100 %)** |

Le pipeline le plus strict est celui qui passe en revue ; le plus permissif est publié
d'office. C'est exactement l'inverse de ce que le risque commande. Et ce n'est pas marginal :
les offres sont **2,5 fois plus nombreuses** que les faits marché, et **45 acteurs sur 54** ont
au moins une offre acceptée contre **25** pour les faits marché. Autrement dit, **l'essentiel
de ce que l'application affiche sur les acteurs n'a jamais été validé par personne**, et le
`confidence_score` de `scoring.py` compte ces 380 offres comme « validées » (`review_status =
'accepted'`), ce qui gonfle mécaniquement la confiance affichée.

Ce n'est pas seulement l'absence d'endpoint que je signalais en §3.2 : le défaut par défaut du
schéma (`offers.review_status DEFAULT 'accepted'`) fait que le pipeline n'écrit **jamais**
`'review'`. Exposer un endpoint ne suffira pas — il faut d'abord que quelque chose alimente la
file. Un seuil sur `field_confidence` est le candidat évident, il est déjà calculé et n'est
utilisé nulle part.

### 9.6 La chronologie, en chiffres

Confirmation quantitative du §8.3 :

- `evidence` : **125/152 en `date_confidence='unknown'` (82 %)**, 12 `observed_only`, 15
  `published` — et ces 15 sont *toutes* `is_backfill=1`.
- `offers` : **285/380 `unknown` (75 %)**.
- Pages effectivement téléchargées : **553/1 708 ont un `published_date` (32 %)**.
- `metric_snapshots` : **une seule période, `2026-08`**. Les « séries temporelles » comptent un
  point.

Il n'y a donc, à ce jour, **aucune profondeur historique exploitable**. Le diff de
`page_versions` (120 lignes, toutes archivées le 30/08) commencera à en produire à partir du
prochain run ; la rétro-datation Wayback (§5.E.2) est le seul moyen d'en avoir avant plusieurs
mois. Je maintiens les deux en lot 1 et lot 4 respectivement.

### 9.7 La dimension technologique est quasi vide — et Crossref n'a jamais tourné

`technology.db` contient **une seule ligne** dans `collection_runs`, et c'est celle d'OpenAlex.
**`scrape_technology()` (les six requêtes Crossref) n'a jamais été exécutée.**

Le reste est à l'avenant :
- **22 documents**, tous des publications OpenAlex, réparties sur **7 acteurs** (ALPHANOV 7,
  IREPA 4, Fraunhofer IWS 4…), toutes datées de 2026. Le run avait pourtant enregistré
  **203 ajouts** : 181 ont été supprimées depuis (`prune_off_topic_sources.py`), soit un taux
  d'élagage de **89 %**. Soit OpenAlex est très bruyant sur ce périmètre, soit l'élagage est
  trop agressif — dans les deux cas, personne ne le mesure.
- **0 brevet**, **0 projet** (le type `project` existe et reste vide).
- **10 signaux technologiques**, tous en `bucket='radar'`, **aucun en `existing`** — et trois
  portent la maturité « non déterminée ». La dimension technologique ne contient donc, à ce
  jour, **aucun signal industrialisé**.

Cela confirme que c'est la dimension la plus faible des trois, et rend le connecteur brevets
(§4.C.1) et le branchement axe/maturité sur les publications (§4.C.2) plus urgents que je ne
l'écrivais.

### 9.8 Ce que la base dit du périmètre acteurs

- **55 acteurs** (contre 25 dans la liste `ACTORS` du code) : 30 ont donc été ajoutés à la main
  via l'API. La croissance du périmètre est entièrement manuelle — confirmation directe du §2.1.
- **`review_status` : 53 `verified`, 1 `rejected`, 0 `candidate`, 0 `monitor`.** La file de
  candidats n'a jamais reçu une seule ligne.
- **9 acteurs actifs sur 54 n'ont ni fait marché ni offre**, dont **TWI et BIAS** — les deux
  centres technologiques ajoutés lors de l'audit précédent, avec 30 pages crawlées chacun. Et
  **29 sur 54 (54 %) n'ont aucun fait marché accepté.** Crawler un acteur ne produit donc pas
  d'information une fois sur deux ; cet écart n'est mesuré nulle part.
- `actor_profile` : **3 lignes sur 55 (5,5 %)** — la couverture firmographique n'a pas suivi la
  croissance du périmètre (les 3 SIREN datent de la période où la base comptait 26 acteurs).
- `actor_relations` : **5 lignes pour 55 acteurs**. Le graphe de réseau est vide en pratique.
- HiLASE Centre est en base avec **1 source et 0 page téléchargée** : un acteur sans aucune
  donnée, indistinguable des autres dans les listes.

### 9.9 Ce que ces mesures changent dans la feuille de route

Trois ajouts au **lot 1**, tous courts et tous correctifs d'erreurs silencieuses :

| Nouveau | Effort | Pourquoi maintenant |
|---|---|---|
| Contrôle de plausibilité sur `capability_spec` + colonne `review_status` | 0,5 j | Publie aujourd'hui des chiffres faux, sans filet (§9.4) |
| Alimenter la file `review` des `offers` depuis `field_confidence` | 0,5 j | 380/380 publiées sans validation, et comptées comme validées par le score de confiance (§9.5) |
| Vue de santé de collecte + réconciliation des runs orphelins | 0,5 j | 4 acteurs bloqués, 45 % d'erreurs sur un run, invisible (§9.2) |

Un ajout au **lot 2** : purge du backlog de sources non visitées et travail sur
`classify_source` (§9.1) — sans quoi chaque run continuera d'ajouter des centaines d'URLs
`other` qui ne seront jamais lues.

Et un constat de priorité : **lancer `scrape_technology()` une première fois** coûte un clic et
comblerait immédiatement une base à 22 documents. C'est probablement l'action la plus rentable
de tout ce document.

---

# 10. Audit approfondi — analyse quantitative

Cette section reprend l'audit à zéro, non plus par lecture de code mais par mesure sur les
données. Douze axes ont été instruits : intégrité référentielle, churn, qualité des citations,
reproductibilité, déduplication, couverture taxonomique, rendement du crawl, pouvoir
discriminant des scores, latence de détection, couverture géographique, normalisation des
taxonomies, économie de la collecte.

Elle contient **trois défauts critiques** qui n'étaient pas visibles dans les sections
précédentes, et **corrige une recommandation** du §9.

## 10.1 Tableau de bord — l'état réel du dispositif

| # | Indicateur | Valeur | Lecture |
|---|---|---|---|
| 1 | Acteurs suivis | 55 (54 actifs) | Croissance 100 % manuelle |
| 2 | **Acteurs hors Europe** | **0** | Angle mort total (§10.8) |
| 3 | URLs découvertes | 16 118 | dont **89,4 % jamais visitées** |
| 4 | Pages téléchargées avec succès | 1 514 | |
| 5 | **Pages ayant produit ≥ 1 preuve** | **259 (17 %)** | **83 % du crawl est stérile** (§10.5) |
| 6 | Faits marché acceptés | 71 | dont **40 (56 %) non reproductibles** (§10.2) |
| 7 | Faits marché en attente de revue | 81 (53 %) | |
| 8 | Offres acceptées | 380 (**100 %**) | Aucune n'a été validée (§9.5) |
| 9 | Preuves non verbatim | 41/198 (**21 %**) | La garantie phare a un trou d'un cinquième |
| 10 | Faits datés de façon fiable | 15/152 (10 %) | Et les 15 sont toutes du backfill |
| 11 | Amplitude du corpus | **7 jours** (23→30/08) | Aucune série temporelle possible |
| 12 | Couverture de la matrice marché × opération | 38/143 (**27 %**) | Triplets : 57/2 431 (**2,3 %**) |
| 13 | Brevets | **0** | Type prévu, jamais alimenté |
| 14 | Signaux techno en `existing` | **0/10** | Aucun signal industrialisé |
| 15 | `capability_spec` : champs numériques remplis | 1 à 10 sur 35 | Le reste : liste de matériaux |
| 16 | Couverture firmographique | 3/55 (5,5 %) | |
| 17 | Acteurs sans aucun fait ni offre | 9/54 | |
| 18 | Acteurs sans score de confiance | 10/55 | Affichés comme les autres |

**Intégrité référentielle : aucune anomalie.** Zéro ligne orpheline dans les sept tables liées à
`actors`, zéro `actor_name` orphelin entre `market.db`/`technology.db` et `actors.db`. Le
`ON DELETE CASCADE` fonctionne et le couplage par nom entre bases tient. C'est un point à
porter au crédit du schéma — c'était le risque structurel évident d'une architecture à trois
fichiers, et il est maîtrisé.

---

## 10.2 CRITIQUE #1 — 56 % des faits marché acceptés ne sont pas reproductibles

**C'est le défaut le plus grave de l'application, et il est invisible dans l'interface.**

Sur les 71 faits marché acceptés, **40 n'ont aucune preuve verbatim**. Leur profil est
homogène et sans ambiguïté :

- `extraction_mode = NULL` pour les 40 ;
- `created_at = 2026-08-25` pour les 40 ;
- `review_status = 'accepted'` pour les 40 ;
- répartis en lots nommés : `optionc-batch` (13), `deep5-2026` (8), `batch2-2026` (7),
  `femtoprint-fiche-2026` (6), `optek-deep-2026` (3), plus 3 unitaires.

Ce sont des données de seed insérées via `SEED_EVIDENCE`. Or **`SEED_EVIDENCE` est aujourd'hui
une liste vide** dans `db.py` (ligne 132). Le code qui a produit 56 % de votre base marché
n'existe plus.

Leurs « citations » sont d'ailleurs des **résumés rédigés à la main en français** décrivant des
pages en anglais :

> « Sous-rubriques sur les guides d'onde en verre, ferrules de fibres, réseaux de trous… »
> (`femtoprint.ch/applications/photonics.asp`)

Ce n'est pas une citation, c'est une note de lecture. La migration `db.py:1192` les a
correctement marquées `is_verbatim=0` — mais **rien n'expose ce drapeau** : dans
`/api/actors` et dans les vues marché, ces faits sont strictement indistinguables d'un fait
extrait, vérifié caractère par caractère et confirmé par plusieurs sources.

**Quatre conséquences, par ordre de gravité :**

1. **Une réinstallation perd 56 % de la base marché, en silence.** `reset_market_db.py` ou un
   déploiement neuf repart de `SEED_EVIDENCE = []`. Rien n'alerte.
2. **La promesse de traçabilité couvre 44 % des faits**, pas 100 %. C'est la caractéristique
   que je citais en §1 comme le principal atout de l'application. Elle est vraie du pipeline,
   fausse de la base.
3. **Ces 40 faits gonflent tous les scores.** Ils sont `accepted` et `validated`, donc comptés
   par `compute_confidence_scores`, `compute_threat_scores` et
   `compute_market_attractiveness_scores`. Ils touchent 17 acteurs, dont FEMTOprint (le plus
   documenté de la base).
4. **Le biais éditorial est indétectable.** Ces faits ont été choisis à la main : ils reflètent
   ce que vous saviez déjà en août 2026, pas ce que le dispositif sait observer. Ils font
   paraître la veille plus performante qu'elle ne l'est, et ils sont concentrés sur les acteurs
   qui vous intéressent le plus — exactement là où l'illusion coûte le plus cher.

**Correctif (0,5 j, à faire en premier) :**
- exposer `is_verbatim` dans l'API et le marquer visuellement (« saisie manuelle » vs
  « extrait vérifié ») ;
- exclure les faits `is_verbatim=0` du calcul des scores, ou les pondérer explicitement ;
- **regénérer `SEED_EVIDENCE` depuis la base** (`SELECT` des 40 lignes → littéral Python) pour
  restaurer la reproductibilité, puis les basculer en `review` afin qu'elles repassent par une
  validation réelle.

---

## 10.3 CRITIQUE #2 — Le score de menace mesure le succès du crawl, pas la menace

`compute_threat_scores()` calcule `base_classe + min(25, faits × 1,5) + min(20, récents × 4)`.
Confronté aux données, il se comporte ainsi :

| Acteur | Score | Faits marché | Offres | Pages crawlées |
|---|---|---|---|---|
| Pulsar Photonics | **100,0** | 7 | 25 | 36 |
| Workshop of Photonics | **100,0** | 8 | 13 | 13 |
| FEMTOprint | **100,0** | 12 | 31 | 32 |
| LLT Applikation | 99,0 | 1 | 15 | 36 |
| **Kirana Srl** | **93,0** | **0** | 12 | 25 |
| **Oxford Lasers** | **90,0** | **0** | 10 | 33 |

Trois problèmes distincts :

**a) Le score est piloté par les offres non validées.** Corrélation entre `threat_score` et
nombre d'offres sur les C1 : **r = 0,73**. Corrélation avec le nombre de pages crawlées :
r = 0,44. Or les offres sont acceptées à 100 % sans revue (§9.5). Kirana Srl obtient 93/100
**sans un seul fait marché**, uniquement sur 12 offres jamais relues. Le score de menace mesure
donc, pour l'essentiel, *combien de pages nous avons réussi à extraire chez cet acteur*.

**b) Le bonus de vélocité est un bruit constant.** `_is_recent()` utilise
`RECENT_WINDOW_DAYS = 60`, mais **tout le corpus tient dans 7 jours** (23 → 30 août 2026).
100 % des faits sont donc « récents ». Le terme `min(20, récents × 4)` vaut 20 pour tout acteur
ayant ≥ 5 faits, et ne discrimine rien. Il apporte **0 bit d'information pour 20 points de
score**.

**c) Le bonus de vélocité utilise la mauvaise date.** `_is_recent` lit `created_at` (quand *nous*
avons vu la page), pas `source_date` (quand le contenu a été publié). Or la latence mesurée sur
les offres datées est : p50 = 0 j, **p75 = 701 j, p90 = 1 636 j, max = 3 492 j**. **22 % des
contenus datés avaient plus de deux ans au moment de la détection.** Une page de 2017 compte
aujourd'hui comme un « signal récent de vélocité concurrentielle ».

**d) L'échelle sature.** Trois acteurs à exactement 100,0. Le score ne peut plus distinguer
FEMTOprint (12 faits) de Workshop of Photonics (8 faits, et 72 échecs de crawl).

**Correctif :** `_is_recent` doit lire `COALESCE(source_date, created_at)` ; le bonus de
vélocité doit être neutralisé tant que le corpus n'a pas au moins deux fenêtres de collecte ;
les offres doivent peser moins que les faits marché, ou ne compter qu'une fois validées. Et
puisque 10 acteurs n'ont **aucun** `confidence_score` (la fonction ne renvoie rien pour eux)
alors qu'ils ont tous un `threat_score`, l'interface doit distinguer « pas de données » de
« score bas » — sinon un acteur non crawlé passe pour un acteur inoffensif.

---

## 10.4 CRITIQUE #3 — Le score d'attractivité de marché est circulaire

`compute_market_attractiveness_scores()` produit aujourd'hui :

| Marché | Score | `existing` | Acteurs |
|---|---|---|---|
| Médical | **89,5** | 20 | 16 |
| Photonique | 68,8 | 5 | 7 |
| Semi-conducteurs | 64,7 | 4 | 7 |
| Hydrogène | 13,8 | 0 | 2 |

Le score est composé à 100 % de mesures de **l'offre concurrente** : nombre de faits `existing`,
nombre de faits `radar`, nombre d'acteurs présents, part de faits en stade « Production ».
Aucune composante ne vient de la demande, de la taille du marché, de sa croissance ou de son
accessibilité.

Trois conséquences :

1. **Un marché est « attractif » précisément quand il est encombré.** Pour du business
   development, c'est l'inverse de l'information utile. Le libellé induit en erreur : ce
   qu'on mesure est une **intensité concurrentielle**, pas une attractivité.
2. **Le classement est un artefact du lexique.** « Médical » sort premier parce que c'est le
   terme le plus fréquemment apparié — il est présent dans presque toutes les listes de marchés
   des sites, et le lexique `MARKETS` lui donne le plus de variantes. Sa `production_share` de
   0,73 repose sur un `industrial_stage='Production'` dont on verra au §10.6 qu'il est attribué
   à des fragments de menu.
3. **Le score est calculé sur une base dont 56 % est de la donnée saisie à la main** (§10.2),
   elle-même sélectionnée selon vos intuitions de marché. La boucle est fermée : le score
   confirme les hypothèses qui ont servi à saisir les données.

**Correctif :** renommer en `competitive_intensity_score` (c'est ce qu'il mesure, et c'est
utile ainsi), et construire séparément un vrai score d'attractivité dès que `market_sizing` et
les signaux de demande existent (§4.B). En attendant, afficher les deux composantes brutes
(nombre d'acteurs, part production) plutôt qu'un agrégat qui masque leur nature.

---

## 10.5 Le rendement du crawl : 83 % des pages téléchargées ne produisent rien

| | |
|---|---|
| Pages téléchargées en HTTP 200 | 1 514 |
| URLs distinctes ayant produit ≥ 1 preuve (faits ou offres) | **259** |
| **Rendement** | **17 %** |

Et la production est très concentrée : 198 preuves de faits proviennent de **108 URLs**, dont
une seule (`ilt.fraunhofer.de/…/ultrashort-pulse-laser-processing.html`) en produit 11. Trois
URLs produisent 3 faits acceptés chacune, le reste 1 ou 2.

Combiné au §9.1 (89 % des URLs découvertes ne sont jamais visitées), l'entonnoir complet est :

```
16 118 découvertes → 1 514 téléchargées (9,4 %) → 259 productives (1,6 % du total)
```

**Une URL découverte sur 62 produit de l'information.** C'est le vrai chiffre de l'efficacité
de la collecte, et il n'est mesuré nulle part. Il donne aussi le levier : le gain ne viendra pas
d'un budget de crawl plus large, mais d'un meilleur ciblage — c'est-à-dire de
`classify_source`, dont 59 % des sorties sont `other`.

---

## 10.6 La qualité d'extraction : des fragments, pas des assertions

Lecture manuelle d'un échantillon de faits acceptés. Quatre modes de défaillance récurrents,
tous présents dans des lignes `review_status='accepted'` :

**a) Fusion d'items de menu sans lien entre eux.** Fraunhofer ILT, fait
*Hydrogène / Moules et outillage / Microperçage*, citation :

> « »Laser Drilling« »Laser Processes for Hydrogen Technology« »Application Center Laser
> Structuring for Tool and Mold Construction« »

Trois entrées de menu distinctes, fusionnées en un triplet qui n'est affirmé nulle part. C'est
le mode d'échec typique de la fenêtre de co-occurrence, et `_relation_window_is_ambiguous` ne
l'attrape pas parce que les termes sont proches dans le texte brut.

**b) Attribution inversée sur une phrase contrastive.** Pulsar Photonics, deux faits tirés de :

> « With the classic laser dicing of thin ceramic substrates […] are mostly used wafer saws or
> laser-based fixed optics systems. »

Cette phrase décrit ce que font les **méthodes classiques concurrentes** (des scies à wafer) —
c'est le repoussoir dont Pulsar se démarque. Elle a produit deux faits attribuant le *dicing* à
Pulsar. `NEGATION_CUES` gère la négation, pas le contraste.

**c) Produit cartésien de deux listes.** KMLT, fait *Luxe / Composants en verre / Microdécoupe*
en stade « Production », citation :

> « Applications/Markets: medical technology, microelectronics, the watchmaking industry, and
> aerospace; materials include glass, ceramics and various plastics. »

« Luxe » vient de *watchmaking*, « verre » vient de la liste de matériaux. Le croisement des
deux listes n'est affirmé nulle part.

**d) Opération erronée.** Workshop of Photonics : une page intitulée *Laser Micro Drilling*
produit à la fois « Microperçage » (correct) et « Microdécoupe » (faux), depuis la citation
« Polymer Micro Drilling… ».

**Indicateur agrégé** : sur les 71 faits acceptés, **54 (76 %) ne contiennent aucun verbe ni
marqueur assertif** (*is/are/we/our/provides/permet/bietet*…). Ce sont, très majoritairement,
des fragments de liste ou de titre plutôt que des affirmations. Le seuil est heuristique, mais
l'ordre de grandeur est confirmé par la lecture manuelle.

**Deux points aggravants :**
- **`industrial_stage='Production'` est attribué à 40 faits sur 71 (56 %)** — y compris à des
  fragments de menu et à des titres de page. C'est pourtant ce champ qui pilote la
  `production_share` du score d'attractivité (§10.4).
- **`language` est NULL pour 49 des 71 faits (69 %)** : `language_from_url()` ne sait lire que
  les segments `/en/`, `/de/`. Les sites monolingues ou à sous-domaine ne sont pas typés, et
  aucune analyse par langue n'est donc fiable.

**Correctif prioritaire** : n'accepter un fait que si sa citation contient un **prédicat**
rattachant le sujet à l'opération. C'est une règle syntaxique simple (présence d'un verbe entre
le terme de composant et le terme d'opération), et elle éliminerait à elle seule les modes (a),
(c) et une partie de (d). Ajouter `CONTRAST_CUES` (*classic*, *conventional*, *traditional*,
*unlike*, *instead of*, *whereas*, *herkömmlich*) à côté de `NEGATION_CUES` traiterait (b).

---

## 10.7 Correction du §9.9 — `field_confidence` n'a pas la résolution nécessaire

J'ai recommandé au §9.9 d'alimenter la file de revue des offres à partir d'un seuil sur
`field_confidence`. **Les données invalident cette recommandation.**

| Valeur | Offres |
|---|---|
| 0,83 | 139 |
| 0,78 | 135 |
| NULL | 72 |
| 0,70 | 17 |
| 0,75 | 15 |
| 0,60 | 2 |

**72 % de la masse tient sur deux valeurs** (0,78 et 0,83), et 19 % n'ont pas de valeur du
tout. Un seuil ne peut donc produire que deux régimes : flaguer 19 offres (inutile) ou en
flaguer 274 (ingérable). `field_confidence` n'est pas un score continu, c'est une étiquette
de règle déguisée en probabilité. Sur `evidence`, la distribution est nettement plus étalée
(0,55 à 0,85, une douzaine de valeurs), mais 40 lignes sont NULL — précisément les 40 faits
de seed du §10.2.

**Recommandation révisée** : router vers la revue sur des **critères structurels**, pas sur un
score. Concrètement, passe en `review` toute offre qui remplit au moins une condition : citation
sans prédicat (§10.6), `page_type IS NULL` (84 offres), citation issue de la page d'accueil,
`operation IS NULL`, ou source unique non confirmée. Ces critères sont explicables, auditables
et directement corrélés aux modes d'échec observés — contrairement à un seuil sur 0,80.

---

## 10.8 Angle mort géographique : zéro acteur hors d'Europe

| Pays | Acteurs |
|---|---|
| Allemagne | 18 |
| Suisse / France | 7 / 7 |
| Royaume-Uni | 6 |
| Espagne | 4 |
| Irlande | 3 |
| Pays-Bas / Lituanie / Belgique | 2 / 2 / 2 |
| Tchéquie / Italie / Autriche | 1 / 1 / 1 |
| **Amérique du Nord / Asie** | **0** |

Sur 54 acteurs actifs, **100 % sont européens de l'Ouest**. Aucun américain, aucun japonais,
aucun chinois, aucun coréen, aucun taïwanais. Pour un observatoire du micro-usinage
femtoseconde, c'est un angle mort de premier ordre : les deux marchés que vos scores placent en
tête après le médical — **semi-conducteurs et photovoltaïque** — sont précisément ceux où les
acteurs asiatiques sont dominants. Un dispositif de veille qui conclut « les semi-conducteurs
sont attractifs » sans avoir regardé l'Asie n'a pas répondu à la question.

La **Lituanie n'a que 2 entrées** (Workshop of Photonics, Femtika) alors qu'elle concentre une
part disproportionnée de la filière femtoseconde mondiale. Ce n'est pas une omission de
jugement, c'est la conséquence mécanique du §2.1 : le périmètre est ce que vous avez saisi à la
main, donc il a la forme de votre réseau.

C'est l'argument le plus fort en faveur du pipeline de découverte (§4.D) : aucune quantité de
saisie manuelle ne corrigera un biais de cette nature, parce qu'on ne saisit que ce qu'on
connaît déjà.

---

## 10.9 La chronologie est fausse dans les deux sens

Trois mesures qui se contredisent utilement :

- **Amplitude du corpus : 7 jours.** Toutes les lignes `evidence` et `offers` ont un
  `created_at` entre le 23 et le 30 août 2026. `metric_snapshots` ne contient qu'une période
  (`2026-08`). Les « séries temporelles » de l'application ont **un point**.
- **`evidence_bucket_transitions` : 104 des 109 lignes sont des créations** (`from_bucket IS
  NULL`). Seules **5** sont de vraies transitions. La table qui devait alimenter le digest de
  changements ne contient presque que du bruit de démarrage.
- **Mais 22 % des contenus datés avaient plus de 2 ans à la détection** (p90 = 1 636 jours,
  max = 3 492 jours). Le corpus est donc simultanément *trop jeune* pour mesurer une dynamique
  et *trop vieux* pour être traité comme un instantané.

Ces deux faits ensemble condamnent tout indicateur de vélocité tant qu'il repose sur
`created_at`, et confirment que la seule voie est le diff de `page_versions` (§8.3) complété par
la rétro-datation Wayback (§5.E.2).

---

## 10.10 Taxonomies non normalisées

Sur 10 signaux technologiques, deux libellés d'axe coexistent pour le même concept :

- `Monitoring + IA / digital twin` (1 signal, issu de CORDIS)
- `Monitoring IA procédé` (4 signaux)

Deux axes distincts dans les agrégations, un seul concept. Avec 10 lignes c'est anecdotique ;
à 500 lignes, tout comptage par axe sera faux et personne ne s'en apercevra. Par ailleurs, deux
signaux portent `maturity_stage='Industrialisation'` **et** `bucket='radar'` — deux champs qui
se contredisent, sans qu'aucune contrainte ne l'empêche.

`vocabulary_candidates` fournit le bon modèle de gouvernance pour les marchés et opérations ;
les **axes technologiques n'ont pas d'équivalent** et sont écrits en texte libre. Il faut un
lexique fermé pour `axis`, et une règle de cohérence entre `maturity_stage` et `bucket`.

---

## 10.11 Ce qu'il faut instrumenter : le tableau de bord de la veille

Aucun des chiffres du §10.1 n'est calculé par l'application. Ils viennent tous de requêtes
ad hoc. Tant que ce sera le cas, aucune des dégradations décrites ici ne sera détectée en
production.

Une table `veille_metrics` (période, indicateur, valeur), alimentée par le même mécanisme que
`capture_metric_snapshot()`, avec **huit indicateurs** :

| Indicateur | Formule | Seuil d'alerte |
|---|---|---|
| Rendement de collecte | URLs productives / pages 200 | < 15 % |
| Taux de découverte non traitée | URLs jamais visitées / total | > 85 % |
| Taux d'erreur de crawl | erreurs / pages tentées | > 10 % |
| Part de faits non verbatim | `is_verbatim=0` / total accepté | > 5 % |
| Part de faits non validés | `accepted` sans passage en revue | > 20 % |
| Fiabilité de datation | `date_confidence='published'` / total | < 30 % |
| Latence de détection | médiane `source_date → created_at` | > 180 j |
| Concentration des sources | part des faits issus du top 10 % d'URLs | > 50 % |

Aucun ne coûte plus qu'une requête. Ensemble, ils auraient signalé six des problèmes de ce
document avant qu'ils ne s'installent.

---

## 10.12 Feuille de route consolidée (v3)

Remplace les §6, §8.8 et §9.9.

### Lot 0 — Rétablir la véracité de ce qui est affiché (2 jours)
*Rien de neuf : rendre honnête ce qui existe. À faire avant toute démonstration.*

| # | Action | Effort | Réf. |
|---|---|---|---|
| 0.1 | Exposer `is_verbatim`, distinguer visuellement saisie manuelle et extrait vérifié | 0,25 j | §10.2 |
| 0.2 | Regénérer `SEED_EVIDENCE` depuis la base, puis basculer les 40 faits en `review` | 0,25 j | §10.2 |
| 0.3 | Exclure les faits non verbatim du calcul des scores | 0,25 j | §10.2 |
| 0.4 | `_is_recent` sur `COALESCE(source_date, created_at)` ; neutraliser le bonus de vélocité tant qu'il n'y a qu'une fenêtre de collecte | 0,25 j | §10.3 |
| 0.5 | Renommer `market_attractiveness` en `competitive_intensity` ; afficher les composantes | 0,25 j | §10.4 |
| 0.6 | Distinguer « score indisponible » de « score bas » pour les 10 acteurs sans confiance | 0,25 j | §10.3 |
| 0.7 | Filtrer `review_status` dans `/api/actors` (`actor_facts`, `actor_events`) | 0,25 j | §3.1 |
| 0.8 | Contrôle de plausibilité sur `capability_spec` + colonne `review_status` | 0,5 j | §9.4 |

### Lot 1 — Rendre la validation exerçable et la collecte pilotable (1 semaine)

| # | Action | Effort | Réf. |
|---|---|---|---|
| 1.1 | File de revue unifiée (7 files), priorisation, motif de rejet typé | 2 j | §5.G |
| 1.2 | Routage des offres vers `review` sur **critères structurels** (pas sur `field_confidence`) | 0,5 j | §10.7 |
| 1.3 | Règle du prédicat + `CONTRAST_CUES` dans l'extraction | 1 j | §10.6 |
| 1.4 | Scheduler + `alerts` + `/api/digest` + **plafond de coût IA** | 2,5 j | §5.F, §8.7 |
| 1.5 | Vue de santé de collecte + réconciliation des runs orphelins | 0,5 j | §9.2 |
| 1.6 | Diff de `page_versions` → `page_changes` | 2 j | §8.3, §10.9 |
| 1.7 | Table `veille_metrics` + les 8 indicateurs | 1 j | §10.11 |

### Lot 2 — Améliorer le rendement, pas le volume (1,5 semaine)

| # | Action | Effort | Réf. |
|---|---|---|---|
| 2.1 | Découverte des PDF (`DISCOVERY_TERMS`, `.pdf` prioritaire, type `datasheet`) | 0,5 j | §8.1, §9.3 |
| 2.2 | Travailler `classify_source` (59 % d'`other`) + purge du backlog non visité | 2 j | §9.1, §10.5 |
| 2.3 | `page_type='careers'` (retirer des `ignore_paths` **et** de la regex en dur) | 1,5 j | §8.4 |
| 2.4 | Table `outbound_links` ; admettre les sous-domaines | 0,5 j | §8.2 |
| 2.5 | Flux RSS des acteurs (`<link rel="alternate">`) | 1 j | §4.A.4 |
| 2.6 | Corriger `language_from_url` (69 % de NULL) | 0,25 j | §10.6 |
| 2.7 | Lexique fermé pour `axis` + cohérence `maturity_stage`/`bucket` | 0,5 j | §10.10 |

### Lot 3 — Ouvrir le périmètre (2,5 semaines)

| # | Action | Effort | Réf. |
|---|---|---|---|
| 3.1 | **Lancer `scrape_technology()`** (jamais exécuté) | 5 min | §9.7 |
| 3.2 | `actor_candidates` (CORDIS + OpenAlex + presse + `outbound_links`) | 3 j | §4.D |
| 3.3 | **Élargir le périmètre hors Europe** (US, Japon, Chine, Corée, Taïwan) | 2 j | §10.8 |
| 3.4 | Connecteur brevets (EPO OPS / Lens) par déposant et CPC B23K26 | 4 j | §4.C.1 |
| 3.5 | Axe + maturité sur les documents Crossref/OpenAlex | 1 j | §4.C.2 |
| 3.6 | GLEIF/LEI, puis Companies House | 3 j | §8.5 |

### Lot 4 — Sortir du miroir de l'offre (3 semaines)
Inchangé (§6, lot 4), avec une priorité renforcée sur `market_sizing` et les signaux de demande,
puisque le §10.4 montre que sans eux le score d'attractivité restera circulaire.

---

## 10.13 Ce que je dirais en comité

**L'ingénierie est bonne, la mesure ne l'est pas encore.** Le pipeline est propre, traçable,
correctement dédupliqué, sans corruption référentielle après plusieurs migrations — c'est rare
et ça vaut d'être dit. Le problème n'est pas la construction, c'est que **personne n'a encore
mesuré ce que le dispositif produit réellement**, et que les trois chiffres mis en avant
(menace, attractivité, confiance) mesurent aujourd'hui autre chose que ce que leur nom indique.

**Trois phrases à retenir :**

1. **56 % des faits marché ont été saisis à la main et ne sont pas reproductibles.** L'outil
   paraît plus performant qu'il ne l'est, et l'écart est concentré sur les acteurs qui comptent
   le plus.
2. **Le score de menace corrèle à 0,73 avec le nombre de pages extraites.** Deux acteurs sans
   aucun fait marché figurent au-dessus de 90/100.
3. **Zéro acteur hors d'Europe**, alors que les deux marchés jugés les plus attractifs après le
   médical sont dominés par l'Asie.

**Ce que je ne changerais pas** : la traçabilité verbatim, le lexique déterministe avant l'IA,
`vocabulary_candidates`, le refus de sur-attribuer. Ce sont les bonnes décisions, elles sont
rares, et tout le reste de ce document consiste à les faire tenir sur 100 % de la base au lieu
de 44 %.

**Le prochain jalon crédible** n'est pas « plus de sources ». C'est : *à la fin du lot 0, tout
chiffre affiché par l'application est défendable devant quelqu'un qui demande d'où il vient.*
Deux jours de travail. C'est la condition pour que la partie business development s'appuie sur
cette base plutôt que de la contourner.
