# Observatoire Laser — v3.3.2-dev

## Objet

Correction de la qualité des faits Marché après le retour ALPHANOV. La v3.3.2 ne bannit aucune famille de source : Applications, News, Projects, Related projects, Collaborative projects et Publications peuvent toutes fournir un fait Marché si leur contenu établit réellement la relation métier.

## 1. Validation relationnelle locale

Un fait Marché n'est plus créé parce que Marché + Composant + Opération sont simplement présents quelque part dans un bloc ou une page.

Deux niveaux sont admis :

- `direct` : Marché + Composant + Opération dans la même phrase / déclaration locale ;
- `contextual` : Composant + Opération liés dans une phrase et Marché porté par le H2/H3/heading immédiat, ou relation complète dans une fenêtre maximale de deux phrases adjacentes avec au moins deux dimensions dans une même phrase.

Le titre global de page ne peut pas fournir le marché. Il reste utilisable uniquement pour confirmer le contexte laser.

## 2. Publications / News / Projects

Aucune de ces rubriques n'est exclue par principe.

- Une publication qui dit explicitement « femtosecond laser texturing of medical stent ... » peut alimenter Marché.
- Une Latest News qui relie marché, composant et opération peut alimenter Marché.
- Un Collaborative / Related Project peut alimenter Marché.
- Une simple liste de titres où `Optique`, `LIPSS` et `Texturation` proviennent de fragments différents est rejetée.

La décision dépend donc de la relation locale, pas de la nature éditoriale de la source.

## 3. Traçabilité

Nouveaux champs dans `market.db` :

- `relation_strength`
- `relation_evidence`
- `source_role`

La fenêtre de preuve de l'interface affiche maintenant `Relation : directe/contextuelle` et le rôle local de la source lorsqu'il est disponible.

## 4. Reconstruction de market.db

Cette version est conçue pour repartir d'une base Marché propre. `actors.db` et `technology.db` sont conservées.

Un script explicite est fourni :

```powershell
python reset_market_db.py
```

Le script :

1. sauvegarde l'ancien `market.db` dans `data/backups/market_<timestamp>.db` ;
2. supprime `market.db` ainsi que ses sidecars WAL/SHM ;
3. recrée un schéma Marché vide ;
4. ne supprime ni `actors.db` ni `technology.db`.

Le reset n'est jamais lancé automatiquement au démarrage.

### Procédure Docker recommandée

```powershell
docker compose down
python reset_market_db.py
docker compose up -d --build
```

Puis recharge l'interface avec `Ctrl + F5`, lance d'abord la collecte Acteurs, puis la collecte Marché.

## 5. Test de référence ALPHANOV

Le cas suivant doit être rejeté : une page/section générale « Lasers » contient une mention optique, puis des titres de publications LIPSS / surface texturing sans relation locale avec un composant et un marché.

En revanche, une Publication, News ou Related Project qui contient une relation directe ou contextuelle complète doit être conservée.

## Validation technique

- `python -m py_compile` : OK
- `node --check static/app.js` : OK
- `pytest` : 18 tests + 5 sous-tests passés

Les tests incluent : contamination inter-section ALPHANOV, publication valide, Latest News valide, Related Project valide, interdiction d'hériter le marché du titre global et reconstruction de `market.db` avec sauvegarde.
