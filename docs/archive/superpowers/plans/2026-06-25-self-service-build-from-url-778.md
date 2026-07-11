# Self-service kernel build from a developer-named git URL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a `contributor` build a kernel from a git URL+ref they name, on a build environment they discover and select, with verified provenance — no per-build operator action.

**Architecture:** A build environment is a read-only *projection* over the existing `build_hosts` rows (no new entity). A new `build_envs.list` tool surfaces it; `build_profile.build_host` selects it (already wired). A parse-boundary validator rejects bare-string git-URLs (pointing at the structured form). The build records the resolved commit on both clone paths and `runs.get` surfaces it. The trust gate is unchanged (the ADR-0162 allowlist already gates only `worker-local`; isolated hosts already clone ungated).

**Tech Stack:** Python 3.14, `uv`, Pydantic v2, FastMCP, psycopg (async), Postgres, pytest. Commands via `just`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-25-self-service-build-from-url-778.md`. ADR: `docs/adr/0242-self-service-build-from-url.md`.
- Guardrails before every commit: `just lint` (ruff check + format), `just type` (whole tree), and the focused tests for the task. Run `just ci` once before the first push.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Google-style docstrings on public APIs. No relative imports.
- Errors use `ToolResponse.failure` / `CategorizedError` with the most specific `ErrorCategory`; **never echo a submitted value** into an error (ADR-0123 redaction).
- Conventional-commit subjects ≤72 chars, imperative; end each commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Migrations are forward-only additive SQL in `src/kdive/db/schema/NNNN_*.sql`; next free number is **0049**.
- Tests mirror the package tree under `tests/`. No `live_vm` for the unit surface; the end-to-end is gated.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/kdive/db/schema/0049_build_host_toolchain_desc.sql` | add nullable `toolchain_desc` column | 1 |
| `src/kdive/db/build_hosts.py` | `BuildHost.toolchain_desc` + `_row_to_host` | 1 |
| `src/kdive/mcp/tools/ops/build_hosts/register.py` | accept+persist `toolchain_desc` | 1 |
| `src/kdive/mcp/tools/ops/build_hosts/build_envs.py` (new) | `build_envs.list` handler + projection | 2 |
| registrar for the new tool + `src/kdive/mcp/exposure.py` | register + `_CONTRIBUTOR` exposure | 2 |
| `src/kdive/profiles/build.py` | bare-URL guard validator | 3 |
| `src/kdive/services/runs/admission.py` (+ message site) | extend worker-local git rejection to name `build_envs.list` | 4 |
| `src/kdive/providers/shared/build_host/transports/shell_transport.py` | `clone(...) -> str` (resolved commit) | 5 |
| `src/kdive/build_artifacts/results.py` | `BuildOutput.build_provenance` | 5 |
| `src/kdive/providers/shared/build_host/configuration/git_source.py` | `strip_userinfo` helper | 5 |
| `src/kdive/services/runs/steps.py` | `BuildStepResult.build_provenance` (load/dump) | 5 |
| `src/kdive/jobs/handlers/runs_build.py` | pass provenance into `BuildStepResult(...)` | 5 |
| `src/kdive/mcp/tools/lifecycle/runs/common.py` | surface `data.build_provenance` via `existing_build_result` | 6 |
| `src/kdive/mcp/resources/_content/build-source-staging.md` | document the lane + guard + `build_envs.list` | 7 |

---

### Task 1: Add `toolchain_desc` to build hosts (migration + model + registration)

**Files:**
- Create: `src/kdive/db/schema/0049_build_host_toolchain_desc.sql`
- Modify: `src/kdive/db/build_hosts.py` (`BuildHost` dataclass ~65-74, `_row_to_host` ~77-89)
- Modify: `src/kdive/mcp/tools/ops/build_hosts/register.py` (`BuildHostRegistration` ~40-46, `_SSH_INSERT`/`_EPHEMERAL_INSERT` ~110-120, `_ssh_plan`/`_ephemeral_plan` value tuples)
- Test: `tests/db/test_migrations.py` (migration-list assertion), `tests/mcp/ops/build_hosts/test_register.py`

**Interfaces:**
- Produces: `BuildHost.toolchain_desc: str | None`; registration accepts optional `toolchain_desc: str | None`.

