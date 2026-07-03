"""Tests for the offline drgn introspection provider (ADR-0033).

The drgn open/helper path is `live_vm`-gated; these tests exercise the orchestration
(provenance, staging, helper dispatch, byte-cap, redaction) against a fake `_Program`
and injected seams — never importing drgn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.debug.introspect import (
    IntrospectOutput,
    LiveIntrospector,
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
    VmcoreIntrospector,
    _raise_on_live_ssh_failure,
)
from kdive.providers.ports.retrieve import LiveScriptOutput
from kdive.providers.shared.debug_common.introspect import (
    _Module,
    _Program,
    _Task,
    helper_modules,
    helper_sysinfo,
    helper_tasks,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def _rows(out: dict[str, object]) -> list[dict[str, object]]:
    """Narrow a helper's row list for typed subscripting in assertions."""
    return cast("list[dict[str, object]]", out["tasks" if "tasks" in out else "modules"])


class _FakeTask:
    """A canned drgn task; ``raises`` makes ``kernel_stack`` blow up mid-decode."""

    def __init__(
        self,
        pid: int,
        comm: str,
        state: str,
        *,
        raises: bool = False,
        stack: list[str] | None = None,
    ) -> None:
        self._pid = pid
        self._comm = comm
        self._state = state
        self._raises = raises
        self._stack = stack or [f"frame_{pid}"]

    def pid(self) -> int:
        return self._pid

    def tgid(self) -> int:
        return self._pid

    def comm(self) -> str:
        return self._comm

    def state(self) -> str:
        return self._state

    def kernel_stack(self) -> list[str]:
        if self._raises:
            raise RuntimeError("unwind failed")
        return self._stack


class _FakeModule:
    """A canned drgn module; ``raises`` makes ``name`` blow up mid-decode."""

    def __init__(
        self,
        name: str,
        *,
        size: int = 4096,
        refcount: int = 1,
        used_by: list[str] | None = None,
        state: str = "live",
        raises: bool = False,
    ) -> None:
        self._name = name
        self._size = size
        self._refcount = refcount
        self._used_by = used_by or []
        self._state = state
        self._raises = raises

    def name(self) -> str:
        if self._raises:
            raise RuntimeError("bad struct offset")
        return self._name

    def size(self) -> int:
        return self._size

    def refcount(self) -> int:
        return self._refcount

    def used_by(self) -> list[str]:
        return self._used_by

    def state(self) -> str:
        return self._state


class _FakeProgram:
    """A hand-rolled `_Program` with canned tasks/modules/uts for the helper tests."""

    def __init__(
        self,
        *,
        tasks: list[_FakeTask] | None = None,
        modules: list[_FakeModule] | None = None,
        uts: dict[str, str] | None = None,
        boot_cmdline: str = "root=/dev/vda1 quiet",
        cpus_online: int = 4,
        mem_total_pages: int = 1048576,
    ) -> None:
        self._tasks = tasks if tasks is not None else [_FakeTask(1, "init", "D")]
        self._modules = modules if modules is not None else [_FakeModule("nfs")]
        self._uts = uts or {
            "release": "6.8.0",
            "version": "#1 SMP",
            "machine": "x86_64",
            "nodename": "guest",
        }
        self._boot_cmdline = boot_cmdline
        self._cpus_online = cpus_online
        self._mem_total_pages = mem_total_pages

    def iter_tasks(self) -> list[_Task]:
        return list(self._tasks)

    def iter_modules(self) -> list[_Module]:
        return list(self._modules)

    def uts(self) -> dict[str, str]:
        return self._uts

    def boot_cmdline(self) -> str:
        return self._boot_cmdline

    def cpus_online(self) -> int:
        return self._cpus_online

    def mem_total_pages(self) -> int:
        return self._mem_total_pages


def test_introspect_output_has_four_fields() -> None:
    out = IntrospectOutput(
        tasks={"tasks": []}, modules={"modules": []}, sysinfo={"release": "x"}, truncated=False
    )
    assert out.tasks == {"tasks": []}
    assert out.modules == {"modules": []}
    assert out.sysinfo == {"release": "x"}
    assert out.truncated is False


