#!/usr/bin/env bash
# Resolve relative markdown links in tracked *.md files against the filesystem.
# Reports only; exits 1 if any relative link target is missing. External (scheme://,
# mailto:) and pure-anchor (#...) links are ignored — only on-disk targets are checked.
# The current architecture, docs/design/top-level-design.md, is checked.
# NOT scanned: docs/archive/** (frozen history), other docs/design/** (narrative specs), the
# vendored agent-tooling dirs .claude/**, .agents/**, .codex/** (not project docs), and the
# generated doc-resource snapshots src/kdive/mcp/resources/_content/** (mirrors of canonical
# docs whose links are policed at the docs/ source; ADR-0151) — same exemptions as
# check-doc-paths.sh, so a restructure that re-nests archived docs does not turn their
# now-stale links into gate failures we'd have to "fix" by editing history, and vendored
# skill files with illustrative links are not policed.
# Usage: check-doc-links.sh [ROOT]   (ROOT defaults to the repo root / cwd)
set -euo pipefail

readonly ROOT="${1:-.}"
cd "${ROOT}"

readonly EXCLUDE='^docs/archive/|^[.](claude|agents|codex)/|^src/kdive/mcp/resources/_content/'

# Git's -z prevents quoting filenames; this existing line-based scan excludes newline names.
# Normalize conversion failures so status 128 identifies Git discovery alone.
if files=$(LC_ALL=C git ls-files -z '*.md' 2>&1 | { tr '\000' '\n' || exit 1; }); then
  :
else
  enumeration_status=$?
  case "$enumeration_status:$files" in
  "128:fatal: not a git repository (or any "*) files='' ;;
  *)
    printf '%s\ncannot enumerate tracked markdown files; check git/tr diagnostics above\n' "$files" >&2
    exit 1
    ;;
  esac
fi
if ! files=$(awk -v exclude="$EXCLUDE" '
  length && $0 !~ exclude && ($0 !~ /^docs\/design\// ||
    $0 == "docs/design/top-level-design.md")' <<<"$files"); then
  printf 'cannot filter markdown files; check awk diagnostics above\n' >&2
  exit 1
fi
if [[ -z "$files" ]]; then
  if ! files=$(
    find . -type f -name '*.md' \
      \( -not -path './docs/design/*' -o -path './docs/design/top-level-design.md' \) \
      -not -path './docs/archive/*' \
      -not -path './.claude/*' -not -path './.agents/*' -not -path './.codex/*' \
      -not -path './src/kdive/mcp/resources/_content/*' \
      -print | awk '{ sub(/^\.\//, ""); print }'
  ); then
    printf 'cannot enumerate markdown files under %s; check find/awk diagnostics above\n' "$ROOT" >&2
    exit 1
  fi
fi

broken=0
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  dir="$(dirname "./$f")"
  # Keep the existing fence toggle and Markdown target expression.
  if ! targets=$(awk '
    BEGIN { fence = 0 }
    /^\140\140\140/ { fence = !fence; next }
    !fence {
      rest = $0
      while (match(rest, /\]\([^)]+\)/)) {
        print substr(rest, RSTART + 2, RLENGTH - 3)
        rest = substr(rest, RSTART + RLENGTH)
      }
    }' <"$f"); then
    printf 'cannot extract markdown links from %s; check awk/read diagnostics above\n' "$f" >&2
    exit 1
  fi
  while IFS= read -r target; do
    [[ -z "$target" ]] && continue
    case "$target" in
    *"://"* | mailto:* | "#"*) continue ;;
    esac
    # strip a trailing CommonMark title:  [t](dest "title")  -> dest
    target="${target%% *}"
    target="${target%%#*}"
    [[ -z "$target" ]] && continue
    if [[ ! -e "${dir}/${target}" ]]; then
      printf "broken link: %s -> %s\n" "$f" "$target" >&2
      broken=1
    fi
  done <<<"$targets"
done <<<"$files"

if ((broken)); then
  printf "\nmarkdown link check failed\n" >&2
  exit 1
fi
printf "markdown links resolve\n"
