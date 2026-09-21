#!/usr/bin/env bash
set -euo pipefail

while IFS= read -r variable; do
  unset "$variable"
done < <(git rev-parse --local-env-vars)

exec just ci
