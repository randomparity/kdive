# Root-build privilege-drop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the kdive worker runs as root, run every local kernel-build subprocess (git clone, `make`, config merge, patch) demoted to an unprivileged `KDIVE_BUILD_USER` account, never as root — deny-by-default (a root worker with no build user fails the BUILD job closed).

**Architecture:** A new `BuildSandbox` value object carries the demotion identity (uid/gid/groups/home). A memoized `SandboxProvider` resolves it once per build, keyed on `os.geteuid()` and `KDIVE_BUILD_USER`, fail-closed when root-without-user. The worker-local build seams (`execution.py` run-steps, `workspace.py` checkout) route every demotable `subprocess.run` through a `sandbox_run` chokepoint that passes the child-side `user=/group=/extra_groups=/umask=` and a build-user `env` only when a sandbox is active. The workspace is handed to the build user (chown / `rsync --chown`) before demoted writes. Remote/SSH build hosts (already isolated) and `objcopy` (trusted read) are untouched.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `subprocess` (child-side `user=`/`group=`), `pwd`/`os.getgrouplist`. Tooling via `just` recipes.

## Global Constraints

- Spec: `docs/design/root-build-privilege-drop.md`; ADR: `docs/adr/0214-root-build-privilege-drop.md`. (Both already on the branch.)
- ADR-0214 already **Accepted**; do not re-litigate its rejected alternatives.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree, src+tests). Absolute imports only.
- Error taxonomy: pick the most specific existing `ErrorCategory` (`domain/errors.py`); fail-closed cases are `CONFIGURATION_ERROR`.
- No KDIVE_* env read outside `kdive.config` (ADR-0087, `just config-guard`). Read `KDIVE_BUILD_USER` via `config.get(BUILD_USER)`.
- The real setuid demotion is exercised only under the `live_vm` marker; unit tests assert the resolution table + kwarg/env assembly with `os.geteuid`/`pwd` patched and a fake subprocess runner — they must NOT spawn a setuid subprocess.
- Guardrails before every commit: `just lint`, `just type`, and the focused tests for the files touched. Doc/config tasks additionally run `just config-docs-check`, `just config-guard`, `just env-docs-check`, `just resources-docs-check`, `just adr-status-check`.
- Conventional-commit subjects ≤72 chars, imperative; end every commit with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- **Create** `src/kdive/providers/shared/build_host/sandbox.py` — `BuildSandbox`, `sandbox_run`, `SandboxProvider`, `resolve_build_sandbox_provider`, `_resolve_sandbox`. One responsibility: the privilege-drop primitive + its resolution.
- **Create** `tests/providers/shared/build_host/test_sandbox.py` — sandbox unit tests.
- **Modify** `src/kdive/config/core_settings.py` — declare + register `KDIVE_BUILD_USER`.
- **Modify** `src/kdive/providers/shared/build_host/execution.py` — thread `sandbox` through `real_run_make`, `run_make_target`, `real_run_olddefconfig`, `real_run_modules_install`.
- **Modify** `src/kdive/providers/shared/build_host/workspaces/workspace.py` — thread `sandbox` through `real_checkout`, `make_checkout`, `clone_tree`, `sync_tree`, `merge_config`, `apply_patch`, `_run_git`; add `_write_fragment` helper; chown handoffs.
- **Modify** `src/kdive/providers/local_libvirt/build.py` — `from_env` builds the provider and threads it into `make_checkout` + run-step seams; `__init__`/`_maybe_publish_modules` chown the modules-staging dir.
- **Modify** tests: `tests/config/test_manifest_completeness.py`, `tests/providers/local_libvirt/test_build.py`, and the build-host workspace/execution tests as needed.
- **Modify** `docs/operating/build-source-staging.md` — `KDIVE_BUILD_USER`, fail-closed behavior, operator prereqs; regenerate the packaged snapshot + config reference.

---

## Task 1: `KDIVE_BUILD_USER` setting + `BuildSandbox` value object

**Files:**
- Modify: `src/kdive/config/core_settings.py` (after `LOCAL_BUILD_REMOTE_ALLOWLIST`, ~line 322 + registry list ~line 588)
- Create: `src/kdive/providers/shared/build_host/sandbox.py`
- Create: `tests/providers/shared/build_host/test_sandbox.py`
- Modify: `tests/config/test_manifest_completeness.py`

**Interfaces:**
- Produces: `BUILD_USER: Setting[str]`; `BuildSandbox(uid:int, gid:int, extra_groups:tuple[int,...], user_name:str, home:str, umask:int=0o077)` with `.run(argv, *, env=None, **kwargs) -> CompletedProcess`, `.own(path) -> None`, `._child_env(env) -> dict`; `sandbox_run(sandbox: BuildSandbox | None, argv, **kwargs) -> CompletedProcess`.

- [ ] **Step 1: Register the setting.** In `core_settings.py`, add after the `LOCAL_BUILD_REMOTE_ALLOWLIST` definition:

```python
BUILD_USER = Setting(
    name="KDIVE_BUILD_USER",
    parse=_str,
    group="build",
    processes=_WORKER,
    help=(
        "Name of an unprivileged passwd account the worker drops to for local kernel "
        "builds (git clone + make) when it runs as root. Empty/unset: a root worker "
        "refuses the local build lane (deny by default); a non-root worker ignores it."
    ),
)
```

Add `BUILD_USER,` to the registry tuple immediately after `LOCAL_BUILD_REMOTE_ALLOWLIST,`.

