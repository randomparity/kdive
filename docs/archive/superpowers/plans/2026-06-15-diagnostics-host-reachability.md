# Plan — Diagnostics host-reachability probe (#453)

- **Spec:** [`docs/specs/2026-06-15-diagnostics-host-reachability.md`](../../specs/2026-06-15-diagnostics-host-reachability.md)
- **ADR:** [`0125`](../../adr/0125-diagnostics-host-reachability.md)
- **Branch:** `feat/diagnostics-reachability-453`
- **No DB migration.**

TDD throughout: write the failing test, confirm it fails for the right reason, write the minimal
implementation, refactor green. Each step ends with `just lint && just type && just test`.

## Step 1 — `failure_category` on `CheckResult` (foundation)

**Test first** (`tests/diagnostics/test_framework.py`):
- A `CheckResult(status=PASS, failure_category="x")` raises `ValueError` (producer-bug invariant).
- A `CheckResult(status=FAIL, ..., fix="f", failure_category="transport_failure")` is accepted and
  carries the category.
- A `CheckResult(status=ERROR, failure_category="configuration_error")` is accepted.
- Existing constructions (no `failure_category`) still default it to `None` — assert one.

**Implement** (`src/kdive/diagnostics/checks.py`):
- Add `failure_category: str | None = None` to the `CheckResult` dataclass (after `provider`).
- Extend `__post_init__`: if `status is PASS and failure_category is not None` → `ValueError`
  ("only a fail/error result may carry a failure_category"). Keep the existing `fix` rules intact.

**Verify:** the new framework tests pass; existing `test_framework.py`/`test_secret_ref.py`/
`test_provider_checks.py` stay green (field is optional/defaulted).

## Step 2 — Reachability outcome + check (pure logic, libvirt-free)

**Test first** (`tests/diagnostics/test_reachability.py`): drive `RemoteLibvirtReachabilityCheck`
with an injected async `probe` returning each `ReachabilityOutcome`:
- `REACHABLE` → `pass`, `failure_category is None`, `provider == "remote-libvirt"`, `fix is None`.
- `UNREACHABLE` → `fail`, `failure_category == "transport_failure"`, `fix` is the bring-host-up
  string (pin it exactly), `provider` set.
- `MISCONFIGURED` → `error`, `failure_category == "configuration_error"`, `fix is None`.
- `check.id == "remote_libvirt_reachability"`, `check.vantage is Vantage.SERVER`.

**Implement** (`src/kdive/diagnostics/checks.py`):
- `REACHABILITY_ID = "remote_libvirt_reachability"`.
- `class ReachabilityOutcome(StrEnum)`: `REACHABLE` / `UNREACHABLE` / `MISCONFIGURED`.
- `ReachabilityProbe = Callable[[], Awaitable[ReachabilityOutcome]]` (no args — the probe closes
  over its config factory; selection is single-instance per the spec).
- `class RemoteLibvirtReachabilityCheck(Check)`: `__init__(*, provider, probe)`; `id`,
  `vantage` (SERVER), and `run()` mapping the three outcomes to the three-state `CheckResult` with
  the `failure_category` from Step 1. The `fail` branch sets the fix string.

Keep this module **libvirt-free** (mirrors how `ProviderTlsCheck` takes an injected `TlsProbe`).

## Step 3 — Production probe adapter (the libvirt boundary)

**Test first** (`tests/diagnostics/test_reachability.py`, boundary-mocked):
- Inject a fake `open_connection` (the `remote_connection` opener seam) + a config factory:
  - opener returns a stub whose `getInfo()` returns a list, `close()` no-ops → `REACHABLE`.
  - opener raises `libvirt.libvirtError` → the `remote_connection` wrapper raises
    `CategorizedError(TRANSPORT_FAILURE)` → adapter returns `UNREACHABLE`.
  - config factory raises `CategorizedError(CONFIGURATION_ERROR)` (zero/>1 instance, bad URI) →
    adapter returns `MISCONFIGURED` (no connection attempt — assert the opener was never called).
  - secret-backend resolve raising `CategorizedError(CONFIGURATION_ERROR)` → `MISCONFIGURED`.
