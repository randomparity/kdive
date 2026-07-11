# Real `crash(8)` Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development per task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a real `crash(8)` subprocess runner into both providers' production Retrieve assembly so `postmortem.crash`/`triage` work on the deployed server, and make a failed crash run surface honestly instead of as an empty "successful" transcript.

**Architecture:** Add `_real_run_crash` to the provider-neutral `debug_common/crash_postmortem.py` (fixed argv `crash -s <vmlinux> <vmcore>`, batch on stdin, injected `shutil.which` finder, `cwd` = worker temp dir, only the `subprocess.run` is `live_vm`-gated). Move a conservative exit-status guard into the shared `run_crash_postmortem`. Replace `default_run_crash` (the stub) at both wiring sites. Promote the two tools to `implemented` after the live proof.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ty`, `ruff`; `crash(8)` on the worker host.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict; zero warnings.
- Subprocess argv is fixed (no shell, batch on stdin only) — justify `S603`/`S607` inline like `introspect.py`.
- Pick the most specific `ErrorCategory`; never invent strings. `MISSING_DEPENDENCY` (binary absent), `INFRASTRUCTURE_FAILURE` (timeout / launch / non-zero-with-empty-stdout).
- Redact + cap (`_STDERR_CAP = 2048`) stderr before it enters any response.
- Guardrail before each commit: `just lint && just type && uv run python -m pytest <focused> -q`.
- Replace, don't deprecate: `default_run_crash` is deleted, not left as a shim.

---

### Task 1: Conservative exit-status guard in the shared helper

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/crash_postmortem.py` (`run_crash_postmortem`, lines 30-80)
- Test: `tests/providers/debug_common/test_crash_postmortem.py`

**Interfaces:**
- Consumes: `run_crash: Callable[[Path, Path, str], CrashResult]` (unchanged signature).
- Produces: `run_crash_postmortem(...)` now raises `INFRASTRUCTURE_FAILURE` when `crash.exit_status != 0` and the redacted transcript is empty/whitespace; otherwise returns the transcript as before.

- [ ] **Step 1: Write failing tests**

```python
# tests/providers/debug_common/test_crash_postmortem.py
def test_nonzero_exit_with_empty_stdout_is_infrastructure_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref", debuginfo_ref="debug-ref",
            expected_build_id="deadbeef", commands=["sys"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "deadbeef",
            run_crash=lambda v, c, s: CrashResult(exit_status=1, stdout=b"  \n", stderr=b"cannot open core"),
            secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["exit_status"] == 1
    assert exc.value.details["stderr"] == "cannot open core"


def test_nonzero_exit_with_transcript_is_returned_not_discarded() -> None:
    out = run_crash_postmortem(
        vmcore_ref="core-ref", debuginfo_ref="debug-ref",
        expected_build_id="deadbeef", commands=["sys", "struct nope"],
        fetch_object=lambda ref: b"CORE",
        read_build_id=lambda data: "deadbeef",
        run_crash=lambda v, c, s: CrashResult(exit_status=1, stdout=b"SYSTEM MAP: ...\n", stderr=b"struct: invalid"),
        secret_registry=SecretRegistry(),
    )
    assert out.transcript == "SYSTEM MAP: ...\n"


def test_nonzero_exit_stderr_is_redacted_and_capped() -> None:
    registry = SecretRegistry()
    registry.register("hunter2-secret", scope=None)
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref", debuginfo_ref="debug-ref",
            expected_build_id="deadbeef", commands=["sys"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "deadbeef",
            run_crash=lambda v, c, s: CrashResult(exit_status=2, stdout=b"", stderr=b"key=hunter2-secret " + b"x" * 4000),
            secret_registry=registry,
        )
    assert "hunter2-secret" not in exc.value.details["stderr"]
    assert len(exc.value.details["stderr"]) <= 2048
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/debug_common/test_crash_postmortem.py -q`
Expected: FAIL (the helper does not yet check exit_status).

- [ ] **Step 3: Implement the guard**

In `run_crash_postmortem`, replace the tail (after `crash = run_crash(...)`):

