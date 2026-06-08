# Per-kind ProviderRuntime registry (#180) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single global `build_default_provider_runtime()` with a static `ResourceKind → ProviderRuntime` registry (`ProviderResolver`); worker job handlers resolve their runtime per-op from the job's System kind, the MCP boundary and discovery are threaded through the resolver, and an unknown kind fails closed — all behavior-preserving for the only registered kind, `local-libvirt`.

**Architecture:** A new `ProviderResolver` holds a `Mapping[ResourceKind, ProviderRuntime]` built per deployment in `providers/composition.py`. Worker handlers keep their `(conn, job, port)` signature but gain a keyword `resolver=`; production passes `resolver=` and the handler resolves the port **lazily, after its existence check** (so gone-target idempotency and error categories are preserved) via `job → system → allocation → resource.kind`. MCP tool registrar **modules are unchanged** — `app.py` resolves the sole `local-libvirt` runtime from the resolver and feeds it to them as today (per-target MCP resolution is deferred to issues 2/4, when a second kind's facets become observable). Discovery fans out over the resolver's composed runtimes. A new `ResourceKind.FAULT_INJECT` enum member and a fail-closed opt-in gate are added now; the migration that widens the DB CHECK and the fault-inject runtime itself land in issue 2 — so a CHECK↔registry parity test holds at this merge with only `local-libvirt`.

**Tech Stack:** Python 3.13, `psycopg`/`psycopg_pool`, `pytest` (+ Docker-gated testcontainers Postgres), `ruff`, `ty`. Run checks via `just lint`, `just type`, `just test`.

**Decisions referenced:** [ADR-0071](../../adr/0071-per-kind-provider-runtime-registry.md), [spec §Provider model / §Decomposition issue 1](../../specs/m1.5-fault-injection-provider.md). These are merged and settled; this plan does **not** reopen them.

**Two design choices confirmed with the maintainer (do not re-litigate):**
1. *Worker-deep, MCP-threaded.* Worker handlers resolve per-op (the real selection seam issues 5/6/7 exercise). MCP tool registrar modules keep their `provider_runtime` param, fed the resolved `local-libvirt` runtime by `app.py`; per-target MCP resolution (debug/introspect/connect) lands in issues 2/4.
2. *Ship the opt-in gate now, fail closed.* `build_provider_resolver(*, enable_fault_inject=False)` exists now; default composition is `{local-libvirt}` only. Enabling it in this PR raises `configuration_error` ("fault-inject not yet registered"); issue 2 replaces the closed branch with the real registration.

---

## File Structure

**Create:**
- `src/kdive/providers/resolver.py` — `ProviderResolver`: the kind→runtime map, fail-closed `resolve()`, `registered_kinds()`, `runtimes()`, `register_all_discovery()`, and async `runtime_for_system()` / `runtime_for_run()` (the `system|run → allocation → resource.kind` joins).
- `tests/providers/test_resolver.py` — unit tests for `resolve()` (hit + fail-closed), `registered_kinds()`, `register_all_discovery()` fan-out.
- `tests/db/test_resource_kind_parity.py` — Docker-gated CHECK↔registry parity test (introspects `resources_kind_check` from a migrated DB) + the `runtime_for_system`/`runtime_for_run` join behavior against real rows.

**Modify:**
- `src/kdive/domain/models.py` — add `ResourceKind.FAULT_INJECT = "fault-inject"`.
- `src/kdive/providers/composition.py` — rename `build_default_provider_runtime` → `build_local_runtime`; add `build_provider_resolver(*, enable_fault_inject=False)`; update `__all__`.
- `src/kdive/mcp/app.py` — thread `ProviderResolver` through both registrar seams; resolve `local-libvirt` for MCP tool registrars; pass the resolver to worker handler registrars; rename the `provider_runtime=` kwargs to `provider_resolver=`.
- `src/kdive/planes/systems.py` — `provision`/`reprovision`/`teardown` handlers: `*, resolver=None`, lazy resolve after existence check; `register_handlers(*, provisioning=None, resolver=None)`.
- `src/kdive/planes/runs.py` — `build`/`install`/`boot` handlers + registrar, same pattern (resolve via run).
- `src/kdive/planes/control.py` — `power`/`force_crash` handlers + registrar, same pattern.
- `src/kdive/planes/vmcore.py` — `capture` handler + registrar, same pattern.
- `src/kdive/__main__.py` — `_run_reconciler`/`_register_provider_resources` use `build_provider_resolver().register_all_discovery(pool)`.
- `src/kdive/admin/bootstrap.py` — discovery registration uses `build_provider_resolver().register_all_discovery(pool)`.
- `tests/providers/test_composition.py`, `tests/providers/test_capture_capabilities.py` — `build_default_provider_runtime` → `build_local_runtime`; add a default-resolver assertion.
- `tests/reconciler/test_main.py` — monkeypatch `build_provider_resolver` returning a fake resolver whose `register_all_discovery` records the `discover` event.

**Untouched on purpose:** every `providers/local_libvirt/*` file (no behavioral diff); the MCP tool registrar modules (`mcp/tools/lifecycle/systems/registrar.py`, `runs/registrar.py`, `lifecycle/vmcore.py`, `debug/sessions.py`, `debug/introspect.py`) and their tests (they still take `provider_runtime`); every handler-invocation test that calls `*_handler(conn, job, port)` positionally; `tests/db/test_migrate.py::test_rerun_is_a_noop` (no new migration — DDL is issue 2).

---

## Task 1: Add the `FAULT_INJECT` resource kind

**Files:**
- Modify: `src/kdive/domain/models.py:53-56`
- Test: `tests/domain/test_models.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_models.py
from kdive.domain.models import ResourceKind


def test_resource_kind_has_local_libvirt_and_fault_inject() -> None:
    assert ResourceKind.LOCAL_LIBVIRT.value == "local-libvirt"
    assert ResourceKind.FAULT_INJECT.value == "fault-inject"
    assert {k.value for k in ResourceKind} == {"local-libvirt", "fault-inject"}
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python -m pytest tests/domain/test_models.py::test_resource_kind_has_local_libvirt_and_fault_inject -q`
Expected: FAIL — `AttributeError: FAULT_INJECT`.

- [ ] **Step 3: Add the enum member**

```python
class ResourceKind(StrEnum):
    """The provider resource kinds; M1.5 adds the fault-injection mock kind.

    ``FAULT_INJECT`` is a forward declaration: its runtime and the
    ``resources_kind_check`` widen that admits it land with the mock provider
    (issue 2). The default production composition does not register it.
    """

    LOCAL_LIBVIRT = "local-libvirt"
    FAULT_INJECT = "fault-inject"
```

- [ ] **Step 4: Run it, expect pass.**

Run: `uv run python -m pytest tests/domain/test_models.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/models.py tests/domain/test_models.py
git commit -m "feat(providers): add fault-inject resource kind"
```

---

## Task 2: `ProviderResolver` — the kind→runtime registry

**Files:**
- Create: `src/kdive/providers/resolver.py`
- Test: `tests/providers/test_resolver.py`

The join queries live here (they are a provider-selection concern). `runtime_for_system`/`runtime_for_run` raise `configuration_error` when no kind row resolves — but handlers only call them **after** confirming the target exists, so in practice the join always finds a granted allocation's `resource_id` (non-null post-grant, ADR-0069).

- [ ] **Step 1: Write the failing unit tests** (no DB — exercise `resolve` and fan-out with fakes)

```python
# tests/providers/test_resolver.py
"""Unit tests for the per-kind ProviderResolver (ADR-0071)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.resolver import ProviderResolver


class _Runtime:
    def __init__(self, label: str) -> None:
        self.label = label
        self.registered: list[object] = []

    async def register_discovery(self, pool: object) -> None:
        self.registered.append(pool)


def _resolver(*kinds: ResourceKind) -> tuple[ProviderResolver, dict[ResourceKind, _Runtime]]:
    runtimes = {k: _Runtime(k.value) for k in kinds}
    return ProviderResolver(cast(dict, runtimes)), runtimes


def test_resolve_returns_the_registered_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.resolve(ResourceKind.LOCAL_LIBVIRT) is runtimes[ResourceKind.LOCAL_LIBVIRT]


def test_resolve_unknown_kind_fails_closed_with_configuration_error() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve(ResourceKind.FAULT_INJECT)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "fault-inject" in str(exc.value)


def test_registered_kinds_reflects_the_map() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})


def test_empty_resolver_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ProviderResolver({})


def test_register_all_discovery_fans_out_over_every_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    pool = cast(AsyncConnectionPool, object())
    asyncio.run(resolver.register_all_discovery(pool))
    assert runtimes[ResourceKind.LOCAL_LIBVIRT].registered == [pool]
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python -m pytest tests/providers/test_resolver.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.resolver`.

- [ ] **Step 3: Implement `ProviderResolver`**

```python
# src/kdive/providers/resolver.py
"""Per-kind provider runtime registry (ADR-0071).

The resolver maps a ``ResourceKind`` to the ``ProviderRuntime`` that serves it.
Post-System worker ops resolve their runtime from the System's Resource kind
(``job -> system -> allocation -> resource.kind``); an unregistered kind fails
closed with ``configuration_error`` rather than falling through to a default.
Concrete runtimes are still constructed only in :mod:`kdive.providers.composition`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.runtime import ProviderRuntime

_KIND_FOR_SYSTEM: Final = (
    "SELECT r.kind AS kind FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE s.id = %s"
)
_KIND_FOR_RUN: Final = (
    "SELECT r.kind AS kind FROM runs rn "
    "JOIN systems s ON s.id = rn.system_id "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE rn.id = %s"
)


class ProviderResolver:
    """A static ``ResourceKind -> ProviderRuntime`` registry.

    Built per deployment by :func:`kdive.providers.composition.build_provider_resolver`.
    Selection is exhaustive and fail-closed: an unregistered kind raises
    ``configuration_error`` at resolution.
    """

    def __init__(self, runtimes: Mapping[ResourceKind, ProviderRuntime]) -> None:
        if not runtimes:
            raise ValueError("ProviderResolver requires at least one registered runtime")
        self._runtimes: dict[ResourceKind, ProviderRuntime] = dict(runtimes)

    def resolve(self, kind: ResourceKind) -> ProviderRuntime:
        """Return the runtime registered for ``kind`` or fail closed."""
        runtime = self._runtimes.get(kind)
        if runtime is None:
            raise CategorizedError(
                f"no provider runtime is registered for resource kind {kind.value!r}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "kind": kind.value,
                    "registered": sorted(k.value for k in self._runtimes),
                },
            )
        return runtime

    def registered_kinds(self) -> frozenset[ResourceKind]:
        return frozenset(self._runtimes)

    def runtimes(self) -> tuple[ProviderRuntime, ...]:
        return tuple(self._runtimes.values())

    async def register_all_discovery(self, pool: AsyncConnectionPool) -> None:
        """Run every composed runtime's discovery registrar (discovery keys on the
        map entry's own kind, not on a Resource that does not yet exist)."""
        for runtime in self._runtimes.values():
            await runtime.register_discovery(pool)

    async def runtime_for_system(self, conn: AsyncConnection, system_id: UUID) -> ProviderRuntime:
        return self.resolve(await self._kind(conn, _KIND_FOR_SYSTEM, system_id, "system"))

    async def runtime_for_run(self, conn: AsyncConnection, run_id: UUID) -> ProviderRuntime:
        return self.resolve(await self._kind(conn, _KIND_FOR_RUN, run_id, "run"))

    async def _kind(
        self, conn: AsyncConnection, sql: str, object_id: UUID, object_kind: str
    ) -> ResourceKind:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (object_id,))
            row = await cur.fetchone()
        if row is None:
            raise CategorizedError(
                f"cannot resolve a provider runtime: no resource kind for {object_kind}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={object_kind: str(object_id)},
            )
        return ResourceKind(row["kind"])
```

- [ ] **Step 4: Run it, expect pass.**

Run: `uv run python -m pytest tests/providers/test_resolver.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/resolver.py tests/providers/test_resolver.py
git commit -m "feat(providers): add per-kind ProviderResolver registry"
```

---

## Task 3: `build_provider_resolver` + rename `build_local_runtime` + fail-closed gate

**Files:**
- Modify: `src/kdive/providers/composition.py:66-113`
- Modify: `tests/providers/test_composition.py:150,164` and `tests/providers/test_capture_capabilities.py:6,11`

- [ ] **Step 1: Update the composition tests (rename + new resolver assertions) — they should now fail**

In `tests/providers/test_capture_capabilities.py` replace the import and call:

```python
from kdive.providers.composition import build_local_runtime


def test_local_libvirt_supports_three_methods_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    assert build_local_runtime().supported_capture_methods == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
```

In `tests/providers/test_composition.py`, replace the two `composition.build_default_provider_runtime()` calls (lines 150, 164) with `composition.build_local_runtime()`, and append:

```python
def test_default_resolver_registers_only_local_libvirt() -> None:
    from kdive.domain.models import ResourceKind

    resolver = composition.build_provider_resolver()
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})
    assert resolver.resolve(ResourceKind.LOCAL_LIBVIRT).component_sources.provider == "local-libvirt"


def test_enabling_fault_inject_before_it_exists_fails_closed() -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    with pytest.raises(CategorizedError) as exc:
        composition.build_provider_resolver(enable_fault_inject=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

Add `import pytest` to the test module's imports.

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python -m pytest tests/providers/test_composition.py tests/providers/test_capture_capabilities.py -q`
Expected: FAIL — `AttributeError: build_local_runtime` / `build_provider_resolver`.

- [ ] **Step 3: Rename + add the resolver builder and gate**

In `src/kdive/providers/composition.py`: rename the function and add the resolver builder. Add imports at the top:

```python
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.resolver import ProviderResolver
```

Rename `build_default_provider_runtime` to `build_local_runtime` (body unchanged) and update its docstring to "Build the typed local-libvirt provider ports …". Then add:

```python
def build_provider_resolver(*, enable_fault_inject: bool = False) -> ProviderResolver:
    """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry.

    The default production composition registers only ``local-libvirt``. The
    ``fault-inject`` provider is opt-in (ADR-0071) and its runtime lands in M1.5
    issue 2; enabling the gate before then is a configuration error, never a
    silent no-op.
    """
    runtimes = {ResourceKind.LOCAL_LIBVIRT: build_local_runtime()}
    if enable_fault_inject:
        raise CategorizedError(
            "fault-inject provider is not yet registered (M1.5 issue 2); "
            "do not enable the gate before its runtime exists",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"enable_fault_inject": True},
        )
    return ProviderResolver(runtimes)
```

Update `__all__` to `["build_local_runtime", "build_provider_resolver", "ensure_local_host_registered"]`.

- [ ] **Step 4: Run it, expect pass.**

Run: `uv run python -m pytest tests/providers/test_composition.py tests/providers/test_capture_capabilities.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/composition.py tests/providers/test_composition.py tests/providers/test_capture_capabilities.py
git commit -m "feat(providers): build a per-deployment ProviderResolver with opt-in gate"
```

---

## Task 4: Per-op resolution in the worker handlers

Apply the **same pattern** to all four plane modules: the handler keeps its positional `port` parameter (defaulting to `None`) and gains a keyword `resolver: ProviderResolver | None = None`; after the existing existence check, resolve the port lazily if it was not injected; `register_handlers` swaps `provider_runtime=` for `resolver=` and passes it through.

> Positional handler-invocation tests (`provision_handler(conn, job, prov)`) are unaffected — `prov` binds the positional `port`, `resolver` stays `None`. Production passes `resolver=`.

### 4a. `systems.py`

**Files:** Modify `src/kdive/planes/systems.py` (handlers + `register_handlers`).

- [ ] **Step 1:** In `provision_handler`, change the signature and resolve after the `if system is None` guard:

```python
async def provision_handler(
    conn: AsyncConnection,
    job: Job,
    provisioning: Provisioner | None = None,
    *,
    resolver: ProviderResolver | None = None,
) -> str | None:
    """Define+start the tagged domain and drive the System ``provisioning -> ready``."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "provision target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    provisioning = await _provisioner(conn, system_id, provisioning, resolver)
    ...  # rest unchanged
```

Apply the same to `reprovision_handler` (resolve after its `if system is None` guard) and `teardown_handler` (resolve inside the txn, after `if system is None: return None`, before `provisioning.teardown(...)`). Add the helper:

```python
async def _provisioner(
    conn: AsyncConnection,
    system_id: UUID,
    explicit: Provisioner | None,
    resolver: ProviderResolver | None,
) -> Provisioner:
    if explicit is not None:
        return explicit
    if resolver is None:
        raise RuntimeError("provision handlers require an explicit provisioner or a resolver")
    return (await resolver.runtime_for_system(conn, system_id)).provisioner
```

Add `from kdive.providers.resolver import ProviderResolver` to the imports.

- [ ] **Step 2:** Rewrite `register_handlers`:

```python
def register_handlers(
    registry: HandlerRegistry,
    *,
    provisioning: Provisioner | None = None,
    resolver: ProviderResolver | None = None,
) -> None:
    """Bind the `provision`/`teardown`/`reprovision` job handlers."""
    if provisioning is None and resolver is None:
        raise RuntimeError("systems handlers require a resolver or an explicit provisioner")
    registry.register(
        JobKind.PROVISION,
        lambda conn, job: provision_handler(conn, job, provisioning, resolver=resolver),
    )
    registry.register(
        JobKind.TEARDOWN,
        lambda conn, job: teardown_handler(conn, job, provisioning, resolver=resolver),
    )
    registry.register(
        JobKind.REPROVISION,
        lambda conn, job: reprovision_handler(conn, job, provisioning, resolver=resolver),
    )
```

- [ ] **Step 3:** Run the systems handler tests, expect PASS (positional injection unchanged):

Run: `uv run python -m pytest tests/adversarial/test_provider_state_races.py -q` → PASS (Docker-gated; skips cleanly if no Docker).

- [ ] **Step 4: Commit**

```bash
git add src/kdive/planes/systems.py
git commit -m "feat(providers): resolve systems handler provider per System kind"
```

### 4b. `runs.py`

**Files:** Modify `src/kdive/planes/runs.py`.

- [ ] **Step 1:** `build_handler(conn, job, builder=None, *, resolver=None)`. Resolve after `if run is None: raise`:

```python
    builder = builder if builder is not None else (
        await _run_runtime(conn, run_id, resolver)
    ).builder
```

`install_handler(conn, job, installer=None, *, resolver=None)`: resolve after the `system` existence check using `run.system_id` (a `runtime_for_system`). `boot_handler(conn, job, booter=None, *, resolver=None)`: resolve after `if run is None: raise` via `runtime_for_run`. Add:

```python
async def _run_runtime(
    conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver | None
) -> ProviderRuntime:
    if resolver is None:
        raise RuntimeError("runs handlers require a resolver or explicit run ports")
    return await resolver.runtime_for_run(conn, run_id)
```

For `install_handler`, resolve via the already-loaded system:

```python
    if installer is None:
        if resolver is None:
            raise RuntimeError("runs handlers require a resolver or explicit run ports")
        installer = (await resolver.runtime_for_system(conn, run.system_id)).installer
```

Add `from kdive.providers.resolver import ProviderResolver` to imports (alongside the existing `ProviderRuntime` import).

- [ ] **Step 2:** Rewrite `register_handlers`:

```python
def register_handlers(
    registry: HandlerRegistry,
    *,
    builder: Builder | None = None,
    installer: Installer | None = None,
    booter: Booter | None = None,
    resolver: ProviderResolver | None = None,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    if builder is None and installer is None and booter is None and resolver is None:
        raise RuntimeError("runs handlers require a resolver or explicit run ports")
    registry.register(
        JobKind.BUILD, lambda conn, job: build_handler(conn, job, builder, resolver=resolver)
    )
    registry.register(
        JobKind.INSTALL, lambda conn, job: install_handler(conn, job, installer, resolver=resolver)
    )
    registry.register(
        JobKind.BOOT, lambda conn, job: boot_handler(conn, job, booter, resolver=resolver)
    )
```

- [ ] **Step 3:** Run: `uv run python -m pytest tests/integration/test_walking_skeleton.py -q` → PASS (Docker-gated). The `build_handler(conn, job, builder)` and `capture_handler(...)` positional calls there are unaffected.

- [ ] **Step 4: Commit**

```bash
git add src/kdive/planes/runs.py
git commit -m "feat(providers): resolve runs handler ports per Run/System kind"
```

### 4c. `control.py`

**Files:** Modify `src/kdive/planes/control.py`.

- [ ] **Step 1:** `power_handler(conn, job, control=None, *, resolver=None)`: resolve after `target = await _control_target(...)` (which raises if gone), before `control.power(...)`. `force_crash_handler(conn, job, control=None, *, resolver=None)`: resolve after `target = await _force_crash_target(...)` returns non-`None`, before `control.force_crash(...)`:

```python
    control = control if control is not None else await _controller(conn, system_id, resolver)
```

with:

```python
async def _controller(
    conn: AsyncConnection, system_id: UUID, resolver: ProviderResolver | None
) -> Controller:
    if resolver is None:
        raise RuntimeError("control handlers require a resolver or an explicit controller")
    return (await resolver.runtime_for_system(conn, system_id)).controller
```

Add the `ProviderResolver` import.

- [ ] **Step 2:** `register_handlers(*, control=None, resolver=None)` with the same guard + pass-through pattern as 4a, binding `POWER`/`FORCE_CRASH`.

- [ ] **Step 3:** Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -q` → PASS.

- [ ] **Step 4: Commit**

```bash
git add src/kdive/planes/control.py
git commit -m "feat(providers): resolve control handler provider per System kind"
```

### 4d. `vmcore.py`

**Files:** Modify `src/kdive/planes/vmcore.py`.

- [ ] **Step 1:** `capture_handler(conn, job, retriever=None, *, resolver=None)`. `precheck_system` returns a `str` (existing same-method key → early return, port unused) or the `System`. Resolve only on the `System` branch, before `retriever.capture(...)`:

```python
    precheck = await precheck_system(conn, system_id, method)
    if isinstance(precheck, str):
        return precheck
    if retriever is None:
        if resolver is None:
            raise RuntimeError("vmcore handlers require a resolver or an explicit retriever")
        retriever = (await resolver.runtime_for_system(conn, system_id)).retriever
    output = await asyncio.to_thread(retriever.capture, system_id, method)
    return await finalize_capture(conn, job, precheck, method, output)
```

Add the `ProviderResolver` import.

- [ ] **Step 2:** `register_handlers(*, retriever=None, resolver=None)` binding `CAPTURE_VMCORE`, same guard + pass-through.

- [ ] **Step 3:** Run: `uv run python -m pytest tests/mcp/lifecycle/test_vmcore_tools.py tests/integration/test_walking_skeleton.py -q` → PASS.

- [ ] **Step 4: Commit**

```bash
git add src/kdive/planes/vmcore.py
git commit -m "feat(providers): resolve vmcore handler retriever per System kind"
```

---

## Task 5: Thread the resolver through `app.py`, reconciler, and bootstrap

**Files:**
- Modify: `src/kdive/mcp/app.py:48-152`
- Modify: `src/kdive/__main__.py:105-140`
- Modify: `src/kdive/admin/bootstrap.py:143-146`
- Modify: `tests/reconciler/test_main.py:46`

- [ ] **Step 1: Update the reconciler test (it should fail after wiring changes)**

In `tests/reconciler/test_main.py`, replace the runtime monkeypatch:

```python
    class _FakeResolver:
        async def register_all_discovery(self, pool: object) -> None:
            events.append("discover")

    monkeypatch.setattr(composition, "build_provider_resolver", lambda **kw: _FakeResolver())
```

(`__main__` imports `build_provider_resolver` from `composition` inside `_run_reconciler`, so patching the attribute on `composition` is sufficient — keep the existing `import ... as composition` reference.)

- [ ] **Step 2: Rewrite `app.py` wiring**

Replace the import and types:

```python
from kdive.domain.models import ResourceKind
from kdive.providers.composition import build_provider_resolver
from kdive.providers.resolver import ProviderResolver
...
type PlaneRegistrar = Callable[
    [FastMCP, AsyncConnectionPool, ProviderResolver, SecretRegistry], None
]
type HandlerRegistrar = Callable[[HandlerRegistry, ProviderResolver], None]
```

`_plain` takes `ProviderResolver` in its ignored slot. The provider-aware **tool** lambdas resolve the local runtime (MCP registrar modules are unchanged):

```python
    lambda app, pool, resolver, registry: systems_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    ...
    lambda app, pool, resolver, registry: runs_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda app, pool, resolver, registry: vmcore_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda app, pool, resolver, registry: debug_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT),
        secret_registry=registry,
    ),
    lambda app, pool, resolver, registry: introspect.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
