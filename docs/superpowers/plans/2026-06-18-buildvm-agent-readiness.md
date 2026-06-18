# Plan — build-VM guest-agent readiness gate + post-readiness code-86 classification (#552)

Derived from [the spec](../../design/buildvm-agent-readiness.md) and
[ADR-0168](../../adr/0168-build-vm-agent-responsiveness-gate.md). TDD throughout: write the
failing test first, confirm it fails for the expected reason, then the minimal implementation.

Guardrails before every commit (CI gates these individually — see `AGENTS.md`):
`just lint` · `just type` · `uv run python -m pytest <focused paths> -q`. Full `just ci` before
the first push.

Scope (files this plan touches — do not edit others):
- `src/kdive/providers/remote_libvirt/guest/agent.py`
- `src/kdive/providers/remote_libvirt/lifecycle/readiness.py`
- `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py`
- `src/kdive/providers/remote_libvirt/guest/build_transport.py`
- `src/kdive/diagnostics/buildhost_agent.py`
- tests mirroring the above
- docs already committed (spec, ADR, README index)

## Task 1 — Parameterize the guest-agent classifier (Part B foundation)

**Where it fits:** ADR-0168 decision #2. Extract the libvirt-error classifier so the
deterministic-code set is per-construction, enabling the build transport to treat code 86 as
deterministic without changing the global default.

**Files:** `guest/agent.py`, `tests/providers/remote_libvirt/guest/test_guest_agent.py`.

**Steps (TDD):**
1. Failing test: construct `GuestAgentExec(..., deterministic_codes=_DETERMINISTIC_CONFIG_CODES | {libvirt.VIR_ERR_AGENT_UNRESPONSIVE})` (or import `BUILD_DETERMINISTIC_CONFIG_CODES`) and assert a code-86 round-trip raises `CONFIGURATION_ERROR`. Confirm it fails (param does not exist yet).
2. Implement: add `classify_agent_libvirt_error(domain, exc, *, deterministic_codes) -> CategorizedError` as a module function (move the body of the current `_classify_libvirt_error`). Add `deterministic_codes: frozenset[int] = _DETERMINISTIC_CONFIG_CODES` to `GuestAgentExec.__init__`; store it; have the instance delegate to the module function with `self._deterministic_codes`. Define `BUILD_DETERMINISTIC_CONFIG_CODES = _DETERMINISTIC_CONFIG_CODES | frozenset({libvirt.VIR_ERR_AGENT_UNRESPONSIVE})` with a docstring citing ADR-0168 (post-readiness scope). Export it in `__all__` if the module has one.
3. Keep the existing `test_transient_libvirt_error_stays_transport_failure` green (default set still maps 86 → `TRANSPORT_FAILURE`) — this pins ADR-0159 is intact. Add an explicit test asserting that.

**Acceptance:** default exec maps 86 → `TRANSPORT_FAILURE`; build-set exec maps 86 → `CONFIGURATION_ERROR`; base deterministic codes map to `CONFIGURATION_ERROR` under both sets. `just type` clean.

**Rollback:** revert the parameter; default-valued so no other caller changes.

## Task 2 — `wait_for_agent_responsive` guest-ping gate (Part A core)

**Where it fits:** ADR-0168 decision #1. The active readiness probe that closes the
channel-connected-but-not-responsive window.

**Files:** `lifecycle/readiness.py`, `tests/providers/remote_libvirt/lifecycle/test_readiness.py`.

**Steps (TDD):**
1. Failing tests for a new `wait_for_agent_responsive(agent_command, domain, domain_name, *, monotonic, sleep, timeout_s, poll_s, call_timeout_s=5)`:
   - returns when the first ping call returns (agent answers);
   - polls past a transient (code-86 then code-86 then success) and returns;
   - raises `CONFIGURATION_ERROR` immediately on a base deterministic-config code (no polling);
   - on the deadline (ping always raises code 86) raises `CONFIGURATION_ERROR` whose `details[AGENT_READINESS_DETAIL_KEY] == AGENT_UNRESPONSIVE`, `domain` and `timeout_s` present.
2. Implement: add the exported constants `AGENT_READINESS_DETAIL_KEY = "agent_readiness"`, `AGENT_UNRESPONSIVE = "unresponsive"`. The loop issues `agent_command(domain, json.dumps({"execute": "guest-ping"}), call_timeout_s, 0)`; on return → done; on `libvirt.libvirtError` use `classify_agent_libvirt_error(domain, exc, deterministic_codes=_DETERMINISTIC_CONFIG_CODES)` — if it returns `CONFIGURATION_ERROR`, re-raise it now; otherwise (transport/transient, incl. 86) keep polling; on deadline raise the marked `CONFIGURATION_ERROR`. Import `classify_agent_libvirt_error` and `_DETERMINISTIC_CONFIG_CODES` from `guest/agent.py`.
3. Mirror the `_ticker`/`libvirt_error` helpers already in the test module / conftest.

**Acceptance:** all four behaviors pass; the deadline error is non-retryable (category `CONFIGURATION_ERROR`) and carries the marker. `just type` clean.

**Rollback:** delete the function + constants; no caller yet until Task 3.

## Task 3 — Wire the gate into `EphemeralBuildVm.session` (Part A integration)