```python
    redactor = Redactor(registry=secret_registry)
    transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
    if crash.exit_status != 0 and not transcript.strip():
        raise CategorizedError(
            "the crash(8) subprocess exited non-zero with no output; the core could not be analyzed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "exit_status": crash.exit_status,
                "stderr": redactor.redact_text(crash.stderr.decode("utf-8", "replace"))[:_STDERR_CAP],
            },
        )
    return CrashOutput(
        results={cmd: {"ran": True} for cmd in commands},
        transcript=transcript,
        truncated=False,
    )
```

Add module constant near the top: `_STDERR_CAP = 2048`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/debug_common/test_crash_postmortem.py -q`
Expected: PASS (existing tests still green — their fakes all use `exit_status=0`).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/debug_common/crash_postmortem.py tests/providers/debug_common/test_crash_postmortem.py
git commit -m "fix(retrieve): fail crash postmortem on non-zero exit with no output"
```

---

### Task 2: `_real_run_crash` runner (stub still in place)

> **Ordering:** This task ADDS the runner but leaves `default_run_crash` in the module, so
> the tree stays importable. Task 3 deletes the stub in the same commit that repoints its two
> importers — never delete it here, or the Task 2 commit breaks `just type` (whole-tree).

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/crash_postmortem.py` (add `_real_run_crash`, `_exec_crash`, `_CRASH_TIMEOUT_S`; keep `default_run_crash` for now)
- Test: `tests/providers/debug_common/test_crash_postmortem.py`

**Interfaces:**
- Produces: `_real_run_crash(vmlinux: Path, vmcore: Path, script: str, *, crash_path_finder: Callable[[str], str | None] = shutil.which) -> CrashResult`. Raises `MISSING_DEPENDENCY` when the finder returns `None`. Exported in `__all__` (replaces `default_run_crash`).

- [ ] **Step 1: Write failing tests** (argv construction + binary-absent; the real `subprocess.run` stays `live_vm`)

```python
def test_real_run_crash_missing_binary_is_missing_dependency() -> None:
    with pytest.raises(CategorizedError) as exc:
        _real_run_crash(Path("/v"), Path("/c"), "sys\nquit\n", crash_path_finder=lambda name: None)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert "crash" in str(exc.value)


