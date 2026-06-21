# Local-libvirt kdump: modules in the guest (#654) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a local-libvirt from-source build publish a kernel-modules artifact, and have the install plane inject `/lib/modules/<ver>` into the per-System qcow2 overlay so a real in-guest kdump can capture a vmcore.

**Architecture:** A shared build-output contract (kernel + modules + debuginfo). The local build runs `make modules_install` and publishes a separate `modules_ref`; the local install plane force-offs the domain, libguestfs-injects the modules into the overlay, runs `depmod`, and the guest's `kdumpctl` builds the crash initramfs in-guest. The kdump install gate is broadened to accept a `modules_ref` (from-source) OR an `initrd_ref` (upload lane).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. libvirt-python, libguestfs (`guestfs`), drgn (live_vm only). Spec: [`../specs/2026-06-21-local-kdump-modules-in-guest-654.md`](../specs/2026-06-21-local-kdump-modules-in-guest-654.md). ADR: [ADR-0206](../../adr/0206-modules-in-guest-shared-contract.md).

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree (`just type`).
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, absolute imports only, Google-style docstrings on non-trivial public APIs.
- Pick the most specific existing `ErrorCategory` (`domain/errors.py`); never invent strings.
- Real libguestfs / `make` / `depmod` / `domain.destroy()` edges are `# pragma: no cover - live_vm`, selected only in `from_env`. Pure orchestration is unit-tested with fakes — no host.
- CI runs `just lint`, `just type`, `just test`, `just docs-links`, `just docs-paths`, `just adr-status-check`, `just check-mermaid` **individually** (not via `just ci`). Run the relevant ones before each commit; run the **full** `just ci` once before the first push.
- Conventional-commit subjects ≤72 chars; end every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Do **not** refactor remote-libvirt's build/install (non-goal). Do **not** rename `initrd_ref` (it is the live upload-lane field).

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/kdive/build_artifacts/results.py` | `BuildOutput` neutral container | Modify: add `modules_ref` |
| `src/kdive/services/runs/steps.py` | `BuildStepResult` ledger codec + readers | Modify: add `modules_ref` to dump/load/refs; add `installed_modules_ref` |
| `src/kdive/providers/local_libvirt/build.py` | local build plane | Modify: produce `modules_ref` config-driven |
| `src/kdive/jobs/handlers/runs_build.py` | build worker handler | Modify: thread `modules_ref` into the ledger result |
| `src/kdive/providers/ports/lifecycle.py` | `InstallRequest` | Modify: add `modules_ref` |
| `src/kdive/jobs/handlers/runs_install.py` | install worker handler | Modify: read + pass `modules_ref` |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | local install plane | Modify: module injection seam + broadened gate |
| `src/kdive/images/rootfs_command.py` | local rootfs image kinds | Modify: add `kdump-utils` to the `debug` kind |
| `docs/operating/runbooks/four-method-live-run.md` | live-run runbook | Modify: §4b local-kdump note |
| `docs/adr/0203-...md` | harvest ADR | Modify: "boot side ready" precondition note |

---

## Task 1: `modules_ref` on the neutral build-result containers

**Files:**
- Modify: `src/kdive/build_artifacts/results.py`
- Modify: `src/kdive/services/runs/steps.py:28-73`
- Test: `tests/services/runs/test_steps.py` (create)

**Interfaces:**
- Produces: `BuildOutput(kernel_ref, debuginfo_ref, build_id, modules_ref: str | None = None)`; `BuildStepResult(..., modules_ref: str | None = None)` with `modules_ref` in `dump()`, `load()`, and `refs()` (key `"modules"`).

- [ ] **Step 1: Write the failing test**

Create `tests/services/runs/test_steps.py`:

```python
from kdive.services.runs.steps import BuildStepResult


def test_modules_ref_round_trips_through_dump_and_load() -> None:
    result = BuildStepResult(
        kernel_ref="k", debuginfo_ref="d", build_id="b", modules_ref="runs/r/modules"
    )
    dumped = result.dump()
    assert dumped["modules_ref"] == "runs/r/modules"
    assert BuildStepResult.load(dumped) == result