def test_vmcore_introspector_is_protocol() -> None:
    # A minimal duck-typed implementation satisfies the structural protocol.
    class _Impl:
        def from_vmcore(
            self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
        ) -> IntrospectOutput:
            return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    impl: VmcoreIntrospector = _Impl()
    result = impl.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="b")
    assert result.truncated is False


# --- tasks helper --------------------------------------------------------------------------


def test_tasks_filters_blocked_only_and_includes_stack() -> None:
    prog = _FakeProgram(
        tasks=[
            _FakeTask(1, "init", "S"),
            _FakeTask(42, "kworker", "D", stack=["__schedule", "io_schedule"]),
            _FakeTask(7, "running", "R"),
        ]
    )
    out = helper_tasks(prog)
    rows = _rows(out)
    assert [r["pid"] for r in rows] == [42]
    assert rows[0]["kernel_stack"] == ["__schedule", "io_schedule"]
    assert out["truncated"] is False


def test_tasks_respects_limit_and_sets_truncated() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(i, "blocked", "D") for i in range(250)])
    out = helper_tasks(prog)
    rows = _rows(out)
    assert len(rows) == 200
    assert out["truncated"] is True


def test_tasks_stack_decode_failure_degrades_per_row() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(9, "stuck", "D", raises=True)])
    out = helper_tasks(prog)
    rows = _rows(out)
    assert rows[0]["pid"] == 9
    assert rows[0]["kernel_stack"] == ["<stack unavailable: RuntimeError>"]


# --- modules helper ------------------------------------------------------------------------


def test_modules_returns_fields_and_decode_error_count() -> None:
    prog = _FakeProgram(
        modules=[
            _FakeModule("nfs", refcount=3, used_by=["lockd"], state="live"),
            _FakeModule("broken", raises=True),
        ]
    )
    out = helper_modules(prog)
    rows = _rows(out)
    assert rows[0]["name"] == "nfs"
    assert rows[0]["refcount"] == 3
    assert rows[0]["used_by"] == ["lockd"]
    assert out["decode_errors"] == 1
    assert out["all_failed"] is False


def test_modules_all_failed_degrades_not_raises() -> None:
    prog = _FakeProgram(modules=[_FakeModule("a", raises=True), _FakeModule("b", raises=True)])
    out = helper_modules(prog)
    assert out["modules"] == []
    assert out["decode_errors"] == 2
    assert out["all_failed"] is True


def test_modules_monolithic_kernel_is_empty_not_all_failed() -> None:
    prog = _FakeProgram(modules=[])
    out = helper_modules(prog)
    assert out["modules"] == []
    assert out["decode_errors"] == 0
    assert out["all_failed"] is False


# --- sysinfo helper ------------------------------------------------------------------------


def test_sysinfo_returns_uts_and_counters() -> None:
    prog = _FakeProgram()
    out = helper_sysinfo(prog)
    assert out["release"] == "6.8.0"
    assert out["machine"] == "x86_64"
    assert out["boot_cmdline"] == "root=/dev/vda1 quiet"
    assert out["cpus_online"] == 4
    assert out["mem_total_pages"] == 1048576


# --- LocalLibvirtVmcoreIntrospect orchestration --------------------------------------------


def _introspector(
    *,
    program: _FakeProgram | None = None,
    observed_build_id: str = "deadbeef",
    open_raises: CategorizedError | None = None,
) -> LocalLibvirtVmcoreIntrospect:
    """Build an introspector with every seam injected as a fake (no drgn, no store)."""
    prog = program if program is not None else _FakeProgram()

    def _open(vmcore: Path, vmlinux: Path) -> _Program:
        if open_raises is not None:
            raise open_raises
        return prog

    return LocalLibvirtVmcoreIntrospect(
        fetch_object=lambda ref: ref.encode("utf-8"),
        read_vmcore_build_id=lambda data: observed_build_id,
        secret_registry=SecretRegistry(),
        open_program=_open,
        run_helper=lambda program, name: (
            helper_modules(program)
            if name == "modules"
            else helper_sysinfo(program)
            if name == "sysinfo"
            else helper_tasks(program)
        ),
    )


