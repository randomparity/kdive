# Local git-clone build lane (#530) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the default `worker-local` build host clone an agent-supplied git remote + ref into the per-run build workspace, gated by a deny-by-default operator allowlist.

**Architecture:** No build-profile schema change — `kernel_source_ref` already parses a `{"git":{"remote","ref"}}` form. Admission stops rejecting git on the local host; the local checkout seam dispatches on provenance (warm-tree `sync_tree` vs new `clone_tree`); a new `git_source.py` holds the allowlist + the relocated git-arg validator; `clone_tree` runs git with redirects/ambient-config/helper-transports disabled so the allowlist actually bounds the connection.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, `ty`; git/rsync subprocesses on the worker.

**Spec:** `docs/design/local-git-build-lane.md` · **ADR:** `docs/adr/0160-local-git-build-lane.md`

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict (whole tree: src + tests).
- Absolute imports only (`kdive.…`); Google-style docstrings on non-trivial public APIs.
- Guardrails before every commit: `just lint`, `just type`, focused `uv run python -m pytest … -q`. Full `just test` before the first push.
- Use the most specific existing `ErrorCategory` (`domain/errors.py`); never invent strings.
- Redact secrets/external output before it reaches an error detail or persistence (`security/`).
- Doc prose: plain/factual; never "Sprint"/"critical"/"robust"/"comprehensive"/"elegant".
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- ADR number is **0160**; already created. Do not pick a new one.

## File Structure

- `src/kdive/config/core_settings.py` — declare + register `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST` (Task 1).
- `src/kdive/providers/shared/build_host/git_source.py` — **new**: `validate_git_arg` (relocated), `parse_remote`, `remote_allowed`, `local_build_remote_allowlist_from_env` (Tasks 2–3).
- `src/kdive/providers/shared/build_host/shell_transport.py` — import the relocated `validate_git_arg` (Task 2).
- `src/kdive/providers/shared/build_host/workspace.py` — `clone_tree` + provenance dispatch in `real_checkout`; thread allowlist through `make_checkout` (Task 4).
- `src/kdive/providers/local_libvirt/build.py` — `from_env` reads the allowlist into `make_checkout` (Task 5).
- `src/kdive/services/runs/build_host_selection.py` — relax the local-host rule (Task 6).
- `docs/operating/` resource + config reference + `_BUILD_LANE_GUIDANCE` string (Task 7).

Tests live in `tests/` mirroring the package tree.

---

### Task 1: Config setting `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST`

**Files:**
- Modify: `src/kdive/config/core_settings.py` (add `Setting` near `BUILD_COMPONENT_ROOTS:288`; add to the registration list near `:506`).
- Test: `tests/config/test_core_settings.py` (or the existing settings-manifest test — match the file that already asserts a setting is registered).

**Interfaces:**
- Produces: `LOCAL_BUILD_REMOTE_ALLOWLIST: Setting[str]` (raw comma-separated string; parsing into a list is Task 3).

- [ ] **Step 1: Write the failing test** — assert the setting is registered, worker-scoped, in the `build` group.

```python
def test_local_build_remote_allowlist_setting_registered() -> None:
    from kdive.config.core_settings import ALL_SETTINGS, LOCAL_BUILD_REMOTE_ALLOWLIST
    assert LOCAL_BUILD_REMOTE_ALLOWLIST in ALL_SETTINGS
    assert LOCAL_BUILD_REMOTE_ALLOWLIST.name == "KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST"
    assert LOCAL_BUILD_REMOTE_ALLOWLIST.processes == frozenset({"worker"})
    assert LOCAL_BUILD_REMOTE_ALLOWLIST.group == "build"
```

