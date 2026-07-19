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