- A `getInfo()` that blocks: assert the probe runs the blocking call via `asyncio.to_thread`
  (cover by a probe-level timeout test or by asserting it is offloaded — the service-level timeout
  is covered in Step 4).

**Implement** (`src/kdive/diagnostics/reachability.py`, new):
- `remote_libvirt_reachability_probe(*, config_factory=remote_config_from_inventory,
  open_connection=open_libvirt, secret_backend_factory=...) -> ReachabilityProbe`. The returned
  async probe:
  1. resolves config (deferred), catching `CategorizedError(CONFIGURATION_ERROR)` → `MISCONFIGURED`.
  2. runs the blocking `with remote_connection(...) as conn: conn.getInfo()` inside
     `asyncio.to_thread` (mirror `SshBuildHostProber._probe_sync` offload).
  3. maps `CategorizedError`: `TRANSPORT_FAILURE` → `UNREACHABLE`; `CONFIGURATION_ERROR` →
     `MISCONFIGURED`. Any other `CategorizedError`/unexpected error → `MISCONFIGURED` (conservative:
     a probe that cannot determine reachability is not a confident "host down"). `run_check` remains
     the outer backstop for a true hang/leak.
- **SecretRegistry source (decided):** the probe builds a **fresh per-probe `SecretRegistry()`**
  (no-arg constructible) and calls `secret_backend_from_env(registry=that_registry)` — short-lived,
  read-only, mirroring `SshBuildHostProber`'s per-probe scope. This **keeps
  `default_service_factory`'s signature unchanged** (no registry threaded through `register()` /
  the `ServiceFactory` Protocol / `_register_diagnostics_tools`), so the diff stays inside the
  Files table. The default `secret_backend_factory` is `lambda: secret_backend_from_env(
  registry=SecretRegistry())`; tests inject a fake backend or a fake `open_connection`.
- Use the existing `remote_config_from_inventory` / `remote_connection` / `open_libvirt` seams,
  injected so tests pass fakes (no real libvirt).

## Step 4 — Wire into `default_service_factory`

**Test first** (`tests/diagnostics/test_default_factory.py`). Split into **assembly** tests (need
no probe control) and **end-to-end run** tests (control the probe by monkeypatching
`reachability.remote_libvirt_reachability_probe` to return a canned async probe — mock the boundary
module, not a test-only factory param; `default_service_factory` keeps its production signature):

Assembly (no probe needed):
- With a `[[remote_libvirt]]` instance declared (reuse the `_write_inventory` shape from
  `tests/providers/remote_libvirt/test_config.py`: `schema_version = 2`, an `[[image]]` block, and
  the `[[remote_libvirt]]` instance; `monkeypatch.setenv("KDIVE_SYSTEMS_TOML", ...)` then
  `config.load()`): `default_service_factory(None)._checks` ids include `secret_ref`,
  `provider_tls`, `gdbstub_acl`, `remote_libvirt_reachability`.
- The assembled service has `worker_available is False`.
- With **no** `[[remote_libvirt]]` instance: ids are `{secret_ref}` only; no exception; no
  reachability/TLS/ACL checks (AC5).

End-to-end run (probe monkeypatched):
- Reachable probe: `provider_tls`/`gdbstub_acl` results are `error` with the worker-unavailable
  detail and `fix=None`; `remote_libvirt_reachability` is `pass`; **`secret_ref` still RUNS**
  (`pass`, not the worker-unavailable error) — pinning that server-vantage checks are not
  substituted under `worker_available=False`.
- With **>1** instance declared: the real (un-monkeypatched) probe path yields
  `remote_libvirt_reachability` = `error` + `configuration_error`, opener never called (AC4) — drive
  this with a fake `open_connection` asserted un-called and the real `remote_config_from_inventory`
  raising on >1 instance.

