#!/usr/bin/env bash
# Build the static export and publish it to the gh-pages branch of origin.
# Run from the repo root after `gh repo create` and `git push`. Needs a fresh snapshot:
#   uv run inspraak ingest hys --skip-closed
#   uv run python scripts/backfill.py agy,claude_cli
#   uv run inspraak demo --replace
set -euo pipefail
cd "$(dirname "$0")/.."
remote=$(git remote get-url origin)
make export
rm -rf site/.git
git -C site init -q -b gh-pages
git -C site add -A
git -C site commit -q -m "Pages $(date -u +%Y-%m-%d)"
git -C site push -f "$remote" gh-pages
rm -rf site/.git
echo "Published. Enable Pages from the gh-pages branch once: Settings, Pages."
