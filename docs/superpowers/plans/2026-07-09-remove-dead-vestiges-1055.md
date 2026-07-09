# Remove dead profile-requirements + BUILD_HOST vestiges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete two dead code seams left inert after the ADR-0316 server-build-lane removal — the unread profile config-gating apparatus and the doubly-dead `BUILD_HOST` inventory/lock family — with no behavior change to any live path.

**Architecture:** Pure removal. Part 1 deletes a module + three models + a fixture field, coupled to fixture YAML via pydantic `extra="forbid"`. Part 2 narrows an enum, drops a lock scope and a tool parameter, and regenerates the agent-facing reference doc. Each task ends green under the full guardrail suite.

**Tech Stack:** Python 3.14, `uv`, pydantic v2, `ruff`, `ty`, `pytest`, `just` recipes. Postgres via testcontainers (`migrated_url` fixture).

**Spec:** [`../specs/2026-07-09-remove-dead-vestiges-1055.md`](../specs/2026-07-09-remove-dead-vestiges-1055.md)
**ADR:** [`../../adr/0319-remove-dead-profile-requirements-buildhost-vestiges.md`](../../adr/0319-remove-dead-profile-requirements-buildhost-vestiges.md)

## Global Constraints

- Branch: `refactor/remove-dead-vestiges-1055` off `origin/main` (`BASE_BRANCH=main`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (`src` + `tests`).
- Absolute imports only; Google-style docstrings on non-trivial public APIs.
- Doc prose: plain and factual — no "Sprint", "critical", "robust", "comprehensive", "elegant".
- Guardrail suite (run before each commit, per `/preflight`): `just lint`, `just type`, `just test`.
  Part 2's Task 3 additionally requires `just docs-check` (removing an agent-facing `Field` changes
  the generated reference; `just test` does not regenerate reference docs).
- Do **not** touch `set_override` / `lookup` / `lookup_many` / `inventory/serialize.py` / the
  reconcile passes / `mcp/tools/ops/tuning.py` / `resources/deregister.py` — all already use
  `InventorySourceKind.RESOURCE` and stay untouched. No DB migration (the `inventory_overrides`
  column/PK are unchanged).
- Naming collision to avoid: `kdive.kernel_config.requirements` is a **live** module (ADR-0318).
  This plan touches only `kdive.components.requirements`. Never edit anything under `kernel_config/`.

## Task ordering

Task 1 (Part 1) is independent of Tasks 2–3. Task 2 must run **before** Task 3: Task 2 removes every
`inventory.py` reference to `BUILD_HOST_RESOURCE_KIND` / `InventorySourceKind.BUILD_HOST` /
`LockScope.BUILD_HOST`, so when Task 3 deletes those symbols the only remaining references are in the
`test_overrides.py` / `test_locks.py` cases Task 3 also removes. Each task is green at its boundary.

---

### Task 1: Remove the profile-requirements apparatus (AC1)

**Files:**
- Delete: `src/kdive/components/requirements.py`
- Modify: `src/kdive/components/catalog.py` (drop import, `ProfileRequirements`, `RootfsRequirements`, `ProfileCatalogEntry.requires`)
- Modify: `src/kdive/admin/default_fixtures.py` (`_PROFILE_YAML` literal + module docstring)
- Modify: `fixtures/local-libvirt/profiles/console-ready_x86_64.yaml`
- Modify: `docs/design/operator-fixture-profile-write-path.md` (stale live design doc)
- Delete: `fixtures/local-libvirt/configs/console-ready.required.config`
- Test: `tests/provider_components/test_catalog.py`, `tests/admin/test_default_fixtures.py`, `tests/mcp/catalog/test_fixtures_validate.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `ProfileCatalogEntry` with fields `provider`, `name`, `arch` only. `FixtureCatalog.profile()` and `load_fixture_catalog()` signatures unchanged.

- [ ] **Step 1: Update the three parse tests to the requires-free shape (they must fail first)**

In `tests/provider_components/test_catalog.py`, replace the `profiles/console.yaml` write (lines 37–52) so it carries no `requires:` block:

```python
    (fixture / "profiles" / "console.yaml").write_text(
        "provider: local-libvirt\n"
        "name: console-ready_x86_64\n"
        "arch: x86_64\n",
        encoding="utf-8",
    )
