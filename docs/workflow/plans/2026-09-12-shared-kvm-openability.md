# Shared KVM openability implementation plan

Goal: converge Python worker-host KVM observation on one `O_RDWR` probe so acceleration diagnostics
and host-appliance budgets agree on static, absent, denied, and openable configured nodes.

KDIVE is Python 3.14 under `uv`. Preserve the provider boundary: shared code may use only the
declared local-libvirt settings exception. Keep shell readiness and per-tool budget callers out of
scope. ADR-0648 supersedes only ADR-0352/0637's shared-probe aspects.

Expected implementation size: 90–150 changed lines (M) — one probe replacement, consumers, focused
tests, catalogue text, and one decision record.

## File map

- `src/kdive/diagnostics/contributions/guest_arch_accel.py`: owns snapshot node resolution and the
  injected openability probe.
- `src/kdive/providers/shared/build_timeouts.py`: removes the duplicate probe and imports the shared
  probe without changing scaler callers.
- `src/kdive/providers/local_libvirt/lifecycle/deadlines.py` and
  `tests/integration/live_stack/conftest.py`: use the shared default without URI selection.
- `src/kdive/config/external_env.py`: lists the diagnostic among existing `KDIVE_KVM_NODE` readers.
- `tests/diagnostics/test_guest_arch_accel.py` and `tests/providers/shared/test_build_timeouts.py`:
  prove the filesystem and consumer contracts.
- `docs/adr/0648-shared-kvm-openability-probe.md`: durable limited supersession.

## Task 1 — Define and prove the shared probe

**Files:** `guest_arch_accel.py`, `test_guest_arch_accel.py`.

**Interfaces:** replace `kvm_probe_for_uri(uri, *, node, access, exists)` with a probe factory that
accepts optional `node`, `open`, and `close` seams and returns `Callable[[], bool]`; defaults use
`env_snapshot`, `os.open`, and `os.close`.

**Verification:**

- Mode: focused-test. Prove that a pre-change test expecting system-URI presence fails; then run
  `uv run python -m pytest tests/diagnostics/test_guest_arch_accel.py -q` green.

**Steps:**

1. Write tests for system/session/transport URI independence, configured-node override, empty
   fallback, `O_RDWR` flags, immediate close, and an injected ENODEV result.
2. Implement snapshot resolution and `try: fd = open(node, os.O_RDWR) ... finally: close(fd)`;
   return false on `OSError` and never call close after an unsuccessful open.
3. Make the default acceleration probe call the shared factory directly; retain URI only for
   remote-target reporting.

**Acceptance:** all direct filesystem tests avoid real `/dev/kvm`; an unopenable native node maps
native acceleration to TCG.

## Task 2 — Route current Python consumers through the probe

**Files:** `build_timeouts.py`, `deadlines.py`, `tests/integration/live_stack/conftest.py`,
`test_build_timeouts.py`, `test_provider_boundaries.py` (verification only).

**Interfaces:** `appliance_budget_s` and `slow_build_tool_timeout_s` keep their existing injected
`kvm_present` parameter; their default is the diagnostics-owned shared probe.

**Verification:**

- Mode: focused-test. Prove the old private-probe symbol no longer exists and run
  `uv run python -m pytest tests/providers/shared/test_build_timeouts.py tests/providers/test_provider_boundaries.py -q` green.

**Steps:**

1. Remove `_worker_host_kvm_usable` and its local constants/imports.
2. Import the provider-agnostic diagnostics probe and use it only when no injected seam is passed.
3. Replace URI-selected default calls in deadline and live-stack helpers while retaining injected
   overrides; update shared-timeout tests to drive the common probe behavior.

**Acceptance:** no scaler/caller signature changes; no `providers/shared` import reaches a
local-libvirt implementation module.

## Task 3 — Document the contract and verify the branch

**Files:** `external_env.py`, ADR-0648, this plan/spec.

**Verification:**

- Mode: task-test-not-applicable. ADR rationale and catalogue prose have no independent executable
  consumer beyond the repository documentation/config guards; run `just records`, `just docs-links`,
  and `just docs-paths`.

**Steps:**

1. Add the diagnostics and unified openability semantics to the catalogue entry without claiming
   shell-tier convergence.
2. Keep ADR-0352 and ADR-0637 unchanged; state the limited supersession and residual architecture
   question only in ADR-0648.
3. Run focused tests, `just lint`, `just type`, records, and doc guards; inspect the diff before
   committing.

**Acceptance:** the ADR index is untouched, every direct code/test change is in Tasks 1–2, and
the residual does not become an undocumented diagnostic claim.