```

The **handler** registrars pass the resolver through (per-op resolution):

```python
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    lambda registry, resolver: systems.register_handlers(registry, resolver=resolver),
    lambda registry, resolver: runs.register_handlers(registry, resolver=resolver),
    lambda registry, resolver: control.register_handlers(registry, resolver=resolver),
    lambda registry, resolver: vmcore.register_handlers(registry, resolver=resolver),
)
```

`build_app` / `build_handler_registry` rename the kwarg:

```python
def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_resolver: ProviderResolver | None = None,
    secret_registry: SecretRegistry | None = None,
) -> FastMCP:
    ...
    resolver = provider_resolver or build_provider_resolver()
    registry = PROCESS_SECRET_REGISTRY if secret_registry is None else secret_registry
    for register in _PLANE_REGISTRARS:
        register(app, pool, resolver, registry)
    return app


def build_handler_registry(*, provider_resolver: ProviderResolver | None = None) -> HandlerRegistry:
    registry = HandlerRegistry()
    resolver = provider_resolver or build_provider_resolver()
    for register in _HANDLER_REGISTRARS:
        register(registry, resolver)
    return registry
```

Update the two docstrings to say "provider resolver" instead of "provider dispatch runtime".

- [ ] **Step 3: Rewrite `__main__` reconciler discovery**

In `_run_reconciler`, replace the import and call:

```python
    from kdive.providers.composition import build_provider_resolver
    ...
    await _register_provider_resources(pool, build_provider_resolver())
