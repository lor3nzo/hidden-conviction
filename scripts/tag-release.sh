#!/bin/sh
set -eu
VERSION="${1:-v0.14.0}"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo 'Not a Git repository.' >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo 'Working tree is not clean.' >&2; exit 1; }
git tag -a "$VERSION" -m "Hidden Conviction $VERSION"
printf 'Created annotated tag %s\n' "$VERSION"