```

The `(fixture / "configs").mkdir()` line (12) is now unused but harmless; leave it or remove it — either keeps the test valid.

In `tests/admin/test_default_fixtures.py`, replace `test_console_ready_profile_carries_required_boot_policy` (lines 20–31) with a requires-free assertion:

```python
def test_console_ready_profile_has_no_requires_block() -> None:
    profile = yaml.safe_load(LOCAL_LIBVIRT_FIXTURES["profiles/console-ready_x86_64.yaml"])

    assert profile == {
        "provider": "local-libvirt",
        "name": "console-ready_x86_64",
        "arch": "x86_64",
    }
```

In `tests/mcp/catalog/test_fixtures_validate.py`, replace `_PROFILE_TEMPLATE` (lines 31–48) with:

```python
_PROFILE_TEMPLATE = """provider: {provider}
name: {name}
arch: {arch}
"""
```

- [ ] **Step 2: Run the tests to confirm they fail against current code**

Run: `uv run python -m pytest tests/provider_components/test_catalog.py tests/admin/test_default_fixtures.py tests/mcp/catalog/test_fixtures_validate.py -q`
Expected: FAIL. The current `ProfileCatalogEntry.requires` field is **required** (no default), so the requires-free fixture YAMLs written in Step 1 fail to parse against current code: `test_load_fixture_catalog_filters_provider` and `test_profiles_are_sorted_by_provider_name_arch` go red as `load_fixture_catalog` raises `CategorizedError` / `configuration_error`, and `test_console_ready_profile_has_no_requires_block` goes red on the dict assertion against the old `_PROFILE_YAML`. `test_valid_catalog_reports_profiles` uses `install_fixtures` (the **old** `_PROFILE_YAML`, not the edited template), so it stays green until Step 5 strips the literal. This anchors the behavior change.

- [ ] **Step 3: Delete the requirements module**

Run: `git rm src/kdive/components/requirements.py`

- [ ] **Step 4: Strip the dead models + field from `catalog.py`**

In `src/kdive/components/catalog.py`:
- Delete the import line `from kdive.components.requirements import CmdlineRequirements, ConfigRequirements` (line 13).
- Delete the entire `RootfsRequirements` class (lines 31–36) and the entire `ProfileRequirements` class (lines 39–44).
- In `ProfileCatalogEntry` (lines 70–76), delete the `requires: ProfileRequirements` field so the class ends at `arch: str`:

```python
class ProfileCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str
    arch: str
