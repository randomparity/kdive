# Ephemeral build-VM network readiness + `git fetch` rc surfacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix #500 — an ephemeral-libvirt git build no longer fails its clone with a misleading
`FETCH_HEAD` error: the build-VM session waits for in-guest network before yielding the transport,
and `clone()` surfaces the real `git init`/`git fetch` failure instead of masking it.

**Architecture:** Two changes at the layers that own each failure. (1) A new `wait_for_network`
poll loop in `lifecycle/readiness.py` plus an in-guest default-route probe gate in
`lifecycle/build_vm.py`, run after `wait_for_agent` and before the transport is yielded. (2)
`ShellBuildTransport.clone()` checks the `git init` and `git fetch` return codes, not only
`checkout`. See [ADR-0144](../../adr/0144-ephemeral-build-network-readiness.md) and the
[spec](../../design/ephemeral-build-network-readiness.md).

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, `ty`. Guest-agent exec over qemu-guest-agent
(ADR-0078/0100). No new dependency, DB column, migration, env var, or MCP tool.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Run `just lint` before every commit.
- `ty` runs whole-tree (src + tests) with strict defaults. Run `just type` before every commit.
- Tests excluding the `live_vm`/`live_stack` markers run via `just test`. Drive units directly with
  injected fakes — never a real libvirt host.
- Pick the most specific existing `ErrorCategory`; never invent a string (`domain/errors.py`).
- All guest/remote output passes `redacted_tail(text, secret_registry)` before it reaches an error
  detail. Never log/return raw stderr.
- Conventional-commit subjects ≤72 chars, imperative mood; end every commit message with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Doc-style: plain, factual; avoid "critical", "robust", "comprehensive", "elegant".

---

## File Structure

- `src/kdive/providers/remote_libvirt/lifecycle/readiness.py` — gains `wait_for_network` + the
  `NetworkProbe`/`TimeoutDetail` type aliases, next to the existing `wait_for_agent`.
- `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py` — gains the default-route probe
  constants, two `BuildVmTiming` fields, and the `_wait_for_network` gate call in `session()`.
- `src/kdive/providers/shared/build_host/shell_transport.py` — `clone()` checks init + fetch rc.
- `tests/providers/remote_libvirt/lifecycle/test_readiness.py` — new `wait_for_network` tests.
- `tests/providers/remote_libvirt/lifecycle/test_build_vm.py` — gate tests (poll-then-ready,
  never-ready-tears-down).
- `tests/providers/build_host/test_shell_transport.py` — clone init/fetch rc tests.

---

## Task 1: `wait_for_network` poll loop

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/readiness.py`
- Test: `tests/providers/remote_libvirt/lifecycle/test_readiness.py` (new file)

**Interfaces:**
- Consumes: existing `Monotonic`, `Sleep` type aliases; `CategorizedError`, `ErrorCategory` from
  `kdive.domain.errors`.
- Produces:
  ```python
  type NetworkProbe = Callable[[], bool]
  type TimeoutDetail = Callable[[], dict[str, object]]

  def wait_for_network(
      probe: NetworkProbe,
      domain_name: str,
      *,
      monotonic: Monotonic,
      sleep: Sleep,
      timeout_s: float,
      poll_s: float,
      timeout_detail: TimeoutDetail | None = None,
  ) -> None: ...
  ```

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/remote_libvirt/lifecycle/test_readiness.py`:

```python
"""Unit tests for the build-VM network-readiness poll loop (ADR-0144)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.readiness import wait_for_network


def _ticker(step: float = 1.0) -> Callable[[], float]:
    now = {"t": 0.0}

    def _monotonic() -> float:
        current = now["t"]
        now["t"] += step
        return current

    return _monotonic


def _sequence_probe(returns: list[bool]) -> Callable[[], bool]:
    calls = {"i": 0}

    def _probe() -> bool:
        value = returns[min(calls["i"], len(returns) - 1)]
        calls["i"] += 1
        return value

    return _probe


def test_returns_when_probe_true_on_first_call() -> None:
    wait_for_network(
        lambda: True,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_polls_until_probe_flips_true() -> None:
    probe = _sequence_probe([False, False, True])
    wait_for_network(
        probe,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_raises_provisioning_failure_past_deadline() -> None:
    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            lambda: False,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=3.0,
            poll_s=1.0,
        )
    assert exc.value.category == ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["domain"] == "kdive-build-x"


def test_timeout_error_carries_timeout_detail_keys() -> None:
    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            lambda: False,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=3.0,
            poll_s=1.0,
            timeout_detail=lambda: {"probe_stderr": "cut: not found", "probe_stdout": ""},
        )
    assert exc.value.details["probe_stderr"] == "cut: not found"


def test_propagates_categorized_error_raised_by_probe() -> None:
    def _broken_probe() -> bool:
        raise CategorizedError("agent gone", category=ErrorCategory.TRANSPORT_FAILURE)

    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            _broken_probe,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=10.0,
            poll_s=1.0,
        )
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_readiness.py -q`
Expected: FAIL with `ImportError: cannot import name 'wait_for_network'`.

