# `KDIVE_GUEST_IMAGE` and the silent `live_vm` skip (#2518) — design

## Problem

`scripts/live-stack/env.sh` never exports `KDIVE_GUEST_IMAGE`; `examples/local-libvirt/env.sh:55`
is the tree's only export. An operator sourcing the ordinary entry point gets `KDIVE_KERNEL_SRC`
but silently not the guest image, so `tests/integration/test_console_parts_live.py` skips and
`pytest -m live_vm` exits 0 having proved nothing (#2497 Instance 2).

## Scope

`KDIVE_GUEST_IMAGE` stays operator-supplied, owned by `examples/local-libvirt/env.sh` and by
`build-fs`'s own `export KDIVE_GUEST_IMAGE=` line. The console proof's guest-image preflight then
fails instead of `pytest.skip`; both `env.sh` files gain a comment naming the deliberate omission
and its owner, matching `env.sh`'s existing `KDIVE_DATABASE_URL` comment; and
`docs/operating/runbooks/live-stack.md` states the requirement and the new failure.

Rejected — hard-require it in `scripts/live-stack/env.sh`. verified: six non-`live_vm` consumers
source it (`stack-services.sh`, `onboard.sh`, `stack-status.sh`, `apply-migrations.sh`,
`worker-lifecycle.sh`, `provision-queue-diagnostics.sh`), so that fails `just migrate` and stack
startup on every host without a built image.

Rejected — default it there. verified: `/var/lib/kdive/rootfs/local/` holds both the x86_64 and
`-ppc64le` kdive-ready qcow2 on a full proof host, so no arch-neutral default or glob resolves.

### Failure model

Actors and deployments:
- a local operator running `just test-live` or `pytest -m live_vm` from a checkout
- the `live.yml` hosted spine, which exports its own guest-image variable (#2518 non-goal)

Invariants at stake:
- a `live_vm` tier that ran no proof must not exit 0 (#2497)
- non-`live_vm` consumers of `scripts/live-stack/env.sh` keep working

Accepted failure classes:
- the other 34 `live_vm` proofs keep their own skip gates — outside this change's surface
- `_preflight`'s `KDIVE_KERNEL_SRC` / `KDIVE_DATABASE_URL` branches still skip, and
  `just test-live` still has no tier `<N> passed` guard — outside the granted surface

Covered elsewhere:
- tier-level zero-proof guards — #2517 (`just test-live-tcg`), `.github/workflows/live.yml`
- `test_console_parts_live.py:55` and `:394` — #2496 / PR #2529

## Success

1. `test_console_parts_live.py` exits non-zero naming `KDIVE_GUEST_IMAGE` when the variable is
   unset, and likewise when it points at a nonexistent path.
2. `scripts/live-stack/env.sh` exports no `KDIVE_GUEST_IMAGE` and names the owner.
3. The runbook's live-proof text matches 1 and 2.

## Validation

- **Guest-image preflight fails loud (1).** Mode: focused-test — `_preflight` raises, not skips.
  Red: before the change that run exits 0 with `1 skipped`. Green: the same run fails naming
  `KDIVE_GUEST_IMAGE` — `uv run python -m pytest tests/integration/test_console_parts_live.py`.
- **Both `env.sh` comments (2).** Mode: task-test-not-applicable — comment-only edits changing no
  shell behavior; `tests/scripts/test_live_stack_scripts.py` already pins the file's export set.
- **Runbook text (3).** Mode: task-test-not-applicable — prose with no executable consumer.
