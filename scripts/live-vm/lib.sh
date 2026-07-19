#!/usr/bin/env bash
# Shared helpers for the live-vm image stores (warm-store.sh, stage-tcg-images.sh).
# SOURCED, never executed (ADR-0388): defines functions only, no side effects at source time.

# Fail loud with an actionable message and a non-zero exit (the require_* pattern from
# scripts/live-stack/lib.sh).
die() {
  printf 'live-vm store: %s\n' "$*" >&2
  exit 1
}

# Apparent size of PATH in bytes.
du_bytes() {
  du -sb -- "$1" | cut -f1
}

# Human-readable measured-usage line to STDERR (stdout is the eval-safe wiring block only).
report_usage() {
  local label="$1" path="$2" bytes
  bytes="$(du_bytes "$path")"
  printf 'live-vm usage: %s=%s bytes (%s)\n' "$label" "$bytes" "$(numfmt --to=iec "$bytes")" >&2
}

# Post-stage footprint cap: die if PATH exceeds CEILING_BYTES; else report. Boundary: == passes.
enforce_budget() {
  local path="$1" ceiling="$2" what="$3" bytes
  bytes="$(du_bytes "$path")"
  if [ "$bytes" -gt "$ceiling" ]; then
    die "$what exceeds budget: ${bytes} bytes > ceiling ${ceiling} bytes at ${path}"
  fi
  printf 'live-vm usage: %s=%s bytes (ceiling %s)\n' "$what" "$bytes" "$ceiling" >&2
}

# Best-effort pre-check (NOT a reservation): die if the fs holding PATH has < NEEDED_BYTES free.
require_free_space() {
  local path="$1" needed="$2" what="$3" free
  free="$(df -B1 --output=avail -- "$path" | tail -n1 | tr -d ' ')"
  if [ "$free" -lt "$needed" ]; then
    die "$what needs ${needed} bytes free at ${path}, only ${free} available"
  fi
}

# Content digest of FILE.
sha256_of() {
  sha256sum -- "$1" | cut -d' ' -f1
}

# Non-fatal digest predicate (completeness — build-id survives truncation, a digest does not).
# Status 0 iff FILE re-hashes to EXPECTED. The warm check treats a mismatch as rebuild, so this
# must NOT die (unlike the fail-loud helpers).
sha256_ok() {
  [ "$(sha256_of "$1")" = "$2" ]
}

# Post-fetch match assertion. Die if EITHER id is empty (even if both are) — no vacuous match.
build_ids_match() {
  local a="$1" b="$2"
  { [ -n "$a" ] && [ -n "$b" ]; } || die "empty build-id (a='${a}' b='${b}') — refusing vacuous match"
  [ "$a" = "$b" ] || die "build-id mismatch: kernel=${a} debuginfo=${b}"
}

# Read the .note.gnu.build-id from an ELF FILE (the fetched debuginfo is a bare ELF). Die (never
# empty) if no id — an empty id must not flow into the match guard.
elf_build_id() {
  local file="$1" id
  id="$(eu-readelf -n "$file" 2>/dev/null | awk '/Build ID:/{print $NF}')"
  [ -n "$id" ] || die "no build-id in ELF ${file}"
  printf '%s\n' "$id"
}

# Read the build-id from the ACTUAL staged kernel artifact (not repo metadata). A bare vmlinux ELF
# (common for ppc64le pseries) is read directly; a compressed bzImage/vmlinuz is first decompressed.
kernel_build_id() {
  local image="$1" magic vmlinux
  magic="$(head -c4 -- "$image" | od -An -tx1 | tr -d ' ')"
  if [ "$magic" = "7f454c46" ]; then
    vmlinux="$image"
  else
    vmlinux="$(mktemp)"
    extract-vmlinux "$image" >"$vmlinux" 2>/dev/null || die "cannot extract vmlinux from ${image}"
  fi
  elf_build_id "$vmlinux"
}

# rename(2) is atomic only within one filesystem: die unless A and B share a device.
assert_same_fs() {
  local a="$1" b="$2"
  [ "$(stat -c %d -- "$a")" = "$(stat -c %d -- "$b")" ] ||
    die "temp and destination not on one filesystem: ${a} vs ${b}"
}

# Record the pinned inputs atomically (write-temp-then-rename).
write_manifest() {
  local manifest="$1" nvr="$2" build_id="$3" rootfs_sha="$4" kernel_sha="$5" debuginfo_sha="$6" tmp
  tmp="$(mktemp -- "${manifest}.XXXXXX")"
  {
    printf 'kernel_nvr=%s\n' "$nvr"
    printf 'build_id=%s\n' "$build_id"
    printf 'rootfs_sha256=%s\n' "$rootfs_sha"
    printf 'kernel_sha256=%s\n' "$kernel_sha"
    printf 'debuginfo_sha256=%s\n' "$debuginfo_sha"
  } >"$tmp"
  mv -f -- "$tmp" "$manifest"
}

# Print a recorded field; rc 1 when the manifest is absent (stale, not an error).
manifest_field() {
  local manifest="$1" key="$2"
  [ -f "$manifest" ] || return 1
  sed -n "s/^${key}=//p" "$manifest"
}

# rc 0 iff the recorded NVR label equals TARGET_NVR (freshness trigger; necessary, not sufficient).
store_manifest_matches() {
  local manifest="$1" target_nvr="$2" have
  have="$(manifest_field "$manifest" kernel_nvr)" || return 1
  [ "$have" = "$target_nvr" ]
}

# Atomic commit point: flip the `current` symlink onto NEW_SET_DIR via one rename. A directory
# rename cannot atomically replace a populated destination (the same-NVR rebuild case); a symlink
# swap can, regardless of whether a prior same-NVR set exists.
commit_set() {
  local store="$1" new_set="$2"
  assert_same_fs "$store" "$new_set"
  ln -sfn -- "$(basename -- "$new_set")" "${store}/.current.tmp"
  mv -Tf -- "${store}/.current.tmp" "${store}/current"
}

# Remove every set-* dir not pointed at by `current` (post-commit prune AND entry orphan-sweep).
# Tolerant of a missing `current` (first run): then nothing is kept.
prune_other_sets() {
  local store="$1" keep="" d
  [ -L "${store}/current" ] && keep="$(readlink -- "${store}/current")"
  for d in "${store}"/set-*/; do
    [ -d "$d" ] || continue
    [ "$(basename -- "$d")" = "$keep" ] || rm -rf -- "$d"
  done
}