def test_from_vmcore_happy_path_populates_report() -> None:
    out = _introspector().from_vmcore(
        vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef"
    )
    assert out.sysinfo["release"] == "6.8.0"
    assert cast("list[object]", out.tasks["tasks"])  # the canned blocked task is present
    assert out.truncated is False


def test_from_vmcore_happy_path_populates_every_section_distinctly() -> None:
    """tasks/modules/sysinfo are each routed to their own helper, not collapsed to one."""
    prog = _FakeProgram(
        tasks=[_FakeTask(42, "blocked", "D")],
        modules=[_FakeModule("nfs", refcount=3)],
    )
    out = _introspector(program=prog).from_vmcore(
        vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef"
    )
    # modules section carries module rows (not task rows misrouted by a bad helper name).
    module_rows = cast("list[dict[str, object]]", out.modules["modules"])
    assert module_rows[0]["name"] == "nfs"
    assert module_rows[0]["refcount"] == 3
    # tasks section carries the blocked task.
    task_rows = cast("list[dict[str, object]]", out.tasks["tasks"])
    assert task_rows[0]["pid"] == 42
    # sysinfo section carries uts data, not module/task rows.
    assert out.sysinfo["release"] == "6.8.0"
    assert "modules" not in out.sysinfo
    assert "tasks" not in out.modules


def test_from_vmcore_build_id_mismatch_is_configuration_error() -> None:
    introspector = _introspector(observed_build_id="0ther")
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_vmcore_build_id_mismatch_records_offending_ref_in_details() -> None:
    """The provenance failure names the offending vmcore ref under the documented key."""
    introspector = _introspector(observed_build_id="0ther")
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(
            vmcore_ref="run-7/core", debuginfo_ref="d", expected_build_id="deadbeef"
        )
    assert exc.value.details == {"vmcore_ref": "run-7/core"}


def test_from_vmcore_requires_both_drgn_seams() -> None:
    """A single configured seam is still off-gate: both must be present to introspect."""
    only_open = LocalLibvirtVmcoreIntrospect(
        fetch_object=lambda ref: ref.encode("utf-8"),
        read_vmcore_build_id=lambda data: "deadbeef",
        secret_registry=SecretRegistry(),
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=None,
    )
    with pytest.raises(CategorizedError) as exc:
        only_open.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_from_vmcore_open_failure_is_debug_attach_failure() -> None:
    boom = CategorizedError("drgn cannot open", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
    introspector = _introspector(open_raises=boom)
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_from_vmcore_redacts_guest_strings_at_the_port_boundary() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(13, "token=hunter2", "D")])
    out = _introspector(program=prog).from_vmcore(
        vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef"
    )
    rows = cast("list[dict[str, object]]", out.tasks["tasks"])
    assert "hunter2" not in str(rows)
    assert "[REDACTED]" in str(rows)


def test_from_vmcore_byte_cap_trims_tasks_and_sets_truncated() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(i, "blocked", "D", stack=["x" * 64]) for i in range(200)])
    introspector = _introspector(program=prog)
    # A tiny cap forces trimming of the (capped-at-200) tasks list.
    introspector._report_byte_cap = 256
    out = introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert out.truncated is True
    trimmed = cast("list[object]", out.tasks["tasks"])
    assert len(trimmed) < 200


def test_from_env_wires_real_drgn_seams() -> None:
    """from_env wires the shared production drgn seams (not None), without importing drgn."""
    from kdive.providers.shared.debug_common.drgn_program import (
        open_vmcore_program,
        read_vmcoreinfo_build_id,
        run_introspection_helper,
    )

    introspector = LocalLibvirtVmcoreIntrospect.from_env(secret_registry=SecretRegistry())
    assert introspector._open_program is open_vmcore_program
    assert introspector._run_helper is run_introspection_helper
    assert introspector._read_vmcore_build_id is read_vmcoreinfo_build_id