(Confirm the exact name of the aggregate list — grep `core_settings.py` for the list that contains `BUILD_COMPONENT_ROOTS`; use that symbol instead of `ALL_SETTINGS` if it differs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/config/test_core_settings.py -k local_build_remote_allowlist -q`
Expected: FAIL (`ImportError`/`AttributeError`).

- [ ] **Step 3: Add the setting and register it**

```python
LOCAL_BUILD_REMOTE_ALLOWLIST = Setting(
    name="KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST",
    parse=_str,
    group="build",
    processes=_WORKER,
    help=(
        "Comma-separated allowlist of git remotes the local (worker-local) build host may "
        "clone for a git kernel_source_ref. Each entry is a host (github.com) or host/path "
        "prefix (github.com/myorg). Empty/unset disables local git builds (deny by default)."
    ),
)
```

Add `LOCAL_BUILD_REMOTE_ALLOWLIST,` to the registration list beside `BUILD_COMPONENT_ROOTS,`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/config/test_core_settings.py -k local_build_remote_allowlist -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/config/core_settings.py tests/config/test_core_settings.py
git commit -m "feat: add KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST worker setting

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Relocate `validate_git_arg` into a shared `git_source.py`

**Files:**
- Create: `src/kdive/providers/shared/build_host/git_source.py`
- Modify: `src/kdive/providers/shared/build_host/shell_transport.py` (replace the private `_validate_git_arg` body with an import of the shared one; keep the `_validate_git_arg` name as a thin re-export OR update call sites).
- Test: `tests/providers/shared/build_host/test_git_source.py` (new)

**Interfaces:**
- Produces: `validate_git_arg(value: str, field: str) -> None` — raises `CategorizedError(CONFIGURATION_ERROR)` for a leading `-` or a control character. Same `_UNSAFE_CHARS` set as `shell_transport.py`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.build_host.git_source import validate_git_arg

def test_validate_git_arg_rejects_leading_dash() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_git_arg("--upload-pack=evil", "remote")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

def test_validate_git_arg_rejects_control_char() -> None:
    with pytest.raises(CategorizedError):
        validate_git_arg("https://github.com/x\n", "remote")

def test_validate_git_arg_accepts_plain() -> None:
    validate_git_arg("https://github.com/torvalds/linux", "remote")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/shared/build_host/test_git_source.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Create `git_source.py` with the relocated validator**

Move the `_UNSAFE_CHARS` frozenset and the `_validate_git_arg` body from `shell_transport.py` into `git_source.py` as public `validate_git_arg`. **`shell_transport.py`'s `_validate_url` (lines 63-69) also uses `_UNSAFE_CHARS`**, so `shell_transport.py` must import *both* names back: `from kdive.providers.shared.build_host.git_source import validate_git_arg, _UNSAFE_CHARS`. Delete the local `_UNSAFE_CHARS`/`_validate_git_arg` definitions, replace internal `_validate_git_arg(...)` calls with `validate_git_arg(...)` (the `clone` method, lines ~193-194), and leave `_validate_url` in place (it now reads the imported `_UNSAFE_CHARS`).

Add a regression assertion in the existing shell_transport test (or the new git_source test) that `_validate_url` still rejects a control char, so the relocation cannot silently break it.

```python
"""Git-source validation and the local-build remote allowlist (ADR-0160)."""
from __future__ import annotations
from kdive.domain.errors import CategorizedError, ErrorCategory

_UNSAFE_CHARS = frozenset(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
)

def validate_git_arg(value: str, field: str) -> None:
    """Reject a git remote/ref that could parse as an option or inject via control chars."""
    if value.startswith("-"):
        raise CategorizedError(
            f"{field} must not start with '-' (would be parsed as a git option)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": field},
        )
    if any(c in _UNSAFE_CHARS for c in value):
        raise CategorizedError(
            f"{field} contains a control character or newline",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": field},
        )