def test_modules_ref_absent_is_omitted_and_loads_none() -> None:
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b")
    assert "modules_ref" not in result.dump()
    assert BuildStepResult.load(result.dump()).modules_ref is None


def test_refs_exposes_modules_under_modules_key() -> None:
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b", modules_ref="m")
    assert result.refs()["modules"] == "m"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/services/runs/test_steps.py -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'modules_ref'`.

- [ ] **Step 3: Add `modules_ref` to `BuildOutput`**

In `src/kdive/build_artifacts/results.py`, change `BuildOutput`:

```python
class BuildOutput(NamedTuple):
    """Stored kernel build artifacts and the produced kernel build id."""

    kernel_ref: str
    debuginfo_ref: str
    build_id: str
    modules_ref: str | None = None
```

- [ ] **Step 4: Add `modules_ref` to `BuildStepResult`**

In `src/kdive/services/runs/steps.py`, add the field and thread it through `load`, `dump`, `refs`:

```python
@dataclass(frozen=True, slots=True)
class BuildStepResult:
    """Typed boundary for the `run_steps(step='build').result` JSON payload."""

    kernel_ref: str | None
    debuginfo_ref: str | None
    build_id: str | None
    initrd_ref: str | None = None
    modules_ref: str | None = None
    cmdline: str | None = None

    @classmethod
    def load(cls, value: object) -> BuildStepResult | None:
        if not isinstance(value, Mapping):
            return None
        result = cast("Mapping[str, object]", value)
        return cls(
            kernel_ref=_optional_str(result.get("kernel_ref")),
            debuginfo_ref=_optional_str(result.get("debuginfo_ref")),
            build_id=_optional_str(result.get("build_id")),
            initrd_ref=_optional_str(result.get("initrd_ref")),
            modules_ref=_optional_str(result.get("modules_ref")),
            cmdline=_optional_str(result.get("cmdline")),
        )

    def dump(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.kernel_ref is not None:
            result["kernel_ref"] = self.kernel_ref
        if self.debuginfo_ref is not None:
            result["debuginfo_ref"] = self.debuginfo_ref
        if self.initrd_ref is not None:
            result["initrd_ref"] = self.initrd_ref
        if self.modules_ref is not None:
            result["modules_ref"] = self.modules_ref
        if self.build_id is not None:
            result["build_id"] = self.build_id
        if self.cmdline is not None:
            result["cmdline"] = self.cmdline
        return result

    def refs(self) -> dict[str, str]:
        refs: dict[str, str] = {}
        if self.kernel_ref is not None:
            refs["kernel"] = self.kernel_ref
        if self.debuginfo_ref is not None:
            refs["vmlinux"] = self.debuginfo_ref
        if self.initrd_ref is not None:
            refs["initrd"] = self.initrd_ref
        if self.modules_ref is not None:
            refs["modules"] = self.modules_ref
        return refs
```

- [ ] **Step 5: Add the `installed_modules_ref` reader**

Below `installed_initrd_ref` in `steps.py`:

```python
async def installed_modules_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.modules_ref
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m pytest tests/services/runs/test_steps.py -q`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add src/kdive/build_artifacts/results.py src/kdive/services/runs/steps.py tests/services/runs/test_steps.py
git commit -m "feat: add modules_ref to BuildOutput and BuildStepResult"
```

---

## Task 2: Local build produces `modules_ref` (config-driven)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/providers/local_libvirt/test_build.py`

**Interfaces:**
- Consumes: `BuildOutput` with `modules_ref` (Task 1); `real_run_modules_install(workspace, mod_root) -> int` and `RunModulesInstall` type (`providers/shared/build_host/execution.py`); `ArtifactBytes`, `ArtifactRemoteFile`, `ArtifactSource`, `publish_artifact_source` (`providers/shared/build_host/publishing/artifact_publish.py`).
- Produces: `LocalLibvirtBuild` gains constructor seams `run_modules_install: RunModulesInstall`, `make_modules_bundle: Callable[[Path, Path], ArtifactSource]`, `staging_factory: Callable[[], Path]`, `staging_cleanup: Callable[[Path], None]`; `build()` returns `BuildOutput.modules_ref` set when `CONFIG_CRASH_DUMP=y`.

> **Why config-driven even though the preflight already requires `CONFIG_CRASH_DUMP`:** the build's `BuildHostOrchestrator._validate_final_config` hard-requires `CONFIG_CRASH_DUMP` today, so in practice this fires on every buildable kernel. Reading the resolved config keeps the build self-consistent (it never publishes a kdump modules artifact for a kernel that lacks crash-dump) and robust if the preflight is ever relaxed.

- [ ] **Step 1: Write the failing test — modules produced when kdump-capable**

Add to `tests/providers/local_libvirt/test_build.py` (the `_Seams`/`_FakeStore`/`_builder` helpers already exist; `_GOOD_CONFIG` already contains `CONFIG_CRASH_DUMP=y`). Extend `_Seams` with module seams and add tests:

```python
def test_build_publishes_modules_ref_when_config_is_kdump_capable(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()  # _GOOD_CONFIG has CONFIG_CRASH_DUMP=y
    builder = _builder(store, seams, tmp_path)
    output = builder.build(uuid4(), _profile())
    assert output.modules_ref is not None
    assert "modules" in store.artifacts


def test_build_skips_modules_ref_when_config_not_kdump_capable(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(config_text="CONFIG_DEBUG_INFO_DWARF5=y\n")
    builder = _builder(store, seams, tmp_path)
    output = builder.build(uuid4(), _profile())
    assert output.modules_ref is None
    assert "modules" not in store.artifacts
```

Add these seam methods to the `_Seams` dataclass:

```python
    modules_install_returncode: int = 0
    modules_install_calls: int = 0

    def run_modules_install(self, workspace: Path, mod_root: Path) -> int:
        self.modules_install_calls += 1
        self.call_order.append("modules_install")
        return self.modules_install_returncode

    def make_modules_bundle(self, workspace: Path, mod_root: Path) -> ArtifactSource:
        return ArtifactBytes(b"modules-bundle")
```

And pass them in `_builder(...)`:

```python
        run_modules_install=seams.run_modules_install,
        make_modules_bundle=seams.make_modules_bundle,
        staging_factory=lambda: tmp_path / "modroot",
        staging_cleanup=lambda _p: None,
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k modules -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'run_modules_install'`.

- [ ] **Step 3: Add the seams + kdump-config predicate to `LocalLibvirtBuild`**

In `build.py`, add the type aliases near the top:

```python
type _MakeModulesBundle = Callable[[Path, Path], ArtifactSource]
type _StagingFactory = Callable[[], Path]
type _StagingCleanup = Callable[[Path], None]
```

Add constructor params (after `read_build_id`) and store them:

```python
        run_modules_install: _build_exec.RunModulesInstall,
        make_modules_bundle: _MakeModulesBundle,
        staging_factory: _StagingFactory,
        staging_cleanup: _StagingCleanup,
```

```python
        self._run_modules_install = run_modules_install
        self._make_modules_bundle = make_modules_bundle
        self._staging_factory = staging_factory
        self._staging_cleanup = staging_cleanup
```

Add a module-level predicate (pure, unit-testable):

```python
def _config_is_kdump_capable(config_text: str) -> bool:
    """True when the resolved ``.config`` enables crash-dump (a kdump modules artifact applies)."""
    return "CONFIG_CRASH_DUMP=y" in config_text
```

- [ ] **Step 4: Produce `modules_ref` in `build()`**

Replace the body of `build()` so it reads the resolved config after `build_workspace`, and (when kdump-capable) runs `modules_install`, packages, and publishes a `modules` artifact:

```python
    def build(
        self,
        run_id: UUID,
        profile: ServerBuildProfile,
        *,
        recorder: BuildPhaseRecorder = DISABLED_RECORDER,
        provider: str = "",
    ) -> BuildOutput:
        workspace = self._orchestrator.workspace_path(run_id)
        try:
            self._orchestrator.build_workspace(
                run_id, profile, recorder=recorder, provider=provider
            )
            with recorder.phase(BuildPhase.ARTIFACT, provider):
                build_id = self._read_build_id(workspace)
                kernel = self.publish(run_id, "kernel", self._read_kernel_source(workspace))
                vmlinux = self.publish(run_id, "vmlinux", self._read_vmlinux_source(workspace))
                modules_ref = self._maybe_publish_modules(run_id, workspace, recorder, provider)
            return BuildOutput(
                kernel_ref=kernel.key,
                debuginfo_ref=vmlinux.key,
                build_id=build_id,
                modules_ref=modules_ref,
            )
        finally:
            self._orchestrator.cleanup_workspace(workspace)

    def _maybe_publish_modules(
        self, run_id: UUID, workspace: Path, recorder: BuildPhaseRecorder, provider: str
    ) -> str | None:
        """Run modules_install + publish a modules tarball iff the kernel is crash-dump-capable."""
        if not _config_is_kdump_capable(self._orchestrator.read_config(workspace)):
            return None
        mod_root = self._staging_factory()
        try:
            with recorder.phase(BuildPhase.MODULES, provider):
                if self._run_modules_install(workspace, mod_root) != 0:
                    raise _build_exec.build_failure("make modules_install exited non-zero", run_id)
                source = self._make_modules_bundle(workspace, mod_root)
                return self.publish(run_id, "modules", source).key
        finally:
            self._staging_cleanup(mod_root)
```

(`BuildPhase.MODULES` already exists — it is used by remote.)

- [ ] **Step 5: Add the worker-local + transport module-bundle seams**

At module scope in `build.py`, add modules-only packaging (mirrors remote's bundle but modules-only; excludes the `build`/`source` backref symlinks):

```python
import io
import tarfile

_MODULE_BACKREF_LINKS = frozenset({"build", "source"})
_MODULES_BUNDLE_NAME = "kdive-modules.tar.gz"


def _local_modules_bundle(workspace: Path, mod_root: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Tar ``<mod_root>/lib/modules`` to gzip bytes, dropping absolute backref symlinks."""
    modules_root = mod_root / "lib" / "modules"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(modules_root.rglob("*")):
            if path.is_symlink() and path.name in _MODULE_BACKREF_LINKS:
                continue
            tar.add(path, arcname="lib/modules/" + str(path.relative_to(modules_root)),
                    recursive=False)
    return ArtifactBytes(buf.getvalue())


def transport_modules_bundle(t: BuildTransport) -> _MakeModulesBundle:
    """Return a seam that tars ``lib/modules`` ON the build host and returns an ArtifactRemoteFile."""

    def _make(workspace: Path, mod_root: Path) -> ArtifactSource:
        bundle_path = str(workspace / _MODULES_BUNDLE_NAME)
        argv = ["tar", "-czf", bundle_path, "--exclude=*/build", "--exclude=*/source",
                "-C", str(mod_root), "lib/modules"]
        result = t.run(argv, cwd=str(workspace), timeout_s=_build_exec.MAKE_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                "tar failed to package the kernel modules on the build host",
                category=ErrorCategory.BUILD_FAILURE,
                details={"output": "modules bundle", "stderr": result.stderr[-512:]},
            )
        return ArtifactRemoteFile(path=bundle_path, transport=t)

    return _make


def _real_staging_factory() -> Path:  # pragma: no cover - live_vm
    import tempfile
    return Path(tempfile.mkdtemp(prefix="kdive-mod-"))
```

Add the imports `from kdive.domain.errors import CategorizedError, ErrorCategory` and `import shutil` at the top if not present.

- [ ] **Step 6: Wire the seams in `from_env` and `over_transport`**

In `from_env(...)`, add:

```python
            run_modules_install=_build_exec.real_run_modules_install,
            make_modules_bundle=_local_modules_bundle,
            staging_factory=_real_staging_factory,
            staging_cleanup=lambda p: shutil.rmtree(p, ignore_errors=True),
```

In `over_transport(...)`, add:

```python
            run_modules_install=transport_run_modules_install(transport),
            make_modules_bundle=transport_modules_bundle(transport),
            staging_factory=lambda: host_root / "modroot",
            staging_cleanup=lambda p: transport.cleanup(str(p)),
```

Add `transport_run_modules_install` to the existing `transport_seams` import.

- [ ] **Step 7: Add a modules_install-failure test**

```python
def test_build_modules_install_failure_is_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(modules_install_returncode=2)
    builder = _builder(store, seams, tmp_path)
    with pytest.raises(CategorizedError) as caught:
        builder.build(uuid4(), _profile())
    assert caught.value.category is ErrorCategory.BUILD_FAILURE
```

- [ ] **Step 8: Run the build tests + lint + type**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -q && just lint && just type`
Expected: PASS; zero lint/type findings.

- [ ] **Step 9: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat: local build publishes a kdump modules_ref when crash-dump-capable"
```

---

## Task 3: Build handler threads `modules_ref` into the ledger

**Files:**
- Modify: `src/kdive/jobs/handlers/runs_build.py:267-272`
- Test: `tests/jobs/handlers/test_runs_build.py`

**Interfaces:**
- Consumes: `BuildOutput.modules_ref` (Task 1/2); `BuildStepResult.modules_ref` (Task 1).

- [ ] **Step 1: Write the failing test**

In `tests/jobs/handlers/test_runs_build.py`, find the test asserting the ledger result shape (search for `kernel_ref` assertions) and add one that the handler copies `modules_ref` from a fake builder's `BuildOutput` into the persisted `BuildStepResult`. Mirror the existing build-success test's fixtures; assert the stored `run_steps` build row's `result` contains `modules_ref`.

```python
async def test_build_handler_persists_modules_ref(...) -> None:
    # arrange a fake builder returning BuildOutput(..., modules_ref="runs/<id>/modules")
    # act: run build_handler
    # assert: existing_build_result(conn, run_id).modules_ref == "runs/<id>/modules"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_build.py -k modules_ref -q`
Expected: FAIL — `modules_ref` is `None` in the persisted result.

- [ ] **Step 3: Thread `modules_ref` in `_build_and_record`**

In `runs_build.py`, the `return BuildStepResult(...)` at the end of `_build_and_record`:

```python
    return BuildStepResult(
        kernel_ref=output.kernel_ref,
        debuginfo_ref=output.debuginfo_ref,
        build_id=output.build_id,
        modules_ref=output.modules_ref,
        cmdline=payload.cmdline,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_build.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/handlers/runs_build.py tests/jobs/handlers/test_runs_build.py
git commit -m "feat: persist modules_ref from the build output to the run-steps ledger"
```

---

## Task 4: `InstallRequest.modules_ref` + install handler wiring

**Files:**
- Modify: `src/kdive/providers/ports/lifecycle.py:59-68`
- Modify: `src/kdive/jobs/handlers/runs_install.py:26,63-79`
- Test: `tests/jobs/handlers/` install handler test (mirror the existing one that asserts `initrd_ref` flows into `InstallRequest`)

**Interfaces:**
- Consumes: `installed_modules_ref(conn, run_id)` (Task 1).
- Produces: `InstallRequest(..., modules_ref: str | None = None)`.

- [ ] **Step 1: Write the failing test**

In the install-handler test module (search `tests/` for `installed_initrd_ref` usage / the install handler test), add a test that a Run whose build ledger carries `modules_ref` produces an `InstallRequest` with that `modules_ref` (capture the `InstallRequest` via a fake installer).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest <that test file> -k modules_ref -q`
Expected: FAIL — `TypeError: ... 'modules_ref'` or the captured request's `modules_ref` is unset.

- [ ] **Step 3: Add the field to `InstallRequest`**

```python
@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Inputs for staging a built kernel into a System for one Run."""

    system_id: UUID
    run_id: UUID
    kernel_ref: str
    cmdline: str
    method: CaptureMethod = CaptureMethod.HOST_DUMP
    initrd_ref: str | None = None
    modules_ref: str | None = None
```

- [ ] **Step 4: Read + pass `modules_ref` in the install handler**

In `runs_install.py`, add the import and the read + pass:

```python
from kdive.services.runs.steps import installed_initrd_ref, installed_modules_ref
```

```python
    initrd_ref = await installed_initrd_ref(conn, run_id)
    modules_ref = await installed_modules_ref(conn, run_id)
```

```python
            InstallRequest(
                system_id=system_id,
                run_id=run_id,
                kernel_ref=kernel_ref,
                cmdline=cmdline,
                method=method,
                initrd_ref=initrd_ref,
                modules_ref=modules_ref,
            ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest <that test file> -q && just type`
Expected: PASS; clean types.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/ports/lifecycle.py src/kdive/jobs/handlers/runs_install.py tests/
git commit -m "feat: thread modules_ref through the install request"
```

---

## Task 5: Install plane — module injection seam + broadened kdump gate

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/install.py`
- Test: `tests/providers/local_libvirt/test_install.py`

**Interfaces:**
- Consumes: `InstallRequest.modules_ref` (Task 4); `overlay_path(system_id)` (`providers/local_libvirt/lifecycle/storage.py`).
- Produces: a `GuestModuleWriter` protocol (`inject(overlay_path, modules_tar) -> None`) injected into `LocalLibvirtInstall`; the broadened gate behavior.

> **Design notes baked into the steps:** (a) the gate is satisfied by `modules_ref` OR `initrd_ref`; only neither → `CONFIGURATION_ERROR`. (b) Before injecting, force-off the domain if active (rw mount of a live overlay corrupts it — ADR-0203). (c) Injection is idempotent: clobber `/lib/modules/<ver>` then verify a completion sentinel.

- [ ] **Step 1: Write failing tests for the broadened gate**

In `tests/providers/local_libvirt/test_install.py`, add a fake module-writer to the install fakes and `_install(...)` helper (param `module_writer`), then:

```python
def test_install_kdump_with_modules_ref_injects_and_no_initrd_rendered(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    writer = _FakeModuleWriter()
    inst = _install(conn=conn, staging_root=tmp_path, module_writer=writer)
    inst.install(_request(method=CaptureMethod.KDUMP, modules_ref="runs/r/modules"))
    assert writer.injected  # modules written to the overlay
    assert len(conn.defined_xml) == 1
    assert "<initrd>" not in conn.defined_xml[0]  # production boot has no initrd


def test_install_kdump_with_neither_modules_nor_initrd_is_config_error(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request(method=CaptureMethod.KDUMP))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.defined_xml == []
```

`_FakeModuleWriter`:

```python
@dataclass
class _FakeModuleWriter:
    injected: bool = False
    fail: bool = False

    def inject(self, overlay: str, modules_tar: Path) -> None:
        if self.fail:
            raise CategorizedError("synthetic inject failure",
                                   category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        self.injected = True
```

The existing `test_install_kdump_without_initrd_is_config_error_before_redefine` stays valid (KDUMP + neither). The existing `test_install_kdump_with_initrd_proceeds` stays valid (KDUMP + `initrd_ref` → upload-lane path admitted).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -k "modules_ref or neither" -q`
Expected: FAIL — `_install()` has no `module_writer` param / gate not broadened.

- [ ] **Step 3: Add the `GuestModuleWriter` seam + injection orchestration**

In `install.py`, add the protocol + injection seam (pure orchestration; real libguestfs is the live edge):

```python
class GuestModuleWriter(Protocol):
    def inject(self, overlay: str, modules_tar: Path) -> None: ...
```

Add constructor params `fetch_modules: Fetch` and `module_writer: GuestModuleWriter | None = None`, store them; `from_env` wires `fetch_modules=_real_fetch` and `module_writer=_RealGuestModuleWriter(connect=...)`.

Add the injection step inside `install()` (after staging the kernel, before the kdump gate):

```python
        if request.modules_ref is not None:
            self._force_off_if_active(request.system_id)
            modules_tar = staging_dir / "modules.tar.gz"
            self._fetch_modules(request.modules_ref, modules_tar)
            self._inject_modules(request.system_id, modules_tar)
```

`_force_off_if_active` opens the connection, looks up the domain, and `destroy()`s it if `isActive()` (idempotent, mirrors `_power_cycle`).

- [ ] **Step 4: Broaden the kdump gate**

Replace the `_kdump_capture_present` call:

```python
        if request.method is CaptureMethod.KDUMP and not (
            request.modules_ref is not None or initrd_path is not None
        ):
            raise CategorizedError(
                "kdump capture environment absent (need injected modules or a staged initrd)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(request.system_id)},
            )
```

Remove the now-unused `_kdump_capture_present` function.

- [ ] **Step 5: Add the real (live_vm) libguestfs module writer**

```python
class _RealGuestModuleWriter:  # pragma: no cover - live_vm
    """Inject /lib/modules/<ver> into the overlay rw via libguestfs; clobber + depmod + verify."""

    def inject(self, overlay: str, modules_tar: Path) -> None:
        import guestfs  # noqa: PLC0415 - optional system binding, imported at call time
        g = guestfs.GUESTFS(python_return_dict=True)
        try:
            g.add_drive_opts(overlay, format="qcow2", readonly=0)
            g.launch()
            roots = g.inspect_os()
            g.mount(roots[0], "/")
            # clobber, extract, depmod, verify modules.dep exists
            g.tar_in(str(modules_tar), "/lib/modules", compress="gzip")
            g.command(["depmod", "-a", _modules_version(g)])
            ...
        finally:
            g.shutdown(); g.close()
```

(Exact clobber/version/sentinel mechanics are live-validated; mark the whole class `# pragma: no cover - live_vm`. Absent `guestfs` import → `MISSING_DEPENDENCY` mirroring `retrieve.py`/ADR-0203.)

- [ ] **Step 6: Run tests + lint + type**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -q && just lint && just type`
Expected: PASS; clean.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/install.py tests/providers/local_libvirt/test_install.py
git commit -m "feat: inject kernel modules into the overlay; broaden the local kdump gate"
```

---

## Task 6: Add `kdump-utils` to the local debug image

**Files:**
- Modify: `src/kdive/images/rootfs_command.py:21`, `_real_virt_builder` (`src/kdive/providers/local_libvirt/rootfs_build.py`)
- Test: `tests/images/` (a unit test asserting the `debug` kind ships `kdump-utils`)

**Interfaces:**
- Produces: `DEFAULT_DEBUG_FS_PACKAGES` includes `kdump-utils`.

- [ ] **Step 1: Write the failing test**

In a new/existing `tests/images/test_rootfs_command.py`:

```python
from kdive.images.rootfs_command import DEFAULT_DEBUG_FS_PACKAGES


def test_debug_image_ships_kdump_service_package() -> None:
    assert "kdump-utils" in DEFAULT_DEBUG_FS_PACKAGES
    assert "kexec-tools" in DEFAULT_DEBUG_FS_PACKAGES
    assert "makedumpfile" in DEFAULT_DEBUG_FS_PACKAGES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/images/test_rootfs_command.py -q`
Expected: FAIL — `kdump-utils` not in the tuple.

- [ ] **Step 3: Add the package + enable the service in the live build**

In `rootfs_command.py`:

```python
DEFAULT_DEBUG_FS_PACKAGES = ("drgn", "kexec-tools", "makedumpfile", "kdump-utils")
```

In `rootfs_build.py` `_real_virt_builder`, after the package install, enable the service when present (the function already receives `packages`):

```python
        if "kdump-utils" in packages:
            argv += ["--run-command", "systemctl enable kdump.service"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/images/test_rootfs_command.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/rootfs_command.py src/kdive/providers/local_libvirt/rootfs_build.py tests/images/test_rootfs_command.py
git commit -m "feat: ship kdump-utils + enable kdump.service in the local debug image"
```

---

## Task 7: Documentation — runbook + ADR-0203 note

**Files:**
- Modify: `docs/operating/runbooks/four-method-live-run.md` (§4b local-libvirt kdump note)
- Modify: `docs/adr/0203-local-libvirt-kdump-overlay-harvest.md` (the "boot side ready" wording)

- [ ] **Step 1: Update the runbook §4b local-libvirt note**

The current note (lines ~189-191) says the install preflight "refuses a kdump boot whose initramfs carries no capture hook." Replace with: a from-source local kdump build now publishes a `modules_ref`; `runs.install` injects `/lib/modules/<ver>` into the per-System overlay (libguestfs) and the guest's `kdumpctl` builds the crash initramfs in-guest, so `control.force_crash` writes a real `/var/crash/<ts>/vmcore` that `vmcore.fetch(method=kdump)` harvests — no staging. Note the broadened gate (modules OR uploaded initrd).

- [ ] **Step 2: Update ADR-0203's "boot side ready" precondition**

ADR-0203's Context says the boot side is "ready ... the install preflight enforces a capture initramfs." Add a one-line forward-reference: the staged-`<initrd>` enforcement is broadened by ADR-0206 (modules-in-guest), so a real in-guest kdump now drives the harvest this ADR implements.

- [ ] **Step 3: Run doc guardrails**

Run: `just docs-links && just docs-paths && just adr-status-check && just check-mermaid`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add docs/operating/runbooks/four-method-live-run.md docs/adr/0203-local-libvirt-kdump-overlay-harvest.md
git commit -m "docs: document local in-guest kdump via injected modules (#654)"
```

---

## Task 8: `live_vm` acceptance — real panic → capture

**Files:**
- Test: `tests/providers/local_libvirt/test_retrieve_kdump.py` (extend) or a new `live_vm`-marked test

**Interfaces:**
- Consumes: the full build→install→boot→force_crash→vmcore.fetch arc.

> This is the falsifiable hardware check the spec defers to `live_vm`. It must stay gated (skipped in CI) — never un-gate it. Per the "functional test drives capability" rule, it asserts a real `/var/crash/<ts>/vmcore` is produced **without** staging.

- [ ] **Step 1: Write the `live_vm`-marked acceptance test**

Add a `@pytest.mark.live_vm` test that, against an operator KVM host with the kdump debug image: builds a kdump-capable kernel, installs it (asserting the gate admits via `modules_ref` and the overlay gains `/lib/modules/<ver>`), boots to multi-user, `control.force_crash`, and `vmcore.fetch(method=kdump)` returns a drgn-loadable core with no staging. Reference the existing `live_vm` retrieve test for fixtures.

- [ ] **Step 2: Confirm it is collected but skipped in CI**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve_kdump.py -q`
Expected: the new test is `SKIPPED` (live_vm marker; no host in CI). Other tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/providers/local_libvirt/test_retrieve_kdump.py
git commit -m "test: live_vm acceptance for local in-guest kdump capture (#654)"
```

---

## Final verification (before pushing)

- [ ] Run the full gate once: `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test). Also run `just docs-links docs-paths adr-status-check` (CI runs these individually).
- [ ] Confirm no `live_vm` test was un-gated and no remote-libvirt build/install file changed.
- [ ] Confirm `initrd_ref` was not renamed anywhere (upload lane intact).
```
