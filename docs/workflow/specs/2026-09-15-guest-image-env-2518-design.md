# `KDIVE_GUEST_IMAGE` and the silent `live_vm` skip (#2518) — design

## Problem

`scripts/live-stack/env.sh` never exports `KDIVE_GUEST_IMAGE`, so `test_console_parts_live.py`
skips and the tier exits 0 having proved nothing (#2497 Instance 2). The issue's non-goal assumed
`.github/workflows/live.yml` was already wired; it is not — its native job runs the non-tcg tier
bare and sources that same `env.sh`, so a green run there reports 22 skips including this proof.

## Scope

The proof's guest-image preflight fails instead of `pytest.skip`, because the non-tcg tier has
no `<N> passed` gate and a skip there reads as a pass.
`preflight-env.sh`'s `check_provisioned` gains `require_path KDIVE_GUEST_IMAGE`, so the native
spine fails at the env contract rather than mid-run — that is the family `mint-system.sh` serves,
and it stages the very file. The native spine exports that staged path (not the warm-store one)
and aliases the bare `KDIVE_DATABASE_URL` the proof reads, as the tcg spine does. Both `env.sh`
files gain a comment naming the omission, and `live-testing.md` records the tri-state exception.

Rejected — hard-require it in `scripts/live-stack/env.sh`. verified: six of that file's nine
callers are non-`live_vm`, so it would break `just stack-migrate` (the PR body lists them).

Rejected — default it there. verified: `/var/lib/kdive/rootfs/local/` holds 11 kdive-ready qcow2
across 4 distro families including `-ppc64le`, so no arch-neutral default resolves.

### Failure model

Actors and deployments:
- a local operator running `just test-live` or `pytest -m live_vm` from a checkout
- the native `live.yml` job, on a self-hosted KVM runner, scheduled only

Invariants at stake:
- this proof must not exit 0 when it cannot run (#2497)
- non-`live_vm` consumers of `scripts/live-stack/env.sh` keep working
- #1929's one-DSN-per-authority split stands: the spine alias is test-side, not a fourth authority

Accepted failure classes:
- the other 34 `live_vm` proofs, and `_preflight`'s kernel-tree branch, keep their skip gates
- whether the runner carries a staged image is unverified here; a failure there is the guard

Covered elsewhere:
- the tier's `<N> passed` gate — #2540; the runner faults that masked this — #2543-#2545

## Success

1. The proof fails naming `KDIVE_GUEST_IMAGE` when it is unset or points at a missing path.
2. `preflight-env.sh provisioned` fails naming it, before pytest is reached.
3. The native spine exports the staged guest image and the bare `KDIVE_DATABASE_URL`.
4. `scripts/live-stack/env.sh` still exports no `KDIVE_GUEST_IMAGE` and names the owner.

## Validation

- **Preflight fails loud (1).** Mode: focused-test — `_preflight` raises, not skips. Red: before
  the change that run exits 0 with `1 skipped`. Green: both fault arms exit non-zero naming it.
- **Provisioned family (2).** Mode: focused-test — `tests/scripts/test_live_vm_preflight.py`
  covers unset, a path that never landed, and a staged image.
- **Native spine wiring (3).** Mode: focused-test for the guest image —
  `test_live_workflow_shape.py` pins it to `mint-system.sh`'s basename inside the allowed root.
- **`env.sh` comments (4).** Mode: task-test-not-applicable — comment-only edits changing no
  shell behavior. Nothing asserts the variable's ABSENCE from `env.sh`; stated, not closed.
