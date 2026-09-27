#!/usr/bin/env bash
# Fetch or verify a Linux source checkout; stdout is its absolute path only.
# Usage: fetch-kernel-tree.sh [DEST_DIR]
# KDIVE_KERNEL_REPO defaults to upstream stable; KDIVE_KERNEL_REF accepts a tag,
# branch or full commit (default v6.9 for existing walking-skeleton callers).
# Existing source is never reset. Git-ignored build products may remain.
set -euo pipefail

readonly DEFAULT_REPO="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
readonly DEFAULT_REF="v6.9"

resolve_ref() {
  local repo=$1 ref=$2 refs commit
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$ref"
    return
  fi
  refs=$(git ls-remote --exit-code -- "$repo" "$ref" "$ref^{}" \
    "refs/tags/$ref" "refs/tags/$ref^{}" "refs/heads/$ref")
  commit=$(printf '%s\n' "$refs" | awk '
    /\^\{\}$/ { peeled=$1 }
    { if (!first) first=$1 }
    END { print peeled ? peeled : first }')
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || {
    echo "error: cannot resolve kernel ref $ref; select an existing tag or full commit" >&2
    return 1
  }
  printf '%s\n' "$commit"
}

main() {
  local dest="${1:-./.live-vm/linux}"
  local repo="${KDIVE_KERNEL_REPO:-$DEFAULT_REPO}" ref="${KDIVE_KERNEL_REF:-$DEFAULT_REF}"
  local expected root actual dirty
  command -v git >/dev/null || {
    echo "error: git is required to verify the kernel source; install git" >&2
    return 1
  }
  expected=$(resolve_ref "$repo" "$ref")
  if [[ -e "$dest" || -L "$dest" ]]; then
    root=$(git -C "$dest" rev-parse --show-toplevel) || {
      echo "error: kernel destination is not a checkout; choose a new destination" >&2
      return 1
    }
    [[ "$(cd -- "$dest" && pwd -P)" == "$(cd -- "$root" && pwd -P)" ]] || {
      echo "error: kernel destination is inside another checkout; choose its root or a new path" >&2
      return 1
    }
  else
    echo "fetching kernel source @ $expected into $dest (shallow)" >&2
    git init --quiet "$dest" >&2
    git -C "$dest" fetch --depth=1 -- "$repo" "$expected" >&2
    git -C "$dest" checkout --quiet --detach FETCH_HEAD >&2
  fi
  actual=$(git -C "$dest" rev-parse HEAD)
  dirty=$(git -C "$dest" status --porcelain --untracked-files=all)
  if [[ "$actual" != "$expected" || -n "$dirty" ]]; then
    echo "error: kernel source must be clean at $expected; preserve this tree and use a new destination" >&2
    return 1
  fi
  echo "kernel tree verified @ $expected (idempotent)" >&2
  (cd -- "$dest" && pwd -P)
}

main "$@"
