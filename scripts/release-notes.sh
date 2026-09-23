#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:-v0.14.0}"
OUT="${2:-RELEASE-NOTES-${VERSION}.md}"
{
  printf '# Hidden Conviction %s\n\n' "$VERSION"
  printf 'Generated from the repository state and changelog.\n\n'
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    PREV="$(git tag --sort=-version:refname 2>/dev/null | grep -v "^${VERSION}$" | head -1 || true)"
    if [ -n "$PREV" ]; then
      printf '## Commits since %s\n\n' "$PREV"
      git log --pretty='* %s (%h)' "$PREV"..HEAD || true
      printf '\n'
    else
      printf '## Current commit\n\n* %s\n\n' "$(git rev-parse --short HEAD 2>/dev/null || echo uncommitted)"
    fi
  fi
  printf '## Changelog excerpt\n\n'
  awk '/^## 0\.13\.0/{flag=1} flag{if(/^## 0\.12\.0/){exit} print}' CHANGELOG.md
} > "$OUT"
printf 'Wrote %s\n' "$OUT"
