#!/bin/sh
set -eu
PUBLIC_MAX="${PUBLIC_MAX_BYTES:-65536}"
MAIN_MAX="${MAIN_MAX_BYTES:-225280}"
public_bytes="$(wc -c < src/public_worker.js | tr -d ' ')"
main_bytes="$(wc -c < src/main.py | tr -d ' ')"
[ "$public_bytes" -le "$PUBLIC_MAX" ] || { echo "public_worker.js exceeds ${PUBLIC_MAX} bytes: ${public_bytes}" >&2; exit 1; }
[ "$main_bytes" -le "$MAIN_MAX" ] || { echo "main.py exceeds ${MAIN_MAX} bytes: ${main_bytes}" >&2; exit 1; }
printf 'Bundle budgets passed: public_worker=%s bytes backend_main=%s bytes\n' "$public_bytes" "$main_bytes"
