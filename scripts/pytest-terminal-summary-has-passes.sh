#!/usr/bin/env bash
# Return success only when pytest's final terminal summary reports passed tests (#2584).
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 SUMMARY_FILE" >&2
  exit 2
fi

# Pytest may color its terminal summary. Output before the last non-empty line, including -ra
# skip reasons, is intentionally not evidence that this invocation proved a test.
# Byte ranges in the ANSI matcher are deliberate; locale collation can make GNU sed reject
# `[ -/]` as an invalid range before it reads the summary.
last_line="$(LC_ALL=C sed -E $'s/\033\\[[0-?]*[ -/]*[@-~]//g' "$1" | awk 'NF { last = $0 } END { print last }')"

[[ "$last_line" =~ ^[0-9] ]] &&
  [[ "$last_line" =~ (^|[[:space:],])[1-9][0-9]*[[:space:]]passed([,[:space:]]|$) ]] &&
  [[ "$last_line" =~ [[:space:]]in[[:space:]][0-9]+([.][0-9]+)?s ]]