- [ ] **Step 2: Write the failing manifest test.** In `tests/config/test_manifest_completeness.py` add:

```python
def test_build_user_setting_registered() -> None:
    # Root-worker build privilege drop (#689, ADR-0214): worker-scoped, build group.
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_BUILD_USER" in names
```

Run: `uv run python -m pytest tests/config/test_manifest_completeness.py::test_build_user_setting_registered -q` → Expected: PASS (the setting was registered in Step 1). If it FAILS with KeyError/missing, the registry edit was missed.

- [ ] **Step 3: Write the failing `BuildSandbox` tests.** Create `tests/providers/shared/build_host/test_sandbox.py`:

```python
"""Build-subprocess privilege-drop primitive (ADR-0214)."""

from __future__ import annotations

import pytest

from kdive.providers.shared.build_host import sandbox as sb


def _sandbox() -> sb.BuildSandbox:
    return sb.BuildSandbox(
        uid=1000, gid=1000, extra_groups=(1000, 100), user_name="builder", home="/home/builder"
    )


def test_run_assembles_demotion_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    _sandbox().run(["make"], cwd="/ws", check=False)
    kw = captured["kwargs"]
    assert kw["user"] == 1000
    assert kw["group"] == 1000
    assert kw["extra_groups"] == [1000, 100]
    assert kw["umask"] == 0o077
    assert kw["cwd"] == "/ws"


def test_child_env_rebases_identity_onto_build_user() -> None:
    env = _sandbox()._child_env({"HOME": "/root", "USER": "root", "PATH": "/usr/bin"})
    assert env["HOME"] == "/home/builder"
    assert env["USER"] == "builder"
    assert env["LOGNAME"] == "builder"
    assert env["PATH"] == "/usr/bin"  # caller env preserved


def test_run_layers_build_user_env_over_caller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(sb.subprocess, "run", lambda argv, **kw: captured.update(kw))
    _sandbox().run(["git"], env={"HOME": "/root", "GIT_CONFIG_GLOBAL": "/dev/null"})
    assert captured["env"]["HOME"] == "/home/builder"
    assert captured["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"  # hardened env kept


def test_own_chowns_path_to_build_user(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(sb.os, "chown", lambda p, u, g, **kw: calls.append((p, u, g, kw)))
    _sandbox().own("/ws")
    assert calls == [("/ws", 1000, 1000, {"follow_symlinks": False})]


def test_sandbox_run_none_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(sb.subprocess, "run", lambda argv, **kw: captured.update(kw=kw))
    sb.sandbox_run(None, ["make"], check=False)
    assert "user" not in captured["kw"]  # a non-root run must not request a setuid
```

Run: `uv run python -m pytest tests/providers/shared/build_host/test_sandbox.py -q` → Expected: FAIL (`No module named ...sandbox`).

- [ ] **Step 4: Implement `sandbox.py` (sandbox + chokepoint only — resolution is Task 2).** Create `src/kdive/providers/shared/build_host/sandbox.py`:

```python
"""Build-subprocess privilege drop for a root worker (ADR-0214).

When the worker runs as root, the local kernel-build lane (ADR-0162) would clone and ``make``
untrusted source as root. ``BuildSandbox`` carries an unprivileged identity and spawns build
subprocesses demoted to it via ``subprocess``'s child-side ``user=``/``group=``. ``SandboxProvider``
resolves the sandbox once per build, fail-closed when root-without-``KDIVE_BUILD_USER``.
"""

from __future__ import annotations

import logging
import os
import pwd
import subprocess  # noqa: S404 - fixed argv, no shell; demotion via user=/group=
from dataclasses import dataclass
from pathlib import Path

import kdive.config as config
from kdive.config.core_settings import BUILD_USER
from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BuildSandbox:
    """An unprivileged identity build subprocesses are demoted to (ADR-0214)."""

    uid: int
    gid: int
    extra_groups: tuple[int, ...]
    user_name: str
    home: str
    umask: int = 0o077

    def run(self, argv: list[str], *, env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
        """Spawn ``argv`` demoted to this identity (child-side setuid/setgid + build-user env)."""
        return subprocess.run(
            argv,
            user=self.uid,
            group=self.gid,
            extra_groups=list(self.extra_groups),
            umask=self.umask,
            env=self._child_env(env),
            **kwargs,
        )

    def own(self, path: str | Path) -> None:
        """``chown`` ``path`` to this identity so a demoted subprocess can write under it."""
        os.chown(path, self.uid, self.gid, follow_symlinks=False)

    def _child_env(self, env: dict | None) -> dict:
        # subprocess user=/group= change uid/gid but NOT the environment. Without this the demoted
        # child inherits the root worker's HOME=/root etc., breaking tools that write under $HOME
        # and leaving an incomplete sandbox. Layer the build-user identity over the caller's env
        # (e.g. the hardened git env) rather than discarding it.
        base = dict(env if env is not None else os.environ)
        base.update(HOME=self.home, USER=self.user_name, LOGNAME=self.user_name)
        base.pop("XDG_RUNTIME_DIR", None)
        base.pop("XDG_CACHE_HOME", None)
        return base


def sandbox_run(
    sandbox: BuildSandbox | None, argv: list[str], **kwargs
) -> subprocess.CompletedProcess:
    """Run ``argv`` demoted when ``sandbox`` is set, else as the current user (no setuid request)."""
    if sandbox is None:
        return subprocess.run(argv, **kwargs)
    return sandbox.run(argv, **kwargs)
```

