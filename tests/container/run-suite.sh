#!/usr/bin/env bash
# Entry point of the `just test-linux` image (ADR-0717). Usage: run-suite <commit> [ARGS...]
# As root, it only gives the non-root tester the group that owns the engine socket, because
# several tests expect a permission refusal that root would not get.
set -euo pipefail

socket=/var/run/docker.sock
if [[ $(id -u) == 0 ]]; then
  gid=$(stat -c %g "$socket")
  group=$(getent group "$gid" | cut -d: -f1) || true
  if [[ -z $group ]]; then
    group=enginesock
    groupadd --gid "$gid" "$group"
  fi
  usermod --append --groups "$group" tester
  exec runuser -u tester -- "$0" "$@"
fi

commit=$1
shift
# The read-only mount is owned by root (Docker Desktop) or a foreign uid, so git refuses it
# as dubious ownership without this entry.
git config --global --add safe.directory /repo
git clone --quiet --no-checkout /repo /work/src
cd /work/src
git checkout --quiet --detach "$commit"
uv sync --locked --quiet
KDIVE_REQUIRE_DOCKER=1 exec just test --maxprocesses=8 "$@"