```

- [ ] **Step 4: Run tests + existing transport tests to verify no regression**

Run: `uv run python -m pytest tests/providers/shared/build_host/test_git_source.py tests/providers/shared/build_host -q -k "git_source or clone or transport"`
Expected: PASS (the relocation keeps `clone`'s behavior identical).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/build_host/git_source.py src/kdive/providers/shared/build_host/shell_transport.py tests/providers/shared/build_host/test_git_source.py
git commit -m "refactor: relocate git-arg validation into shared git_source module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Allowlist parse + matching (`remote_allowed`, `parse_remote`, env reader)

**Files:**
- Modify: `src/kdive/providers/shared/build_host/git_source.py`
- Test: `tests/providers/shared/build_host/test_git_source.py`

**Interfaces:**
- Produces:
  - `parse_remote(remote: str) -> tuple[str, str, str]` → `(scheme, host, path)`; `host` lowercased, port/userinfo stripped; scp-like `git@host:path` / `host:path` → `("ssh", host, "/" + path)`. Returns `("", "", "")` for an unparseable/empty remote.
  - `remote_allowed(remote: str, allowlist: Sequence[str]) -> bool` — scheme gate (`https`/`ssh`/`git`), exact case-insensitive host match, `/`-boundary path-prefix match.
  - `local_build_remote_allowlist_from_env() -> tuple[str, ...]` — reads `LOCAL_BUILD_REMOTE_ALLOWLIST`, splits on commas, strips, drops empties.

- [ ] **Step 1: Write the failing test (matching table)**

```python
import pytest
from kdive.providers.shared.build_host.git_source import remote_allowed

ALLOW = ("github.com/myorg", "git.example.com")

@pytest.mark.parametrize("remote", [
    "https://github.com/myorg/linux",
    "https://github.com/myorg/linux.git",
    "https://GitHub.com/myorg/linux",          # host case-insensitive
    "git@git.example.com:team/linux.git",       # scp-like, host-only entry
    "ssh://git.example.com/team/linux",
    "git://git.example.com/team/linux",
])
def test_remote_allowed_accepts(remote: str) -> None:
    assert remote_allowed(remote, ALLOW) is True

@pytest.mark.parametrize("remote", [
    "https://github.com.evil.com/myorg/linux",  # not a substring match
    "https://github.com/myorg-evil/linux",      # path boundary
    "https://github.com/other/linux",           # wrong path on path-scoped entry
    "https://gitlab.com/myorg/linux",           # host not listed
    "file:///etc/passwd",                        # scheme rejected
    "http://git.example.com/team/linux",        # http not eligible
    "ext::sh -c id",                             # helper transport / not a host
    "",
])
def test_remote_allowed_rejects(remote: str) -> None:
    assert remote_allowed(remote, ALLOW) is False

def test_remote_allowed_empty_allowlist_denies_all() -> None:
    assert remote_allowed("https://github.com/myorg/linux", ()) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/shared/build_host/test_git_source.py -k remote_allowed -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `parse_remote`, `remote_allowed`, env reader**

```python
from collections.abc import Sequence
from urllib.parse import urlsplit

import kdive.config as config
from kdive.config.core_settings import LOCAL_BUILD_REMOTE_ALLOWLIST

_ELIGIBLE_SCHEMES = frozenset({"https", "ssh", "git"})

def parse_remote(remote: str) -> tuple[str, str, str]:
    """Return (scheme, host, path) for a git remote; ('', '', '') if unparseable."""
    if "://" in remote:
        parts = urlsplit(remote)
        host = (parts.hostname or "").lower()
        return parts.scheme.lower(), host, parts.path or ""
    # scp-like: [user@]host:path  (no scheme, single colon before the path)
    if ":" in remote and "/" not in remote.split(":", 1)[0]:
        location, path = remote.split(":", 1)
        host = location.rsplit("@", 1)[-1].lower()
        return "ssh", host, "/" + path
    return "", "", ""

def _entry_matches(host: str, path: str, entry: str) -> bool:
    entry = entry.strip().lower()
    if not entry:
        return False
    entry_host, _, entry_path = entry.partition("/")
    if host != entry_host:
        return False
    if not entry_path:
        return True
    prefix = "/" + entry_path
    return path == prefix or path.startswith(prefix + "/")

def remote_allowed(remote: str, allowlist: Sequence[str]) -> bool:
    """True iff the remote's scheme is eligible and its host/path matches an allowlist entry."""
    scheme, host, path = parse_remote(remote)
    if scheme not in _ELIGIBLE_SCHEMES or not host:
        return False
    return any(_entry_matches(host, path, entry) for entry in allowlist)

def local_build_remote_allowlist_from_env() -> tuple[str, ...]:
    """Read the worker's local-build remote allowlist; () when unset/empty (lane off)."""
    raw = config.get(LOCAL_BUILD_REMOTE_ALLOWLIST)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())
```