def test_from_env_reaches_drgn_import_missing_dependency() -> None:
    """The wired real seams drive the import-reaching path, not the removed None-guard.

    A provenance-valid core (matching VMCOREINFO BUILD-ID) plus a fetch fake serving *both* the
    vmcore and vmlinux refs drives control past provenance and both fetches into the drgn open seam.
    drgn is an operator-provided live-host prerequisite absent on CI, so the open raises
    MISSING_DEPENDENCY from ``_require_drgn``. We accept DEBUG_ATTACH_FAILURE too (defensive: a dev
    venv that *does* carry drgn would fail to open the synthetic blob as a real core) — either
    proves the import was reached and the old up-front None-guard is gone (live-dep divergence).
    """
    from kdive.providers.shared.debug_common.drgn_program import (
        open_vmcore_program,
        read_vmcoreinfo_build_id,
        run_introspection_helper,
    )

    build_id = "ab" * 20
    core = b"\x00" * 64 + b"VMCOREINFO\x00BUILD-ID=%s\n" % build_id.encode("ascii")

    def _open(vmcore: Path, vmlinux: Path) -> _Program:
        return cast("_Program", open_vmcore_program(vmcore, vmlinux))

    introspector = LocalLibvirtVmcoreIntrospect(
        fetch_object=lambda ref: core if "core" in ref else b"vmlinux-bytes",
        read_vmcore_build_id=read_vmcoreinfo_build_id,
        secret_registry=SecretRegistry(),
        open_program=_open,
        run_helper=run_introspection_helper,
    )
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(
            vmcore_ref="run/core", debuginfo_ref="run/vmlinux", expected_build_id=build_id
        )
    assert exc.value.category in (
        ErrorCategory.MISSING_DEPENDENCY,
        ErrorCategory.DEBUG_ATTACH_FAILURE,
    )


# --- LocalLibvirtLiveIntrospect orchestration (ADR-0219, SSH-exec kdive-drgn) ---------------


def _section_for(helper: str, program: _FakeProgram) -> dict[str, object]:
    """The section dict the in-guest helper would emit for ``helper`` (host-side fake)."""
    return {
        "tasks": helper_tasks,
        "modules": helper_modules,
        "sysinfo": helper_sysinfo,
    }[helper](program)


def _live_introspector(
    *,
    program: _FakeProgram | None = None,
    seam_raises: Exception | None = None,
) -> LocalLibvirtLiveIntrospect:
    """Build a live introspector with the SSH-exec helper seam injected as a fake.

    The fake stands in for ``_real_run_live_helper``: given ``(transport_handle, helper, key_path)``
    it returns the section the in-guest ``kdive-drgn <helper>`` would print, without SSH or drgn.
    """
    prog = program if program is not None else _FakeProgram()

    def _run_live(transport_handle: str, helper: str, key_path: str) -> dict[str, object]:
        if seam_raises is not None:
            raise seam_raises
        return _section_for(helper, prog)

    return LocalLibvirtLiveIntrospect(secret_registry=SecretRegistry(), run_live_helper=_run_live)


def test_live_introspector_is_protocol() -> None:
    class _Impl:
        def introspect_live(
            self, *, transport_handle: str, helper: str, key_path: str
        ) -> IntrospectOutput:
            return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

        def run_script(
            self, *, transport_handle: str, script: str, timeout_sec: float, key_path: str
        ) -> LiveScriptOutput:
            return LiveScriptOutput(output="", truncated=False)

    impl: LiveIntrospector = _Impl()
    assert (
        impl.introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
        ).truncated
        is False
    )


def test_run_happy_path_runs_selected_helper() -> None:
    out = _live_introspector().introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
    )
    assert cast("list[object]", out.tasks["tasks"])  # the canned blocked task is present
    assert out.modules == {}
    assert out.sysinfo == {}
    assert out.truncated is False