- [ ] **Step 3: Implement `wait_for_network`**

In `src/kdive/providers/remote_libvirt/lifecycle/readiness.py`, after the existing `type Sleep` /
`type Monotonic` aliases add:

```python
type NetworkProbe = Callable[[], bool]
type TimeoutDetail = Callable[[], dict[str, object]]
```

Then add the function (place it after `wait_for_agent`, before `_infra`):

```python
def wait_for_network(
    probe: NetworkProbe,
    domain_name: str,
    *,
    monotonic: Monotonic,
    sleep: Sleep,
    timeout_s: float,
    poll_s: float,
    timeout_detail: TimeoutDetail | None = None,
) -> None:
    """Poll an in-guest network-readiness probe until it succeeds or the deadline passes.

    A ``False`` from *probe* means "not ready, keep polling"; a ``CategorizedError`` raised by
    *probe* (the agent dropped) propagates unchanged. On deadline, raise ``PROVISIONING_FAILURE``
    merged with ``timeout_detail()`` (the last probe output) so a broken probe is diagnosable
    rather than a bare timeout.
    """
    deadline = monotonic() + timeout_s
    while True:
        if probe():
            return
        if monotonic() >= deadline:
            details: dict[str, object] = {"domain": domain_name, "timeout_s": timeout_s}
            if timeout_detail is not None:
                details.update(timeout_detail())
            raise CategorizedError(
                f"guest network did not come up within {timeout_s:g}s",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details=details,
            )
        sleep(poll_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_readiness.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/lifecycle/readiness.py \
        tests/providers/remote_libvirt/lifecycle/test_readiness.py
git commit -m "feat: add wait_for_network readiness poll loop (#500)"
```

(Append the `Co-Authored-By` trailer.)

---

## Task 2: `clone()` checks `git init` + `git fetch` return codes

**Files:**
- Modify: `src/kdive/providers/shared/build_host/shell_transport.py:172-201` (the `clone` method)
- Test: `tests/providers/build_host/test_shell_transport.py`

**Interfaces:**
- Consumes: existing `_run_remote`, `redacted_tail`, `CategorizedError`, `ErrorCategory`,
  `_validate_git_arg` — all already imported in the module.
- Produces: no new public symbol; `clone`'s behavior is tightened.

This task is independent of Task 1 (different file/module) and can be done in either order.

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/build_host/test_shell_transport.py`:

```python
def test_clone_init_non_zero_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(returncode=1, stderr="permission denied")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "permission denied" in exc.value.details["stderr"]