Note for the `ext::sh -c id` case: `parse_remote` returns `("", "", "")` (no `://`, the pre-colon segment `ext` has no `/`, so it is treated scp-like → `("ssh", "ext", "/sh -c id")`); host `ext` is not in the allowlist, so it is rejected. The `validate_git_arg` + scheme/host gate + (Task 4) `protocol.allow` env together keep helper transports off.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/shared/build_host/test_git_source.py -k "remote_allowed or parse_remote" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/build_host/git_source.py tests/providers/shared/build_host/test_git_source.py
git commit -m "feat: add local-build git remote allowlist matching

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `clone_tree` + provenance dispatch in `workspace.py`

**Files:**
- Modify: `src/kdive/providers/shared/build_host/workspace.py`
- Test: `tests/providers/local_libvirt/test_build.py` (the file that already exercises `real_checkout`/`sync_tree`).

**Interfaces:**
- Consumes: `validate_git_arg`, `remote_allowed` (Tasks 2–3); `GitSourceRef`, `is_git_source` (`profiles/build.py`).
- Produces:
  - `clone_tree(source: GitSourceRef, workspace: Path, allowlist: Sequence[str], *, run_id: UUID, secret_registry: SecretRegistry) -> None`
  - `real_checkout(..., *, run_id, secret_registry, allowlist: Sequence[str] = ()) -> None` — now dispatches on `is_git_source(profile)`.
  - `make_checkout(kernel_src: str, secret_registry: SecretRegistry, *, allowlist: Sequence[str] = ()) -> Checkout` — backward-compatible default so the `remote_libvirt` caller is unaffected.

- [ ] **Step 1: Write failing tests** — dispatch + allowlist gate + clean-workspace + hardened-env. Use an injected runner so no network/git runs.

```python
# In tests/providers/local_libvirt/test_build.py
from kdive.profiles.build import GitKernelSource, GitSourceRef, ServerBuildProfile
from kdive.providers.shared.build_host import workspace as build_host_workspace
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.domain.errors import CategorizedError, ErrorCategory

def _git_profile() -> ServerBuildProfile:
    return ServerBuildProfile(
        schema_version=1,
        kernel_source_ref=GitKernelSource(
            git=GitSourceRef(remote="https://github.com/myorg/linux", ref="v6.9")
        ),
    )

def test_real_checkout_git_source_invokes_clone_tree(monkeypatch, tmp_path) -> None:
    seen = {}
    monkeypatch.setattr(
        build_host_workspace, "clone_tree",
        lambda source, ws, allow, *, run_id, secret_registry: seen.update(
            remote=source.remote, allow=tuple(allow)
        ),
    )
    monkeypatch.setattr(build_host_workspace, "merge_config", lambda *a, **k: None)
    build_host_workspace.real_checkout(
        "/unused", _git_profile(), tmp_path / "ws", b"",
        run_id=__import__("uuid").uuid4(),
        secret_registry=SecretRegistry(),
        allowlist=("github.com/myorg",),
    )
    assert seen["remote"] == "https://github.com/myorg/linux"
    assert seen["allow"] == ("github.com/myorg",)

def test_clone_tree_rejects_disallowed_remote(tmp_path) -> None:
    source = GitSourceRef(remote="https://gitlab.com/x/linux", ref="v6.9")
    with pytest.raises(CategorizedError) as exc:
        build_host_workspace.clone_tree(
            source, tmp_path / "ws", ("github.com/myorg",),
            run_id=__import__("uuid").uuid4(), secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # No URL echo into either the message or the details (details may be None).
    assert "gitlab.com" not in str(exc.value)
    assert "gitlab.com" not in repr(exc.value.details)

def test_clone_tree_empty_allowlist_reports_lane_disabled(tmp_path) -> None:
    source = GitSourceRef(remote="https://github.com/myorg/linux", ref="v6.9")
    with pytest.raises(CategorizedError) as exc:
        build_host_workspace.clone_tree(
            source, tmp_path / "ws", (),
            run_id=__import__("uuid").uuid4(), secret_registry=SecretRegistry(),
        )
    assert "disabled" in str(exc.value).lower()
```

