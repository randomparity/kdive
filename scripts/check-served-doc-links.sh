#!/usr/bin/env bash
# For each canonical doc registered in DOC_RESOURCES
# (src/kdive/mcp/resources/registrar.py), fail if an internal markdown link points at another
# served doc by a relative path instead of that doc's resource:// URI. check-doc-links.sh
# resolves a relative link against the filesystem and passes it once the target file exists,
# but an MCP client only ever sees the flat resource:// allowlist, never the docs/ filesystem
# tree, and cannot follow a relative .md path — the blind spot behind finding F1 (#1361,
# ADR-0403). A relative link to a doc that is NOT itself served (e.g. docs/adr/**, deliberately
# unserved per ADR-0270, or docs/guide/reference/**) is out of scope here: this check only
# polices reachability between docs the allowlist already claims to serve.
#
# The DOC_RESOURCES entries are always read from the real project (via `uv run --project`
# rooted at this script's repo, not ROOT), so a test harness may point ROOT at a partial tmp
# tree; an entry whose source file is absent under ROOT is skipped (existence of every entry
# is already gated by `just resources-docs-check`, not here).
# Usage: check-served-doc-links.sh [ROOT]
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SELF_DIR
readonly REPO_ROOT="${SELF_DIR}/.."
readonly ROOT="${1:-.}"

# "source<TAB>uri" lines, one per DOC_RESOURCES entry — the allowlist is the single source of
# truth, so this script cannot drift from what registrar.py actually serves.
mapfile -t entries < <(
  uv run --project "${REPO_ROOT}" python3 -c '
from kdive.mcp.resources.registrar import DOC_RESOURCES

for e in DOC_RESOURCES:
    print(f"{e.source}\t{e.uri}")
'
)

declare -A uri_by_source
for entry in "${entries[@]}"; do
  uri_by_source["${entry%%$'\t'*}"]="${entry#*$'\t'}"
done

broken=0
for entry in "${entries[@]}"; do
  src="${entry%%$'\t'*}"
  path="${ROOT}/${src}"
  [[ -f "$path" ]] || continue
  dir="$(dirname "$src")"
  # Extract "lineno:](target)" pairs. Fenced code blocks are blanked first (the awk toggles on
  # triple-backtick fence lines; \140 is the octal for a backtick, so this script holds no
  # literal fence marker) while keeping one output line per input line, so line numbers still
  # line up — illustrative example links inside code samples are not real cross-references.
  while IFS=: read -r lineno match; do
    target="${match#\](}"
    target="${target%)}"
    case "$target" in
    *"://"* | mailto:* | "#"*) continue ;;
    esac
    target="${target%% *}" # strip a trailing CommonMark title
    target="${target%%#*}" # strip a fragment
    [[ -z "$target" ]] && continue
    resolved="$(realpath -m --relative-to="${ROOT}" -- "${ROOT}/${dir}/${target}")"
    if [[ -n "${uri_by_source[$resolved]:-}" ]]; then
      printf 'unfetchable served-doc link: %s:%s -> %s (cite %s instead)\n' \
        "$src" "$lineno" "$target" "${uri_by_source[$resolved]}" >&2
      broken=1
    fi
  done < <(awk 'BEGIN { fence = 0 } /^\140\140\140/ { fence = !fence; print ""; next }
    { print (fence ? "" : $0) }' "$path" | grep -noE '\]\([^)]+\)')
done

if ((broken)); then
  printf "\nserved-doc link check failed\n" >&2
  exit 1
fi
printf "served-doc links resolve to the resource:// allowlist\n"