- [ ] **Step 1: Write the failing migration-list test.** Find the migration-count/list assertion (grep `0048` in `tests/db/`) and add `0049_build_host_toolchain_desc.sql` to the expected ordered list (mirror how `0048` is asserted — there are typically THREE places: count, ordered names, latest).

- [ ] **Step 2: Run it, expect FAIL.** `uv run python -m pytest tests/db/test_migrations.py -q` → fails on the missing/extra migration.

- [ ] **Step 3: Write the migration.**
```sql
-- 0049_build_host_toolchain_desc.sql — operator-asserted build-env toolchain description (ADR-0242).
-- Additive, forward-only (ADR-0015). Nullable; existing rows read as NULL ("no description").
ALTER TABLE build_hosts ADD COLUMN toolchain_desc text;
```

- [ ] **Step 4: Add the model field.** In `build_hosts.py`, add `toolchain_desc: str | None` to `BuildHost` (after `state`) and to `_row_to_host`: `toolchain_desc=cast("str | None", row["toolchain_desc"]),`. Update the dataclass docstring `Attributes:` block with one line.

- [ ] **Step 5: Accept + persist it in registration.** In `register.py`: add to `BuildHostRegistration` base model `toolchain_desc: str | None = Field(default=None, description="Operator-asserted toolchain summary shown to developers in build_envs.list, e.g. 'gcc11, binutils2.40; suits rhel9/5.14'. Not verified against the image.")`. Append `toolchain_desc` to both `_SSH_INSERT` and `_EPHEMERAL_INSERT` column lists and `VALUES` placeholders, and append `request.toolchain_desc` to both `_ssh_plan`/`_ephemeral_plan` value tuples (last position).

- [ ] **Step 6: Write the registration round-trip test.** In `test_register.py`, register an ssh host with `toolchain_desc="gcc11"`, assert the stored row's `toolchain_desc == "gcc11"`; register one omitting it, assert `toolchain_desc is None`.

- [ ] **Step 7: Run tests + guardrails.** `uv run python -m pytest tests/db/test_migrations.py tests/mcp/ops/build_hosts/test_register.py -q` → PASS. `just lint && just type`.

- [ ] **Step 8: Commit.**
```bash
git add src/kdive/db/schema/0049_build_host_toolchain_desc.sql src/kdive/db/build_hosts.py src/kdive/mcp/tools/ops/build_hosts/register.py tests/db/test_migrations.py tests/mcp/ops/build_hosts/test_register.py
git commit -m "feat(build-hosts): add toolchain_desc column + registration field (#778)"
```

---

### Task 2: `build_envs.list` discovery tool (contributor-readable projection)

**Files:**
- Create: `src/kdive/mcp/tools/ops/build_hosts/build_envs.py` (handler + projection)
- Modify: the build_hosts registrar (`src/kdive/mcp/tools/ops/build_hosts/registrar.py`) to register `build_envs.list`
- Modify: `src/kdive/mcp/exposure.py` (add `"build_envs.list": _CONTRIBUTOR`)
- Test: `tests/mcp/ops/build_hosts/test_build_envs.py`

**Interfaces:**
- Consumes: `list_all_hosts(conn)` → `list[BuildHost]` (`db/build_hosts.py:121`); `BuildHost.toolchain_desc` (Task 1).
- Produces: tool `build_envs.list` → `ToolResponse` whose `data["build_envs"]` is a list of `{name, kind, toolchain_desc, enabled}` dicts.

- [ ] **Step 1: Write the failing projection test.** In `test_build_envs.py`: seed two hosts (one ssh with `toolchain_desc`, one ephemeral without), call the handler, assert `data["build_envs"]` lists both `{name, kind, toolchain_desc, enabled}`, that the descriptor-less one has `toolchain_desc is None`, and that **no** key `address`/`ssh_credential_ref`/`base_image_volume` appears in any item.

- [ ] **Step 2: Run it, expect FAIL** (`ModuleNotFoundError` / handler missing). `uv run python -m pytest tests/mcp/ops/build_hosts/test_build_envs.py -q`.

