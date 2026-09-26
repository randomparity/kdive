---
title: macOS `just test` gives no usable baseline; run the suite in a local Linux container
date: 2026-09-25
tags: [environment-quirk, test-baseline, docker, testcontainers, macos]
components: [justfile, tests/db/conftest.py, .github/workflows/ci.yml]
---

## Problem

On macOS, `just test` fails hundreds of tests for host reasons, on `main` and on every branch.
One branch run on 2026-09-25 gave `321 failed, 18921 passed, 142 skipped, 37 errors`, and
`main` failed in the same way. A comparison against `main` on macOS cannot show whether a change
broke a test, because the noise is larger than the signal. The first attempt at a Linux container
also had noise: `60 failed, 37 errors` on `main`.

## Root cause

The failures come from the test host, not from the code:

- macOS: tests need Linux behavior (for example `/proc`, pidfd, and Linux tool output).
- An arm64 container on Apple silicon: `error: unsupported architecture: aarch64` and
  `capture bootstrap manifest architecture drift`. CI runs on x86_64 (`ubuntu-latest`).
- A container that runs as root: `stack-services.sh must run as the provisioned
  lifecycle-control operator, not UID 0`, and the permission tests that expect a refusal
  do not get one.
- Missing tools: `FileNotFoundError: ... 'just'`, and no Docker CLI or compose plugin.
- Too many xdist workers against the one testcontainers PostgreSQL cluster:
  `cluster-global runtime-role test lock timed out after 60000 milliseconds`. These errors
  depend on timing, so they can appear on one side of a comparison only.

## Solution

Run the `just test` selection in an x86_64 Linux container that is close to the CI job, and
compare failing test IDs between an `origin/main` baseline and the branch.

`Dockerfile` (in a scratch directory, not in the repository):

```dockerfile
FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.14-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends libvirt-dev pkg-config build-essential git ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin
RUN UV_TOOL_BIN_DIR=/usr/local/bin UV_TOOL_DIR=/opt/uv-tools uv tool install rust-just \
 && useradd -m -u 1000 tester \
 && install -d -o tester -g tester /work /venv
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv UV_CACHE_DIR=/work/.uv-cache
WORKDIR /work
```

`run.sh` (same scratch directory; the container mounts it at `/out`):

```bash
#!/usr/bin/env bash
# Usage (container entry, as root): run.sh <sha> <name>
# Grants the non-root tester the Docker socket's group, then runs the suite as tester.
set -uo pipefail
sha=$1; name=$2
if [[ $(id -u) == 0 ]]; then
  gid=$(stat -c %g /var/run/docker.sock)
  getent group "$gid" >/dev/null || groupadd -g "$gid" dockersock
  usermod -aG "$(getent group "$gid" | cut -d: -f1)" tester
  exec runuser -u tester -- "$0" "$@"
fi
git clone -q /repo /work/src && cd /work/src && git checkout -q "$sha" || exit 3
uv sync --locked --quiet || exit 4
PYTHONHASHSEED=0 KDIVE_REQUIRE_DOCKER=1 uv run --no-sync python -m pytest \
  -m "not live_vm and not live_stack and not agent_smoke" -n auto --maxprocesses=8 --dist worksteal \
  -q --tb=short -p no:cacheprovider > "/out/$name.log" 2>&1
rc=$?
grep -E "^(FAILED|ERROR) " "/out/$name.log" | sed 's/ - .*//' | sort -u > "/out/$name.fail"
echo "$name rc=$rc $(tail -1 "/out/$name.log")"
```

Build once, then run the baseline and the branch one after the other, and compare:

```bash
cd "$SCRATCH" && docker build --platform linux/amd64 -t kdive-linux-test:py314-amd64 .
for pair in "main:$(git rev-parse origin/main)" "branch:$(git rev-parse HEAD)"; do
  docker run --rm --platform linux/amd64 \
    -v "$REPO:/repo:ro" -v "$SCRATCH:/out" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
    --add-host host.docker.internal:host-gateway \
    kdive-linux-test:py314-amd64 /out/run.sh "${pair#*:}" "${pair%%:*}"
done
comm -13 "$SCRATCH/main.fail" "$SCRATCH/branch.fail"   # new on the branch
comm -23 "$SCRATCH/main.fail" "$SCRATCH/branch.fail"   # fixed on the branch
```

Notes on the parts that matter:

- `$REPO` is the main checkout, not a worktree. Commits made in a worktree are in the main
  repository's object store, so `git clone /repo` then `git checkout <sha>` works for a
  worktree branch. The clone happens inside the container, so the macOS `.venv` and the
  worktree files are not touched (`UV_PROJECT_ENVIRONMENT=/venv`).
- The DB tests start PostgreSQL through testcontainers (`tests/db/conftest.py`). The container
  uses the host's Docker through the mounted socket, and reaches the sibling PostgreSQL
  container through `host.docker.internal`.
- Match the CI job: `KDIVE_REQUIRE_DOCKER=1` and the `just test` marker selection
  (`.github/workflows/ci.yml`, step `Test`).

Verified on 2026-09-25 with Docker Desktop 29.8.0 on Apple silicon, `main` at `d2b0686ed`:

```text
main rc=1 50 failed, 19154 passed, 181 skipped in 339.92s (0:05:39)
branch rc=1 50 failed, 19190 passed, 181 skipped in 345.71s (0:05:45)
NEW on branch:
FIXED on branch:
```

The 50 failures that remain on `main` in this container are a known baseline, not a
regression signal: `tests/jobs/capture_operations/` (36, the process sandbox and launcher
tests), `tests/scripts/test_live_stack_scripts.py` (11), and 3 others. Compare against the
baseline; do not expect a zero-failure run.

## Prevention

- Do not use a macOS `just test` result as regression evidence. Use this container comparison,
  or CI.
- Keep `--maxprocesses` at 8 or lower. With 12 workers the shared PostgreSQL global lock
  timed out and added errors on one side of the comparison only.
- In an agent session, a hook blocks any Bash command whose text contains `rm` with `-r` and
  `-f`. A Dockerfile written through a heredoc with an `rm -rf /var/lib/apt/lists/*` cleanup
  is blocked too, so leave that cleanup out of this test-only image.
