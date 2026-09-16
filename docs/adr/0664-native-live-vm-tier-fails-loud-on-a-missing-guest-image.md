# 0664 — The native live_vm tier fails loud on a missing guest image

## Status

Accepted (2026-09-16)

## Context

[ADR-0035](0035-walking-skeleton-e2e-harness.md) §4 prescribes one rule for every gated
`live_vm` fixture: a preflight checks the fixture paths exist and `pytest.skip`s with the exact
script to run when they do not — "a missing fixture is a clear, actionable skip, never a
confusing mid-path failure" (`docs/adr/0035-walking-skeleton-e2e-harness.md:158-160`). That rule
is still in force for `KDIVE_KERNEL_SRC` everywhere, and for the `live_stack` tier's
`KDIVE_GUEST_IMAGE` check (`tests/integration/test_live_stack.py:130-145`, which still
`pytest.skip`s on the same variable it names).

#2518 (epic #2497) reversed the rule for one prerequisite in one tier. The native `live_vm`
tier's `_preflight` (`tests/integration/test_console_parts_live.py:83-109`) now `pytest.fail`s,
rather than skipping, when `KDIVE_GUEST_IMAGE` is unset or points at a missing file — checked
before `KDIVE_KERNEL_SRC` and `KDIVE_DATABASE_URL`, which still skip. `scripts/live-vm/preflight-env.sh`'s
`provisioned` family (`check_provisioned`, :65-78) likewise `require_path`s `KDIVE_GUEST_IMAGE`
rather than letting an unstaged rootfs read as "not configured."

That change was deliberate and correct: the native tier carries no `<N> passed` summary gate of
the kind `.github/workflows/live.yml`'s tcg spine has, so a skip there is indistinguishable from
a proof that never ran — run 35053412070 showed exactly this, `13 passed, 22 skipped` with the
untried proof buried in the skip count. It is disclosed in-code
(`test_console_parts_live.py:94-96` cites ADR-0035 §4 and #2497 by name) and in
`docs/operating/runbooks/live-testing.md:127-159`, which already documents the resulting
tri-state (unset → skip; set but wrong → fail loud; valid → proceed) and the native tier's
carve-out from it for this one variable. What was missing is the decision record: #2518 took no
ADR number, by the parallel-run convention that reserves numbering to the orchestrator, so an ADR
still in force kept saying the opposite of what the shipped code does. This record is that
write-up; it changes no behavior.

## Decision

**For the native `live_vm` tier's `KDIVE_GUEST_IMAGE` check only, ADR-0035 §4's
skip-on-missing-fixture rule is narrowed: unset or invalid is a hard failure, never a skip.**

Scoped to exactly what shipped in #2518:

- `tests/integration/test_console_parts_live.py`'s `_preflight` `pytest.fail`s when
  `KDIVE_GUEST_IMAGE` is unset or does not exist. `KDIVE_KERNEL_SRC` and `KDIVE_DATABASE_URL` in
  the same function still `pytest.skip`, unchanged.
- `scripts/live-vm/preflight-env.sh`'s `provisioned` family `require_path`s `KDIVE_GUEST_IMAGE`.
- The `live_stack` tier's own `KDIVE_GUEST_IMAGE` check
  (`tests/integration/test_live_stack.py:130-145`) is unaffected and keeps ADR-0035 §4's
  skip-on-unset behavior. The two tiers disagree about the same variable by design, not by drift:
  the native tier has no passing-count summary gate to catch a masquerading skip, and the
  `live_stack` tier does.
- `KDIVE_KERNEL_SRC` is unaffected everywhere; ADR-0035 §4 still governs it in both tiers.

Whether the `live_stack` tier should ever adopt the same fail-loud rule is out of scope here —
tracked, if and when it is decided, under epic #2497.

## Consequences

- ADR-0035 is amended (see its Status and §4) to record this narrowing instead of standing
  silently contradicted.
- A correctly provisioned native `live_vm` host must set `KDIVE_GUEST_IMAGE` before running; an
  operator who forgets gets a failed job, not a quietly skipped one.
- The divergence between the two tiers over the same variable is now a recorded decision, not an
  undocumented accident a future cleanup might "fix" by forcing them to match.

## Considered & rejected

- **Revert #2518 and restore the skip.** Rejected: that reintroduces the silent-green failure
  mode #2497 exists to close — a masquerading skip on a tier with no `<N> passed` gate to catch
  it.
- **Make `live_stack` fail loud too, for consistency.** Rejected here: not evidenced by this
  issue and explicitly out of scope. `live_stack`'s contract for this variable has not been
  revisited; epic #2497 owns that decision if and when it is made.
- **Fold this into ADR-0035 by editing it in place.** Rejected:
  [`docs/adr/README.md`](README.md) forbids rewriting an accepted decision — a merged ADR is
  append-only outside `## Status`. This record, plus ADR-0035's own append-only amendment, is the
  compliant path.
