# `KDIVE_GUEST_IMAGE` and the silent `live_vm` skip (#2518) — design

## Problem

`scripts/live-stack/env.sh` never exports `KDIVE_GUEST_IMAGE`, so `test_console_parts_live.py`
skips and `pytest -m live_vm` exits 0 having proved nothing (#2497 Instance 2). The issue's
non-goal assumed `.github/workflows/live.yml` was already wired; it is not: its native job runs
the non-tcg tier bare (`:786`) and sources that same `env.sh` (`:775`), which assigns nothing.

## Scope

The guest-image preflight fails loud instead of skipping: `-m live_vm` asks for the proof, and the
non-tcg tier carries no `<N> passed` guard, so a skip there reads as a pass. The native `live.yml`
job gains one line exporting `KDIVE_GUEST_IMAGE` — pointed, unlike the tcg job's `:538` line, at
the rootfs `mint-system.sh` stages inside the provider's allowed root. Both `env.sh` files gain a
comment naming the omission, and `live-testing.md`'s skip/fail tri-state records the exception.

Rejected — hard-require it in `scripts/live-stack/env.sh`. verified: six of that file's nine
callers are non-`live_vm` (stack-services, onboard, stack-status, apply-migrations,
worker-lifecycle, provision-queue-diagnostics), so it would break `just stack-migrate`.

Rejected — default it there. verified: `/var/lib/kdive/rootfs/local/` holds 11 kdive-ready qcow2
across 4 distro families including `-ppc64le`, so no arch-neutral default resolves.

### Failure model

Actors and deployments:
- a local operator running `just test-live` or `pytest -m live_vm` from a checkout
- the native `live.yml` job, which needs its runner and DB alias fixed before it reaches this

Invariants at stake:
- this proof must not exit 0 when it cannot run (#2497)
- non-`live_vm` consumers of `scripts/live-stack/env.sh` keep working

Accepted failure classes:
- the other 34 `live_vm` proofs, and `_preflight`'s two later branches, keep their skip gates
- in CI this changes nothing observable yet: that job fails at `require_compatible_lifecycle`
  before pytest, never runs on `pull_request`, and still lacks the bare `KDIVE_DATABASE_URL` the
  proof reads — so there the export moves the skip from the image gate to the database gate
- the runner's staged image is unverified here; a later failure there is the guard, not a bug

Covered elsewhere:
- that tier's `<N> passed` guard, stale runner, and bare `KDIVE_DATABASE_URL` alias — orchestrator

## Success

1. The proof fails naming `KDIVE_GUEST_IMAGE` when it is unset or points at a missing path.
2. The native `live.yml` job exports `KDIVE_GUEST_IMAGE`, one added line, that job only.
3. `scripts/live-stack/env.sh` still exports no `KDIVE_GUEST_IMAGE` and names the owner.

## Validation

- **Preflight fails loud (1).** Mode: focused-test — `_preflight` raises, not skips. Red: before
  the change that run exits 0 with `1 skipped`. Green: both fault arms exit non-zero naming
  `KDIVE_GUEST_IMAGE` under `pytest tests/integration/test_console_parts_live.py`.
- **Native job export (2).** Mode: task-test-not-applicable — CI cannot exercise the line (see
  failure model) and `tests/scripts/test_live_workflow_shape.py` is outside this surface, leaving
  the staged-rootfs literal it shares with `mint-system.sh` unpinned — a stated follow-up.
- **`env.sh` comments (3).** Mode: task-test-not-applicable — comment-only edits changing no shell
  behavior. Nothing asserts the variable's ABSENCE from `env.sh`; that gap is stated, not closed.
