# Phase 6A-bis — Audit DOM et granularité des blocs

Cette phase ne branche toujours pas Playwright automatiquement dans le crawler. Elle répond au diagnostic observé sur ALPHANOV : Playwright rend davantage de HTML, mais `parse_document()` produisait exactement les mêmes 2–13 blocs qu'HTTPX.

## Changements

1. `diagnose_document()` expose le nombre de H1/H2/H3/H4, sections, articles, ancres, candidats CSS, candidats utilisables, parents écartés, blocs sémantiques, segments par titres et blocs finaux.
2. `audit_dom_6a_bis.py` compare HTTPX et Playwright avec ces métriques et affiche les 20 premiers blocs retenus.
3. `parse_document()` possède maintenant une segmentation éditoriale H2/H3/H4 conservatrice. Elle ne s'active que si l'extraction sémantique retourne au plus 3 blocs alors que la page contient au moins 4 titres H2/H3/H4. Elle doit aussi produire au moins deux blocs supplémentaires pour remplacer l'extraction sémantique.
4. La segmentation coupe au prochain titre de rang égal ou supérieur. Cela réduit les contaminations entre rubriques éloignées.

## Lancer l'audit réel ALPHANOV

Depuis le projet Docker :

```powershell
docker compose -f docker-compose.yml -f docker-compose.playwright.yml exec observatoire python audit_dom_6a_bis.py --renderer http://browser:8780
```

Pour obtenir le JSON complet :

```powershell
docker compose -f docker-compose.yml -f docker-compose.playwright.yml exec observatoire python audit_dom_6a_bis.py --renderer http://browser:8780 --json
```

## Ce qu'il faut regarder

Pour chaque page, relever surtout :

- `H2/H3/H4` : richesse éditoriale réelle ;
- `candidats`, `utilisables`, `feuilles` : où le sélecteur réduit la page ;
- `rejetés parents` : quantité de grands conteneurs écartés car un descendant est aussi candidat ;
- `blocs sémantiques` vs `segments titres` vs `FINAL` ;
- `extraction_method` : `semantic` ou `heading-segments` ;
- titres des blocs finaux.

La prochaine décision doit se faire sur ces résultats réels avant tout branchement automatique Playwright (6B) ou Browser Use/Ollama.