```

and change `_register_provider_resources` to take a `ProviderResolver` and call `register_all_discovery`:

```python
async def _register_provider_resources(
    pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    """Best-effort provider discovery registration so allocations.request has a Resource."""
    try:
        await resolver.register_all_discovery(pool)
    except Exception:  # noqa: BLE001 - registration failure must not crash the reconciler
        _log.warning("reconciler: provider discovery registration failed at startup", exc_info=True)
```

Update the `TYPE_CHECKING` import: `from kdive.providers.resolver import ProviderResolver` (drop the now-unused `ProviderRuntime` import if nothing else uses it).

- [ ] **Step 4: Rewrite `bootstrap.py` discovery**

```python
    from kdive.providers.composition import build_provider_resolver

    await build_provider_resolver().register_all_discovery(pool)
```

- [ ] **Step 5: Run the affected suites, expect pass**

Run: `uv run python -m pytest tests/mcp/core/test_app.py tests/reconciler/test_main.py tests/mcp/debug/test_introspect_tools.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/app.py src/kdive/__main__.py src/kdive/admin/bootstrap.py tests/reconciler/test_main.py
git commit -m "feat(providers): thread the ProviderResolver through app, reconciler, bootstrap"
```

---

## Task 6: CHECK↔registry parity test

Assert every kind the live `resources_kind_check` admits has a registered, reachable runtime, and that the resolver's join helpers map real rows to the right runtime. Docker-gated (uses the `pg_conn` fixture in `tests/db/`).

**Files:** Create `tests/db/test_resource_kind_parity.py`.

- [ ] **Step 1: Write the test**

```python
# tests/db/test_resource_kind_parity.py
"""CHECK<->registry parity: every resources_kind_check kind has a runtime (ADR-0071)."""

from __future__ import annotations

import re

import psycopg

from kdive.db import migrate
from kdive.domain.models import ResourceKind
from kdive.providers.composition import build_provider_resolver


def _check_allowed_kinds(conn: psycopg.Connection) -> set[str]:
    """Read the kinds admitted by the live resources_kind_check constraint."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'resources_kind_check'"
    ).fetchone()
    assert row is not None, "resources_kind_check constraint is missing"
    return set(re.findall(r"'([^']+)'", row[0]))


def test_every_check_allowed_kind_has_a_registered_runtime(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)
    assert allowed == {"local-libvirt"}  # the CHECK widen to fault-inject lands in issue 2
    resolver = build_provider_resolver()
    buildable = {k.value for k in resolver.registered_kinds()}
    # Every kind the DB will admit must resolve to a runtime (no admit-then-throw drift).
    assert allowed <= buildable
    for kind in allowed:
        assert resolver.resolve(ResourceKind(kind)) is not None


def test_every_registered_kind_is_check_allowed(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)
    resolver = build_provider_resolver()
    # No runtime for a kind the DB forbids (discovery insert would fail otherwise).
    for kind in resolver.registered_kinds():
        assert kind.value in allowed
```

- [ ] **Step 2: Run it, expect pass (with Docker) or clean skip (without)**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_resource_kind_parity.py -q` → PASS (needs Docker; CI sets this flag).
Without Docker: `uv run python -m pytest tests/db/test_resource_kind_parity.py -q` → SKIPPED.

- [ ] **Step 3: Commit**

```bash
git add tests/db/test_resource_kind_parity.py
git commit -m "test(providers): assert CHECK<->registry parity for resource kinds"
```

---

## Task 7: Full guardrails + final sweep

- [ ] **Step 1: Format + lint + type**

Run: `just lint && just type`
Expected: clean. Fix every warning (zero-warnings policy). Common items: an unused `ProviderRuntime` import left in `__main__.py`/a plane after the rename; `ResourceKind` import ordering (ruff will autofix with `just format`).

- [ ] **Step 2: Full test suite**

Run: `just test`
Expected: green (Docker-gated db/integration tests run when Docker is present; otherwise skip cleanly — they must not fail).

- [ ] **Step 3: Confirm no behavioral diff under the provider**

Run: `git diff --stat main..HEAD -- src/kdive/providers/local_libvirt/` → **empty** (acceptance criterion: no behavioral diff under `providers/local_libvirt/*`).

- [ ] **Step 4: Confirm no new migration**

Run: `git diff --name-only main..HEAD -- src/kdive/db/schema/` → **empty** (No DDL — the CHECK widen is issue 2).

- [ ] **Step 5: Commit any lint/type fixups**

```bash
git add -A
git commit -m "chore(providers): satisfy lint/type after resolver threading"
```

---

## Self-Review

**Spec coverage (issue #180 acceptance):**
- `ResourceKind.FAULT_INJECT` enum member → Task 1. ✓
- Composition map per deployment + opt-in gate (fault-inject absent from default prod) → Task 3. ✓
- `kind`-keyed resolver threaded to worker handlers (per-op) + post-System MCP boundary (via `app.py` resolving the local runtime; per-target MCP resolution deferred to issues 2/4 per the confirmed decision) → Tasks 4, 5. ✓
- Resolution scoped to post-System ops; pre-grant allocation plane and discovery do not resolve a runtime (discovery fans out over the map's own kinds via `register_all_discovery`; `allocations.*` untouched) → Tasks 2, 5. ✓
- Unknown kind → `configuration_error`, fail closed → Task 2 (`resolve`). ✓
- CHECK↔registry parity test over local-libvirt only → Task 6. ✓
- No DDL → verified in Task 7 Step 4. ✓
- local-libvirt behavior unchanged; only wiring changes → handler signatures preserve positional port injection; MCP registrar modules untouched; verified in Task 7 Step 3. ✓

**Placeholder scan:** none — every code step shows the code.

**Type/name consistency:** `ProviderResolver`, `build_provider_resolver`, `build_local_runtime`, `register_all_discovery`, `runtime_for_system`, `runtime_for_run`, `resolve`, `registered_kinds`, `enable_fault_inject`, kwarg `resolver=` (handlers) / `provider_resolver=` (`build_app`/`build_handler_registry`) used consistently across tasks.

**Risk note:** the only behavior-sensitive change is moving port acquisition inside the handlers. Each resolution is placed *after* the existing existence/precheck guard and *before* the first provider call, so gone-target idempotency (`teardown` returns `None`) and the "target is gone" `infrastructure_failure` category are preserved unchanged.