**Where it fits:** ADR-0168 decision #1, build path.

**Files:** `lifecycle/build_vm.py`, `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`.

**Steps (TDD):**
1. Update the existing test agent fakes (`_agent_ok`, `_agent_route_after`, `_EgressAgent`) to answer `{"execute":"guest-ping"}` with `{"return": {}}` (a new readiness step now precedes guest-exec). Failing test first: a `_build_vm_with_agent` whose agent raises code 86 on guest-ping (always) → `vm.session(...)` raises `CONFIGURATION_ERROR` with the readiness marker and still tears the VM down (domain gone, overlay deleted). Add a small `agent_responsive_timeout_s`/`poll_s` override path.
2. Implement: add `agent_responsive_timeout_s: float = 120.0` and `agent_responsive_poll_s: float = 2.0` to `BuildVmTiming`. In `session`, after `wait_for_agent`, look up the domain once (`conn.lookupByName(domain_name)`) and call `wait_for_agent_responsive(self._agent_command, domain, domain_name, monotonic=..., sleep=..., timeout_s=self._timing.agent_responsive_timeout_s, poll_s=self._timing.agent_responsive_poll_s)`. Reuse that domain handle for the transport (avoid a second lookup). The gate runs inside the existing `try:`/`finally:` so teardown still fires.
3. Confirm existing session tests stay green once the fakes answer guest-ping (yields-after-route, egress preflight, wait_network=False diagnostic path).

**Acceptance:** never-responsive agent → non-retryable `CONFIGURATION_ERROR` + teardown; healthy agent (ping answers) → existing yield/route/egress behavior unchanged. `just type` clean.

**Rollback:** remove the gate call + timing fields; fakes' guest-ping answers are harmless.

## Task 4 — Build transport uses the build deterministic set (Part B integration)

**Where it fits:** ADR-0168 decision #2, build path.

**Files:** `guest/build_transport.py`, `tests/providers/remote_libvirt/guest/test_build_transport.py`.

**Steps (TDD):**
1. Failing test: a `GuestExecBuildTransport` whose `agent_command` raises code 86 on a `run(...)` → `CategorizedError` with category `CONFIGURATION_ERROR` (and, via `ToolResponse`, `retryable=false`). Keep/confirm a base-deterministic-code test → `CONFIGURATION_ERROR` and a transient-non-86 test → `TRANSPORT_FAILURE`.
2. Implement: in `_agent_for`, pass `deterministic_codes=BUILD_DETERMINISTIC_CONFIG_CODES` to the `GuestAgentExec` constructor (import the constant from `guest/agent.py`).

**Acceptance:** build transport maps 86 → `CONFIGURATION_ERROR`; other categories unchanged. `just type` clean.

**Rollback:** drop the `deterministic_codes` argument (falls back to the default base set).

## Task 5 — Diagnostic recognizes the agent-readiness marker (verdict mapping)

**Where it fits:** ADR-0168 decision #3 (ADR-0167 diagnostic).

**Files:** `diagnostics/buildhost_agent.py`, `tests/diagnostics/test_buildhost_agent.py` (and `_check.py` if it asserts the FAIL verdict).

**Steps (TDD):**
1. Failing test: a session factory whose agent raises code 86 on guest-ping drives the real `wait_for_agent_responsive` gate to its deadline; assert `_blocking_probe` / `buildhost_agent_probe` classifies the host as `AGENT_UNREACHABLE` (FAIL), not `HOST_UNREACHABLE`. Add/confirm a test that an *unmarked* `CONFIGURATION_ERROR` (e.g. no base image) stays `HOST_UNREACHABLE`.
2. Implement: in `_blocking_probe`'s `except CategorizedError as exc:` branch, treat `exc.details.get(AGENT_READINESS_DETAIL_KEY) == AGENT_UNRESPONSIVE` as `AGENT_UNREACHABLE` alongside the existing `PROVISIONING_FAILURE` check. Import the constants from `lifecycle/readiness.py`. Update the module docstring's discriminator sentence.

**Acceptance:** marked `CONFIGURATION_ERROR` → `AGENT_UNREACHABLE`; unmarked → `HOST_UNREACHABLE`; existing diagnostic tests green (their fakes may need a guest-ping answer if they drive the real session).

**Rollback:** remove the marker branch; reverts to category-only discrimination.

## Task 6 — Full guardrails, branch review, security review, PR

1. `just ci` (full suite — architecture/boundary/doc-gen tests live outside touched dirs).
2. Review loop: `/challenge --json --base main` until approve.
3. `security-review` on the branch; address findings.
4. Fold fixups into their logical commits, push, `gh pr create` against `main`, `Closes #552`.
5. Drive to CI-green **and** `mergeStateStatus=CLEAN`/`mergeable=MERGEABLE`.

## Ordering / dependencies

Task 1 → Task 4 (build transport imports the constant). Task 2 → Task 3 (session calls the gate)
→ Task 5 (diagnostic drives the gate). Task 2 imports from Task 1's module but only the existing
`classify_agent_libvirt_error`/`_DETERMINISTIC_CONFIG_CODES`, so do Task 1 first. Linear order
1→2→3→4→5→6 is safe; tasks share files within the build provider so implement sequentially on the
one branch (no parallel worktrees).
</content>