- [ ] **Step 5: Run the tests.** Run: `uv run python -m pytest tests/providers/shared/build_host/test_sandbox.py tests/config/test_manifest_completeness.py -q` → Expected: PASS.

- [ ] **Step 6: Regenerate the config reference.** Registering a setting makes the committed config reference stale (the `just config-docs-check` CI gate), so regenerate it in the same commit that adds the setting (keeps every commit config-doc-consistent — do NOT defer this to Task 6). Run:

```bash
just config-docs            # regenerate docs/reference/config.md (or wherever it writes) from the registry
just config-docs-check && just env-docs-check && just config-guard   # all must pass
```

`git status` shows the regenerated reference file; stage exactly that path in Step 7.

- [ ] **Step 7: Guardrails + commit.** Run `just lint && just type`. Then (stage the regenerated config-reference path `just config-docs` produced, shown by `git status`):

```bash
git add src/kdive/config/core_settings.py src/kdive/providers/shared/build_host/sandbox.py \
  tests/providers/shared/build_host/test_sandbox.py tests/config/test_manifest_completeness.py \
  docs/reference/config.md   # adjust to the actual regenerated path from Step 6
git commit -m "feat(build): add KDIVE_BUILD_USER + BuildSandbox demotion primitive"
```

---

## Task 2: `SandboxProvider` resolution table (fail-closed, memoized)

**Files:**
- Modify: `src/kdive/providers/shared/build_host/sandbox.py`
- Modify: `tests/providers/shared/build_host/test_sandbox.py`

**Interfaces:**
- Consumes: `BuildSandbox` (Task 1), `BUILD_USER`, `config.get`.
- Produces: `SandboxProvider` with `.get() -> BuildSandbox | None`; module func `resolve_build_sandbox_provider() -> SandboxProvider`; `_resolve_sandbox() -> BuildSandbox | None` (patch target for tests).

- [ ] **Step 1: Write the failing resolution tests.** Append to `tests/providers/shared/build_host/test_sandbox.py`:

```python
import types

import kdive.config as config


def _fake_pwnam(name: str):
    return types.SimpleNamespace(
        pw_name=name, pw_uid=1000, pw_gid=2000, pw_dir="/home/builder"
    )


def test_resolve_non_root_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 1000)
    assert sb._resolve_sandbox() is None


def test_resolve_root_unset_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: None)
    with pytest.raises(sb.CategorizedError) as exc:
        sb._resolve_sandbox()
    assert exc.value.category is sb.ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_BUILD_USER" in str(exc.value)


def test_resolve_root_unknown_account_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: "ghost")
    monkeypatch.setattr(sb.pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    with pytest.raises(sb.CategorizedError) as exc:
        sb._resolve_sandbox()
    assert exc.value.category is sb.ErrorCategory.CONFIGURATION_ERROR


def test_resolve_root_uid0_account_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: "root")
    monkeypatch.setattr(
        sb.pwd, "getpwnam",
        lambda n: types.SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0, pw_dir="/root"),
    )
    with pytest.raises(sb.CategorizedError):
        sb._resolve_sandbox()


def test_resolve_root_valid_account_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: "builder")
    monkeypatch.setattr(sb.pwd, "getpwnam", _fake_pwnam)
    monkeypatch.setattr(sb.os, "getgrouplist", lambda n, g: [2000, 100])
    box = sb._resolve_sandbox()
    assert box == sb.BuildSandbox(
        uid=1000, gid=2000, extra_groups=(2000, 100), user_name="builder", home="/home/builder"
    )


def test_provider_memoizes_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def boom() -> sb.BuildSandbox | None:
        calls["n"] += 1
        raise sb.CategorizedError("nope", category=sb.ErrorCategory.CONFIGURATION_ERROR)

    monkeypatch.setattr(sb, "_resolve_sandbox", boom)
    provider = sb.SandboxProvider()
    with pytest.raises(sb.CategorizedError):
        provider.get()
    with pytest.raises(sb.CategorizedError):
        provider.get()
    assert calls["n"] == 1  # resolved once, error re-raised on every call
```

Run: `uv run python -m pytest tests/providers/shared/build_host/test_sandbox.py -k resolve -q` → Expected: FAIL (`_resolve_sandbox`/`SandboxProvider` undefined).

- [ ] **Step 2: Implement the resolution + provider.** Append to `src/kdive/providers/shared/build_host/sandbox.py`:

