# Spec — Diagnostics host-reachability probe (#453)

- **Issue:** [#453](https://github.com/randomparity/kdive/issues/453) (work item D of epic #449)
- **ADR:** [`0125`](../adr/0125-diagnostics-host-reachability.md)
- **Design:** [`mcp-onboarding-error-ergonomics.md`](../design/mcp-onboarding-error-ergonomics.md) §"Work item D"
- **Date:** 2026-06-15

## Problem

`ops.diagnostics` (ADR-0091) assembles only the server-vantage `secret_ref` check
(`src/kdive/diagnostics/service.py:204-235`). `ProviderTlsCheck`/`GdbstubAclCheck` exist
(`src/kdive/diagnostics/checks.py:260-362`) but are unwired, and there is no `qemu+tls://`
connection probe. So when remote-libvirt provisioning fails, diagnostics cannot tell an operator
whether the host was unreachable or the profile was bad — it reports `secret_ref: 0 refs` and
nothing about the host.

## Decision (per ADR-0125)

Add a **server-vantage** remote-libvirt reachability check that opens `remote_connection()` and
calls `conn.getInfo()` under a bounded per-check timeout (reusing the `SshBuildHostProber`
`asyncio.to_thread` + timeout offload pattern), reporting three-state:

| Outcome | Status | failure_category |
|---|---|---|
| connection opens, `getInfo()` returns | `pass` | — |
| host unreachable / TLS connect fails | `fail` | `transport_failure` |
| bad URI / unresolvable cert refs / malformed inventory | `error` | `configuration_error` |

Wire the existing `ProviderTlsCheck`/`GdbstubAclCheck` into the default factory alongside it (see
"TLS/ACL checks" below for how their worker vantage is honored).

### Failure-category carrier

`CheckResult` (`checks.py:52-71`) currently carries `check_id`/`status`/`detail`/`fix`/`provider`
with no category field, and the MCP `_item()` projection (`diagnostics.py:177-188`) emits exactly
those. To make the decision table's `failure_category` a first-class, test-assertable field (not a
brittle `detail` substring), this slice **adds** `failure_category: str | None` to `CheckResult`
and surfaces it in `_item()`. The value is the lowercased `ErrorCategory` name
(`transport_failure` / `configuration_error`) the reachability check derives from the
`CategorizedError.category` raised by `remote_connection()`.

`__post_init__` invariant: `failure_category` may be set only on `fail`/`error` (a `pass` carrying
a category is a producer bug), mirroring the existing `fix`-only-on-`fail` invariant. Existing
`CheckResult` constructions default it to `None`, so the secret_ref / TLS / ACL / egress results
are unaffected. The field is additive and optional; no call site outside diagnostics constructs a
`CheckResult`, so the blast radius is the diagnostics package + its tests.

### Three-state mapping rationale

The reachability check's `fail`-vs-`error` split is driven by `CategorizedError.category` raised
by `remote_connection()` (`transport.py:146-181`):

- `TRANSPORT_FAILURE` (the `qemu+tls connect ... failed` path) → **`fail`** with a `transport_failure`
  category and a fix string. The contract "the host is reachable" is violated and the remediation
  is actionable ("bring the host up / open the libvirt TLS port").
- `CONFIGURATION_ERROR` (unsafe URI from `validate_remote_uri`, unresolvable cert refs from
  `materialized_pkipath`, malformed/absent inventory from `remote_config_from_inventory`) →
  **`error`** with a `configuration_error` category and no fix. This is a check-cannot-run
  condition: the probe never got far enough to observe reachability, so emitting a "host is down"
  fix would be the confident-wrong-fix failure ADR-0091 forbids. The operator's own config is the
  blocker and is named in `detail` (no secret values; the URI/ref names are operator-owned).

This is the inverse of `secret_ref`'s mapping (there, a per-ref miss is `fail` and a dead backend
is `error`) but the same principle: a violated contract the probe *observed* is `fail`; a probe
that *could not run* is `error`.

### Anti-amplification (ADR-0125)

The probe targets a **single** `[[remote_libvirt]]` instance. Anti-amplification is enforced at two
layers, both pre-existing:

- The **inventory loader** rejects more than one `[[remote_libvirt]]` instance (per-op remote
  selection is not wired), so `is_remote_libvirt_configured()` **degrades to `False`** on a
  multi-instance inventory and the reachability check is **not assembled at all** — one authz'd MCP
  call cannot fan out into N TLS handshakes against remote hosts.
- For a single declared instance, `remote_config_from_inventory()` resolves exactly that one host;
  the probe opens exactly one connection.

No new `host` argument is added in this slice: the existing single-instance resolver is the
selection. This matches the design's "with no argument it probes the default/sole instance".

### Inclusion gate and lazy config resolution (AC4 vs AC5)

