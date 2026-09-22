#!/usr/bin/env bash
# Fail when a concrete docs/<path> reference in justfile / scripts / *.yml / *.py / operational
# *.md points at a target that does not exist. Illustrative ellipses (docs/... and the
# unicode docs/…) and angle-bracket placeholders (docs/<seg>) are excluded. Catches
# non-markdown rot (e.g. a justfile recipe's output path, AGENTS.md code spans, a docstring
# that cites a moved spec). NOT scanned:
#   - docs/design/** — design specs narrate path moves (e.g. specs/ -> design/), so their
#     docs/... mentions are intentional and must not be policed here. The current
#     architecture, docs/design/top-level-design.md, is checked;
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
#   - the guard machinery test (test_check_doc_paths.py) — it constructs synthetic,
#     intentionally-missing docs/ paths to exercise this check.
# The docs/ token is anchored on a left word boundary so substrings like mkdocs/ or
# subdocs/ are not mistaken for a docs/ reference.
# Generator constants built from slash-joined string literals are also out of scope
# (covered by `just docs-check`/`config-docs-check`).
# Usage: check-doc-paths.sh [ROOT]
set -euo pipefail

readonly ROOT="${1:-.}"
cd "${ROOT}"

readonly EXCLUDE='^docs/archive/|^CHANGELOG[.]md$|^[.](claude|agents|codex)/|^src/kdive/mcp/resources/_content/|^tests/scripts/test_check_doc_paths[.]py$'

# -z avoids Git quoting backslashes, tabs, and quotes in filenames. The existing
# line-based scan does not support filenames containing newlines. Normalize conversion
# failures so status 128 identifies Git discovery alone.
if files=$(LC_ALL=C git ls-files -z 'justfile' 'scripts/*' '*.yml' '*.yaml' '*.md' '*.py' 2>&1 |
  { tr '\000' '\n' || exit 1; }); then
  :
else
  enumeration_status=$?
  case "$enumeration_status:$files" in
  "128:fatal: not a git repository (or any "*) files='' ;;
  *)
    printf '%s\ncannot enumerate tracked files; check git/tr diagnostics above\n' "$files" >&2
    exit 1
    ;;
  esac
fi
if ! files=$(awk -v exclude="$EXCLUDE" '
  length && $0 !~ exclude && ($0 !~ /^docs\/design\// ||
    $0 == "docs/design/top-level-design.md")' <<<"$files"); then
  printf 'cannot filter source files; check awk diagnostics above\n' >&2
  exit 1
fi
if [[ -z "$files" ]]; then
  if ! files=$(
    find . -type f \( -name justfile -o -path './scripts/*' -o -name '*.yml' \
      -o -name '*.yaml' -o -name '*.md' -o -name '*.py' \) \
      \( -not -path './docs/design/*' -o -path './docs/design/top-level-design.md' \) \
      -not -path './docs/archive/*' \
      -not -path './CHANGELOG.md' \
      -not -path './.claude/*' -not -path './.agents/*' -not -path './.codex/*' \
      -not -path './src/kdive/mcp/resources/_content/*' \
      -not -path './tests/scripts/test_check_doc_paths.py' \
      -print | awk '{ sub(/^\.\//, ""); print }'
  ); then
    printf 'cannot enumerate source files under %s; check find/awk diagnostics above\n' "$ROOT" >&2
    exit 1
  fi
fi

missing=0
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  [[ -e "$f" ]] || continue
  # docs/ followed by path chars. Fenced code blocks are stripped first (the awk toggles on
  # triple-backtick fence lines; \140 is the octal for a backtick, so this script holds no
  # literal fence marker) so example paths in code samples are not policed; design/archive
  # records (except the current architecture) are excluded from the file set above.
  if ! refs=$(awk '
    BEGIN { fence = 0 }
    /^\140\140\140/ { fence = !fence; next }
    !fence {
      offset = 1
      while (match(substr($0, offset), /docs\/[A-Za-z0-9._\/-]+/)) {
        start = offset + RSTART - 1
        ref = substr($0, start, RLENGTH)
        if ((start == 1 || substr($0, start - 1, 1) !~ /[A-Za-z0-9_.\/-]/) &&
            !seen[ref]++) print ref
        offset = start + RLENGTH
      }
    }' <"$f"); then
    printf 'cannot extract doc paths from %s; check awk/read diagnostics above\n' "$f" >&2
    exit 1
  fi
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
  done <<<"$refs"
done <<<"$files"

if ((missing)); then
  printf "\ndoc path-existence check failed\n" >&2
  exit 1
fi
printf "doc paths resolve\n"
