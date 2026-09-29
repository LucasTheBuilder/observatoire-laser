# Observatoire Laser femtoseconde

FastAPI + vanilla JS, 3 bases SQLite indépendantes (`actors.db`/`market.db`/`technology.db`),
suivi concurrentiel du micro-usinage laser ultra-rapide. Voir `audit_veille.md` pour la feuille
de route (§10.12 "Feuille de route consolidée (v3)" fait autorité, remplace les sections
antérieures du même document).

## Sessions Claude Code concurrentes — utiliser un worktree

Ce dépôt a régulièrement plusieurs sessions Claude Code actives en parallèle (constaté à
plusieurs reprises : des commits d'une session ont plusieurs fois ramassé les changements non
commités d'une autre, une fois en coupant en deux un changement réparti sur deux fichiers).

**Avant de commencer un travail de plusieurs fichiers**, vérifiez qu'aucune autre session n'est
déjà active dans ce même dossier (`git status` propre, pas de fichier modifié que vous n'avez pas
touché). Si c'est le cas, ou si vous ne pouvez pas en être sûr, travaillez dans un **worktree**
séparé plutôt que dans ce dossier directement :

```bash
./scripts/new_worktree.sh ma-feature
```

Ça crée `../observatoire_v3_4_1_monthly-worktrees/ma-feature` sur une nouvelle branche, avec son
propre `.env` copié (nécessaire pour les clés API). `data/` n'est PAS partagé entre worktrees :
chaque worktree démarre avec des bases vides, réseedées automatiquement par `init_databases()` —
c'est voulu, ça évite qu'un même fichier SQLite soit écrit par deux conteneurs Docker en même
temps. Le worktree principal (ce dossier) reste la seule copie qui fait tourner les vraies
données de production (`docker compose up -d --build` ici, jamais ailleurs).

Une fois le travail terminé et vérifié dans le worktree, fusionnez sur `master` **depuis ce
dossier principal** (merge ou rebase), puis relancez `docker compose up -d --build` ici pour
redéployer contre les vraies données.

**Avant CHAQUE `git commit`** (pas seulement en cas de doute) : relisez `git status`/`git diff
--cached --stat` pour confirmer que tout ce qui est indexé vous appartient. Après CHAQUE commande
`git commit`, si la sortie ne montre pas la ligne de confirmation `[branche hash] sujet`, lancez
`git log -1` immédiatement — un commit sans rien à committer (ramassé par une autre session
entre le `git add` et le `git commit`) échoue silencieusement, sans erreur visible.

## Discipline de preuve

Jamais de fait marché/composant/acteur inventé. Toute donnée (chiffre de marché, cellule de
matrice de référence, fait golden, alias LEI/SIREN...) vient d'une source réelle vérifiée avant
d'être écrite — voir les docstrings de `market_sizing.py`, `reference_matrix.py`,
`data_quality.py`, `gleif.py`, `firmographics.py` pour le détail par module.

## Boucle de vérification standard

Implémenter → tests → `py -m pytest -q` (suite complète) → `py -m ruff check .` → `py -m mypy .`
(0 erreur attendue : toute erreur est nouvelle) → `git add` des fichiers précis (jamais `-A`) →
commit → `git push` (la CI GitHub `.github/workflows/ci.yml` rejoue ruff/mypy/pytest, et l'audit
hebdomadaire du lundi ne voit que ce qui est poussé) → `docker compose up -d --build` → health
check (`curl http://127.0.0.1:8765/`) → déclencher le vrai collecteur concerné contre la
production → inspecter les données réellement écrites avant de considérer une tâche terminée.

## Données de production

`data/` n'est pas dans git. Une tâche planifiée Windows (« Observatoire - sauvegarde des bases »,
13h chaque jour) lance `backup_databases.py`, qui copie `data/*.db` vers
`%OneDrive%\observatoire-backups\AAAA-MM-JJ\` (30 jours conservés). Avant une intervention
destructive sur une base, lancez-le à la main (`py backup_databases.py`) plutôt que de copier le
fichier : les bases sont en WAL, une copie brute du `.db` perd les écritures non checkpointées.