def test_clone_fetch_non_zero_is_configuration_error_with_fetch_stderr() -> None:
    # init ok, fetch fails (no checkout result is consumed — the regression for the masked bug).
    t = _RecordingTransport([_ok(), _ok(returncode=128, stderr="Could not resolve host")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert "Could not resolve host" in exc.value.details["stderr"]
    # Only init + fetch ran; checkout was never reached.
    assert [c[0][:2] for c in t.calls] == [["git", "init"], ["git", "-C"]]
```

(`test_clone_checkout_non_zero_is_configuration_error` already exists and stays green: it now
passes `_ok(), _ok(), _ok(returncode=1, ...)` — init ok, fetch ok, checkout fails.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/build_host/test_shell_transport.py -q`
Expected: the two new tests FAIL — `test_clone_init_non_zero...` does not raise (init rc ignored),
`test_clone_fetch_non_zero...` raises a checkout/pathspec error or the wrong call sequence.

- [ ] **Step 3: Implement the rc checks**

Replace the body of `clone` after the two `_validate_git_arg(...)` calls
(`shell_transport.py:187` onward) with:

```python
        init = self._run_remote(["git", "init", dest], cwd="/", timeout_s=_CLONE_TIMEOUT_S)
        if init.returncode != 0:
            raise CategorizedError(
                "git init failed on remote",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"stderr": redacted_tail(init.stderr, self._secret_registry)},
            )
        fetch = self._run_remote(
            ["git", "-C", dest, "fetch", "--depth", "1", remote, ref],
            cwd="/",
            timeout_s=_CLONE_TIMEOUT_S,
        )
        if fetch.returncode != 0:
            raise CategorizedError(
                "git fetch failed on remote",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"stderr": redacted_tail(fetch.stderr, self._secret_registry)},
            )
        result = self._run_remote(
            ["git", "-C", dest, "checkout", "FETCH_HEAD"], cwd="/", timeout_s=_CLONE_TIMEOUT_S
        )
        if result.returncode != 0:
            raise CategorizedError(
                "git checkout FETCH_HEAD failed on remote",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"stderr": redacted_tail(result.stderr, self._secret_registry)},
            )
```

Update the `clone` docstring's `Raises:` block to read: `CONFIGURATION_ERROR` for an unsafe
remote/ref, a failed `git fetch`, or a failed `git checkout FETCH_HEAD`; `INFRASTRUCTURE_FAILURE`
for a failed `git init`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/build_host/test_shell_transport.py -q`
Expected: PASS (all tests, including the pre-existing init→fetch→checkout order test and the
checkout-failure test).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/shared/build_host/shell_transport.py \
        tests/providers/build_host/test_shell_transport.py
git commit -m "fix: surface git init/fetch rc in clone() (#500)"
```

(Append the `Co-Authored-By` trailer.)

---

## Task 3: Default-route probe + gate in `EphemeralBuildVm.session`

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py`
- Test: `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`

**Interfaces:**
- Consumes: `wait_for_network` (Task 1); `redacted_tail` (import from
  `kdive.providers.shared.build_host.workspace`); `CommandResult` (import from
  `kdive.providers.ports.build_transport`); existing `GuestExecBuildTransport`, `BuildVmTiming`.
- Produces: module constants `_DEFAULT_ROUTE_PROBE`, `_NETWORK_PROBE_ARGV`,
  `_NETWORK_PROBE_CALL_TIMEOUT_S`, `_NETWORK_TIMEOUT_S`, `_NETWORK_POLL_S`; `BuildVmTiming` fields
  `network_timeout_s`/`network_poll_s`; private `EphemeralBuildVm._wait_for_network`.

Depends on Task 1 (`wait_for_network` must exist).

- [ ] **Step 1: Write the failing tests**

In `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`, add a controllable agent fake and
two tests. Place near the top, after the existing `_agent_ok`:

```python
def _agent_route_after(polls: int) -> Any:
    """A guest-agent fake whose route probe reports rc!=0 for the first `polls` checks then rc 0.

    The probe is the only guest-exec issued in these tests, so each guest-exec/guest-exec-status
    pair is one probe. Returns rc 1 (no route) until `polls` checks have happened, then rc 0.
    """
    state = {"checks": 0}

    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-exec":
            return json.dumps({"return": {"pid": 1}})
        state["checks"] += 1
        rc = 0 if state["checks"] > polls else 1
        return json.dumps({"return": {"exited": True, "exitcode": rc}})

    return _agent
```

Then the tests (use a fast `BuildVmTiming` so the never-ready case does not need ~120 ticks):

```python
def _build_vm_with_agent(conn: FakeProvisionConn, tmp_path: Any, agent: Any, **timing: Any):
    return EphemeralBuildVm(
        secret_registry=SecretRegistry(),
        connections=remote_libvirt_connections(
            secret_registry=SecretRegistry(),
            config_factory=_config,
            open_connection=lambda _uri: conn,
            secret_backend_factory=RecordingBackend,
            pki_base_dir=tmp_path,
        ),
        agent_command=agent,
        timing=BuildVmTiming(sleep=lambda _s: None, monotonic=_ticker(), **timing),
    )


def test_session_yields_only_after_route_appears(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm_with_agent(conn, tmp_path, _agent_route_after(2))
    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        assert conn.domains[DOMAIN_NAME].active
    assert DOMAIN_NAME not in conn.domains


def test_session_network_never_ready_raises_and_tears_down(tmp_path: Any) -> None:
    conn = _conn_with_base()
    # Route never appears; small network timeout so the fake clock reaches the deadline quickly.
    vm = _build_vm_with_agent(
        conn, tmp_path, _agent_route_after(10_000), network_timeout_s=5.0, network_poll_s=1.0
    )
    with pytest.raises(CategorizedError) as exc, vm.session(_BASE_VOLUME, run_id=RUN_ID):
        pass
    assert exc.value.category == ErrorCategory.PROVISIONING_FAILURE
    # Teardown still ran.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted
```

Add the imports the new tests need at the top of the test file:
`from kdive.domain.errors import CategorizedError, ErrorCategory`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_build_vm.py -q`
Expected: `test_session_yields_only_after_route_appears` may pass vacuously (no gate yet, but the
agent fake answers); `test_session_network_never_ready_raises_and_tears_down` FAILS — no
`PROVISIONING_FAILURE` is raised because the gate does not exist yet. (If the first test also
passes pre-implementation, that is fine; the never-ready test is the gating one.)

- [ ] **Step 3: Implement the constants, timing fields, and gate**

In `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py`:

(a) Add imports near the existing readiness import:

```python
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.remote_libvirt.lifecycle.readiness import (
    Monotonic,
    Sleep,
    wait_for_agent,
    wait_for_network,
)
from kdive.providers.shared.build_host.workspace import redacted_tail
```

(The existing import line is `from ...lifecycle.readiness import Monotonic, Sleep, wait_for_agent` —
extend it with `wait_for_network`; add the `CommandResult` and `redacted_tail` imports.)

(b) Add the constants near `_AGENT_TIMEOUT_S`:

```python
# A default route is installed exactly when the guest's DHCP lease lands, so its presence is the
# precise "network is up" signal. /proc/net/route is kernel truth; cut+grep avoid an iproute2 dep.
_DEFAULT_ROUTE_PROBE = "cut -f2 /proc/net/route | grep -qx 00000000"
_NETWORK_PROBE_ARGV = ["/bin/sh", "-c", _DEFAULT_ROUTE_PROBE]
_NETWORK_PROBE_CALL_TIMEOUT_S = 10
_NETWORK_TIMEOUT_S = 120.0
_NETWORK_POLL_S = 2.0
```

(c) Add two fields to `BuildVmTiming` (after `agent_poll_s`):

```python
    network_timeout_s: float = _NETWORK_TIMEOUT_S
    network_poll_s: float = _NETWORK_POLL_S
```

(d) In `session()`, after constructing `transport` and before `yield transport`, insert:

```python
                self._wait_for_network(transport, domain_name)
```

(e) Add the private method (after `session`, before `_connection`):

```python
    def _wait_for_network(self, transport: GuestExecBuildTransport, domain_name: str) -> None:
        """Block until the build guest has a default route, so the clone sees working network.

        A non-zero probe rc means "no route yet, keep polling"; a raised CategorizedError (the
        agent dropped) propagates. On the deadline, the last probe output is surfaced so a broken
        probe (missing cut/grep) is diagnosable rather than a bare timeout (ADR-0144).
        """
        last: list[CommandResult] = []

        def probe() -> bool:
            result = transport.run(
                _NETWORK_PROBE_ARGV, cwd="/", timeout_s=_NETWORK_PROBE_CALL_TIMEOUT_S
            )
            last.append(result)
            return result.returncode == 0

        def timeout_detail() -> dict[str, object]:
            if not last:
                return {}
            return {
                "probe_stderr": redacted_tail(last[-1].stderr, self._secret_registry),
                "probe_stdout": last[-1].stdout[-200:],
            }

        wait_for_network(
            probe,
            domain_name,
            monotonic=self._timing.monotonic,
            sleep=self._timing.sleep,
            timeout_s=self._timing.network_timeout_s,
            poll_s=self._timing.network_poll_s,
            timeout_detail=timeout_detail,
        )
```

- [ ] **Step 4: Run the build_vm + readiness suites to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_build_vm.py tests/providers/remote_libvirt/lifecycle/test_readiness.py -q`
Expected: PASS. The pre-existing `_agent_ok` tests (always rc 0) still yield immediately because
the first probe returns rc 0.

- [ ] **Step 5: Run the full provider + build-host suites for regressions**

Run: `uv run python -m pytest tests/providers -q`
Expected: PASS. Watch specifically for any test that asserts an exact guest-exec call count on a
full build (the added probe issues one extra guest-exec); if one breaks, the assertion must absorb
the probe call, not the gate be removed.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/lifecycle/build_vm.py \
        tests/providers/remote_libvirt/lifecycle/test_build_vm.py
git commit -m "feat: gate build-VM transport on in-guest network readiness (#500)"
```

(Append the `Co-Authored-By` trailer.)

---

## Final verification (after all tasks)

- [ ] Run the full local gate the way CI runs it (individual recipes):
  `just lint && just type && just test`
- [ ] Run the doc guardrails (ADR/spec/plan already committed):
  `just check-mermaid docs-links docs-paths`
- [ ] Confirm no `live_vm`/`live_stack` gate was widened or un-gated.

## Rollback / cleanup

- Each task is one commit; revert in reverse order if needed. Task 2 (clone) is independent of
  Tasks 1+3 and can be reverted alone.
- No DB migration, no persisted state, no external-service change — rollback is a pure code revert.

## Self-review notes

- **Spec coverage:** AC1 → Task 1 + Task 3 (gate before yield); AC2 → Task 2 (fetch rc + stderr);
  AC3 → Task 3 places the gate in readiness logic, no guest-image change. Finding-A diagnosability
  → Task 1 `timeout_detail` + Task 3 closure (tested in Task 1 step 1 + Task 3). Finding-D budget
  → fields injectable (Task 3c), test uses small `network_timeout_s` (Task 3 step 1).
- **Type consistency:** `wait_for_network` signature is identical across Task 1 (def) and Task 3
  (call); `timeout_detail` returns `dict[str, object]` in both; `CommandResult` is the
  `transport.run` return type.