```python
def _resolve_sandbox() -> BuildSandbox | None:
    """Resolve the build sandbox from euid + ``KDIVE_BUILD_USER`` (ADR-0214 resolution table)."""
    if os.geteuid() != 0:
        return None
    name = (config.get(BUILD_USER) or "").strip()
    if not name:
        raise CategorizedError(
            "the worker runs as root but KDIVE_BUILD_USER is not set, so the local build lane "
            "would compile untrusted source as root; set KDIVE_BUILD_USER to an unprivileged "
            "account (see resource://kdive/docs/operating/build-source-staging.md)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        entry = pwd.getpwnam(name)
    except KeyError as exc:
        raise CategorizedError(
            "KDIVE_BUILD_USER does not name a known account on the worker host",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_user": name},
        ) from exc
    if entry.pw_uid == 0:
        raise CategorizedError(
            "KDIVE_BUILD_USER must be an unprivileged account, not root (uid 0)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_user": name},
        )
    return BuildSandbox(
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        extra_groups=tuple(os.getgrouplist(entry.pw_name, entry.pw_gid)),
        user_name=entry.pw_name,
        home=entry.pw_dir,
    )


class SandboxProvider:
    """Resolve the build sandbox once per build, memoized; re-raise a fail-closed error."""

    def __init__(self) -> None:
        self._resolved = False
        self._sandbox: BuildSandbox | None = None
        self._error: CategorizedError | None = None

    def get(self) -> BuildSandbox | None:
        """The resolved sandbox (``None`` when the worker is not root); raise if fail-closed."""
        if not self._resolved:
            try:
                self._sandbox = _resolve_sandbox()
                self._log_outcome()
            except CategorizedError as exc:
                self._error = exc
            self._resolved = True
        if self._error is not None:
            raise self._error
        return self._sandbox

    def _log_outcome(self) -> None:
        if self._sandbox is None:
            _log.debug("build: no privilege drop (worker euid != 0)")
        else:
            _log.info(
                "build: dropping privileges to %s (uid=%d gid=%d)",
                self._sandbox.user_name,
                self._sandbox.uid,
                self._sandbox.gid,
            )


def resolve_build_sandbox_provider() -> SandboxProvider:
    """A fresh memoizing provider; resolution is deferred to the first ``.get()`` at build time."""
    return SandboxProvider()
```

- [ ] **Step 3: Run the tests.** Run: `uv run python -m pytest tests/providers/shared/build_host/test_sandbox.py -q` → Expected: PASS.

- [ ] **Step 4: Guardrails + commit.** `just lint && just type`. Then:

```bash
git add src/kdive/providers/shared/build_host/sandbox.py tests/providers/shared/build_host/test_sandbox.py
git commit -m "feat(build): resolve build sandbox fail-closed when worker is root"
```

---

## Task 3: Demote the `execution.py` make run-steps

**Files:**
- Modify: `src/kdive/providers/shared/build_host/execution.py:97-142` (`real_run_make`, `run_make_target`, `real_run_olddefconfig`, `real_run_modules_install`)
- Modify/Create test: `tests/providers/shared/build_host/test_execution_sandbox.py`

**Interfaces:**
- Consumes: `BuildSandbox`, `sandbox_run` (Tasks 1-2).
- Produces: `real_run_make(workspace, sandbox=None)`, `run_make_target(workspace, args, label, sandbox=None)`, `real_run_olddefconfig(workspace, sandbox=None)`, `real_run_modules_install(workspace, mod_root, sandbox=None)` — all defaulting `sandbox=None` (unchanged behavior).

- [ ] **Step 1: Write the failing test.** Create `tests/providers/shared/build_host/test_execution_sandbox.py`:

```python
"""execution.py run-steps route through the sandbox chokepoint (ADR-0214)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.providers.shared.build_host import execution as ex
from kdive.providers.shared.build_host import sandbox as sb


def _box() -> sb.BuildSandbox:
    return sb.BuildSandbox(uid=7, gid=7, extra_groups=(7,), user_name="b", home="/home/b")


def test_run_make_passes_sandbox_to_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen["sandbox"] = sandbox
        seen["argv"] = argv
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    assert ex.real_run_make(Path("/ws"), sandbox=box) == 0
    assert seen["sandbox"] is box
    assert seen["argv"][0] == "make"


def test_run_make_default_sandbox_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        ex, "sandbox_run",
        lambda sandbox, argv, **kw: seen.setdefault("sandbox", sandbox) or type("R", (), {"returncode": 0})(),
    )
    ex.real_run_make(Path("/ws"))
    assert seen["sandbox"] is None


def test_modules_install_threads_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        ex, "sandbox_run",
        lambda sandbox, argv, **kw: seen.update(sandbox=sandbox, argv=argv) or type("R", (), {"returncode": 0})(),
    )
    box = _box()
    ex.real_run_modules_install(Path("/ws"), Path("/mod"), sandbox=box)
    assert seen["sandbox"] is box
    assert "modules_install" in seen["argv"]
```

Run: `uv run python -m pytest tests/providers/shared/build_host/test_execution_sandbox.py -q` → Expected: FAIL (`real_run_make() got an unexpected keyword argument 'sandbox'`).

- [ ] **Step 2: Implement the threading.** In `execution.py`, add the import near the top:

```python
from kdive.providers.shared.build_host.sandbox import BuildSandbox, sandbox_run
```

Replace `real_run_make`:

```python
def real_run_make(workspace: Path, sandbox: BuildSandbox | None = None) -> int:  # pragma: no cover - live_vm
    """Run the default parallel kernel build (demoted when a sandbox is active)."""
    try:
        return sandbox_run(
            sandbox,
            ["make", "-C", str(workspace), f"-j{os.cpu_count() or 1}"],
            timeout=MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "make exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
```

Replace `real_run_olddefconfig` / `real_run_modules_install`:

```python
def real_run_olddefconfig(workspace: Path, sandbox: BuildSandbox | None = None) -> int:  # pragma: no cover
    return run_make_target(workspace, ["olddefconfig"], "make olddefconfig", sandbox=sandbox)


def real_run_modules_install(
    workspace: Path, mod_root: Path, sandbox: BuildSandbox | None = None
) -> int:  # pragma: no cover
    return run_make_target(
        workspace,
        [f"INSTALL_MOD_PATH={mod_root}", "modules_install"],
        "make modules_install",
        sandbox=sandbox,
    )
```