- [ ] **Step 3: Write the handler.**
```python
"""The build_envs.list discovery tool (ADR-0242): a contributor-readable projection of
build hosts as selectable build environments, omitting infra/secret detail."""
from __future__ import annotations

from psycopg import AsyncConnection

from kdive.db.build_hosts import list_all_hosts
from kdive.mcp.responses import ToolResponse

_TOOL = "build_envs.list"


async def list_build_envs(conn: AsyncConnection) -> ToolResponse:
    """Project registered build hosts into developer-facing build environments.

    Returns name, kind, the operator-asserted toolchain description, and enabled — never
    address, credential reference, or base-image volume (ADR-0242 §1).
    """
    hosts = await list_all_hosts(conn)
    envs = [
        {
            "name": h.name,
            "kind": h.kind.value,
            "toolchain_desc": h.toolchain_desc,
            "enabled": h.enabled,
        }
        for h in hosts
    ]
    return ToolResponse.success(_TOOL, data={"build_envs": envs})
```
(Confirm the exact `ToolResponse.success(object_id, data=...)` signature against a neighboring read tool, e.g. `build_hosts/lifecycle.py`, and match it.)

- [ ] **Step 4: Register the tool + exposure.** Add a `@app.tool(name="build_envs.list", annotations=_docmeta.read_only(), meta={"maturity": "implemented"})` wrapper in the build_hosts registrar that calls `list_build_envs(pool-connection)`, mirroring the existing `build_hosts.list` wrapper. In `exposure.py` add `"build_envs.list": _CONTRIBUTOR,` (keep alphabetical grouping).

- [ ] **Step 5: Write the exposure test.** Assert `build_envs.list` is in the `_CONTRIBUTOR` exposure set and that a `viewer`-scoped catalog does **not** advertise it (mirror an existing exposure-map test in `tests/mcp/`).

- [ ] **Step 6: Run tests + guardrails.** `uv run python -m pytest tests/mcp/ops/build_hosts/ -q` → PASS. `just lint && just type`.

- [ ] **Step 7: Commit.**
```bash
git add src/kdive/mcp/tools/ops/build_hosts/build_envs.py src/kdive/mcp/tools/ops/build_hosts/registrar.py src/kdive/mcp/exposure.py tests/mcp/ops/build_hosts/test_build_envs.py
git commit -m "feat(build-envs): contributor-readable build_envs.list projection (#778)"
```

---

### Task 3: Bare-URL guard on `ServerBuildProfile.kernel_source_ref`

**Files:**
- Modify: `src/kdive/profiles/build.py` (import `field_validator`; add `_URI_SCHEME_PREFIXES`, `_match_uri_scheme`, and the validator on `ServerBuildProfile`)
- Test: `tests/profiles/test_build.py`; `tests/mcp/lifecycle/` for the `runs.create` no-leak boundary test

**Interfaces:**
- Produces: parsing a `ServerBuildProfile` whose bare-string `kernel_source_ref` begins with a rejected scheme raises `CONFIGURATION_ERROR` via `BuildProfile.parse`.

- [ ] **Step 1: Write failing tests.** In `test_build.py`:
```python
import pytest
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile

@pytest.mark.parametrize("ref", [
    "git:abc", "git://h/r", "git+ssh://h/r", "ssh://h/r",
    "https://h/r", "http://h/r", "HTTPS://h/r",
])
def test_bare_uri_kernel_source_ref_rejected(ref):
    data = {"schema_version": 1, "kernel_source_ref": ref}
    with pytest.raises(CategorizedError) as e:
        BuildProfile.parse(data)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR

@pytest.mark.parametrize("ref", [
    "linux-6.9", "/srv/linux", "git+https://git.kernel.org/linux.git#v6.9",
    "file:///src/linux", "git@github.com:torvalds/linux", "git-6.9",
])
def test_bare_label_kernel_source_ref_accepted(ref):
    data = {"schema_version": 1, "kernel_source_ref": ref}
    profile = BuildProfile.parse(data)
    assert profile.kernel_source_ref == ref

def test_structured_git_with_https_remote_not_rejected():
    data = {"schema_version": 1,
            "kernel_source_ref": {"git": {"remote": "https://h/r", "ref": "v6.9"}}}
    profile = BuildProfile.parse(data)
    assert profile.kernel_source_ref.git.remote == "https://h/r"

def test_rejected_uri_error_does_not_leak_value():
    data = {"schema_version": 1,
            "kernel_source_ref": "https://USER-PLANTED-TOKEN@h/r"}
    with pytest.raises(CategorizedError) as e:
        BuildProfile.parse(data)
    assert "PLANTED-TOKEN" not in str(e.value.details)
```

- [ ] **Step 2: Run, expect FAIL** (the URI strings currently parse as labels). `uv run python -m pytest tests/profiles/test_build.py -q -k uri`.

