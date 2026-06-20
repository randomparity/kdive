# Build-Host Resolvability Surfacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `runs.profile_examples` and `build_hosts.list` from advertising an `ephemeral_libvirt` build host that has no backing `[[remote_libvirt]]` instance, by surfacing a derived "resolves" fact on both read tools.

**Architecture:** One shared predicate `build_host_resolves(...)` plus a degrade-to-empty inventory wrapper `declared_remote_instance_names()` in `services/runs/build_host_selection.py`. `runs.profile_examples` omits non-resolving hosts; `build_hosts.list` adds a `resolves` string scalar to each item's `data`. Read-only — no schema, no migration, no change to build admission or execution.

**Tech Stack:** Python 3.14, psycopg, FastMCP, pytest. ADR-0195.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict (whole tree incl. tests).
- Every tool returns a `ToolResponse` (`mcp/responses.py`); `data` values on `build_hosts.list` are string scalars by existing convention.
- Guardrail before each commit: `just lint && just type` and the focused tests for the touched files. Full `just ci` before push.
- Commit messages: conventional, imperative ≤72-char subject, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Inventory reads must degrade (never raise) out of either read tool (ADR-0112 fault isolation): a missing or malformed `systems.toml` → empty declared set.

---

### Task 1: Shared predicate + degrade-to-empty inventory wrapper

**Files:**
- Modify: `src/kdive/services/runs/build_host_selection.py`
- Test: `tests/services/test_build_host_selection.py`

**Interfaces:**
- Consumes: `BuildHostKind` (`kdive.db.build_hosts`), `remote_instance_names` + `CategorizedError` semantics from `kdive.providers.remote_libvirt.config`.
- Produces:
  - `build_host_resolves(host_kind: BuildHostKind, host_name: str, declared_instances: Collection[str]) -> bool`
  - `declared_remote_instance_names() -> list[str]`

- [ ] **Step 1: Write the failing tests** in `tests/services/test_build_host_selection.py`

```python
from collections.abc import Collection

import pytest

from kdive.db.build_hosts import BuildHostKind
from kdive.services.runs import build_host_selection
from kdive.services.runs.build_host_selection import (
    build_host_resolves,
    declared_remote_instance_names,
)


@pytest.mark.parametrize("kind", [BuildHostKind.LOCAL, BuildHostKind.SSH])
def test_local_and_ssh_always_resolve(kind: BuildHostKind) -> None:
    assert build_host_resolves(kind, "anything", []) is True
    assert build_host_resolves(kind, "anything", ["other"]) is True


def test_ephemeral_resolves_only_when_named_instance_declared() -> None:
    assert (
        build_host_resolves(BuildHostKind.EPHEMERAL_LIBVIRT, "ub24", ["ub24"]) is True
    )
    assert build_host_resolves(BuildHostKind.EPHEMERAL_LIBVIRT, "ub24", []) is False
    assert (
        build_host_resolves(BuildHostKind.EPHEMERAL_LIBVIRT, "ub24", ["other"]) is False
    )


def test_declared_names_degrade_to_empty_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    def boom() -> list[str]:
        raise CategorizedError("bad toml", category=ErrorCategory.CONFIGURATION_ERROR)

    monkeypatch.setattr(build_host_selection, "remote_instance_names", boom)
    assert declared_remote_instance_names() == []


def test_declared_names_passes_through_when_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_host_selection, "remote_instance_names", lambda: ["a", "b"]
    )
    assert declared_remote_instance_names() == ["a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/dave/src/kdive-worktrees/feat-validate-build-host-resolve-626 && uv run python -m pytest tests/services/test_build_host_selection.py -q -k "resolve or declared"`
Expected: FAIL (ImportError: cannot import name `build_host_resolves`).

- [ ] **Step 3: Implement** in `src/kdive/services/runs/build_host_selection.py`

Add to imports (top of file, with the other `from collections...`/typing imports):

```python
from collections.abc import Collection
```

Add this import near the other `kdive.` imports:

```python
from kdive.providers.remote_libvirt.config import remote_instance_names
```

Add after `accepted_source_kinds`:

```python
def build_host_resolves(
    host_kind: BuildHostKind, host_name: str, declared_instances: Collection[str]
) -> bool:
    """Whether a build host of this kind/name can be built on, given the declared instances.

    ``local`` and ``ssh`` hosts have no ``[[remote_libvirt]]`` dependency: a ``local`` host
    builds on the worker, an ``ssh`` host connects to its own ``address``/credential. An
    ``ephemeral_libvirt`` host provisions its build VM on the ``[[remote_libvirt]]`` instance
    whose name equals the build host's name (the worker resolves it by name, ADR-0187), so it
    resolves only when that instance is declared.

    Args:
        host_kind: The build host's transport kind.
        host_name: The build host's name (the ``[[remote_libvirt]]`` instance name for an
            ``ephemeral_libvirt`` host).
        declared_instances: The declared ``[[remote_libvirt]]`` instance names.

    Returns:
        ``True`` if the host can be built on; ``False`` for an ``ephemeral_libvirt`` host
        whose name is not a declared instance.
    """
    if host_kind is BuildHostKind.EPHEMERAL_LIBVIRT:
        return host_name in declared_instances
    return True


def declared_remote_instance_names() -> list[str]:
    """The declared ``[[remote_libvirt]]`` instance names, degrading to empty on a config error.

    Wraps :func:`remote_instance_names` (ADR-0187) for the read-only discovery surfaces. A
    missing ``systems.toml`` already returns an empty list; a present-but-malformed file raises
    ``CONFIGURATION_ERROR`` there, which this swallows to an empty list so a bad operator edit
    cannot crash ``runs.profile_examples`` / ``build_hosts.list`` (ADR-0112 fault isolation).
    The precise parse error still surfaces fail-closed at build time.
    """
    try:
        return remote_instance_names()
    except CategorizedError:
        return []
```