@pytest.mark.parametrize(
    ("helper", "field"),
    [("tasks", "tasks"), ("modules", "modules"), ("sysinfo", "sysinfo")],
)
def test_run_routes_section_into_the_matching_report_field(helper: str, field: str) -> None:
    """The selected helper's section lands in its field; the other two stay empty."""
    prog = _FakeProgram(
        tasks=[_FakeTask(42, "blocked", "D")], modules=[_FakeModule("nfs", refcount=3)]
    )
    out = _live_introspector(program=prog).introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper=helper, key_path="/tmp/key"
    )
    fields = {"tasks": out.tasks, "modules": out.modules, "sysinfo": out.sysinfo}
    assert fields[field]  # the routed section is non-empty
    # The other two sections carry no helper data. assemble_report's byte-cap always re-keys the
    # tasks field to an empty row list, so a non-selected `tasks` field is `{"tasks": []}`, while a
    # non-selected modules/sysinfo field stays `{}` — either way, no misrouted helper data.
    for other in (f for f in ("tasks", "modules", "sysinfo") if f != field):
        assert fields[other] in ({}, {"tasks": []})


def test_run_unknown_helper_is_configuration_error_before_seam_runs() -> None:
    """An unknown helper is rejected before any SSH round-trip (the seam is never called)."""
    calls: list[tuple[str, str]] = []

    def _run_live(transport_handle: str, helper: str, key_path: str) -> dict[str, object]:
        calls.append((transport_handle, helper))
        return {}

    introspector = LocalLibvirtLiveIntrospect(
        secret_registry=SecretRegistry(), run_live_helper=_run_live
    )
    with pytest.raises(CategorizedError) as exc:
        introspector.introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="bogus", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert calls == []  # the seam was never reached


def test_run_threads_handle_and_helper_into_the_seam() -> None:
    """The seam receives exactly the persisted transport handle, helper, and key path."""
    seen: list[tuple[str, str, str]] = []

    def _run_live(transport_handle: str, helper: str, key_path: str) -> dict[str, object]:
        seen.append((transport_handle, helper, key_path))
        return _section_for(helper, _FakeProgram())

    introspector = LocalLibvirtLiveIntrospect(
        secret_registry=SecretRegistry(), run_live_helper=_run_live
    )
    introspector.introspect_live(
        transport_handle="ssh://127.0.0.1:2222", helper="modules", key_path="/tmp/key"
    )
    assert seen == [("ssh://127.0.0.1:2222", "modules", "/tmp/key")]


def test_run_transport_failure_propagates_typed() -> None:
    boom = CategorizedError("ssh dropped", category=ErrorCategory.TRANSPORT_FAILURE)
    with pytest.raises(CategorizedError) as exc:
        _live_introspector(seam_raises=boom).introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_run_attach_failure_propagates_typed() -> None:
    boom = CategorizedError("drgn cannot attach", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
    with pytest.raises(CategorizedError) as exc:
        _live_introspector(seam_raises=boom).introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_run_arbitrary_seam_error_becomes_debug_attach_failure() -> None:
    # A non-categorized fault from the live seam is normalized to an attach failure (as offline).
    with pytest.raises(CategorizedError) as exc:
        _live_introspector(seam_raises=RuntimeError("kcore permission denied")).introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_run_redacts_guest_strings_at_the_port_boundary() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(13, "token=hunter2", "D")])
    out = _live_introspector(program=prog).introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
    )
    rows = cast("list[dict[str, object]]", out.tasks["tasks"])
    assert "hunter2" not in str(rows)
    assert "[REDACTED]" in str(rows)


def test_run_modules_decode_skew_degrades_not_raises() -> None:
    prog = _FakeProgram(modules=[_FakeModule("a", raises=True), _FakeModule("b", raises=True)])
    out = _live_introspector(program=prog).introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="modules", key_path="/tmp/key"
    )
    assert out.modules["all_failed"] is True
    assert out.modules["modules"] == []