- [ ] **Step 3: Implement the guard** in `build.py`. Add `field_validator` to the pydantic import. Above `ServerBuildProfile`:
```python
_URI_SCHEME_PREFIXES = ("git+ssh://", "git://", "ssh://", "https://", "http://", "git:")


def _match_uri_scheme(value: str) -> str | None:
    """Return the recognized git clone-URL scheme prefix ``value`` begins with, else None."""
    lowered = value.strip().lower()
    for prefix in _URI_SCHEME_PREFIXES:
        if lowered.startswith(prefix):
            return prefix
    return None
```
Inside `ServerBuildProfile`, add:
```python
    @field_validator("kernel_source_ref", mode="after")
    @classmethod
    def _reject_uri_bare_source(
        cls, value: NonEmptyStr | GitKernelSource
    ) -> NonEmptyStr | GitKernelSource:
        """Reject a bare-string ref that looks like a git clone URL (ADR-0242).

        A bare string is warm-tree provenance metadata, never cloned; a developer who means
        to clone a URL must pass the structured ``{"git": {...}}`` form. Only the matched
        scheme token appears in the message — never the submitted value.
        """
        if isinstance(value, str):
            scheme = _match_uri_scheme(value)
            if scheme is not None:
                raise ValueError(
                    f"a bare kernel_source_ref that looks like a git URL (scheme {scheme!r}) is "
                    "warm-tree provenance metadata and will not be cloned; for a git build pass "
                    'the structured {"git": {"remote": ..., "ref": ...}} object, and select a '
                    "build environment from build_envs.list"
                )
        return value
```

- [ ] **Step 4: Run, expect PASS.** `uv run python -m pytest tests/profiles/test_build.py -q`.

- [ ] **Step 5: Write the `runs.create` tool-boundary no-leak test.** In the runs lifecycle tests, drive `runs.create` (or its `_create_run` handler) with `build_profile={"schema_version":1,"kernel_source_ref":"https://PLANTED-TOKEN@h/r"}` and assert the returned `ToolResponse` is `CONFIGURATION_ERROR` whose serialized form contains neither `PLANTED-TOKEN`, the host, nor a literal `"input"` key (exercises `BindingErrorMiddleware._build_profile_envelope`, `include_input=False`). Mirror an existing runs.create error test for the harness.

- [ ] **Step 6: Scan for build-profile fixtures using a rejected scheme.** Run `rg -n 'kernel_source_ref"?\s*[:=]\s*"(git:|git\+ssh|ssh://|https://|http://|git://)' tests/` — confirm the only hits are `test_systems_profile_schema.py` (the **provisioning** profile, out of scope; leave them). If any **build-profile** fixture appears, change it to a plain label (e.g. `linux-6.9`) in the same commit.

- [ ] **Step 7: Run guardrails.** `just lint && just type && uv run python -m pytest tests/profiles tests/mcp/lifecycle -q`.

- [ ] **Step 8: Commit.**
```bash
git add src/kdive/profiles/build.py tests/profiles/test_build.py tests/mcp/lifecycle/
git commit -m "feat(build): reject bare git-URL kernel_source_ref (#778)"
```

---

### Task 4: Surface the self-service env path from the worker-local git rejection

**Files:**
- Modify: the worker-local non-allowlisted-remote rejection message (find via `rg -n "not allowlisted\|allowlist" src/kdive/providers/shared/build_host/ src/kdive/services/runs/`)
- Test: the existing test asserting that rejection message

**Interfaces:**
- Produces: the `configuration_error` for a non-allowlisted remote on `worker-local` names `build_envs.list` / "select an isolated build environment" in addition to the allowlist guidance.

- [ ] **Step 1: Locate the message + its test.** `rg -n "allowlist" src/kdive/providers/shared/build_host/configuration/git_source.py` and find the rejection string; grep the test asserting it.

- [ ] **Step 2: Update the test first.** Add an assertion that the rejection detail contains `build_envs.list` (and keep the existing allowlist-guidance assertion).

- [ ] **Step 3: Run, expect FAIL.** `uv run python -m pytest <that test> -q`.

- [ ] **Step 4: Extend the message.** Append to the rejection detail: ` or select an isolated build environment from build_envs.list to clone any remote`. Do not echo the submitted remote (keep the no-leak guarantee).

