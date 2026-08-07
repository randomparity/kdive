#!/usr/bin/env bash
# Pull the testcontainer images the suite starts, ahead of the suite, with a bounded retry.
#
# The db and store fixtures start their containers lazily from inside pytest, so an
# unreachable registry surfaced as `3 failed, 8938 passed, 16 skipped, 3448 errors` with the
# real cause several screens down (#1913). Pulling here makes a registry outage one red step
# naming one image, and a transient blip a retry instead of a re-run (ADR-0553).
#
# This does not make the suite tolerate a missing image: the fixtures still pull lazily if
# this never ran. It changes what a failure looks like, not whether it fails.
#
# `docker pull` has no verdict/transport ambiguity to resolve — unlike the pip-audit path in
# audit-deps.sh, a non-zero exit here can only mean the pull did not happen — so a plain
# bounded retry is correct.
#
# Usage: pull-test-images.sh
set -euo pipefail

# Keep in step with the fixture constants, which is not optional: pre-pulling a tag the suite
# never requests warms nothing and still passes. tests/guards/test_prepull_images_match_fixtures.py
# parses this array and both constants and fails on any drift.
readonly IMAGES=(
  "postgres:17@sha256:7958605b474b3d264a969cb3a123d6aa00ad1e1fe9da8a69984dabb704d93317"                              # tests/db/conftest.py::_POSTGRES_IMAGE  # noqa: E501  # 17
  "minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e" # tests/store/conftest.py::_MINIO_IMAGE  # noqa: E501  # RELEASE.2025-09-07T16-13-09Z
)

# One attempt per backoff entry, plus the first. Deriving ATTEMPTS keeps the two in step:
# raising it alone would index past the array, and `set -u` aborts on an unbound element.
readonly BACKOFF_S=(5 15)
readonly ATTEMPTS=$((${#BACKOFF_S[@]} + 1))

pull_with_retry() {
  local image="$1" attempt
  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if docker pull --quiet "$image"; then
      return 0
    fi
    if ((attempt < ATTEMPTS)); then
      local delay="${BACKOFF_S[attempt - 1]}"
      echo "pull-test-images: '$image' failed (attempt ${attempt}/${ATTEMPTS}); retrying in ${delay}s" >&2
      sleep "$delay"
    fi
  done
  echo "::error::could not pull '$image' after ${ATTEMPTS} attempts — the registry is unreachable" >&2
  return 1
}

for image in "${IMAGES[@]}"; do
  pull_with_retry "$image"
done

echo "pull-test-images: ${#IMAGES[@]} image(s) present locally."
