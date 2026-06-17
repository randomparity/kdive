# Local warm-tree build admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject a `LOCAL` warm-tree build with an empty/invalid `KDIVE_KERNEL_SRC` at the worker's build-dispatch `LOCAL` branch — before any workspace side effect — reusing `sync_tree`'s existing predicate and messages, and give the demo a documented one-step bootstrap.

**Architecture:** Factor `sync_tree`'s leading emptiness/usability guard into a shared pure predicate in `providers/shared/build_host/workspace.py`. Add a thin admission helper in `services/runs/build_host_selection.py` (beside ADR-0157's `check_source_kind_compatibility`) that raises the existing messages for a `LOCAL` host. The worker BUILD handler reads `config.get(KERNEL_SRC)` once and threads it through `_run_build` → `run_build_on_host`, which calls the helper at the top of its `if host.kind is BuildHostKind.LOCAL` branch. `sync_tree` keeps its own check as a backstop (it now calls the shared predicate). Demo path is a doc section + commented compose stanza — no kernel bytes committed.

**Tech Stack:** Python 3.13, `uv`/`ruff`/`ty`/`pytest`, `just` recipes. Conventions: `AGENTS.md`, `CLAUDE.md`. ADR-0158, spec `docs/specs/2026-06-17-local-warm-tree-build-admission.md`.

---

## Guardrails (run before every commit)

- Focused test: `uv run python -m pytest <path>::<test> -q`
- `just lint` (ruff check + format check) — fix all.
- `just type` (ty, whole tree) — fix all.
- Doc scripts under modern bash: `/opt/homebrew/bin/bash scripts/check-doc-links.sh`; `just adr-status-check`. (macOS system bash 3.2 lacks `mapfile`; CI uses GNU bash — local link/paths checks need bash 5.)
- Before first push: `just ci` (full gate). DB tests skip without Docker; that is expected locally.
- Conventional commit, ≤72-char imperative subject, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- Modify `src/kdive/providers/shared/build_host/workspace.py` — add `warm_tree_source_error(kernel_src) -> str | None`; `sync_tree` calls it. Owns the two message constants (unchanged).
- Modify `src/kdive/services/runs/build_host_selection.py` — add `check_warm_tree_source_admission(kernel_src, *, host_kind)`.
- Modify `src/kdive/providers/shared/build_host/dispatch.py` — `run_build_on_host` gains `kernel_src: str` kwarg; calls the helper in the `LOCAL` branch.
- Modify `src/kdive/jobs/handlers/runs.py` — `_build_and_record` reads `config.get(KERNEL_SRC)`; `_run_build` forwards it; passes to `run_build_on_host`.
- Modify `docs/operating/build-source-staging.md` — demo/compose bootstrap subsection.
- Modify `docker-compose.yml` — commented `KDIVE_KERNEL_SRC` env + bind-mount in `worker`.
- Tests: `tests/providers/build_host/test_workspace_predicate.py` (new), `tests/services/test_build_host_selection.py` (extend), `tests/providers/build_host/test_dispatch_admission.py` (new).

---

## Task 1: Factor `sync_tree`'s guard into a shared predicate

**Files:**
- Modify: `src/kdive/providers/shared/build_host/workspace.py:183-197`
- Test: `tests/providers/build_host/test_workspace_predicate.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/providers/build_host/test_workspace_predicate.py`:

```python
"""warm_tree_source_error predicate: the single source of the unset/invalid rule."""

from __future__ import annotations

import pytest

from kdive.providers.shared.build_host.workspace import (
    KERNEL_SRC_INVALID_DETAIL,
    KERNEL_SRC_UNSET_DETAIL,
    warm_tree_source_error,
)


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_unset_or_whitespace_returns_unset_detail(value: str) -> None:
    assert warm_tree_source_error(value) == KERNEL_SRC_UNSET_DETAIL


@pytest.mark.parametrize("value", ["relative/path", "/", "/does/not/exist/kdive-xyz"])
def test_present_but_unusable_returns_invalid_detail(value: str) -> None:
    assert warm_tree_source_error(value) == KERNEL_SRC_INVALID_DETAIL


def test_usable_absolute_dir_returns_none(tmp_path) -> None:
    assert warm_tree_source_error(str(tmp_path)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/build_host/test_workspace_predicate.py -q`