Split the remaining tests by seam — the hardened flags live *inside* `_run_git`, so an injected `_run_git` fake cannot observe them:

- **Orchestration / clean-workspace** — monkeypatch `build_host_workspace._run_git` with a fake returning a successful `subprocess.CompletedProcess` (and writing nothing). Plant a `stale.txt` in the workspace beforehand and assert it is gone after `clone_tree` (the `rmtree` before `git init`). Assert the fake `_run_git` was called with the `init` / `fetch --depth 1 <remote> <ref>` / `rev-parse … FETCH_HEAD` / `checkout FETCH_HEAD` arg sequences (the inner args, without the hardened flags).
- **`_run_git` hardening (dedicated test)** — monkeypatch `subprocess.run` to capture its `args`/`env`, call `build_host_workspace._run_git(["init", str(tmp)], cwd=None, run_id=…)`, and assert the captured argv contains `-c http.followRedirects=false` (and the `protocol.*` flags) and the captured env has `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`. No network runs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k "clone_tree or git_source_invokes" -q`
Expected: FAIL (`AttributeError: clone_tree`, `real_checkout` lacks `allowlist`).

- [ ] **Step 3: Implement `clone_tree`, dispatch, and threaded `make_checkout`**

Add imports: `from collections.abc import Sequence`, `from kdive.profiles.build import GitSourceRef, is_git_source`, `from kdive.providers.shared.build_host.git_source import remote_allowed, validate_git_arg`.

```python
GIT_CLONE_TIMEOUT_S = 10 * 60