```

Leave `RootfsCatalogEntry.capabilities` and everything else untouched (that is a separate, live model).

- [ ] **Step 5: Strip the `requires:` block from the packaged fixture literal + fix its docstring**

In `src/kdive/admin/default_fixtures.py`, replace `_PROFILE_YAML` (lines 23–44) with:

```python
_PROFILE_YAML = """provider: local-libvirt
name: console-ready_x86_64
arch: x86_64
"""
```

Correct the module docstring (lines 5–7) so it no longer claims the bundle carries checked policy. Replace the sentence "The installed fixture bundle now carries only the **profiles** half — the kernel-config/cmdline policy the local-libvirt provider checks a built kernel against — plus a manifest that declares an empty rootfs list (the rootfs catalog is the DB now)." with:

```
The installed fixture bundle now carries only the **profiles** half — a per-profile
(provider, name, arch) triple — plus a manifest that declares an empty rootfs list (the rootfs
catalog is the DB now). kdive no longer inspects a kernel .config (ADR-0316), so a profile carries
no kernel-config/cmdline requirements.
```

- [ ] **Step 6: Strip the `requires:` block from the on-disk fixture YAML**

Overwrite `fixtures/local-libvirt/profiles/console-ready_x86_64.yaml` with exactly:

```yaml
provider: local-libvirt
name: console-ready_x86_64
arch: x86_64
```

- [ ] **Step 6b: Mark the stale live design doc superseded**

`docs/design/operator-fixture-profile-write-path.md` (ADR-0120/#439) documents a write path for a profile's `requires` apparatus. That apparatus is now doubly-dead: ADR-0316 already deleted its build consumers (its lines 41/108/109 point at `_external_config_requirements` and `providers/shared/build_host/config.py`, both gone), and this change removes the `requires` data shape itself. Rather than rewrite the body (out of scope), prepend a superseded banner and drop the deleted class from the models row — no guardrail catches a stale markdown reference, so this is the only thing keeping the doc honest.

Prepend immediately after the H1 title (before the first section):

```markdown
> **Superseded (2026-07-09).** The server-build lane that read a profile's `requires` was removed by
> [ADR-0316](../adr/0316-remove-server-build-lane.md); the `requires` data shape itself
> (`ProfileRequirements` / `ConfigRequirements` / `CmdlineRequirements`) was removed by
> [ADR-0319](../adr/0319-remove-dead-profile-requirements-buildhost-vestiges.md) (#1055). A fixture
> profile now carries only `(provider, name, arch)`; the consumer references below no longer exist.
> Retained for historical context only.
```

Edit line 37 to drop the deleted class:

```markdown
| Fixture catalog models (`FixtureManifest`, `ProfileCatalogEntry`) | `src/kdive/components/catalog.py` |
```

Leave the rest of the body unchanged — the banner disclaims it. Confirm `just docs-links` still resolves the two new ADR links.

- [ ] **Step 7: Delete the orphaned materialized config**

Run: `git rm fixtures/local-libvirt/configs/console-ready.required.config`
(If `fixtures/local-libvirt/configs/` becomes empty, git drops the directory automatically.)

- [ ] **Step 8: Run the guardrail suite**

Run: `just lint && just type && just test`
Expected: PASS. If `ty` reports an unused import in `catalog.py`, remove it; if `ruff` reports one, `just format` fixes it. Confirm `rg 'ConfigRequirements|CmdlineRequirements|ProfileRequirements|RootfsRequirements|\.required\.config' src/` returns zero hits. Also scan **live docs** (historical ADRs legitimately name the symbols as they were, so exclude them): `rg 'ProfileRequirements|ConfigRequirements|CmdlineRequirements|RootfsRequirements|\.required\.config' docs/ --glob '!docs/adr/**' --glob '!docs/archive/**' --glob '!docs/superpowers/**'` returns zero hits (the design-doc reference is gone; the superseded banner names the classes only inside a `> ` blockquote pointing at the removal ADRs, which is expected — if the grep flags the banner line, that single blockquote hit is the accepted residual).

- [ ] **Step 9: Commit**

```bash
git add src/kdive/components/catalog.py src/kdive/admin/default_fixtures.py \
        fixtures/local-libvirt/profiles/console-ready_x86_64.yaml \
        docs/design/operator-fixture-profile-write-path.md \
        tests/provider_components/test_catalog.py tests/admin/test_default_fixtures.py \
        tests/mcp/catalog/test_fixtures_validate.py
git add -u src/kdive/components/requirements.py fixtures/local-libvirt/configs/console-ready.required.config
git commit -m "refactor: remove dead profile-requirements apparatus (#1055)"
```

**Acceptance criteria (reviewer-checkable):** AC1–AC4 of the spec. `requirements.py` gone; `ProfileCatalogEntry` has only provider/name/arch; both fixture sources and all three parse tests carry no `requires:`; `console-ready.required.config` gone; `just lint && just type && just test` green. **AC3 witness:** `tests/mcp/catalog/test_fixtures_validate.py::test_valid_catalog_reports_profiles` is the `install-fixtures → load_fixture_catalog` round-trip (it calls `install_fixtures(dest)` then validates the written catalog), so its green state proves the packaged fixture re-parses requires-free.

---

### Task 2: Remove `source_kind` from `inventory.clear_override` (AC2, part A)

**Files:**
- Modify: `src/kdive/mcp/tools/ops/inventory.py`
- Modify: `docs/guide/reference/inventory.md` (regenerated, not hand-edited)
- Test: `tests/mcp/ops/test_inventory_clear_override.py`

**Interfaces:**
- Consumes: `OverrideIdentity` (field order `source_kind, resource_kind, name`), `InventorySourceKind.RESOURCE`, `BUILD_HOST_RESOURCE_KIND` (still exported by `overrides.py` until Task 3), `resource_identity_lock(conn, ResourceKind, name)`.
- Produces: `clear_override(pool, ctx, *, resource_kind: str, name: str) -> ToolResponse`. No `source_kind` token remains except the single internal `OverrideIdentity(source_kind=InventorySourceKind.RESOURCE, …)` constructor.

- [ ] **Step 1: Rewrite the tool's tests to the new signature (fail first)**

In `tests/mcp/ops/test_inventory_clear_override.py`:
- Update the module docstring (lines 1–13): drop the "build-host override" bullet and the "illegal `(source_kind, resource_kind)` pairing" wording; describe only the resource override, `not_found` idempotency, an invalid `resource_kind` → `configuration_error`, and the RBAC denials.
- Remove `BUILD_HOST_RESOURCE_KIND` from the import (line 27).
- Delete `test_clear_override_removed_build_host` (lines 149–176), `test_clear_override_unknown_source_kind_rejected` (lines 202–214), and `test_clear_override_illegal_build_host_resource_kind_rejected` (lines 217–229) — all exercise the removed parameter/branch.
- In `_seed_override` keep the `source_kind: InventorySourceKind` argument (it seeds the DB via `set_override`, unchanged); callers pass `InventorySourceKind.RESOURCE`.
- In every remaining `inventory_tools.clear_override(...)` call, delete the `source_kind="resource",` keyword argument. Affected: `test_clear_override_removed_resource`, `test_clear_override_absent_is_not_found_and_idempotent`, `test_clear_override_illegal_resource_kind_rejected`, `test_clear_override_non_admin_denied_and_audited_for_operator`, `test_clear_override_project_only_denied_unaudited`.
- Add an assertion in `test_clear_override_removed_resource` that the success payload no longer advertises `source_kind`:

```python
        assert resp.status == "cleared", resp.model_dump()
        assert "source_kind" not in (resp.data or {})
```

- [ ] **Step 2: Run the tool tests to confirm they fail**

Run: `uv run python -m pytest tests/mcp/ops/test_inventory_clear_override.py -q`
Expected: FAIL — `clear_override` still requires the `source_kind` keyword, so the updated calls raise `TypeError`.

- [ ] **Step 3: Remove `source_kind` from the handler and its helpers**

In `src/kdive/mcp/tools/ops/inventory.py`:

Imports (lines 33–37): drop `BUILD_HOST_RESOURCE_KIND` from the `from kdive.inventory.overrides import (...)` block, leaving `InventorySourceKind, OverrideIdentity`. Drop `LockScope, advisory_xact_lock` from the `from kdive.db.locks import ...` line (line 30) — they are used only by the deleted lock branch (verify with `rg 'LockScope|advisory_xact_lock' src/kdive/mcp/tools/ops/inventory.py`). Drop the now-unused `from contextlib import AbstractAsyncContextManager` (line 19) — it typed only the deleted `_override_identity_lock` helper.

Handler signature + docstring (lines 228–250): drop the `source_kind: str` parameter and rewrite the docstring:

```python
async def clear_override(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resource_kind: str,
    name: str,
) -> ToolResponse:
    """Delete the override-ledger entry for a config-declared identity (platform_admin).

    Clears a ``removed``/``detached`` override so the next no-entry reconcile pass re-asserts the
    file — the supported re-add path for a config host removed at runtime. Validates ``resource_kind``
    against the resource-kind enum, takes the per-identity lock, and deletes the entry. Returns
    ``cleared`` on success, ``not_found`` when no entry exists (idempotent), or
    ``configuration_error`` on an unknown ``resource_kind``.

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        resource_kind: The resource kind (e.g. ``remote-libvirt``).
        name: The identity name.
    """
```

Denial-audit block (lines 254–261): the `scope`/`args` reference the removed parameter, so rewrite:

```python
            await audit_platform_denial(
                pool,
                ctx,
                tool=_CLEAR_TOOL,
                scope=f"denied:{resource_kind}/{name}",
                args={"resource_kind": resource_kind, "name": name},
            )
```

Identity parse call (line 267): `identity = _parse_override_identity(resource_kind, name)`.

The lock `async with` (lines 270–274): replace `_override_identity_lock(conn, identity)` with a direct `resource_identity_lock` call (the helper collapses to one branch, so inline and delete it):

```python
        async with (
            pool.connection() as conn,
            conn.transaction(),
            resource_identity_lock(conn, ResourceKind(identity.resource_kind), identity.name),
        ):
```

Success payload (lines 283–292): drop `source_kind`:

```python
        return ToolResponse.success(
            _CLEAR_OBJECT_ID,
            "cleared",
            suggested_next_actions=["inventory.list"],
            data={
                "resource_kind": identity.resource_kind,
                "name": identity.name,
            },
        )
```

`_parse_override_identity` (lines 295–318): drop the `source_kind` parameter and the `BUILD_HOST` branch; validate `resource_kind` and build the identity:

```python
def _parse_override_identity(resource_kind: str, name: str) -> OverrideIdentity | ToolResponse:
    """Validate ``resource_kind`` against the resource-kind enum; return the identity or a
    config-error envelope."""
    try:
        ResourceKind(resource_kind)
    except ValueError:
        return _clear_config_error(f"resource_kind {resource_kind!r} is not a valid resource kind")
    return OverrideIdentity(
        source_kind=InventorySourceKind.RESOURCE, resource_kind=resource_kind, name=name
    )
```

Delete the entire `_override_identity_lock` helper (lines 330–336).

`_audit_clear` (lines 339–358): drop `source_kind` from the `scope` string and `args` dict:

```python
        event=audit.PlatformAuditEvent(
            tool=_CLEAR_TOOL,
            scope=f"{identity.resource_kind}:{identity.name}",
            args={"resource_kind": identity.resource_kind, "name": identity.name},
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
```

- [ ] **Step 4: Remove `source_kind` from the `@app.tool` wrapper**

In the `inventory_clear_override` wrapper (lines 396–423), make three removals: (a) delete the `source_kind: Annotated[str, Field(...)]` parameter (lines 397–399); (b) **rewrite the `resource_kind` Field description** to drop the "or 'build-host' for a build host" sentinel (current text: "Resource kind (e.g. 'remote-libvirt') for a resource, or 'build-host' for a build host." at lines 400–408) — that Field text is exactly what renders the `build-host` string into `inventory.md:18`, so leaving it fails AC8's `rg -w 'source_kind'` / `rg 'build-host' docs/guide/reference/inventory.md` gate after `just docs`; (c) drop `source_kind=source_kind,` from the inner `clear_override(...)` call. The wrapper docstring already carries no `source_kind` token — no change needed there. Target state:

```python
    async def inventory_clear_override(
        resource_kind: Annotated[
            str,
            Field(description="Resource kind (e.g. 'remote-libvirt') whose override to clear."),
        ],
        name: Annotated[str, Field(description="The identity name whose override to clear.")],
    ) -> ToolResponse:
        """Clear a config identity's override so reconcile re-asserts the file. Platform admin.

        The re-add path for a config host removed at runtime: deletes the ledger entry so the
        next no-entry reconcile pass re-creates the file-declared identity. Returns not_found
        when no override exists (idempotent).
        """
        return await clear_override(
            pool,
            current_context(),
            resource_kind=resource_kind,
            name=name,
        )
```

- [ ] **Step 5: Run the tool tests + falsifiable greps**

Run: `uv run python -m pytest tests/mcp/ops/test_inventory_clear_override.py -q`
Expected: PASS.

Run: `rg -w 'build_host' src/kdive/mcp/tools/ops/inventory.py` (and `rg 'build-host' …`)
Expected: zero hits.

Run: `rg -w 'source_kind' src/kdive/mcp/tools/ops/inventory.py` (word-bounded — an unbounded match hits `resource_kind` as a substring)
Expected: exactly one line — the `OverrideIdentity(source_kind=InventorySourceKind.RESOURCE, …)` constructor.

- [ ] **Step 6: Regenerate the agent-facing reference doc**

Run: `just docs`
This regenerates `docs/guide/reference/inventory.md` from the live tool schema; the `source_kind` parameter row disappears.
Run: `rg -w 'source_kind' docs/guide/reference/inventory.md` and `rg 'build-host' docs/guide/reference/inventory.md`
Expected: zero hits (word-bounded `source_kind`; `resource_kind` stays and contains it as a substring).
Run: `just docs-check`
Expected: PASS (committed reference matches a fresh generation).

- [ ] **Step 7: Run the full guardrail suite**

Run: `just lint && just type && just test && just docs-check`
Expected: PASS. (`overrides.py` still exports `BUILD_HOST_RESOURCE_KIND` and `locks.py` still has `LockScope.BUILD_HOST`; their tests still pass — Task 3 removes them.)

- [ ] **Step 8: Commit**

```bash
git add src/kdive/mcp/tools/ops/inventory.py docs/guide/reference/inventory.md \
        tests/mcp/ops/test_inventory_clear_override.py
git commit -m "refactor: drop source_kind from inventory.clear_override (#1055)"
```

**Acceptance criteria:** Spec AC6 + AC8. Tool takes `(resource_kind, name)`; success/denial/audit rows carry no `source_kind`; the two greps hold; `inventory.md` regenerated and `just docs-check` green.

---

### Task 3: Narrow `InventorySourceKind` + drop `LockScope.BUILD_HOST` (AC2, part B)

**Files:**
- Modify: `src/kdive/inventory/overrides.py`
- Modify: `src/kdive/db/locks.py`
- Test: `tests/inventory/test_overrides.py`, `tests/db/test_locks.py`

**Interfaces:**
- Consumes: nothing new. After Task 2, `inventory.py` no longer references the symbols removed here.
- Produces: `InventorySourceKind` with a single `RESOURCE` member; `LockScope` without `BUILD_HOST`; `OverrideIdentity`/`set_override`/`lookup`/`lookup_many` signatures unchanged.

- [ ] **Step 1: Update the two test modules (fail/drop first)**

In `tests/inventory/test_overrides.py`:
- Remove `BUILD_HOST_RESOURCE_KIND` from the import (line 16).
- Delete `_BUILD_HOST = InventorySourceKind.BUILD_HOST` (line 27) and the `_build_host_identity` helper (lines 34–37).
- Rewrite `test_lookup_many_filters_by_source_kind_and_keys_correctly` (lines 138–178) to drop the build-host seed and the cross-family assertion, keeping the two-resource-kind coexistence check:

```python
def test_lookup_many_keys_by_resource_kind_and_name(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await set_override(
                conn,
                _resource_identity("h1", kind="remote-libvirt"),
                disposition=InventoryOverrideDisposition.REMOVED,
                reason="r",
                actor="a",
            )
            await set_override(
                conn,
                _resource_identity("h1", kind="fault-inject"),
                disposition=InventoryOverrideDisposition.DETACHED,
                reason="r",
                actor="a",
            )
            resources = await lookup_many(conn, _RESOURCE)
        # Same name across two resource kinds coexists, keyed by (resource_kind, name).
        assert set(resources) == {("remote-libvirt", "h1"), ("fault-inject", "h1")}
        assert resources[("remote-libvirt", "h1")].disposition is (
            InventoryOverrideDisposition.REMOVED
        )
        assert resources[("fault-inject", "h1")].disposition is (
            InventoryOverrideDisposition.DETACHED
        )

    asyncio.run(_run())
```

In `tests/db/test_locks.py`, delete `test_build_host_scope_value_and_key_distinctness` (lines 59–66).

- [ ] **Step 2: Run the two test modules to confirm the collected set is red/clean against current code**

Run: `uv run python -m pytest tests/inventory/test_overrides.py tests/db/test_locks.py -q`
Expected: the edited `test_overrides.py` imports still resolve against current code (the symbols still exist), so this collects and passes **now**; the point of Step 1 is that these modules no longer reference the soon-to-be-deleted symbols. Proceed to remove the symbols; re-run in Step 5.

- [ ] **Step 3: Narrow `InventorySourceKind` + drop the sentinel in `overrides.py`**

In `src/kdive/inventory/overrides.py`:
- Remove `"BUILD_HOST_RESOURCE_KIND"` from `__all__` (line 41).
- Narrow the enum (lines 58–67):

```python
class InventorySourceKind(StrEnum):
    """The inventory family an override targets (the ledger's ``source_kind``)."""

    RESOURCE = "resource"
```

- Delete the `BUILD_HOST_RESOURCE_KIND` sentinel and its comment (lines 65–67).
- Update the module docstring (lines 23–26): the ledger now has a single family. Replace the "Identity is ``(source_kind, resource_kind, name)`` … the fixed sentinel ``build-host`` for a build host (build-host names are globally unique, so the sentinel keeps the PK total)." paragraph with:

```
Identity is ``(source_kind, resource_kind, name)`` — the ledger table's PK. ``source_kind`` is the
inventory family (only ``resource`` today); ``resource_kind`` is the resource ``kind``.
```

Also update the earlier docstring mention "inventory family (``resource`` | ``build_host``); ``resource_kind`` is the resource ``kind`` for a resource, or the fixed sentinel ``build-host`` for a build host" (lines 24–26) to name only the resource family.

- [ ] **Step 4: Drop `LockScope.BUILD_HOST` + fix the docstring in `locks.py`**

In `src/kdive/db/locks.py`:
- Delete the `BUILD_HOST = "build_host"` member (line 49).
- Fix the `LockScope` class docstring (lines 37–40). Remove the sentence describing `BUILD_HOST` as the `inventory.clear_override` per-identity lock (which is false — that path locks on `RESOURCE`):

```
    ``PROJECT`` is keyed by the ``project`` string; every other scope is keyed by an object
    :class:`~uuid.UUID`, except ``RESOURCE``, which the inventory per-identity lock keys by a
    ``"{kind}:{name}"`` string (the ``inventory.clear_override`` / reconcile lock).
```

(The `_lock_key` derivation is unchanged; removing an enum member does not shift any other scope's key, which folds only its own `scope.value` string.)

- [ ] **Step 5: Run the guardrail suite + falsifiable grep**

Run: `just lint && just type && just test`
Expected: PASS.
Run: `rg 'BUILD_HOST_RESOURCE_KIND|LockScope\.BUILD_HOST|InventorySourceKind\.BUILD_HOST' src/`
Expected: zero hits.
Run: `rg 'ConfigRequirements|CmdlineRequirements|ProfileRequirements|RootfsRequirements|BUILD_HOST_RESOURCE_KIND|LockScope\.BUILD_HOST|InventorySourceKind\.BUILD_HOST|\.required\.config' src/`
Expected: zero hits (the whole spec grep guard).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/inventory/overrides.py src/kdive/db/locks.py \
        tests/inventory/test_overrides.py tests/db/test_locks.py
git commit -m "refactor: narrow InventorySourceKind, drop LockScope.BUILD_HOST (#1055)"
```

**Acceptance criteria:** Spec AC5 + AC7. `InventorySourceKind` has one member; `BUILD_HOST_RESOURCE_KIND` and `LockScope.BUILD_HOST` gone; the `LockScope` docstring no longer claims a `BUILD_HOST` scope; whole-tree grep guard returns zero hits; suite green.

---

## Final verification (after all tasks)

Run: `just lint && just type && just test && just docs-check`
Expected: all PASS. Then the spec's grep guard (Task 3 Step 5) returns zero hits, and
`rg -w 'source_kind' src/kdive/mcp/tools/ops/inventory.py` returns exactly the one constructor line.

## Rollback

Each task is an isolated deletion commit on the feature branch. Revert the branch (or an individual
commit); no migration, no data change. Operational note for pre-upgrade fixture install dirs is in
the spec ("Operational note — previously-installed fixtures").
