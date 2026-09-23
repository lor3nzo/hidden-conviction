#!/bin/sh
set -eu

if command -v pytest >/dev/null 2>&1; then
  PYTHONPATH=.:src pytest -q
else
  PYTHONPATH=.:src uv run pytest -q
fi

if command -v ruff >/dev/null 2>&1; then
  ruff check src tests
elif command -v uv >/dev/null 2>&1; then
  uv run ruff check src tests
fi

if command -v basedpyright >/dev/null 2>&1; then
  basedpyright
elif command -v uv >/dev/null 2>&1; then
  uv run basedpyright
fi

if command -v python3 >/dev/null 2>&1; then
  python3 -m compileall -q src
else
  python -m compileall -q src
fi

node --check public/app.js
node --check src/public_worker.js
node --test tests_js/frontend.test.mjs

./scripts/coverage.sh

if command -v npm >/dev/null 2>&1; then
  npm install --no-audit --no-fund
  npm run lint
fi