The check is added to the assembled set **only when** `is_remote_libvirt_configured()`
(`config.py:100`) is true. That gate **degrades to `False` and never raises** — a missing or
malformed inventory means "not configured", so a deployment with no `[[remote_libvirt]]` instance
gets no reachability check (AC5) and the factory does not throw during assembly. This matters
because `_diagnostics_report_from_service` (`diagnostics.py:119-138`) catches an assembly exception
and collapses the **entire** report to one generic "could not be assembled" error, which would
drop `secret_ref`; calling the raising `remote_config_from_inventory()` at factory time would
trigger exactly that collapse on a zero-instance deployment.

Config resolution is therefore **deferred to `run()`** (inside the probe): the probe holds a
`config_factory` (defaulting to `remote_config_from_inventory`) and calls it when run. A
`CategorizedError(CONFIGURATION_ERROR)` from the factory — a single instance whose URI/cert/gdbstub
range is malformed, or an inventory that became malformed/multi-instance after the gate passed — is
caught and mapped to the check's own `error` + `configuration_error` (AC4); it never collapses the
sibling checks. This resolves the AC4/AC5 tension: **zero (or >1) declared at assembly → no check**
(AC5, since the gate degrades to `False`); **single-but-unresolvable at run time → `error` +
`configuration_error`** (AC4).

### Scope of the claim

A successful connection proves the host is **libvirt-reachable**, not provision-ready. A
reachable-but-misconfigured host (no storage pool / network) reports `pass`; that config failure
surfaces at provision time (now legible via the ADR-0123 `detail`). The check's `detail` states
this boundary so the verdict is not over-read.

### TLS/ACL checks — worker vantage, surfaced via the existing worker-unavailable path

`ProviderTlsCheck`/`GdbstubAclCheck` are `Vantage.WORKER` (they observe the worker→hypervisor TLS
chain and the host ACL, which the server cannot see). They are assembled into the factory so the
report **names** them (AC1), but their production **worker-job probe dispatch** is the separate
egress-probe wave and is **not** wired in #453.

An earlier draft fed them a forced-`UNREACHABLE`/`None` probe so they would map to `error`. That is
rejected: `ProviderTlsCheck`'s `UNREACHABLE` branch emits `detail="provider host unreachable;
cannot validate the TLS chain"` (`checks.py:289-295`) and `GdbstubAclCheck`'s `None` branch emits
`"could not determine the ACL on {host}"` (`checks.py:339-345`) — both **factually wrong** on a
healthy, reachable host (the host is reachable; we simply have no worker-vantage probe). Emitting
"host unreachable" when it is not is the confident-wrong-verdict failure mode the design forbids.

Instead these checks are wired as the worker-vantage checks they are, and the default factory
builds the service with the service-wide **`worker_available=False`** (honest: this slice wires no
worker-job dispatch at all, so no worker-vantage check can actually be run). The existing
`DiagnosticsService` substitution path (`service.py:119-122,158-159` → `worker_unavailable_results`)
then surfaces each as an `error` with the **honest** existing detail `"worker could not pick up the
diagnostic job; check /livez and /readyz"` (`service.py:37`) and `fix=None` — no fabricated probe,
no false claim about the host. The server-vantage `secret_ref` and reachability checks are
unaffected by the flag (`_can_run` only gates `Vantage.WORKER`, `service.py:158-159`). When the
worker-job dispatch wave lands, those checks get a real probe and `worker_available` reflects true
worker health; this slice does not pre-empt that design.

The new **reachability check** is the concrete `Vantage.SERVER` probe that actually closes finding
4 — it runs from the server (which *does* open the libvirt client connection) regardless of worker
health, so it must be `Vantage.SERVER` (a `WORKER` tag would wrongly substitute it away exactly
when an operator needs it). This is pinned with a test (AC6).

Because the TLS/ACL checks are substituted (never `run()`) under `worker_available=False`, only
their `id` and `vantage` are consulted (`service.py:119-122` → `worker_unavailable_results` reads
`check.id`). The factory therefore constructs them **without resolving `RemoteLibvirtConfig`** —
passing empty `ca_path`/`host`/`port_range` and a `probe` that raises `NotImplementedError` if ever
called (a guard that surfaces a future wiring mistake). This is deliberate: resolving
`remote_config_from_inventory()` at factory time would raise on a >1-instance deployment and
collapse the whole report (the same trap the reachability check avoids by deferring resolution to
`run()`). The checks are gated on `is_remote_libvirt_configured()` only (a declared instance
exists); their field values are immaterial because the substitution path never reads them.

This keeps the diff confined to `diagnostics/` (+ a thin `remote_libvirt` probe adapter) and does
not invent a worker-job dispatch subsystem, which is out of scope for #453.

### Orphaned-thread / pkipath residual on a black-holing host