def test_real_run_crash_builds_fixed_argv_and_pipes_script(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_exec(argv, script, cwd):
        captured["argv"] = argv
        captured["script"] = script
        captured["cwd"] = cwd
        return CrashResult(exit_status=0, stdout=b"OK", stderr=b"")

    monkeypatch.setattr(crash_postmortem, "_exec_crash", fake_exec)
    out = _real_run_crash(
        Path("/tmp/x.vmlinux"), Path("/tmp/x.vmcore"), "sys\nquit\n",
        crash_path_finder=lambda name: "/usr/bin/crash",
    )
    assert out.stdout == b"OK"
    assert captured["argv"] == ["/usr/bin/crash", "-s", "/tmp/x.vmlinux", "/tmp/x.vmcore"]
    assert captured["script"] == "sys\nquit\n"
    # cwd is the vmcore's parent (worker-owned spool dir), not the process CWD.
    assert captured["cwd"] == Path("/tmp")
```

(Import `from kdive.providers.shared.debug_common import crash_postmortem` and
`_real_run_crash` at the top of the test module.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/debug_common/test_crash_postmortem.py -k real_run_crash -q`
Expected: FAIL (`_real_run_crash` undefined).

- [ ] **Step 3: Implement** (add the runner; leave `default_run_crash` untouched — Task 3 deletes it)

```python
import shutil
import subprocess  # noqa: S404 - fixed argv only, no shell; batch via stdin

_CRASH_TIMEOUT_S = 300.0


def _real_run_crash(
    vmlinux: Path,
    vmcore: Path,
    script: str,
    *,
    crash_path_finder: Callable[[str], str | None] = shutil.which,
) -> CrashResult:
    """Run the real ``crash(8)`` over the spooled core; batch on stdin only.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` when ``crash`` is not installed on this
            worker host; ``INFRASTRUCTURE_FAILURE`` for a launch failure or timeout.
    """
    crash_path = crash_path_finder("crash")
    if crash_path is None:
        raise CategorizedError(
            "the crash(8) utility is not installed on this worker host",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    argv = [crash_path, "-s", str(vmlinux), str(vmcore)]
    return _exec_crash(argv, script, vmcore.parent)


def _exec_crash(  # pragma: no cover - live_vm
    argv: list[str], script: str, cwd: Path
) -> CrashResult:
    """Spawn ``crash`` with the batch on stdin; ``cwd`` is the worker-owned spool dir.

    The ``# pragma: no cover - live_vm`` covers the real subprocess (a host with
    ``/usr/bin/crash`` and a real core). ``_CRASH_TIMEOUT_S`` bounds a wedged crash so the
    worker thread is always released.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; batch via stdin only
            argv,
            input=script.encode("utf-8"),
            timeout=_CRASH_TIMEOUT_S,
            check=False,
            capture_output=True,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "the crash(8) subprocess exceeded the timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _CRASH_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise CategorizedError(
            "could not launch the crash(8) subprocess",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    return CrashResult(exit_status=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
```

Leave `__all__` unchanged in this task (`default_run_crash` is still present and exported).
`_real_run_crash` has a leading underscore and is imported by name, so it needs no `__all__`
entry.

- [ ] **Step 4: Run to verify pass + full module**

Run: `uv run python -m pytest tests/providers/debug_common/test_crash_postmortem.py -q && just lint && just type`
Expected: PASS, zero warnings (tree still imports cleanly — the stub is still present).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/debug_common/crash_postmortem.py tests/providers/debug_common/test_crash_postmortem.py
git commit -m "feat(retrieve): add real crash(8) runner alongside the stub"
```

---

### Task 3: Wire `_real_run_crash` into both providers and delete the stub

> **Single commit:** the stub deletion and BOTH importer edits land together, so the tree is
> never left importing a deleted name. `just type` is whole-tree — a half-done wiring fails it.

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/crash_postmortem.py` (delete `default_run_crash`; drop it from `__all__`)
- Modify: `src/kdive/providers/local_libvirt/retrieve.py` (import + `from_env`, lines 58-61, 134-135)
- Modify: `src/kdive/providers/remote_libvirt/retrieve/facade.py` (import + default param, lines 40-47, 67)
- Test: `tests/providers/local_libvirt/test_retrieve.py` (local), `tests/providers/remote_libvirt/retrieve/test_facade_postmortem.py` (remote)

**Interfaces:**
- Consumes: `_real_run_crash` from Task 2.

- [ ] **Step 1: Write failing tests (local + remote)**

```python
# tests/providers/local_libvirt/test_retrieve.py
def test_local_retrieve_wires_real_crash_runner() -> None:
    from kdive.providers.shared.debug_common.crash_postmortem import _real_run_crash
    r = LocalLibvirtRetrieve.from_env(secret_registry=SecretRegistry())
    assert r._run_crash is _real_run_crash
```

```python
# tests/providers/remote_libvirt/retrieve/test_facade_postmortem.py
# The remote facade wraps run_crash inside CrashPostmortemAdapter (no `_run_crash` on the
# facade), so assert the constructor DEFAULT is the real runner — that is the wiring site.
import inspect
from kdive.providers.remote_libvirt.retrieve.facade import RemoteLibvirtRetrieve
from kdive.providers.shared.debug_common.crash_postmortem import _real_run_crash

def test_remote_facade_defaults_to_real_crash_runner() -> None:
    default = inspect.signature(RemoteLibvirtRetrieve.__init__).parameters["run_crash"].default
    assert default is _real_run_crash
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py tests/providers/remote_libvirt/retrieve/test_facade_postmortem.py -k crash_runner -q`
Expected: FAIL (both still wired to `default_run_crash`).

- [ ] **Step 3: Implement (delete stub + repoint both importers, one commit)**

`crash_postmortem.py`: delete `default_run_crash` and remove `"default_run_crash"` from `__all__`.

`local_libvirt/retrieve.py`: change the import
`from ...crash_postmortem import (default_fetch_object, default_run_crash)` →
`(default_fetch_object, _real_run_crash)` and `from_env`'s `run_crash=default_run_crash` →
`run_crash=_real_run_crash`.

`remote_libvirt/retrieve/facade.py`: change the import line (drop `default_run_crash`, add
`_real_run_crash`) and the default `run_crash: RunCrash = default_run_crash` →
`run_crash: RunCrash = _real_run_crash`.

- [ ] **Step 4: Run to verify pass + grep the stub is gone + whole-tree type**

Run:
```bash
uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py tests/providers/remote_libvirt -q
just type
rg -n "default_run_crash" src/ tests/ || echo "stub fully removed"
```
Expected: PASS; `just type` clean (no dangling import); the grep prints "stub fully removed".

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/debug_common/crash_postmortem.py src/kdive/providers/local_libvirt/retrieve.py src/kdive/providers/remote_libvirt/retrieve/facade.py tests/
git commit -m "feat(retrieve): wire the real crash runner into both providers"
```

---

### Task 4: `live_vm` test driving the real `/usr/bin/crash`

**Files:**
- Create or extend: `tests/live_vm/` crash postmortem test (match the existing `live_vm` marker + fixtures)
- Test: itself

**Interfaces:**
- Consumes: a real captured vmcore + matching vmlinux from the `live_vm` harness; `_real_run_crash`.

- [ ] **Step 1: Locate the live_vm harness fixtures**

Run: `rg -ln "live_vm" tests/ | head; rg -n "vmcore|vmlinux|debuginfo" tests/live_vm/ 2>/dev/null | head`
Map the existing real-core fixture (or the lifecycle that produces one).

- [ ] **Step 2: Write the `live_vm` test**

A `@pytest.mark.live_vm` test that calls `_real_run_crash(vmlinux, vmcore, "sys\nquit\n")`
against a real captured core and asserts `exit_status == 0` and non-empty stdout (the `sys`
banner). Gate strictly behind the existing marker — do not un-gate. If no real-core fixture
exists, drive `vmcore.fetch` → `postmortem.crash` end-to-end in the `live_vm`/`live_stack`
suite.

- [ ] **Step 3: Run under the gate**

Run: `just test-live` (or `just test-live-stack`) — confirm it executes (not skipped) on this host.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test(retrieve): drive real crash(8) over a real core under live_vm"
```

---

### Task 5: Live proof + promote tools to `implemented`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/vmcore.py` (lines 497-542, the two `partial` meta blocks)
- Modify: `tests/mcp/core/test_tool_docs.py` (maturity guard for the two tools)

**Interfaces:** none new.

- [ ] **Step 1: Run the full live proof**

Drive the lifecycle on this host over the live stack: build/boot → `force_crash` →
`vmcore.fetch` (kdump or host_dump) → `postmortem.crash(commands=["sys"])` /
`postmortem.triage`. Record the transcript and the `crash --version` in the PR body. This is
the authoritative check of the crash invocation.

- [ ] **Step 2: Promote** (only if Step 1 passed)

In `vmcore.py`, change both `meta=_docmeta.maturity_meta("partial", …)` blocks to
`meta=_docmeta.maturity_meta("implemented")` (drop `reason`/`detail`/`promotion`).

- [ ] **Step 3: Update the maturity guard**

In `tests/mcp/core/test_tool_docs.py`, move `postmortem.crash`/`triage` from the
partial-expectation set to the implemented set (mirror how the 13 promoted tools in PR #815
were moved). Run the guard:

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py -q`
Expected: PASS (no `partial`-tool offenders; no stale `maturity_detail`).

- [ ] **Step 4: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/vmcore.py tests/mcp/core/test_tool_docs.py
git commit -m "feat(retrieve): promote postmortem.crash/triage to implemented"
```

---

### Task 6: Full suite + docs regen

- [ ] **Step 1:** `just docs` (regen tool reference — maturity flips may change it), then `just ci`.
- [ ] **Step 2:** Fix any drift; commit regenerated docs separately:
  `git commit -m "docs: regen tool reference for postmortem maturity"`.

## Self-Review

- **Spec coverage:** runner (T2), wiring (T3), exit guard (T1), live_vm test (T4), live proof + promotion (T5), maturity text (T5), docs regen (T6). All spec acceptance criteria mapped.
- **Placeholders:** none — code shown for each code step; T4/T5 live steps are environment-driven and name exact tools.
- **Type consistency:** `_real_run_crash(vmlinux, vmcore, script, *, crash_path_finder)` and `_exec_crash(argv, script, cwd)` are used consistently across T2/T3; `CrashResult(exit_status, stdout, stderr)` matches `ports/retrieve.py`.
