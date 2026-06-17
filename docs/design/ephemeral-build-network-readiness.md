# Ephemeral build-VM network readiness + `git fetch` rc surfacing (#500)

- **Issue:** [#500](https://github.com/randomparity/kdive/issues/500)
- **ADR:** [ADR-0144](../adr/0144-ephemeral-build-network-readiness.md)
- **Status:** design
- **Date:** 2026-06-17

This spec is the design for the decisions formalized in ADR-0144. It does not re-open the
choices settled in that ADR's "Considered & rejected" section.

## Problem

A git-lane build on an `ephemeral_libvirt` build host fails its clone with a misleading error.
Two compounding defects (full detail in ADR-0144 "Context"):

1. **Readiness gap.** `EphemeralBuildVm.session` (`src/kdive/providers/remote_libvirt/lifecycle/
   build_vm.py:202`) yields the build transport as soon as `wait_for_agent` returns. The agent
   connects ~boot+2s (device-activated off virtio-serial), *before* DHCP completes, but the
   first caller operation — the git clone — needs network egress, so it fails.

2. **Masked cause.** `ShellBuildTransport.clone()` (`src/kdive/providers/shared/build_host/
   shell_transport.py:172`) checks only `git checkout FETCH_HEAD`'s return code; a failed
   `git fetch` (no network) surfaces as a confusing "FETCH_HEAD pathspec" checkout error.

## Acceptance criteria (from the issue)

1. After `wait_for_agent`, the build-VM session confirms in-guest network readiness with a
   bounded deadline **before** the clone runs.
2. `clone()` checks `git fetch`'s return code and surfaces its stderr, so a network failure is
   not misreported as a checkout/pathspec error.
3. The fix lives in the build-VM readiness logic, not the guest image (the operator note: gating
   the agent service on `network-online.target` flaps the device-activated agent).

## Design

### A. `wait_for_network` poll loop (`lifecycle/readiness.py`)

A new function alongside `wait_for_agent`, sharing its `Monotonic`/`Sleep` seams:

```python
type NetworkProbe = Callable[[], bool]

def wait_for_network(
    probe: NetworkProbe,
    domain_name: str,
    *,
    monotonic: Monotonic,
    sleep: Sleep,
    timeout_s: float,
    poll_s: float,
) -> None:
    """Poll an in-guest network-readiness probe until it succeeds or the deadline passes."""
```

Behavior:

- Compute `deadline = monotonic() + timeout_s`. Loop: if `probe()` is `True`, return; if
  `monotonic() >= deadline`, raise `PROVISIONING_FAILURE` ("guest network did not come up within
  Ns", `details={"domain": domain_name, "timeout_s": timeout_s}`); else `sleep(poll_s)`.
- The probe owns the "not ready" vs "fatal" distinction: a `False` return means keep polling; a
  raised `CategorizedError` (agent unreachable mid-probe) propagates. This mirrors
  `wait_for_agent`, which propagates a `libvirtError`. Rationale: `wait_for_agent` already
  confirmed the channel connected, so a drop during the probe is a real `transport_failure`.
- The deadline check uses `>=` and runs **before** the first `sleep`, matching `wait_for_agent`,
  so a `timeout_s` that has already elapsed raises rather than sleeping.

### B. In-guest default-route probe + gate (`lifecycle/build_vm.py`)

Module constants:

```python
# A default route is installed exactly when the guest's DHCP lease lands, so its presence is the
# precise "network is up" signal. /proc/net/route is kernel truth; cut+grep avoid an iproute2 dep.
_DEFAULT_ROUTE_PROBE = "cut -f2 /proc/net/route | grep -qx 00000000"
_NETWORK_PROBE_ARGV = ["/bin/sh", "-c", _DEFAULT_ROUTE_PROBE]
_NETWORK_PROBE_CALL_TIMEOUT_S = 10
_NETWORK_TIMEOUT_S = 120.0
_NETWORK_POLL_S = 2.0
```

`/proc/net/route` is tab-separated; column 2 (`Destination`) is `00000000` for the default
route. `cut -f2` (default tab delimiter) emits each route's destination; `grep -qx 00000000`
exits 0 iff a line is exactly `00000000`. The header line's field 2 is `Destination`, never
matched.

`BuildVmTiming` gains two fields with the constants above as defaults:

```python
network_timeout_s: float = _NETWORK_TIMEOUT_S
network_poll_s: float = _NETWORK_POLL_S
```

`session()` inserts the gate after `wait_for_agent` and after constructing the transport, before
`yield`:

```python
wait_for_agent(...)                       # unchanged
transport = GuestExecBuildTransport(...)  # unchanged
self._wait_for_network(transport, domain_name)
yield transport
```

`_wait_for_network` builds the probe closure over the transport and delegates to
`wait_for_network`:

```python
def _wait_for_network(self, transport: GuestExecBuildTransport, domain_name: str) -> None:
    def probe() -> bool:
        result = transport.run(
            _NETWORK_PROBE_ARGV, cwd="/", timeout_s=_NETWORK_PROBE_CALL_TIMEOUT_S
        )
        return result.returncode == 0

    wait_for_network(
        probe,
        domain_name,
        monotonic=self._timing.monotonic,
        sleep=self._timing.sleep,
        timeout_s=self._timing.network_timeout_s,
        poll_s=self._timing.network_poll_s,
    )
```

`transport.run` composes the argv as one `cd / && exec /bin/sh -c '<probe>'` guest-agent hop
(the transport's existing `_run_remote` form), so `argv[0]` is the allowlisted `/bin/sh` — no
allowlist change. The gate is inside the `try:`/`finally:` that owns teardown, so a probe that
times out (or an agent that drops) still tears the domain + overlay down.

### C. `clone()` checks init + fetch return codes (`shell_transport.py`)

The current body issues init/fetch without checking their rc and only guards checkout. Replace
with:

```python
init = self._run_remote(["git", "init", dest], cwd="/", timeout_s=_CLONE_TIMEOUT_S)
if init.returncode != 0:
    raise CategorizedError(
        "git init failed on remote",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"stderr": redacted_tail(init.stderr, self._secret_registry)},
    )
fetch = self._run_remote(
    ["git", "-C", dest, "fetch", "--depth", "1", remote, ref], cwd="/", timeout_s=_CLONE_TIMEOUT_S
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

Categories: init failure → `INFRASTRUCTURE_FAILURE` (environment/filesystem); fetch failure →
`CONFIGURATION_ERROR` (bad remote/ref once the readiness gate guarantees network); checkout
unchanged. See ADR-0144 "Considered & rejected" for why fetch is not network-categorized.

## Edge / failure cases

| Condition | Behavior |
|---|---|
| Network up by the time the agent connects | First probe returns rc 0; gate returns immediately; clone runs (the common case). |
| Network slow (DHCP not done) | Probe returns rc≠0; poll every `network_poll_s` until the route appears, then clone. |
| Network never comes up within `network_timeout_s` | `wait_for_network` raises `PROVISIONING_FAILURE`; `finally:` tears down the build VM. |
| Agent drops during the probe | `transport.run` raises `TRANSPORT_FAILURE`; it propagates (real failure, not "not ready"); `finally:` tears down. |
| `git init` fails (perms/disk) | `clone()` raises `INFRASTRUCTURE_FAILURE` with init stderr (was previously masked). |
| `git fetch` fails (bad remote/ref, or rare residual network) | `clone()` raises `CONFIGURATION_ERROR` with the fetch's stderr (was previously masked as a checkout/pathspec error). |
| Remote URL carries a credential | `redacted_tail(stderr, secret_registry)` redacts it before it reaches the error detail. |

## Out of scope

- The SSH build host lane's host-network provisioning (its network is already up; it shares only
  `clone()` and benefits from the fetch-rc surfacing).
- Retry/backoff on the build operation itself (the readiness gate is the chosen mechanism;
  ADR-0144 rejected retry as the primary fix).
- Any guest-image change (the operator note rules out gating the agent on `network-online`).

## Test plan (behavior, at the boundary)

- **`wait_for_network`** (`tests/providers/remote_libvirt/lifecycle/test_readiness.py`):
  returns when `probe` is `True` on the first call; polls N times then returns when the probe
  flips `True`; raises `PROVISIONING_FAILURE` when the probe stays `False` past the deadline;
  propagates a `CategorizedError` raised by the probe (does not swallow it as "not ready").
  Drive with a fake clock (`_ticker`) and a stub probe whose return sequence is controlled.
- **build-VM gate** (`tests/providers/remote_libvirt/lifecycle/test_build_vm.py`): with an agent
  fake whose route-probe `guest-exec-status` returns rc≠0 for the first K polls then rc 0, the
  session yields the transport only after the route appears (assert the probe argv was issued and
  the transport is yielded); with a probe that never returns rc 0, `session()` raises
  `PROVISIONING_FAILURE` **and** the domain/overlay are still torn down (assert via the existing
  `FakeProvisionConn`). The existing `_agent_ok` fake (always rc 0) keeps the current
  yield-immediately tests green.
- **`clone()`** (`tests/providers/build_host/test_shell_transport.py`): `git init` non-zero →
  `INFRASTRUCTURE_FAILURE`; `git fetch` non-zero → `CONFIGURATION_ERROR` with the fetch stderr in
  `details` (the regression test for the masked-cause bug); checkout non-zero still →
  `CONFIGURATION_ERROR`; the happy path still issues init→fetch→checkout in order.
