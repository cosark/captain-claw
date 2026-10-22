#!/usr/bin/env bash
# Runs every topic-divider scenario against the chat store (Node 23.6+, which
# runs TypeScript as is). No server, no browser. Exits non-zero on a failure.
set -uo pipefail
cd "$(dirname "$0")"
status=0
for n in $(node --import ./register.mjs scen.mjs 2>/dev/null | sed 's/^scenarios: //'); do
  out=$(node --import ./register.mjs scen.mjs "$n" 2>&1 | grep -v -i 'experimental\|--trace')
  rc=${PIPESTATUS[0]}
  echo "$n: $(echo "$out" | tail -1)"
  echo "$out" | grep 'FAIL ' | sed 's/^/    /'
  [ "$rc" -eq 0 ] || status=1
done
exit $status