def test_run_byte_cap_trims_tasks_and_sets_truncated() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(i, "blocked", "D", stack=["x" * 64]) for i in range(200)])
    introspector = _live_introspector(program=prog)
    introspector._report_byte_cap = 256
    out = introspector.introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
    )
    assert out.truncated is True
    assert len(cast("list[object]", out.tasks["tasks"])) < 200


def test_run_requires_the_live_seam() -> None:
    """A ``None`` live seam is off-gate: the port raises before any IO."""
    off_gate = LocalLibvirtLiveIntrospect(secret_registry=SecretRegistry(), run_live_helper=None)
    with pytest.raises(CategorizedError) as exc:
        off_gate.introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_live_from_env_wires_the_real_seam() -> None:
    """from_env wires a real SSH-exec seam (not None), without opening SSH or importing drgn."""
    introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=SecretRegistry())
    assert introspector._run_live_helper is not None


def test_live_from_env_bad_handle_is_configuration_error_before_io() -> None:
    """The wired real seam rejects a non-ssh handle as CONFIGURATION_ERROR before any SSH/drgn."""
    introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=SecretRegistry())
    with pytest.raises(CategorizedError) as exc:
        introspector.introspect_live(
            transport_handle="gdbstub://127.0.0.1:22", helper="tasks", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_live_from_env_non_loopback_handle_is_configuration_error_before_io() -> None:
    """A loopback re-check at use time rejects an off-loopback ssh handle before any SSH/drgn."""
    introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=SecretRegistry())
    with pytest.raises(CategorizedError) as exc:
        introspector.introspect_live(
            transport_handle="ssh://10.0.0.5:22", helper="tasks", key_path="/tmp/key"
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_real_run_live_helper_threads_key_path_into_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_real_run_live_helper` passes the caller-supplied key_path straight to `_live_ssh_argv`.

    It no longer resolves the managed key itself (that moved to the tool boundary, ADR-0289): a
    caller-supplied path — even one that doesn't exist — reaches the argv unchanged, so this
    drives the real seam up to (but never actually spawning) ssh via the exec seam.
    """
    from kdive.providers.local_libvirt.debug import introspect as introspect_mod

    key = tmp_path / "absent_id_ed25519"
    seen_argv: list[str] = []
    monkeypatch.setattr(
        introspect_mod, "_exec_live_helper", lambda argv: seen_argv.extend(argv) or {}
    )
    introspect_mod._real_run_live_helper(
        "ssh://127.0.0.1:2222", "tasks", str(key), secret_registry=SecretRegistry()
    )
    assert seen_argv[seen_argv.index("-i") + 1] == str(key)


def test_live_ssh_argv_uses_the_passed_key_path_and_appends_command() -> None:
    from kdive.providers.local_libvirt.debug import introspect as introspect_mod

    registry = SecretRegistry()

    argv = introspect_mod._live_ssh_argv(
        "ssh://127.0.0.1:2222", registry, ["run-script", "5"], "/tmp/kdive-bootkey-use-x/id"
    )

    assert argv == [
        "ssh",
        "-i",
        "/tmp/kdive-bootkey-use-x/id",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        "2222",
        "root@127.0.0.1",
        "--",
        "/usr/local/sbin/kdive-drgn",
        "run-script",
        "5",
    ]


# --- LocalLibvirtLiveIntrospect.run_script (ADR-0240, arbitrary drgn over SSH stdin) ---------


def _live_script_introspector(
    *,
    stdout: str = "",
    seam_raises: Exception | None = None,
    seen: list[tuple[str, str, float, str]] | None = None,
) -> LocalLibvirtLiveIntrospect:
    """A live introspector whose run-script seam is a fake (no SSH, no drgn)."""

    def _run_script(transport_handle: str, script: str, timeout_sec: float, key_path: str) -> str:
        if seen is not None:
            seen.append((transport_handle, script, timeout_sec, key_path))
        if seam_raises is not None:
            raise seam_raises
        return stdout

    return LocalLibvirtLiveIntrospect(secret_registry=SecretRegistry(), run_live_script=_run_script)


def test_run_script_returns_redacted_capped_stdout() -> None:
    out = _live_script_introspector(stdout="d_hash_shift = 0x14\n").run_script(
        transport_handle="ssh://127.0.0.1:22",
        script="print(prog['d_hash_shift'])",
        timeout_sec=5.0,
        key_path="/tmp/key",
    )
    assert "0x14" in out.output
    assert out.truncated is False


def test_run_script_threads_handle_script_timeout_and_key_path_into_the_seam() -> None:
    seen: list[tuple[str, str, float, str]] = []
    _live_script_introspector(stdout="ok", seen=seen).run_script(
        transport_handle="ssh://127.0.0.1:2222",
        script="print(1)",
        timeout_sec=12.0,
        key_path="/tmp/key",
    )
    assert seen == [("ssh://127.0.0.1:2222", "print(1)", 12.0, "/tmp/key")]


def test_run_script_off_gate_is_missing_dependency() -> None:
    off_gate = LocalLibvirtLiveIntrospect(secret_registry=SecretRegistry(), run_live_script=None)
    with pytest.raises(CategorizedError) as exc:
        off_gate.run_script(
            transport_handle="ssh://127.0.0.1:22",
            script="print(1)",
            timeout_sec=5.0,
            key_path="/tmp/key",
        )
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_run_script_transport_failure_propagates_typed() -> None:
    boom = CategorizedError("ssh dropped", category=ErrorCategory.TRANSPORT_FAILURE)
    with pytest.raises(CategorizedError) as exc:
        _live_script_introspector(seam_raises=boom).run_script(
            transport_handle="ssh://127.0.0.1:22",
            script="print(1)",
            timeout_sec=5.0,
            key_path="/tmp/key",
        )
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_run_script_arbitrary_seam_error_becomes_debug_attach_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        _live_script_introspector(seam_raises=RuntimeError("drgn died")).run_script(
            transport_handle="ssh://127.0.0.1:22",
            script="boom",
            timeout_sec=5.0,
            key_path="/tmp/key",
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_run_script_byte_caps_and_sets_truncated() -> None:
    introspector = _live_script_introspector(stdout="y" * 5000)
    introspector._live_script_byte_cap = 64
    out = introspector.run_script(
        transport_handle="ssh://127.0.0.1:22",
        script="print('y'*5000)",
        timeout_sec=5.0,
        key_path="/tmp/key",
    )
    assert out.truncated is True
    assert len(out.output.encode("utf-8")) <= 64


def test_run_script_from_env_wires_the_real_seam() -> None:
    introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=SecretRegistry())
    assert introspector._run_live_script is not None


# --- Drgn-live SSH failure diagnosability (#1008): the shared classifier is reused here too. ---


def _live_proc(returncode: int, stderr: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=b"", stderr=stderr
    )


@pytest.mark.parametrize(
    ("returncode", "stderr", "reason"),
    [
        (255, b"ssh: connect to host 127.0.0.1 port 22: Connection refused", "connection_refused"),
        (255, b"kex_exchange_identification: Connection reset by peer", "banner_timeout"),
        (255, b"root@127.0.0.1: Permission denied (publickey).", "auth_rejected"),
        (1, b"drgn: could not attach to the live kernel", "remote_command_failed"),
    ],
)
def test_live_ssh_failure_classifies_reason(returncode: int, stderr: bytes, reason: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        _raise_on_live_ssh_failure(_live_proc(returncode, stderr), "in-guest drgn helper failed")
    assert excinfo.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert excinfo.value.details["reason"] == reason
    assert excinfo.value.details["exit_status"] == returncode


def test_live_ssh_success_does_not_raise() -> None:
    _raise_on_live_ssh_failure(_live_proc(0, b""), "in-guest drgn helper failed")  # no error