Ensure `CategorizedError` is imported (it is already imported in this module from `kdive.domain.errors`; confirm before adding a duplicate).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/services/test_build_host_selection.py -q`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/services/runs/build_host_selection.py tests/services/test_build_host_selection.py
git commit -m "feat(build): add build_host_resolves predicate + declared-names wrapper"
```

---

### Task 2: `runs.profile_examples` omits non-resolving hosts

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/profile_examples.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py:345-359`
- Test: `tests/mcp/lifecycle/test_runs_profile_examples.py`

**Interfaces:**
- Consumes: `build_host_resolves`, `declared_remote_instance_names` (Task 1).
- Produces: `build_host_profile_examples(hosts: list[BuildHost], declared_instances: Collection[str]) -> ToolResponse` (new required second parameter).

- [ ] **Step 1: Write the failing tests** — append to `tests/mcp/lifecycle/test_runs_profile_examples.py`

Note: the existing `_host(...)` helper and `_ALL_KINDS` already exist; existing calls `build_host_profile_examples(_ALL_KINDS)` must be updated to pass a declared set in Step 3. Add these tests:

```python
def test_unresolvable_ephemeral_host_is_omitted() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=[])
    names = {item.object_id for item in resp.items}
    assert "eph-host" not in names
    assert {"worker-local", "ssh-host"} <= names


def test_resolvable_ephemeral_host_is_emitted() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    names = {item.object_id for item in resp.items}
    assert "eph-host" in names
```

Update the existing calls in this file (`build_host_profile_examples(_ALL_KINDS)` and `build_host_profile_examples([])`) to pass a declared set that keeps the eph host present, e.g. `declared_instances=["eph-host"]` (and `declared_instances=[]` for the empty-list test). For the registrar boundary test (`test_runs_profile_examples_registered_read_only_and_auth_only`), it inserts an `ssh` host, which always resolves, so it needs no declared instances — but the registrar now calls `declared_remote_instance_names()`, which reads the (absent) default `systems.toml` and returns `[]`. That is fine; assert `examples-ssh` still present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_profile_examples.py -q`
Expected: FAIL (TypeError: missing `declared_instances`, and the two new assertions).

- [ ] **Step 3: Implement**

In `profile_examples.py`, change the signature and filter:

```python
from collections.abc import Collection
...
from kdive.services.runs.build_host_selection import (
    SourceKind,
    accepted_source_kinds,
    build_host_resolves,
)
```

```python
def build_host_profile_examples(
    hosts: list[BuildHost], declared_instances: Collection[str]
) -> ToolResponse:
    """Build the example-build-profiles collection from a list of build hosts.

    Omits any host that does not resolve to a declared ``[[remote_libvirt]]`` instance
    (an ``ephemeral_libvirt`` host whose name names no instance), so every emitted example
    is buildable for its host (ADR-0195, #626). ``local`` and ``ssh`` hosts always resolve.

    Args:
        hosts: The registered build-host rows (e.g. from
            :func:`~kdive.db.build_hosts.list_all_hosts`).
        declared_instances: The declared ``[[remote_libvirt]]`` instance names
            (from :func:`~kdive.services.runs.build_host_selection.declared_remote_instance_names`).

    Returns:
        A :class:`ToolResponse` collection with one item per *resolving* host.
    """
    items = [
        _example_item(host)
        for host in hosts
        if build_host_resolves(host.kind, host.name, declared_instances)
    ]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )
```

In `registrar.py`, update the import block (around line 29-31) and the handler (line 345-359):

```python
from kdive.mcp.tools.lifecycle.runs.profile_examples import (
    build_host_profile_examples as _build_host_profile_examples,
)
from kdive.services.runs.build_host_selection import declared_remote_instance_names
```

```python
    async def runs_profile_examples() -> ToolResponse:
        """Return a ready-to-edit build profile per registered build host. Requires a token."""
        # Auth-only (ADR-0117): the verifier already gated the transport; enforce token
        # presence as defence-in-depth. No platform/project gate, no audit — the projection
        # is the public host-kind/source-kind rule only (ADR-0160).
        current_context()
        declared = declared_remote_instance_names()
        async with pool.connection() as conn:
            hosts = await list_all_hosts(conn)
        return _build_host_profile_examples(hosts, declared)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_profile_examples.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/mcp/tools/lifecycle/runs/profile_examples.py src/kdive/mcp/tools/lifecycle/runs/registrar.py tests/mcp/lifecycle/test_runs_profile_examples.py
git commit -m "feat(runs): omit unresolvable build hosts from profile_examples"
```