- [ ] **Step 5: Run, expect PASS** + `just lint && just type`.

- [ ] **Step 6: Commit.**
```bash
git add -A
git commit -m "feat(build): name build_envs.list in the worker-local clone rejection (#778)"
```

---

### Task 5: Build provenance — capture the resolved commit on both clone paths

**Files:**
- Modify: `src/kdive/providers/shared/build_host/transports/shell_transport.py` (`clone` → return resolved commit)
- Modify: callers of `.clone(` (find via `rg -n "\.clone\(" src/kdive/providers/shared/build_host/`) + `src/kdive/build_artifacts/results.py` (`BuildOutput` → carry provenance) + `src/kdive/providers/shared/build_host/workspaces/workspace.py` (local lane: return its existing `FETCH_HEAD` rev-parse output)
- Modify: `src/kdive/providers/shared/build_host/configuration/git_source.py` (`strip_userinfo` helper)
- Test: `tests/providers/build_host/test_transport_seams.py`, `tests/providers/.../test_build*.py`, `tests/providers/.../test_git_source.py`

**Interfaces:**
- Produces: `BuildOutput` (`build_artifacts/results.py`) carries `build_provenance: dict[str, str] | None` with keys `{remote, ref, resolved_commit, build_host}` (remote userinfo-stripped). Task 6 reads it back through `BuildStepResult`.
- **Note (the success channel, not the failure one):** a *succeeded* build records via `BuildStepResult` (`services/runs/steps.py:44-78`, `load`/`dump`), written by `finalize_build` (`jobs/handlers/runs_shared.py:19-26`) from the `BuildStepResult(...)` constructed in `jobs/handlers/runs_build.py:~325`, and read back by `existing_build_result()` (`steps.py:92`). Do **not** use the ADR-0238 `build_log_ref` failure-context path — that fires only on failure.

- [ ] **Step 1: Write the failing transport test.** Assert `ShellTransport.clone(remote, ref, dest)` returns the resolved commit SHA (a fake `_run_remote` returns a canned SHA for `rev-parse HEAD`).

- [ ] **Step 2: Run, expect FAIL** (currently returns `None`).

- [ ] **Step 3: Implement.** Change `clone(...) -> str`; after the successful `checkout FETCH_HEAD`, add:
```python
        head = self._run_remote(
            ["git", "-C", dest, "rev-parse", "HEAD"], cwd="/", timeout_s=_CLONE_TIMEOUT_S
        )
        if head.returncode != 0:
            raise CategorizedError(
                "git rev-parse HEAD failed on remote",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"stderr": redacted_tail(head.stderr, self._secret_registry)},
            )
        return head.stdout.strip()
```
Update the docstring `Returns:`. Update every `.clone(` caller to capture the returned SHA.

- [ ] **Step 4: Add a `strip_userinfo(remote: str) -> str` helper** beside `parse_remote` in `configuration/git_source.py` (drops any `…@` userinfo from a URL form; returns scp-style/label forms unchanged) with unit tests (`https://u:p@h/r` → `https://h/r`; `https://h/r` unchanged; `linux-6.9` unchanged).

- [ ] **Step 5: Thread provenance through the success channel.** (a) Add `build_provenance: dict[str, str] | None = None` to `BuildOutput` (`build_artifacts/results.py`); the builder/transport dispatch populates it `{remote: strip_userinfo(remote), ref, resolved_commit, build_host}` for a git source, and for warm-tree best-effort `rev-parse HEAD` of the staged tree (`{label, resolved_commit?}`), wrapped in try/except so capture failure degrades and never fails the build. (b) Add a `build_provenance: dict[str, str] | None = None` field to `BuildStepResult` (`services/runs/steps.py:44-78`) and round-trip it in `load` (read `result.get("build_provenance")` when it is a `Mapping[str,str]`) and `dump` (emit when not `None`). (c) In `jobs/handlers/runs_build.py:~295-325`, where the `BuildStepResult(...)` is constructed from the `BuildOutput`, pass `build_provenance=output.build_provenance`. `finalize_build` (`runs_shared.py:19-26`) already persists `result.dump()`, so no change there.

- [ ] **Step 6: Tests.** Transport returns SHA; git build records full provenance; warm-tree degrades to `{label}`; a forced rev-parse failure does not fail the build; `strip_userinfo` drops credentials.