Expected: FAIL with `ImportError: cannot import name 'warm_tree_source_error'`.

- [ ] **Step 3: Add the predicate and route `sync_tree` through it**

In `src/kdive/providers/shared/build_host/workspace.py`, add after the two message constants (after line 50):

```python
def warm_tree_source_error(kernel_src: str) -> str | None:
    """Return the offending message for an unusable warm-tree source, or ``None``.

    The single definition of the warm-tree ``KDIVE_KERNEL_SRC`` rule, shared by
    ``sync_tree`` (build-time backstop) and the admission helper
    (``check_warm_tree_source_admission``). Empty/whitespace is "unset"; a present
    value that is not an absolute path to an existing directory is "invalid".

    Args:
        kernel_src: The resolved ``KDIVE_KERNEL_SRC`` value.

    Returns:
        ``KERNEL_SRC_UNSET_DETAIL``, ``KERNEL_SRC_INVALID_DETAIL``, or ``None`` when
        the value is a usable absolute directory.
    """
    if not kernel_src.strip():
        return KERNEL_SRC_UNSET_DETAIL
    source = Path(kernel_src)
    if not source.is_absolute() or source == source.parent or not source.is_dir():
        return KERNEL_SRC_INVALID_DETAIL
    return None
```

Then replace the leading guard in `sync_tree` (currently lines 187-197):

```python
def sync_tree(
    kernel_src: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Mirror the warm kernel source tree into ``workspace`` with ``rsync -a --delete``."""
    detail = warm_tree_source_error(kernel_src)
    if detail is not None:
        raise CategorizedError(detail, category=ErrorCategory.CONFIGURATION_ERROR)
    source = Path(kernel_src)
    if shutil.which("rsync") is None:
```

