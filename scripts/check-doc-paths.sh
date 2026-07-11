#!/usr/bin/env bash
# Fail when a concrete docs/<path> reference in justfile / scripts / *.yml / *.py / operational
# *.md points at a target that does not exist. Illustrative ellipses (docs/... and the
# unicode docs/…) and angle-bracket placeholders (docs/<seg>) are excluded. Catches
# non-markdown rot (e.g. justfile m2-report output, AGENTS.md code spans, a docstring that
# cites a moved spec). NOT scanned:
#   - docs/design/** — design specs narrate path moves (e.g. specs/ -> design/), so their
#     docs/... mentions are intentional and must not be policed here;
#   - docs/archive/** — frozen dated design records reference paths as they were when written and
#     are never edited after creation; unlike an ADR (a living decision log that is superseded,
#     not excluded — see docs/adr/README.md), they carry no supersede mechanism, so their
#     historical path mentions are not policed;
#   - CHANGELOG.md — git-cliff-generated; it reproduces commit subjects verbatim, which
#     may contain "docs"-slash tokens that are recipe names, not paths (a commit titled
#     "Add just docs-check" rendered the recipe with a slash). Generated history, not an
#     authored operational doc.
#   - .claude/**, .agents/**, .codex/** — vendored agent-tooling config, not project docs;
#     their example strings (e.g. docs/<overlay>.md) are illustrative, not real references.
#   - src/kdive/mcp/resources/_content/** — generated mirrors of canonical docs (ADR-0151);
#     any docs/ token they carry is policed at the docs/ source, not in the snapshot copy.
#   - the guard/gate machinery tests (test_check_doc_paths.py, test_m2_portability_gate.py) —
#     they construct synthetic, intentionally-missing docs/ paths to exercise the checks.
# The docs/ token is anchored on a left word boundary so substrings like mkdocs/ or
# subdocs/ are not mistaken for a docs/ reference.
# Generator constants built from slash-joined string literals are also out of scope
# (covered by `just docs-check`/`config-docs-check`).
# Usage: check-doc-paths.sh [ROOT]
set -euo pipefail

readonly ROOT="${1:-.}"
cd "${ROOT}"

readonly EXCLUDE='^docs/(design|archive)/|^CHANGELOG\.md$|^\.(claude|agents|codex)/|^src/kdive/mcp/resources/_content/|^tests/scripts/test_check_doc_paths\.py$|^tests/scripts/test_m2_portability_gate\.py$'

mapfile -t files < <(
  { git ls-files 'justfile' 'scripts/*' '*.yml' '*.yaml' '*.md' '*.py' 2>/dev/null || true; } |
    grep -vE "${EXCLUDE}"
)
if ((${#files[@]} == 0)); then
  mapfile -t files < <(
    find . -type f \( -name justfile -o -path './scripts/*' -o -name '*.yml' \
      -o -name '*.yaml' -o -name '*.md' -o -name '*.py' \) \
      -not -path './docs/design/*' -not -path './docs/archive/*' \
      -not -path './CHANGELOG.md' \
      -not -path './.claude/*' -not -path './.agents/*' -not -path './.codex/*' \
      -not -path './src/kdive/mcp/resources/_content/*' \
      -not -path './tests/scripts/test_check_doc_paths.py' \
      -not -path './tests/scripts/test_m2_portability_gate.py' \
      -printf '%P\n'
  )
fi

missing=0
for f in "${files[@]}"; do
  [[ -e "$f" ]] || continue
  # docs/ followed by path chars. Fenced code blocks are stripped first (the awk toggles on
  # triple-backtick fence lines; \140 is the octal for a backtick, so this script holds no
  # literal fence marker) so example paths in code samples are not policed; design/archive
  # trees are already excluded from the file set above.
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    # Skip illustrative ellipses (ASCII ... or unicode …) and <placeholders>.
    case "$ref" in
    *"..."* | *"…"* | *"<"*) continue ;;
    esac
    ref="${ref%.}" # drop a trailing sentence period
    if [[ ! -e "$ref" ]]; then
      printf "missing doc path: %s references %s\n" "$f" "$ref" >&2
      missing=1
    fi
  done < <(awk 'BEGIN { fence = 0 } /^\140\140\140/ { fence = !fence; next } !fence' "$f" |
    grep -oP '(?<![A-Za-z0-9_./-])docs/[A-Za-z0-9._/-]+' | sort -u)
done

if ((missing)); then
  printf "\ndoc path-existence check failed\n" >&2
  exit 1
fi
printf "doc paths resolve\n"