- [ ] **Step 7: Guardrails + commit.**
```bash
just lint && just type && uv run python -m pytest tests/providers/build_host tests/providers -q
git add -A
git commit -m "feat(build): capture resolved-commit provenance on both clone paths (#778)"
```

---

### Task 6: Surface `data.build_provenance` on `runs.get`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py` (envelope) — reads `BuildStepResult.build_provenance` via `existing_build_result()`/`step_progress`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: `BuildStepResult.build_provenance` (Task 5) via `existing_build_result(conn, run_id)` (`services/runs/steps.py:92`).
- Produces: `runs.get` `data.build_provenance` present when the build recorded it.

- [ ] **Step 1: Write the failing test.** A Run whose build recorded provenance → `runs.get` envelope `data["build_provenance"] == {"remote": ..., "ref": ..., "resolved_commit": ..., "build_host": ...}`; absent when no provenance recorded.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** In `common.py`'s SUCCEEDED-run envelope, call `existing_build_result(conn, run.id)` (the same reader used at `steps.py:202/214/252`) and add `data["build_provenance"] = result.build_provenance` when it is not `None` (the envelope `data` is free-form, #565 — no outputSchema change). Omit the key when `None`. This is the success channel — not the failure-only `build_log_ref` path.

- [ ] **Step 4: Run, expect PASS** + `just lint && just type`.

- [ ] **Step 5: Audit closed-key-set assertions.** Grep `test_runs_tools.py` for exact `data == {...}` assertions on the SUCCEEDED build path and update any that would break when `build_provenance` appears.

- [ ] **Step 6: Commit.**
```bash
git add -A
git commit -m "feat(runs): surface data.build_provenance on runs.get (#778)"
```

---

### Task 7: Documentation — `build-source-staging.md`

**Files:**
- Modify: `src/kdive/mcp/resources/_content/build-source-staging.md`
- Verify: `just docs-check`, `just resources-docs-check`, `just docs-links`, `just docs-paths`

- [ ] **Step 1: Rewrite the phantom-guard paragraph (lines 21-25)** to the true, now-implemented behavior: a bare string is warm-tree provenance; a bare string that looks like a git clone-URL (`git:`/`git://`/`git+ssh://`/`ssh://`/`http(s)://`) is **rejected** at the build-profile boundary pointing at the structured `{"git": {...}}` form and `build_envs.list`; `git+https://`/`file://`/scp-style stay labels. Add that a git build clones the structured remote on a build environment selected via `build_host` (discoverable with `build_envs.list`) and that `runs.get` reports `data.build_provenance`.

- [ ] **Step 2: Run doc guardrails.** `just docs-check && just resources-docs-check && just docs-links && just docs-paths`. Fix any generated-resource diff (`just` recipe may regenerate an index).

- [ ] **Step 3: Commit.**
```bash
git add src/kdive/mcp/resources/_content/build-source-staging.md
git commit -m "docs(build): align build-source-staging with the implemented guard + envs (#778)"
```

---

### Task 8: Security review of the discovery exposure + full CI

- [ ] **Step 1: Run `security-review`** on the branch, focused on `build_envs.list` (Task 2): confirm the projection carries no infra/secret detail (no address/credential/volume) and that `contributor` visibility of env name/kind/toolchain prose is acceptable. Address any finding.
- [ ] **Step 2: Run the full local gate.** `just ci`. Fix anything red (format, type, lint-shell, check-mermaid, docs-checks, tests).
- [ ] **Step 3: Confirm clean tree + all commits present.** `git status --short` empty; `git log --oneline origin/main..HEAD`.

---

## Self-review notes

- **Spec coverage:** discovery (T2), descriptor+migration (T1), selection reuse (existing `build_host` + T4 message), bare-URL guard (T3), provenance both paths (T5) + surfacing (T6), trust gate unchanged (no task — verified), build-log inherited (no task), docs (T7), security-review on discovery (T8). All spec sections map to a task.
- **Fixture migration:** the only rejected-scheme bare values (`git:abc123`) are **provisioning**-profile fixtures, out of scope for the build-profile guard — so the guard breaks zero build fixtures (confirmed by grep in T3 Step 6).
- **Type consistency:** `toolchain_desc: str | None` used consistently (T1 model, T2 projection); `build_provenance` dict keys `{remote, ref, resolved_commit, build_host}` identical in T5/T6; `_match_uri_scheme`/`_URI_SCHEME_PREFIXES` defined once (T3).
