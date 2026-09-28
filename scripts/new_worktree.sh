#!/usr/bin/env bash
# Crée un git worktree isolé pour une session Claude Code (ou humaine) qui travaille en
# parallèle d'une autre sur ce dépôt -- voir CLAUDE.md "Sessions Claude Code concurrentes".
#
# Usage: ./scripts/new_worktree.sh <nom-de-branche> [branche-de-depart]
#   nom-de-branche    : nom de la nouvelle branche ET du dossier du worktree (obligatoire)
#   branche-de-depart : par defaut, master
#
# Le worktree est cree en dossier FRERE de ce depot (../<repo>-worktrees/<nom-de-branche>),
# jamais a l'interieur : un worktree imbrique dans le depot principal serait lui-meme suivi
# par erreur par le premier `git add -A` venu.

set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: $0 <nom-de-branche> [branche-de-depart]" >&2
  exit 1
fi

BRANCH_NAME="$1"
BASE_BRANCH="${2:-master}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
REPO_NAME="$(basename "$REPO_ROOT")"
WORKTREE_DIR="$(dirname "$REPO_ROOT")/${REPO_NAME}-worktrees/${BRANCH_NAME}"

if [ -e "$WORKTREE_DIR" ]; then
  echo "Erreur : $WORKTREE_DIR existe deja." >&2
  exit 1
fi

echo "Depot principal : $REPO_ROOT"
echo "Nouveau worktree : $WORKTREE_DIR (branche '$BRANCH_NAME' depuis '$BASE_BRANCH')"

git -C "$REPO_ROOT" fetch origin "$BASE_BRANCH" --quiet 2>/dev/null || true
git -C "$REPO_ROOT" worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "$BASE_BRANCH"

# .env est gitignore (contient des cles reelles) -- jamais copie automatiquement par git
# worktree. Sans lui, les connecteurs tournent en mode "not_configured" plutot que planter,
# mais rien ne collecte reellement -- on le copie donc explicitement s'il existe.
if [ -f "$REPO_ROOT/.env" ]; then
  cp "$REPO_ROOT/.env" "$WORKTREE_DIR/.env"
  echo ".env copie."
else
  echo "Attention : pas de .env dans le depot principal, aucun copie dans le worktree."
fi

echo
echo "Pret. data/ demarrera vide dans ce worktree (jamais partage entre worktrees, voir"
echo "CLAUDE.md) -- init_databases() la reseedera automatiquement au premier lancement."
echo
echo "cd \"$WORKTREE_DIR\""
