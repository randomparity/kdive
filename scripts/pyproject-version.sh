#!/usr/bin/env bash
# Print the pyproject version, stripped of ANSI colour escapes.
#
# `uv version --short` colours its output when FORCE_COLOR is set in the environment (#1883
# reproduced with a dev shell exporting FORCE_COLOR=3; #1886 generalized the sweep), which
# breaks any string compare against a plain-text read — a Chart.yaml appVersion, a release tag.
# Strip escape sequences unconditionally rather than special-casing FORCE_COLOR, so every caller
# gets a colour-free version regardless of the caller's environment, TTY, or a future uv default
# that colours by default.
#
# Self-locates via BASH_SOURCE (like scripts/stamp-buildinfo.sh) so the result is correct
# regardless of the caller's working directory.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
(cd "$repo_root" && uv version --short) | sed -E $'s/\x1b\\[[0-9;]*m//g'
