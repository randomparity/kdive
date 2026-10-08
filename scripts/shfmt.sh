#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd -P)"
binary="${root}/build/dev-tools/bin/shfmt"
if [[ ! -e "${binary}" && ! -L "${binary}" ]]; then
  binary="$(command -v shfmt || true)"
fi
if [[ -z "${binary}" || ! -x "${binary}" ]] || ! version="$("${binary}" --version)" || [[ "${version}" != v3.13.1 ]]; then
  printf 'KDIVE requires shfmt v3.13.1; run just setup to install its checkout-local formatter.\n' >&2
  exit 1
fi
exec "${binary}" "$@"