`run_check`'s `asyncio.timeout` cancels the *awaiting coroutine* at the per-check bound, but the
blocking `libvirt.open()` runs in an `asyncio.to_thread` worker Python cannot kill (the same
residual `SshBuildHostProber` lives with). Against a host that black-holes the TLS connect, the
caller correctly sees `error` at the timeout, but the OS thread keeps the handshake alive — and the
materialized pkipath (private key on disk, `transport.py:118-143`) is not deleted until that thread
finally unwinds and the `materialized_pkipath` `finally` runs. The residual is **bounded and
worker-local**: libvirt's client applies its own connect timeout (it does not block forever on an
unroutable host), the pkipath is `0700`/`0600` under worker-local temp storage, and `rmtree` in the
`finally` reclaims it once the thread returns. This slice accepts that bounded residual (the same
tradeoff work item C documents for its pre-mutation segment) rather than adding a thread-kill
mechanism; it does not introduce an *unbounded* leak. The per-check timeout still guarantees the
**report** is never stalled past the bound.

## Files

| Change | File |
|---|---|
| Add `failure_category: str \| None` to `CheckResult` + `__post_init__` invariant | `src/kdive/diagnostics/checks.py` |
| New `RemoteLibvirtReachabilityCheck` + `ReachabilityProbe`/`ReachabilityOutcome` (`Vantage.SERVER`) | `src/kdive/diagnostics/checks.py` |
| New `remote_libvirt_reachability_probe()` (opens `remote_connection` + `getInfo` under offload) | `src/kdive/diagnostics/reachability.py` (new) |
| Gate inclusion on `is_remote_libvirt_configured()`; wire reachability + worker-vantage TLS/ACL via `worker_available=False` substitution | `src/kdive/diagnostics/service.py` |
| Surface `failure_category` in the `_item()` projection | `src/kdive/mcp/tools/ops/diagnostics.py` |
| Unit tests (check logic, probe outcomes, factory assembly, vantage) | `tests/diagnostics/test_reachability.py` (new), `tests/diagnostics/test_default_factory.py` |

No DB migration. The only `mcp/` touch is the additive `_item()` projection of the new optional
field — it does **not** touch `responses.py`, so #453 stays disjoint from concurrent #450.

## Acceptance (each gets a test)

1. **Factory includes TLS/ACL + reachability checks.** With a `[[remote_libvirt]]` instance
   declared, `default_service_factory(None)` assembles a check set whose ids include
   `provider_tls`, `gdbstub_acl`, and `remote_libvirt_reachability`. The TLS/ACL checks, having no
   worker-job probe in this slice, surface as `error` with the honest worker-unavailable detail
   (`worker could not pick up the diagnostic job; check /livez and /readyz`) and `fix=None`, **not**
   a fabricated "host unreachable".
2. **Three-state reachability + failure_category.** With injected libvirt doubles:
   - reachable host (`getInfo()` returns) → `pass`, `failure_category=None`
   - unreachable host (`libvirt.libvirtError` on open → `TRANSPORT_FAILURE`) → `fail` +
     `failure_category="transport_failure"`, with a fix string
   - bad URI / unresolvable cert / bad inventory (→ `CONFIGURATION_ERROR`) → `error` +
     `failure_category="configuration_error"`, `fix=None`
3. **Bound + offload.** A hung `getInfo()`/open is bounded by the per-check timeout → `error` (via
   the service's `run_check` backstop), never a hang. The blocking libvirt call runs under
   `asyncio.to_thread` so the probe never stalls the event loop.
4. **Anti-amplification / run-time config error.** With >1 declared instance the gate degrades to
   `False`, so no reachability check is assembled (no fan-out). With a single declared instance
   whose config is unresolvable at run time (e.g. an inverted gdbstub range), the check reports
   `error` + `configuration_error` and makes **no** connection attempt (config resolved before any
   open).
5. **No reachability check when remote-libvirt is not configured.** `default_service_factory`
   assembles only `secret_ref` (the worker-vantage TLS/ACL checks require a declared instance too)
   when no `[[remote_libvirt]]` instance is declared — no spurious `error` for a provider the
   deployment does not use, and no assembly-time exception (the gate degrades to `False`).
6. **Reachability vantage is SERVER.** `RemoteLibvirtReachabilityCheck.vantage is Vantage.SERVER`,
   and a `DiagnosticsService` built with `worker_available=False` still runs it (it is not
   substituted away when the worker is down).
7. **`failure_category` projection + invariant.** `_item()` emits `failure_category`; constructing
   a `CheckResult` with `status=pass` and a non-`None` `failure_category` raises (producer-bug
   invariant), mirroring the `fix`-only-on-`fail` rule.

## Test strategy

Mock the **boundary** (the libvirt connection: a fake `open_connection` returning a stub with
`getInfo`/`close`, or raising `libvirt.libvirtError`), not the logic. Reachable / unreachable /
bad-cert doubles drive the three states. Inventory resolution is driven by writing a `systems.toml`
under a `tmp_path` (the existing `remote_config_from_inventory` path) or by injecting the config
factory. Honor existing gating: no live suite is un-gated; all new tests run in the default suite.