---

### Task 3: `build_hosts.list` exposes a `resolves` field

**Files:**
- Modify: `src/kdive/mcp/tools/ops/build_hosts/lifecycle.py:58-105`
- Test: `tests/mcp/ops/test_build_hosts.py`

**Interfaces:**
- Consumes: `build_host_resolves`, `declared_remote_instance_names` (Task 1), `BuildHostKind`.
- Produces: `build_hosts.list` items whose `data` carries `"resolves"` (`"true"`/`"false"`).

- [ ] **Step 1: Write the failing tests** — add to `tests/mcp/ops/test_build_hosts.py`

Inspect the file first for the existing list-test helpers and platform-auditor context fixture; mirror them. Add a test that:
- registers an `ephemeral_libvirt` host with a name **not** in `systems.toml` → its item `data["resolves"] == "false"`;
- registers an `ssh` host → its item `data["resolves"] == "true"`;
- still asserts `ssh_credential_ref` is the reference string only (redaction intact).

Use the existing default (absent `systems.toml` in the test env) so `declared_remote_instance_names()` returns `[]`. If the test harness needs a declared instance, monkeypatch `kdive.mcp.tools.ops.build_hosts.lifecycle.declared_remote_instance_names` to return the names.

```python
async def test_list_marks_unresolvable_ephemeral_host(...) -> None:
    # register an ephemeral_libvirt host named "eph-noinstance"
    # call list_build_hosts as platform_auditor
    # assert items["eph-noinstance"]["resolves"] == "false"
    ...


async def test_list_marks_resolvable_hosts_true(monkeypatch, ...) -> None:
    monkeypatch.setattr(
        "kdive.mcp.tools.ops.build_hosts.lifecycle.declared_remote_instance_names",
        lambda: ["eph-ok"],
    )
    # register an ephemeral_libvirt host named "eph-ok" and an ssh host
    # assert items["eph-ok"]["resolves"] == "true"
    # assert items[<ssh>]["resolves"] == "true"
```

(Write the full bodies following the file's existing registration/list patterns — do not leave the `...`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/ops/test_build_hosts.py -q -k resolv`
Expected: FAIL (KeyError `resolves`).

- [ ] **Step 3: Implement** in `lifecycle.py`

Add import:

```python
from kdive.services.runs.build_host_selection import (
    accepted_source_kinds,
    build_host_resolves,
    declared_remote_instance_names,
)
```

In `list_build_hosts`, resolve the declared set once before building items, and add the field:

```python
    declared = declared_remote_instance_names()
    items = [
        ToolResponse.success(
            str(row["id"]),
            "ok",
            data={
                "id": str(row["id"]),
                "name": row["name"],
                "kind": row["kind"],
                "address": row["address"] or "",
                "ssh_credential_ref": row["ssh_credential_ref"] or "",
                "workspace_root": row["workspace_root"],
                "max_concurrent": str(row["max_concurrent"]),
                "enabled": str(row["enabled"]).lower(),
                "state": row["state"],
                "resolves": str(
                    build_host_resolves(
                        BuildHostKind(row["kind"]), row["name"], declared
                    )
                ).lower(),
                "supported_source_kinds": [
                    kind.value for kind in accepted_source_kinds(BuildHostKind(row["kind"]))
                ],
            },
        )
        for row in rows
    ]
```

`declared_remote_instance_names()` is sync and reads the file once; call it before the comprehension (outside the loop) so the inventory is read once per list call, not per row.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/ops/test_build_hosts.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/mcp/tools/ops/build_hosts/lifecycle.py tests/mcp/ops/test_build_hosts.py
git commit -m "feat(build-hosts): expose resolves field on build_hosts.list"
```

---

### Task 4: Full guardrails + docs regen

**Files:** none new — verification + any generated-doc regen.

- [ ] **Step 1:** Run the full gate: `just ci`. Fix anything red.
- [ ] **Step 2:** If a generated tool-reference doc changed (`docs/.../tool reference`), regenerate via the repo's generator recipe and review the diff. (Adding a `data` field is not expected to change input-schema docs, but confirm `just ci`'s doc tests are green.)
- [ ] **Step 3:** Commit any regenerated artifacts separately:
```bash
git commit -m "docs(build): regenerate tool reference for build_hosts.list resolves"
```
(skip if nothing regenerated.)

---

## Self-Review

- **Spec coverage:** SC1 → Task 1 tests; SC2 → Task 2 tests; SC3 → Task 3 tests; SC4 → Task 1 degrade tests; SC5 → existing suites run in Task 2/3 + `just ci` (Task 4). All covered.
- **Placeholder scan:** Task 3 test bodies are described against the existing file's patterns (the file's exact registration helpers must be read at implementation time); every code step in Tasks 1-2 shows full code.
- **Type consistency:** `build_host_resolves` / `declared_remote_instance_names` signatures identical across Tasks 1-3; `build_host_profile_examples` second param `declared_instances: Collection[str]` consistent.