**Implement** (`src/kdive/diagnostics/service.py`):
- In `default_service_factory`, after `_secret_ref_check()`:
  - `checks = [_secret_ref_check()]`.
  - `if is_remote_libvirt_configured():` append the reachability check (probe built from the
    production seams) **and** the worker-vantage `ProviderTlsCheck`/`GdbstubAclCheck` constructed
    with empty `ca_path`/`host`/`port_range` and a `NotImplementedError`-raising probe (never run
    under `worker_available=False`).
  - Build `DiagnosticsService(checks=checks, per_check_timeout=..., overall_timeout=...,
    worker_available=False)`.
- Keep the `with_egress` fail-fast branch unchanged.
- Update the `default_service_factory` docstring to describe the new assembled set + the
  worker-unavailable substitution for TLS/ACL.

Note: `worker_available=False` is now the default-factory stance because no worker-job dispatch is
wired. Confirm no other caller relies on the previous implicit `True` (search shows
`default_service_factory` is the only production construction; `app.py:180` passes it as the
factory).

## Step 5 — Surface `failure_category` in the MCP projection

**Test first** (`tests/mcp/ops/test_diagnostics.py`): a verdict item for a `fail`/`error` check
carrying a `failure_category` includes it in the `_item` data; a `pass` item has
`failure_category` `None`.

**Implement** (`src/kdive/mcp/tools/ops/diagnostics.py`): add `"failure_category":
result.failure_category` to the `_item()` `data` dict. No `responses.py` change.

**outputSchema watch (#404):** `build_app` sweeps every tool to a flat `{"type":"object"}`
outputSchema, so a new optional string field inside the `ToolResponse.success` data is expected to
be schema-neutral (`detail`/`fix`/`provider` are already string-or-None in this dict). This is an
**expectation to VERIFY, not assume**: after adding the field, run the generated-doc / tool-catalog
check (`test_tool_docs` + the repo docs-check recipe) and confirm green; if a verdict-shape snapshot
exists, update it in the same commit.

## Step 6 — Full guardrails + generated-doc refresh

- `just lint && just type && just test` green.
- Run `test_tool_docs` and the repo's docs-check / generated-docs recipe (recurring rebase zone per
  project memory). `ops.diagnostics` has no new tool param, so the catalog should be untouched; if
  any generated doc/check trips, regenerate via the `just` recipe and commit it in the relevant
  step's commit.
- Run the full superset `just ci` before pushing (boundary/arch tests live outside `diagnostics/`).

## Commit sequence (one logical change each, never squash)

1. `feat(diagnostics): add failure_category to CheckResult` (Step 1).
2. `feat(diagnostics): add remote-libvirt reachability check` (Steps 2-3).
3. `feat(diagnostics): wire reachability + TLS/ACL into default factory` (Step 4).
4. `feat(diagnostics): surface failure_category in ops.diagnostics verdict` (Step 5).

Each commit message ends with the Co-Authored-By trailer.

## Risks / watch-items

- **`worker_available=False` flips the factory default.** Verify the existing `test_service.py` /
  `test_default_factory.py` assertions don't assume worker-vantage checks run; they currently
  assemble only `secret_ref` (server-vantage), so they should be unaffected — confirm. Step 4 adds
  a **positive** test that `secret_ref` (and reachability) still RUN under `worker_available=False`
  (the mirror of AC6), so a future mis-tag of a server-vantage check as `WORKER` is caught.
- **Inventory test seam.** `remote_config_from_inventory` reads `KDIVE_SYSTEMS_TOML`; tests must set
  it via the config registry (`config.load()` after `monkeypatch.setenv`) exactly as
  `test_default_factory.py::_set_env` does for secrets. Reuse that helper shape.
- **Probe injection in the factory test.** The factory builds the probe from production seams; to
  drive the three states in `test_default_factory` without real libvirt, inject the
  `open_connection`/`config_factory` via the same seam the probe adapter takes, or monkeypatch the
  adapter builder. Prefer a parameterized factory seam over deep monkeypatching.
- **Disjointness from #450.** Only `mcp/` touch is the additive `_item` field. Do not touch
  `responses.py`.