# Closed ambient escape hatches so the allowlist bounds the actual connection (ADR-0160).
_GIT_HARDENED_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "GIT_PROTOCOL_FROM_USER": "0", "GIT_TERMINAL_PROMPT": "0"}
_GIT_HARDENED_FLAGS = ["-c", "http.followRedirects=false", "-c", "protocol.allow=never",
                       "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                       "-c", "protocol.git.allow=always"]

def _run_git(args: list[str], *, cwd: Path | None, run_id: UUID) -> subprocess.CompletedProcess[str]:
    """Run `git <hardened flags> <args>` with redirects/ambient config/helpers disabled."""
    try:
        return subprocess.run(
            ["git", *_GIT_HARDENED_FLAGS, *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, check=False,
            timeout=GIT_CLONE_TIMEOUT_S,
            env={**os.environ, **_GIT_HARDENED_ENV, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise build_failure("git clone step exceeded the timeout", run_id) from exc
    except OSError as exc:
        raise launch_failure("git", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc

def clone_tree(
    source: GitSourceRef, workspace: Path, allowlist: Sequence[str],
    *, run_id: UUID, secret_registry: SecretRegistry,
) -> None:
    """Clone `source.remote` at `source.ref` into `workspace` (ADR-0160), allowlist-gated."""
    validate_git_arg(source.remote, "remote")
    validate_git_arg(source.ref, "ref")
    if not allowlist:
        raise CategorizedError(
            "local git builds are disabled: the operator has not set "
            "KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST (see docs/operating/build-source-staging.md)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if not remote_allowed(source.remote, allowlist):
        raise CategorizedError(
            "the git remote is not on KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST "
            "(see docs/operating/build-source-staging.md)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if shutil.which("git") is None:
        raise CategorizedError("git is required to clone a kernel source",
                               category=ErrorCategory.MISSING_DEPENDENCY)
    shutil.rmtree(workspace, ignore_errors=True)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise workspace_failure("mkdir", "build_workspace", exc) from exc
    init = _run_git(["init", str(workspace)], cwd=None, run_id=run_id)
    if init.returncode != 0:
        raise CategorizedError("git init failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                               details={"stderr": redacted_tail(init.stderr, secret_registry)})
    fetch = _run_git(["-C", str(workspace), "fetch", "--depth", "1", source.remote, source.ref],
                     cwd=None, run_id=run_id)
    if fetch.returncode != 0:
        raise CategorizedError("git fetch failed", category=ErrorCategory.CONFIGURATION_ERROR,
                               details={"stderr": redacted_tail(fetch.stderr, secret_registry)})
    verify = _run_git(["-C", str(workspace), "rev-parse", "--verify", "--quiet", "FETCH_HEAD"],
                      cwd=None, run_id=run_id)
    if verify.returncode != 0:
        raise CategorizedError("git fetch produced no FETCH_HEAD (the fetch did not complete)",
                               category=ErrorCategory.TRANSPORT_FAILURE,
                               details={"stderr": redacted_tail(fetch.stderr, secret_registry)})
    checkout = _run_git(["-C", str(workspace), "checkout", "FETCH_HEAD"], cwd=None, run_id=run_id)
    if checkout.returncode != 0:
        raise CategorizedError("git checkout FETCH_HEAD failed",
                               category=ErrorCategory.CONFIGURATION_ERROR,
                               details={"stderr": redacted_tail(checkout.stderr, secret_registry)})
```

Update `real_checkout` to dispatch and `make_checkout` to thread the allowlist:

```python
def real_checkout(kernel_src, profile, workspace, fragment_bytes, *, run_id, secret_registry,
                  allowlist: Sequence[str] = ()) -> None:
    if is_git_source(profile):
        clone_tree(profile.kernel_source_ref.git, workspace, allowlist,
                   run_id=run_id, secret_registry=secret_registry)
    else:
        sync_tree(kernel_src, workspace, secret_registry)
    merge_config(fragment_bytes, workspace, run_id)
    if profile.patch_ref is not None:
        apply_patch(profile.patch_ref, workspace, secret_registry)

def make_checkout(kernel_src, secret_registry, *, allowlist: Sequence[str] = ()) -> Checkout:
    def _checkout(run_id, profile, workspace, fragment_bytes) -> None:
        real_checkout(kernel_src, profile, workspace, fragment_bytes,
                      run_id=run_id, secret_registry=secret_registry, allowlist=allowlist)
    return _checkout
```

Ensure `import os` and `subprocess` (already imported) and that `build_failure`/`launch_failure`/`workspace_failure`/`redacted_tail` are in scope (they are, in `workspace.py`).

- [ ] **Step 4: Run tests to verify they pass + existing workspace tests stay green**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -q`
Expected: PASS (new + existing `sync_tree`/`real_checkout` tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/build_host/workspace.py tests/providers/local_libvirt/test_build.py
git commit -m "feat: add provenance-aware local git-clone checkout lane

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `LocalLibvirtBuild.from_env` reads the allowlist

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py:133` (`from_env`)
- Test: `tests/providers/local_libvirt/test_build.py`

**Interfaces:**
- Consumes: `local_build_remote_allowlist_from_env()` (Task 3), `make_checkout(..., allowlist=...)` (Task 4).

- [ ] **Step 1: Write the failing test** — `from_env` wires the env allowlist into the checkout seam.

```python
def test_from_env_threads_remote_allowlist(monkeypatch) -> None:
    from kdive.providers.local_libvirt import build as local_build
    captured = {}
    monkeypatch.setenv("KDIVE_BUILD_WORKSPACE", "/tmp/ws")
    monkeypatch.setenv("KDIVE_KERNEL_SRC", "/srv/linux")
    monkeypatch.setenv("KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST", "github.com/myorg, git.example.com")
    monkeypatch.setattr(
        local_build._build_workspace, "make_checkout",
        lambda kernel_src, secret_registry, *, allowlist: captured.update(allow=tuple(allowlist)),
    )
    local_build.LocalLibvirtBuild.from_env(secret_registry=SecretRegistry())
    assert captured["allow"] == ("github.com/myorg", "git.example.com")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k from_env_threads_remote_allowlist -q`
Expected: FAIL (allowlist not passed).

- [ ] **Step 3: Wire it in `from_env`**

Add import `from kdive.providers.shared.build_host.git_source import local_build_remote_allowlist_from_env`, then:

```python
        checkout=_build_workspace.make_checkout(
            kernel_src, secret_registry, allowlist=local_build_remote_allowlist_from_env()
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k from_env -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat: thread the local-build remote allowlist from env into the checkout seam

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Admission relaxation (`build_host_selection.py`)

**Files:**
- Modify: `src/kdive/services/runs/build_host_selection.py:77-89`
- Test: `tests/services/runs/test_build_host_selection.py` (match the existing test module for this function — grep for `resolve_and_admit`).

**Interfaces:**
- Consumes: `is_git_source` (already imported there).

- [ ] **Step 1: Write the failing test** — git + local host is now admitted; warm-tree + remote still rejected.

```python
async def test_git_source_on_local_host_is_admitted(...) -> None:
    # local host + a git kernel_source_ref no longer raises; returns the local BuildHost.
    host = await resolve_and_admit(conn, _git_local_profile(), run_id)
    assert host.kind is BuildHostKind.LOCAL

async def test_warm_tree_on_remote_host_still_rejected(...) -> None:
    with pytest.raises(CategorizedError) as exc:
        await resolve_and_admit(conn, _warm_tree_remote_profile(), run_id)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

Mirror the fixtures/connection setup already used by the neighboring admission tests in that file (it sets up a `worker-local` row and a remote host row). Re-use the existing helpers rather than inventing new DB plumbing.

- [ ] **Step 2: Run tests to verify the first fails**

Run: `uv run python -m pytest tests/services/runs/test_build_host_selection.py -k "git_source_on_local or warm_tree_on_remote" -q`
Expected: the git-on-local test FAILs (currently raises); the warm-tree-on-remote test PASSes (unchanged rule).

- [ ] **Step 3: Remove the local+git rejection branch**

Delete the `if host.kind is BuildHostKind.LOCAL and git:` block (lines 78-83). Keep the remote+warm-tree rejection (lines 84-89) and the lease acquisition for non-local hosts. The local lane's git enablement/allowlist is enforced at build time (Task 4), not here.

- [ ] **Step 4: Run tests to verify both pass + the full module**

Run: `uv run python -m pytest tests/services/runs/test_build_host_selection.py -q`
Expected: PASS (including the now-admitted git-on-local case; check no other test asserted the old rejection — update it if so, since the rejection is intentionally removed).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/services/runs/build_host_selection.py tests/services/runs/test_build_host_selection.py
git commit -m "feat: admit a git kernel_source_ref on the local build host

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Docs — staging guide, config reference, lane-guidance string

**Files:**
- Modify: `docs/operating/build-source-staging.md` — **canonical** source. The `src/kdive/mcp/resources/_content/build-source-staging.md` copy is a generated snapshot; regenerate it with `just resources-docs` (do NOT hand-edit the snapshot — `resources-docs-check` gates it).
- Modify: `src/kdive/providers/shared/build_host/workspace.py` (the `_BUILD_LANE_GUIDANCE` string, lines 36-42)
- Regenerate: `docs/guide/reference/config.md` via `just config-docs` (generated from the registry; `config-docs-check` gates it — do NOT hand-edit).

**CI gates this touches (all run individually in CI):** `config-docs-check`, `resources-docs-check`, `env-docs-check` (satisfied by the registry entry from Task 1), `adr-status-check` (ADR-0160 is Accepted + the README row matches).

- [ ] **Step 1: Update the canonical `docs/operating/build-source-staging.md`**

In the two-lane table, add a third lane row: **Git on local host (allowlisted)** | `{"git": {"remote": …, "ref": …}}` | the seeded `worker-local` host | operator sets `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST`. Add a subsection describing the allowlist format (host or host/path-prefix, comma-separated, deny-by-default), the lane-disabled vs not-allowlisted rejection, and that `ref` must be a server-advertised tag/branch (not an arbitrary SHA). Keep the existing "a bare string never overrides `KDIVE_KERNEL_SRC`" caveat. **Anchor the table-row Edit on the last existing table row, not on the prose after the table, to avoid splitting the table with a blank line.**

- [ ] **Step 2: Update `_BUILD_LANE_GUIDANCE`**

Append a third option to the guidance string in `workspace.py`: a git `kernel_source_ref` can also build on the local host when the operator allowlists its remote via `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST`.

- [ ] **Step 3: Regenerate the generated docs and run the doc gates**

Run: `just config-docs && just resources-docs && just check-mermaid && just config-docs-check && just resources-docs-check && just env-docs-check && just adr-status-check`
Expected: all clean (config.md picks up the new setting; the resource snapshot picks up the staging-doc edit).

- [ ] **Step 4: Commit**

```bash
git add docs/operating/build-source-staging.md src/kdive/mcp/resources/_content/build-source-staging.md \
        src/kdive/providers/shared/build_host/workspace.py docs/guide/reference/config.md
git commit -m "docs: document the local git-clone build lane and its allowlist

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** admission relaxation → Task 6; provenance dispatch + `clone_tree` → Task 4; allowlist matching + env reader → Task 3; `validate_git_arg` relocation → Task 2; setting → Task 1; `from_env` wiring → Task 5; git egress hardening (redirects/ambient config/protocols) → Task 4 `_GIT_HARDENED_FLAGS`/`_GIT_HARDENED_ENV`; clean-workspace → Task 4; lane-disabled vs not-allowlisted detail → Task 4; SHA-ref qualification → covered by the fetch-failure path (Task 4) + doc note (Task 7); redaction → Task 4 `redacted_tail` + no-URL-echo tests; docs → Task 7. All spec sections map to a task.

**Placeholder scan:** none — every code step shows complete code; doc steps name exact files and the table-edit anchor gotcha.

**Type consistency:** `clone_tree(source, workspace, allowlist, *, run_id, secret_registry)`, `remote_allowed(remote, allowlist) -> bool`, `parse_remote(remote) -> tuple[str,str,str]`, `local_build_remote_allowlist_from_env() -> tuple[str,...]`, `make_checkout(kernel_src, secret_registry, *, allowlist=())` are used identically across Tasks 3–5. The `make_checkout` default keeps the `remote_libvirt/build.py:158` caller unchanged.

**Open verification for the implementer (resolve before coding the step):**
- Confirm the aggregate settings list symbol in `core_settings.py` (Task 1 Step 1).
- Confirm the admission test module + fixtures path (Task 6).
- Confirm the config reference doc path and whether a doc-drift gate exists (Task 7).
