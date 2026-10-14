#!/usr/bin/env bash
# Publish a new version in one step:   tools/release.sh 3.1 "What changed"
# Needs: git, and the GitHub CLI logged in (gh auth login).
# Sets the version, commits, tags, pushes, builds the downloads and creates the GitHub release.
# Every sign then shows "update available" within a few hours (or right away with "Check now").
set -euo pipefail
cd "$(dirname "$0")/.."
VER="${1:?Usage: tools/release.sh NEW_VERSION [\"notes\"]}"
NOTES="${2:-JoeDeck $VER}"
[[ "$VER" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || { echo "Version must look like 3.1 or 3.1.2"; exit 1; }
sed -i -E "s/^VERSION = \"[^\"]+\"/VERSION = \"$VER\"/" app/cluster.py
git add -A
git commit -m "JoeDeck $VER" || true
git tag -a "v$VER" -m "JoeDeck $VER"
git push origin HEAD --tags
tools/build-release.sh
gh release create "v$VER" dist/* --title "JoeDeck $VER" --notes "$NOTES"
echo "Released v$VER: https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/tag/v$VER"
