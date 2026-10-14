#!/usr/bin/env bash
# One-time: put this code in the GitHub repository jrquigley/JoeDeck and publish the first release.
# Works whether or not the repository already exists (it keeps this folder's files if both have a README etc.).
# Needs git and the GitHub CLI (https://cli.github.com). First run:  gh auth login
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="${1:-jrquigley/JoeDeck}"
VER="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' app/cluster.py)"
gh auth status >/dev/null 2>&1 || { echo "First run:  gh auth login"; exit 1; }
GIT=(git -c user.name="${GIT_NAME:-Joe Quigley}" -c user.email="${GIT_EMAIL:-jrquigley@users.noreply.github.com}")

[ -d .git ] || git init -q -b main
git add -A
"${GIT[@]}" commit -qm "JoeDeck $VER" || true

if gh repo view "$REPO" >/dev/null 2>&1; then
  git remote remove origin 2>/dev/null || true
  git remote add origin "${REMOTE_URL:-https://github.com/$REPO.git}"
  gh auth setup-git
  if [ -n "$(git ls-remote --heads origin 2>/dev/null)" ]; then
    # the repo already has commits (e.g. a README made on github.com): combine them, keeping our versions
    DEFAULT="$(gh repo view "$REPO" --json defaultBranchRef -q .defaultBranchRef.name)"
    git fetch -q origin "$DEFAULT"
    "${GIT[@]}" merge -q --allow-unrelated-histories -X ours --no-edit "origin/$DEFAULT" || true
    git branch -M "$DEFAULT"
  fi
  git push -u origin HEAD
else
  gh repo create "$REPO" --public --source=. --remote=origin --push \
    --description "Remote-managed digital signs for Raspberry Pi: images, websites, playlists, schedules, many signs, users and activity log."
fi

git tag -a "v$VER" -m "JoeDeck $VER" 2>/dev/null || true
git push origin "v$VER"
tools/build-release.sh
gh release create "v$VER" dist/* --title "JoeDeck $VER" --notes "First release of JoeDeck $VER." \
  || gh release upload "v$VER" dist/* --clobber
echo
echo "Done! Install command for your signs:"
echo "  curl -fsSL https://github.com/$REPO/releases/latest/download/joedeck-install.sh | sudo bash"