(Delete the old `if not kernel_src.strip(): ...` and `if not source.is_absolute() ...` blocks; keep everything from `if shutil.which("rsync")` onward. `source` is still needed below for the rsync argv.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/build_host/test_workspace_predicate.py tests/providers/local_libvirt/test_build.py -q`
Expected: PASS (new predicate tests + the existing `sync_tree` unset/invalid tests still green — the backstop is unchanged behaviorally).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/shared/build_host/workspace.py tests/providers/build_host/test_workspace_predicate.py
git commit -m "refactor(build): extract warm_tree_source_error predicate

Single-source the KDIVE_KERNEL_SRC unset/invalid rule sync_tree owns so
the upcoming admission check reuses it without copying a message string.

Refs #532

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Add the admission helper in `build_host_selection.py`

**Files:**
- Modify: `src/kdive/services/runs/build_host_selection.py` (add helper after `check_source_kind_compatibility`, ends line 63)
- Test: `tests/services/test_build_host_selection.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_build_host_selection.py`:

```python
def test_warm_tree_admission_rejects_empty_for_local() -> None:
    from kdive.providers.shared.build_host.workspace import KERNEL_SRC_UNSET_DETAIL

    with pytest.raises(CategorizedError) as excinfo:
        build_host_selection.check_warm_tree_source_admission(
            "", host_kind=BuildHostKind.LOCAL
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == KERNEL_SRC_UNSET_DETAIL


def test_warm_tree_admission_rejects_invalid_for_local() -> None:
    from kdive.providers.shared.build_host.workspace import KERNEL_SRC_INVALID_DETAIL

    with pytest.raises(CategorizedError) as excinfo:
        build_host_selection.check_warm_tree_source_admission(
            "relative/path", host_kind=BuildHostKind.LOCAL
        )
    assert str(excinfo.value) == KERNEL_SRC_INVALID_DETAIL


def test_warm_tree_admission_admits_usable_local(tmp_path) -> None:
    build_host_selection.check_warm_tree_source_admission(
        str(tmp_path), host_kind=BuildHostKind.LOCAL
    )  # no raise


@pytest.mark.parametrize(
    "kind", [BuildHostKind.SSH, BuildHostKind.EPHEMERAL_LIBVIRT]
)
def test_warm_tree_admission_noop_for_non_local(kind: BuildHostKind) -> None:
    build_host_selection.check_warm_tree_source_admission("", host_kind=kind)  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/services/test_build_host_selection.py -k warm_tree_admission -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'check_warm_tree_source_admission'`.

- [ ] **Step 3: Add the helper**

In `src/kdive/services/runs/build_host_selection.py`, add the import near the top imports:

```python
from kdive.providers.shared.build_host.workspace import warm_tree_source_error
```

Add after `check_source_kind_compatibility` (after line 63):

```python
def check_warm_tree_source_admission(
    kernel_src: str, *, host_kind: BuildHostKind
) -> None:
    """Reject a LOCAL warm-tree build whose ``KDIVE_KERNEL_SRC`` is unset or unusable.

    A no-op for any non-``LOCAL`` host kind (git/remote lanes never read
    ``KDIVE_KERNEL_SRC``). For a ``LOCAL`` host this applies the same predicate
    ``sync_tree`` applies (``warm_tree_source_error``) and raises the identical
    ``KERNEL_SRC_UNSET_DETAIL`` / ``KERNEL_SRC_INVALID_DETAIL`` (ADR-0158), so an
    admission rejection is byte-identical to the build-time backstop. The worker BUILD
    handler calls this at the dispatch ``LOCAL`` branch before any workspace side
    effect; ``sync_tree`` keeps its own check as defense-in-depth.

    Args:
        kernel_src: The worker's resolved ``KDIVE_KERNEL_SRC`` value.
        host_kind: The resolved build host's transport kind.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``host_kind`` is ``LOCAL`` and
            ``kernel_src`` is empty or not an absolute path to an existing directory.
    """
    if host_kind is not BuildHostKind.LOCAL:
        return
    detail = warm_tree_source_error(kernel_src)
    if detail is not None:
        raise CategorizedError(detail, category=ErrorCategory.CONFIGURATION_ERROR)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/services/test_build_host_selection.py -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/services/runs/build_host_selection.py tests/services/test_build_host_selection.py
git commit -m "feat(runs): add warm-tree KDIVE_KERNEL_SRC admission helper

A LOCAL-only check that reuses sync_tree's predicate/messages; no-op for
remote hosts. Sits beside check_source_kind_compatibility (ADR-0157).

Refs #532

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Call the helper in the dispatch `LOCAL` branch

**Files:**
- Modify: `src/kdive/providers/shared/build_host/dispatch.py:45-56`
- Test: `tests/providers/build_host/test_dispatch_admission.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/providers/build_host/test_dispatch_admission.py`:

```python
"""run_build_on_host admits a LOCAL warm-tree build only when KDIVE_KERNEL_SRC is usable."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.dispatch import run_build_on_host
from kdive.providers.shared.build_host.workspace import KERNEL_SRC_UNSET_DETAIL
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN_ID = UUID("00000000-0000-0000-0000-0000000000d1")

_WARM_PROFILE = {
    "schema_version": 1,
    "kernel_source_ref": "linux-6.9",
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
}


def _local_host() -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-0000000000d2"),
        name="worker-local",
        kind=BuildHostKind.LOCAL,
        address=None,
        ssh_credential_ref=None,
        base_image_volume=None,
        workspace_root="/build",
        max_concurrent=1,
        enabled=True,
        state=BuildHostState.READY,
    )


class _RecordingBuilder:
    def __init__(self) -> None:
        self.called = False

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        self.called = True
        return BuildOutput(kernel_ref="k", debuginfo_ref="d", build_id="b")


def _parsed() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_WARM_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def test_empty_kernel_src_rejected_before_builder_runs() -> None:
    builder = _RecordingBuilder()
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            run_build_on_host(
                builder,
                _local_host(),
                _RUN_ID,
                _parsed(),
                secret_registry=SecretRegistry(),
                kernel_src="",
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == KERNEL_SRC_UNSET_DETAIL
    assert builder.called is False


def test_usable_kernel_src_runs_builder(tmp_path) -> None:
    builder = _RecordingBuilder()
    out = asyncio.run(
        run_build_on_host(
            builder,
            _local_host(),
            _RUN_ID,
            _parsed(),
            secret_registry=SecretRegistry(),
            kernel_src=str(tmp_path),
        )
    )
    assert builder.called is True
    assert out.kernel_ref == "k"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/build_host/test_dispatch_admission.py -q`
Expected: FAIL — `run_build_on_host` has no `kernel_src` kwarg (`TypeError: unexpected keyword argument 'kernel_src'`).

- [ ] **Step 3: Thread `kernel_src` into `run_build_on_host` and call the helper**

In `src/kdive/providers/shared/build_host/dispatch.py`, add the import:

```python
from kdive.services.runs.build_host_selection import check_warm_tree_source_admission
```

Change `run_build_on_host`'s signature and the LOCAL branch (lines 45-56):

```python
async def run_build_on_host(
    builder: Builder,
    host: BuildHost,
    run_id: UUID,
    parsed: ServerBuildProfile,
    *,
    secret_registry: SecretRegistry,
    kernel_src: str,
    transport_factories: BuildHostTransportFactories | None = None,
) -> BuildOutput:
    """Run ``builder`` on the selected build host.

    For a ``LOCAL`` host the warm-tree ``KDIVE_KERNEL_SRC`` (``kernel_src``, resolved by
    the worker BUILD handler) is admitted before the build runs (ADR-0158), so an
    unset/invalid tree fails before any workspace side effect; ``sync_tree`` keeps the
    backstop. ``kernel_src`` is ignored for non-``LOCAL`` (git/remote) hosts.
    """
    if host.kind is BuildHostKind.LOCAL:
        check_warm_tree_source_admission(kernel_src, host_kind=host.kind)
        return await asyncio.to_thread(builder.build, run_id, parsed)
```

(Leave the rest of the function unchanged.)

> If `ty` flags a circular import (`services.runs.build_host_selection` ← `providers.shared.build_host.dispatch`), import inside the function body instead of at module top. Verify with `just type`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/build_host/test_dispatch_admission.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/shared/build_host/dispatch.py tests/providers/build_host/test_dispatch_admission.py
git commit -m "feat(build): admit local warm-tree build at the dispatch LOCAL branch

run_build_on_host now takes kernel_src and rejects an unset/invalid
KDIVE_KERNEL_SRC for a LOCAL host before builder.build runs (ADR-0158).

Refs #532

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Read `KDIVE_KERNEL_SRC` in the worker BUILD handler and forward it

**Files:**
- Modify: `src/kdive/jobs/handlers/runs.py:127-147` (`_run_build`), `:248-289` (`_build_and_record`)
- Test: `tests/jobs/handlers/test_build_handler_transport.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/jobs/handlers/test_build_handler_transport.py` a test that seeds a worker-local running run and asserts an empty `KDIVE_KERNEL_SRC` fails the BUILD job with `CONFIGURATION_ERROR` and `KERNEL_SRC_UNSET_DETAIL`, without the builder producing artifacts. Mirror the existing local-dispatch test in this file (it already seeds `build_hosts`/run rows and substitutes the runtime builder via `provider_resolver`); set the env with `monkeypatch.setenv("KDIVE_KERNEL_SRC", "")` and `config.reset()` so the handler's `config.get` re-reads it. Reuse the file's existing `_WARM`/local-profile seed helper if present; otherwise seed a `ServerBuildProfile` warm profile (`kernel_source_ref="linux-6.9"`). Assert the run reaches `FAILED` and the recorded reason category is `CONFIGURATION_ERROR`.

> The exact fixture wiring lives in this file already (see its local-host dispatch test). Match its `provider_resolver`, `seed_running_run`, and `BuildPayload` usage; do not introduce a new harness.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/test_build_handler_transport.py -k kernel_src -q`
Expected: FAIL — currently the empty value is caught later (in `sync_tree`) or the call errors because `run_build_on_host` is invoked without `kernel_src`.

- [ ] **Step 3: Read and forward `kernel_src`**

In `src/kdive/jobs/handlers/runs.py`, add the import:

```python
from kdive.config.core_settings import KERNEL_SRC
```
and ensure `import kdive.config as config` is present (add if missing).

In `_build_and_record` (line 270-280 region), read once and pass to `_run_build`:

```python
        host = await _resolve_build_host(conn, payload, run_id)
        kernel_src = config.get(KERNEL_SRC) or ""
        output = await _run_build(
            conn,
            run,
            parsed,
            host=host,
            resolver=resolver,
            secret_registry=secret_registry,
            kernel_src=kernel_src,
            transport_factories=transport_factories,
        )
```

In `_run_build` (lines 127-147), add the `kernel_src: str` keyword param and forward it:

```python
async def _run_build(
    conn: AsyncConnection,
    run: Run,
    parsed: ServerBuildProfile,
    *,
    host: BuildHost,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    kernel_src: str,
    transport_factories: BuildHostTransportFactories | None = None,
) -> BuildOutput:
    """Resolve the runtime builder and run it on ``host`` through the build-host seam."""
    run_id = run.id
    builder = (await _run_runtime(conn, run_id, resolver)).builder
    return await run_build_on_host(
        builder,
        host,
        run_id,
        parsed,
        secret_registry=secret_registry,
        kernel_src=kernel_src,
        transport_factories=transport_factories,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/jobs/handlers/test_build_handler_transport.py -q`
Expected: PASS (new + existing local/remote dispatch tests).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/jobs/handlers/runs.py tests/jobs/handlers/test_build_handler_transport.py
git commit -m "feat(jobs): read KDIVE_KERNEL_SRC once and pass to build dispatch

The worker BUILD handler resolves KDIVE_KERNEL_SRC and threads it into
run_build_on_host so the LOCAL admission check fires before the build job
materializes a workspace (ADR-0158). dispatch/workspace stay config-free.

Refs #532

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Document the demo bootstrap + commented compose stanza

**Files:**
- Modify: `docs/operating/build-source-staging.md`
- Modify: `docker-compose.yml:161-186` (`worker` service)

- [ ] **Step 1: Add the doc subsection**

In `docs/operating/build-source-staging.md`, after the "Warm-tree lane: stage `KDIVE_KERNEL_SRC`" section, add:

```markdown
### Demo / compose bootstrap (one step)

The bundled `docker-compose.yml` does not stage a kernel tree (none is shipped — a
buildable tree is hundreds of MB and version/licence-coupled). To make `worker-local`
buildable in the compose demo, bind-mount a buildable tree into the `worker` service
and point `KDIVE_KERNEL_SRC` at the mount:

1. Have a buildable kernel tree on the host, e.g. `~/src/linux` (a git checkout or an
   unpacked tarball — not a bare repo).
2. In `docker-compose.yml`'s `worker` service, uncomment the two lines the file marks
   for this (a `KDIVE_KERNEL_SRC` env entry and a read-only bind-mount), and set the
   host path to your tree:

   ```yaml
   worker:
     environment:
       KDIVE_KERNEL_SRC: /srv/linux
     volumes:
       - ~/src/linux:/srv/linux:ro
   ```
3. `docker compose up -d worker` (or restart it) so it reads the value.

With an empty/unset `KDIVE_KERNEL_SRC`, a warm-tree `runs.build` against `worker-local`
is now rejected at admission (before the build job materializes a workspace) with the
`KDIVE_KERNEL_SRC is not set on the build worker` configuration error, rather than
failing deep in the build (ADR-0158).
```

- [ ] **Step 2: Add the commented compose stanza**

In `docker-compose.yml`'s `worker` service, under `environment:` add a commented line, and under `volumes:` add a commented bind-mount:

```yaml
    environment:
      <<: *backends
      KDIVE_HEALTH_BIND_ADDR: 0.0.0.0:9465
      # Demo warm-tree build bootstrap (see docs/operating/build-source-staging.md):
      # uncomment and set to the in-container mount path of a buildable kernel tree.
      # KDIVE_KERNEL_SRC: /srv/linux
    ...
    volumes:
      - kdive-build:/var/lib/kdive/build
      - kdive-install:/var/lib/kdive/install
      # Demo warm-tree build bootstrap: bind-mount a host kernel tree read-only.
      # - ~/src/linux:/srv/linux:ro
```

- [ ] **Step 3: Run doc guardrails**

Run: `/opt/homebrew/bin/bash scripts/check-doc-links.sh && just adr-status-check`
Expected: `markdown links resolve`; ADR guard clean.
Also confirm no banned doc-style words ("critical", "robust", "comprehensive", "elegant", "Sprint") were introduced: `rg -n -i "critical|robust|comprehensive|elegant|sprint" docs/operating/build-source-staging.md` — review any hit.

- [ ] **Step 4: Commit**

```bash
git add docs/operating/build-source-staging.md docker-compose.yml
git commit -m "docs: document the demo warm-tree build bootstrap

Add a one-step compose bootstrap (bind-mount a kernel tree + set
KDIVE_KERNEL_SRC) and a commented worker stanza, so worker-local is
buildable on a fresh demo without bundling a kernel tree (ADR-0158).

Refs #532

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Ratify ADR-0158 and full-suite verification

**Files:**
- Modify: `docs/adr/0158-local-warm-tree-build-admission.md` (Status → Accepted)
- Modify: `docs/adr/README.md` (0158 row → Accepted)

- [ ] **Step 1: Flip ADR status to Accepted**

ADR-0158 is now cited in `src/` (the helper docstring references it), so the
`adr-status-check` "no shipped-but-Proposed drift" invariant requires Accepted. In
`docs/adr/0158-local-warm-tree-build-admission.md` change `- **Status:** Proposed` to
`- **Status:** Accepted`. In `docs/adr/README.md` change the trailing `| Proposed |`
on the 0158 row to `| Accepted |`.

- [ ] **Step 2: Run the ADR guard**

Run: `just adr-status-check`
Expected: clean (`index in sync, no shipped-but-Proposed drift`).

- [ ] **Step 3: Full local gate**

Run: `just lint && just type && uv run python -m pytest tests/providers/build_host tests/services/test_build_host_selection.py tests/jobs/handlers/test_build_handler_transport.py tests/providers/local_libvirt/test_build.py -q`
Then the full gate: `just ci` (DB tests skip without Docker — expected).
Expected: all green (or only Docker-gated skips).

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0158-local-warm-tree-build-admission.md docs/adr/README.md
git commit -m "docs: ratify ADR-0158 as Accepted on implementation

Refs #532

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review checklist (run before handing to executor)

- Spec AC#1 → Task 3 (recording builder asserts `called is False`). AC#2/#3 → Task 1 + Task 2 (whitespace/invalid). AC#4 → Task 2/3 (usable admits). AC#5 → Task 2 (non-LOCAL no-op). AC#6 → Task 1 Step 4 (existing `sync_tree` tests stay green). AC#7 → Task 1 (single predicate) + grep that `dispatch.py`/`workspace.py` import no config registry. AC#8 → Task 4 (handler test asserts FAILED + no stranded lease; LOCAL holds none). AC#9 → Task 5.
- No placeholders except the Task 4 test body, which is intentionally described-not-coded because it must match an existing in-file harness (`provider_resolver`/`seed_running_run`); the executor copies the file's existing local-dispatch test and changes the env + assertion. Flagged explicitly.
- Type consistency: `check_warm_tree_source_admission(kernel_src, *, host_kind)`, `warm_tree_source_error(kernel_src) -> str | None`, `run_build_on_host(..., kernel_src: str, ...)`, `_run_build(..., kernel_src: str, ...)` — names match across tasks.
- Circular-import risk (dispatch importing build_host_selection, which imports workspace) is flagged in Task 3 Step 3 with a function-local-import fallback.