Replace `run_make_target`:

```python
def run_make_target(
    workspace: Path, args: list[str], label: str, sandbox: BuildSandbox | None = None
) -> int:
    """Run ``make -C <workspace> <args...>`` (demoted when a sandbox is active); map faults."""
    try:
        return sandbox_run(
            sandbox,
            ["make", "-C", str(workspace), *args],
            timeout=MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{label} exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
```

(`real_read_build_id` / objcopy is **not** changed — it stays root per ADR-0214 §5.)

- [ ] **Step 3: Run the tests.** Run: `uv run python -m pytest tests/providers/shared/build_host/test_execution_sandbox.py -q` → Expected: PASS.

- [ ] **Step 4: Guardrails + commit.** `just lint && just type`. Then:

```bash
git add src/kdive/providers/shared/build_host/execution.py tests/providers/shared/build_host/test_execution_sandbox.py
git commit -m "feat(build): demote make run-steps to the build sandbox"
```

---

## Task 4: Demote `workspace.py` checkout + chown handoff

**Files:**
- Modify: `src/kdive/providers/shared/build_host/workspaces/workspace.py`
- Create/Modify test: `tests/providers/shared/build_host/test_workspace_sandbox.py`

**Interfaces:**
- Consumes: `BuildSandbox`, `sandbox_run`, `SandboxProvider` (Tasks 1-3).
- Produces: optional `sandbox: BuildSandbox | None = None` on `real_checkout`, `clone_tree`, `sync_tree`, `merge_config`, `apply_patch`, `_run_git`; `make_checkout(..., sandbox_provider: SandboxProvider | None = None)`; `_write_fragment(fragment_bytes, workspace, sandbox) -> None`.

- [ ] **Step 1: Write the failing tests.** Create `tests/providers/shared/build_host/test_workspace_sandbox.py`:

```python
"""workspace.py demotes build subprocesses and hands the workspace to the build user (ADR-0214)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.providers.shared.build_host import sandbox as sb
from kdive.providers.shared.build_host.workspaces import workspace as ws


def _box() -> sb.BuildSandbox:
    return sb.BuildSandbox(uid=9, gid=9, extra_groups=(9,), user_name="b", home="/home/b")


def test_write_fragment_owns_file_when_sandboxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owned: list = []
    box = _box()
    monkeypatch.setattr(box, "own", lambda p: owned.append(Path(p)))
    ws._write_fragment(b"CONFIG_X=y\n", tmp_path, box)
    frag = tmp_path / "kdump.config.fragment"
    assert frag.read_bytes() == b"CONFIG_X=y\n"
    assert owned == [frag]


def test_write_fragment_no_chown_without_sandbox(tmp_path: Path) -> None:
    ws._write_fragment(b"X", tmp_path, None)  # must not raise
    assert (tmp_path / "kdump.config.fragment").read_bytes() == b"X"


def test_sync_tree_adds_chown_under_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(ws.shutil, "which", lambda t: "/usr/bin/rsync")
    monkeypatch.setattr(ws, "warm_tree_source_error", lambda src: None)

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    box = _box()
    monkeypatch.setattr(box, "own", lambda p: seen.setdefault("owned", p))
    ws.sync_tree("/warm", tmp_path, sandbox=box)
    assert "--chown=9:9" in seen["argv"]
    assert seen["owned"] == tmp_path


def test_sync_tree_no_chown_without_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(ws.shutil, "which", lambda t: "/usr/bin/rsync")
    monkeypatch.setattr(ws, "warm_tree_source_error", lambda src: None)
    monkeypatch.setattr(
        ws.subprocess, "run",
        lambda argv, **kw: seen.update(argv=argv) or type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    ws.sync_tree("/warm", tmp_path)
    assert not any(a.startswith("--chown") for a in seen["argv"])
```

Run: `uv run python -m pytest tests/providers/shared/build_host/test_workspace_sandbox.py -q` → Expected: FAIL (`_write_fragment` undefined / `sync_tree` has no `sandbox`).

- [ ] **Step 2: Add the import + `_write_fragment` helper.** In `workspace.py`, add the import:

```python
from kdive.providers.shared.build_host.sandbox import BuildSandbox, SandboxProvider, sandbox_run
```

Add the helper (above `merge_config`):

```python
def _write_fragment(fragment_bytes: bytes, workspace: Path, sandbox: BuildSandbox | None) -> None:
    """Write the kdump fragment file, then hand it to the build user.

    ``write_bytes`` honors the worker umask, so a root worker with a hardened umask would leave the
    fragment ``0600 root:root`` and the demoted ``merge_config.sh`` could not read it. ``chown`` it
    to the build user (ADR-0214) so any file the root worker drops into the build-user-owned
    workspace that a demoted step must read stays readable.
    """
    fragment_path = workspace / "kdump.config.fragment"
    try:
        fragment_path.write_bytes(fragment_bytes)
    except OSError as exc:
        raise workspace_failure("write", "kdump.config.fragment", exc) from exc
    if sandbox is not None:
        sandbox.own(fragment_path)
```

- [ ] **Step 3: Thread `sandbox` through the checkout functions.** Edit each:

`_run_git` — add `sandbox: BuildSandbox | None = None` param and route through `sandbox_run`:

```python
def _run_git(
    args: list[str], *, cwd: Path | None, run_id: UUID, sandbox: BuildSandbox | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` hardened (demoted when a sandbox is active): no redirect-follow, vetted protos."""
    try:
        return sandbox_run(
            sandbox,
            ["git", *_GIT_HARDENED_FLAGS, *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_CLONE_TIMEOUT_S,
            env={**os.environ, **_GIT_HARDENED_ENV, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise build_failure("a git clone step exceeded the build timeout", run_id) from exc
    except OSError as exc:
        raise launch_failure("git", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
```

`clone_tree` — add the param; after the `workspace.mkdir(...)` block (just before `init = _run_git(["init", ...])`) insert the chown, and pass `sandbox=sandbox` to every `_run_git(...)` call:

```python
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise workspace_failure("mkdir", "build_workspace", exc) from exc
    if sandbox is not None:
        sandbox.own(workspace)  # demoted git writes into a build-user-owned dir (ADR-0214)
    init = _run_git(["init", str(workspace)], cwd=None, run_id=run_id, sandbox=sandbox)
    ...
```

(Update the four `_run_git(...)` calls — init/fetch/rev-parse/checkout — to pass `sandbox=sandbox`. Signature: `def clone_tree(source, workspace, allowlist, *, run_id, secret_registry, sandbox: BuildSandbox | None = None)`.)

`sync_tree` — add the param; build the rsync argv with an optional `--chown`, run it as **root** (plain `subprocess.run`, NOT `sandbox_run` — it reads the operator tree), then chown the dest dir:

```python
def sync_tree(
    kernel_src: str,
    workspace: Path,
    secret_registry: SecretRegistry | None = None,
    sandbox: BuildSandbox | None = None,
) -> None:
    """Mirror the warm kernel source tree into ``workspace`` with ``rsync -a --delete``.

    rsync runs as the worker (root) because it must read an operator-staged tree whose permissions
    kdive does not control; ``--chown`` + a dest-dir chown then hand the materialized tree to the
    build user for the demoted ``make`` (ADR-0214).
    """
    detail = warm_tree_source_error(kernel_src)
    if detail is not None:
        raise CategorizedError(detail, category=ErrorCategory.CONFIGURATION_ERROR)
    source = Path(kernel_src)
    if shutil.which("rsync") is None:
        raise CategorizedError(
            "rsync is required to materialize the warm kernel tree",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise workspace_failure("mkdir", "build_workspace", exc) from exc
    argv = ["rsync", "-a", "--delete"]
    if sandbox is not None:
        argv.append(f"--chown={sandbox.uid}:{sandbox.gid}")
    argv += ["--", f"{source}/", f"{workspace}/"]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=RSYNC_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "rsync exceeded the workspace sync timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": RSYNC_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("rsync", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "rsync failed to materialize the workspace tree",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
    if sandbox is not None:
        sandbox.own(workspace)  # rsync --chown owns the contents; chown the dest dir too
```

`merge_config` — add the param; call `run_make_target(..., sandbox=sandbox)`, replace the fragment write block with `_write_fragment(fragment_bytes, workspace, sandbox)`, and route the `merge_config.sh` call through `sandbox_run`:

```python
def merge_config(
    fragment_bytes: bytes, workspace: Path, run_id: UUID, sandbox: BuildSandbox | None = None
) -> None:  # pragma: no cover
    """Run base defconfig (demoted), merge the kdump fragment, leave olddefconfig to the caller."""
    if run_make_target(workspace, ["defconfig"], "make defconfig", sandbox=sandbox) != 0:
        raise build_failure("make defconfig exited non-zero", run_id)
    _write_fragment(fragment_bytes, workspace, sandbox)
    fragment_path = workspace / "kdump.config.fragment"
    try:
        merge = sandbox_run(
            sandbox,
            ["scripts/kconfig/merge_config.sh", "-m", ".config", str(fragment_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=MAKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise build_failure("merge_config.sh -m exceeded the build timeout", run_id) from exc
    except OSError as exc:
        raise launch_failure(
            "merge_config.sh", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
        ) from exc
    if merge.returncode != 0:
        raise build_failure("merge_config.sh -m exited non-zero", run_id)
```

`apply_patch` — add the param; route the `git apply` call through `sandbox_run`:

```python
def apply_patch(
    patch_ref: str,
    workspace: Path,
    secret_registry: SecretRegistry | None = None,
    sandbox: BuildSandbox | None = None,
) -> None:
```

and within it replace the `result = subprocess.run([...], ...)` for `git apply` with:

```python
        result = sandbox_run(
            sandbox,
            ["git", "apply", "-p1", "-v", "--", str(patch)],
            cwd=workspace,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=GIT_APPLY_TIMEOUT_S,
            check=False,
        )
```

`real_checkout` — add the param and thread it into the dispatch:

```python
def real_checkout(
    kernel_src: str,
    profile: ServerBuildProfile,
    workspace: Path,
    fragment_bytes: bytes,
    *,
    run_id: UUID,
    secret_registry: SecretRegistry,
    allowlist: Sequence[str] = (),
    sandbox: BuildSandbox | None = None,
) -> None:
    git_source = git_source_of(profile)
    if git_source is not None:
        clone_tree(git_source, workspace, allowlist, run_id=run_id,
                   secret_registry=secret_registry, sandbox=sandbox)
    else:
        sync_tree(kernel_src, workspace, secret_registry, sandbox=sandbox)
    merge_config(fragment_bytes, workspace, run_id, sandbox=sandbox)
    if profile.patch_ref is not None:
        apply_patch(profile.patch_ref, workspace, secret_registry, sandbox=sandbox)
```

`make_checkout` — take a `SandboxProvider | None` and resolve it lazily inside the closure (so a fail-closed provider raises only when the checkout actually runs, at build time):

```python
def make_checkout(
    kernel_src: str,
    secret_registry: SecretRegistry,
    *,
    allowlist: Sequence[str] = (),
    sandbox_provider: SandboxProvider | None = None,
) -> Checkout:
    """Create the default checkout seam (warm tree or, for a git source, an allowlisted clone)."""

    def _checkout(
        run_id: UUID, profile: ServerBuildProfile, workspace: Path, fragment_bytes: bytes
    ) -> None:
        sandbox = sandbox_provider.get() if sandbox_provider is not None else None
        real_checkout(
            kernel_src, profile, workspace, fragment_bytes,
            run_id=run_id, secret_registry=secret_registry, allowlist=allowlist, sandbox=sandbox,
        )

    return _checkout
```

> **Other `make_checkout` caller — leave it alone.** `src/kdive/providers/remote_libvirt/build.py:160` also calls `make_checkout(kernel_src, secret_registry)`. The new `sandbox_provider` is keyword-only with a `None` default, so that call is unaffected and correctly gets no demotion — remote builds run on an isolated host (ADR-0101), out of scope. Do **not** thread a provider into the remote caller. (The existing direct callers of `clone_tree`/`sync_tree`/`merge_config`/`apply_patch` in `tests/providers/build_host/test_transport_seams.py` and `test_build.py` are likewise unaffected by the `sandbox=None` defaults.)

- [ ] **Step 4: Run the tests + existing workspace tests.** Run: `uv run python -m pytest tests/providers/shared/build_host/test_workspace_sandbox.py "tests/providers/shared" -q` → Expected: PASS (new) and all pre-existing workspace/clone tests stay green (default `sandbox=None` → unchanged behavior).

- [ ] **Step 5: Guardrails + commit.** `just lint && just type`. Then:

```bash
git add src/kdive/providers/shared/build_host/workspaces/workspace.py tests/providers/shared/build_host/test_workspace_sandbox.py
git commit -m "feat(build): demote checkout + hand workspace to the build user"
```

---

## Task 5: Wire the provider into `LocalLibvirtBuild.from_env`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Modify: `tests/providers/local_libvirt/test_build.py`

**Interfaces:**
- Consumes: `resolve_build_sandbox_provider`, `SandboxProvider`, `make_checkout(..., sandbox_provider=...)`, run-step seams with `sandbox=` (Tasks 1-4).
- Produces: `LocalLibvirtBuild.__init__(..., sandbox_provider: SandboxProvider | None = None)`; `from_env` wiring.

- [ ] **Step 1: Write the failing test.** In `tests/providers/local_libvirt/test_build.py` add:

This test drives the real `make_checkout` checkout closure (which Task 4 gave a `sandbox_provider`) and asserts it fails closed before any subprocess. It reuses the test module's existing helpers — `_profile()` (line 81) and `SecretRegistry` (already imported, used at line 198); `tmp_path` is the pytest fixture; `pytest` is already imported in the module.

```python
def test_fail_closed_checkout_when_root_without_build_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root worker with no KDIVE_BUILD_USER must refuse the local build lane before any subprocess.
    import uuid

    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.providers.shared.build_host import sandbox as sb
    from kdive.providers.shared.build_host.workspaces import workspace as ws

    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sb.config, "get", lambda s: None)
    provider = sb.resolve_build_sandbox_provider()
    checkout = ws.make_checkout("/warm", SecretRegistry(), sandbox_provider=provider)
    with pytest.raises(CategorizedError) as exc:
        checkout(uuid.uuid4(), _profile(), tmp_path / "run", b"frag")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py::test_fail_closed_checkout_when_root_without_build_user -q` → Expected: FAIL until Task 4's `make_checkout(..., sandbox_provider=...)` is on the branch; with Task 4 merged the closure resolves the provider and raises `CONFIGURATION_ERROR` (the assertion target). If `make_checkout` does not accept `sandbox_provider`, Task 4 was not completed first.

- [ ] **Step 2: Wire the provider in `from_env`.** In `build.py`, add imports:

```python
from kdive.providers.shared.build_host.sandbox import SandboxProvider, resolve_build_sandbox_provider
```

In `from_env`, build the provider and thread it:

```python
        sandbox_provider = resolve_build_sandbox_provider()
        return cls(
            tenant="local",
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(
                kernel_src,
                secret_registry,
                allowlist=local_build_remote_allowlist_from_env(),
                sandbox_provider=sandbox_provider,
            ),
            run_olddefconfig=lambda ws: _build_exec.real_run_olddefconfig(
                ws, sandbox=sandbox_provider.get()
            ),
            read_config=_build_exec.real_read_config,
            run_make=lambda ws: _build_exec.real_run_make(ws, sandbox=sandbox_provider.get()),
            read_kernel_source=_local_kernel_source,
            read_vmlinux_source=_local_vmlinux_source,
            read_build_id=_build_exec.real_read_build_id,
            run_modules_install=lambda ws, mr: _build_exec.real_run_modules_install(
                ws, mr, sandbox=sandbox_provider.get()
            ),
            make_modules_bundle=_local_modules_bundle,
            staging_factory=_real_staging_factory,
            staging_cleanup=lambda p: shutil.rmtree(p, ignore_errors=True),
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
            secret_registry=secret_registry,
            sandbox_provider=sandbox_provider,
        )
```

- [ ] **Step 3: Accept + use `sandbox_provider` in `__init__` / `_maybe_publish_modules`.** Add the param to `__init__` (default `None`), store it, and chown the modules-staging dir before `run_modules_install`:

```python
    def __init__(
        self,
        *,
        ...,  # existing params unchanged
        workspace_cleanup: WorkspaceCleanup | None = None,
        sandbox_provider: SandboxProvider | None = None,
    ) -> None:
        ...
        self._sandbox_provider = sandbox_provider
```

In `_maybe_publish_modules`, after `mod_root = self._staging_factory()` and before `run_modules_install`:

```python
        mod_root = self._staging_factory()
        sandbox = self._sandbox_provider.get() if self._sandbox_provider is not None else None
        if sandbox is not None:
            sandbox.own(mod_root)  # demoted modules_install writes here (ADR-0214)
        try:
            with recorder.phase(BuildPhase.MODULES, provider):
                if self._run_modules_install(workspace, mod_root) != 0:
                    ...
```

In `over_transport`, leave `sandbox_provider` unset (default `None`) — the remote host is already isolated (ADR-0101); the transport seams do not use the sandbox.

- [ ] **Step 4: Run the tests.** Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -q` → Expected: PASS (new fail-closed test + all existing build tests, which construct `LocalLibvirtBuild` without `sandbox_provider` → `None` → unchanged).

- [ ] **Step 5: Guardrails + commit.** `just lint && just type`. Then:

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): wire build sandbox into local builder from_env"
```

---

## Task 6: Operator docs + regenerate snapshots

**Files:**
- Modify: `docs/operating/build-source-staging.md`
- Regenerate: packaged doc snapshot (`just resources-docs`)

**Interfaces:** none (docs only). The config reference was already regenerated in Task 1 (where the setting was added); this task is the operator prose + the packaged resource snapshot only.

- [ ] **Step 1: Document the setting + prereqs.** In `docs/operating/build-source-staging.md`, add a section describing: `KDIVE_BUILD_USER` (name of an unprivileged account); the root-worker behavior (a root worker demotes every build subprocess to that account; with no build user it fails the BUILD job closed with a `CONFIGURATION_ERROR`); and the two operator prerequisites — the build-workspace parent (`KDIVE_BUILD_WORKSPACE`, default `/var/lib/kdive/build`) must be traversable (`o+x`) by the build user, and the warm tree (`KDIVE_KERNEL_SRC`) and any patch refs must be readable by it. Keep prose plain (no "robust"/"comprehensive"/etc.). Cross-reference ADR-0214.

- [ ] **Step 2: Regenerate the packaged doc snapshot.** Run:

```bash
just resources-docs     # regenerate the packaged MCP doc-resource snapshot from canonical docs/
```

- [ ] **Step 3: Verify the doc/config gates.** Run:

```bash
just resources-docs-check && just docs-links && just docs-paths \
  && just config-docs-check && just env-docs-check
```

Expected: all pass (config-docs-check is already green from Task 1).

- [ ] **Step 4: Commit.** Stage the prose doc + the regenerated snapshot path `git status` shows:

```bash
git add docs/operating/build-source-staging.md src/kdive/mcp/resources/_content/build-source-staging.md
git commit -m "docs(build): document KDIVE_BUILD_USER root-build privilege drop"
```

> Implementer note: the exact regenerated paths for the config reference and the resource snapshot are whatever `just config-docs`/`just resources-docs` write — `git status` after Step 2 shows them; stage exactly those.

---

## Self-Review

**Spec coverage:**
- `KDIVE_BUILD_USER` setting → Task 1. Resolution table (euid/unset/unknown/uid-0/valid) → Task 2. `BuildSandbox`/`sandbox_run`/env rebase → Tasks 1-2. Demoted run-steps → Task 3. Demoted checkout + chown handoff (clone empty-dir chown, rsync `--chown`, fragment chown, modules-staging chown, git apply) → Tasks 4-5. `from_env` wiring + observability logs → Tasks 2 (logs) + 5 (wiring). Fail-closed fails the BUILD job → Task 5 test. Docs + prereqs → Task 6. objcopy untouched → Task 3 note. over_transport unaffected → Task 5.
- Error contract rows → Task 2 (`CONFIGURATION_ERROR` cases) + Task 4 (existing categories for mkdir/rsync).
- Testing bullets (resolution table, memoization, kwarg+env assembly, demotion wiring, fail-closed, no-op-when-unprivileged) → Tasks 1-5 tests.

**Placeholder scan:** The Task 5 test now uses the real `test_build.py` fixtures (`_profile()`, `SecretRegistry`, `tmp_path`) — no placeholder helper names remain. Config-doc regeneration is folded into Task 1 (the commit that adds the setting), so no commit is config-doc-stale. The second `make_checkout` caller (`remote_libvirt`) is called out as an intentional no-op.

**Type consistency:** `sandbox: BuildSandbox | None` default `None` is uniform across `execution.py` and `workspace.py`; `make_checkout`/`LocalLibvirtBuild.__init__` take `sandbox_provider: SandboxProvider | None`; `SandboxProvider.get() -> BuildSandbox | None`; `BuildSandbox.own(path)` and `.run(argv, *, env=None, **kwargs)` names match every call site. `sandbox_run(sandbox, argv, **kwargs)` signature is identical at all call sites.
