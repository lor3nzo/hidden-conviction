#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${PYTHONPATH:-.}:src"
coverage erase
coverage run -m pytest -q
coverage report --fail-under=34
