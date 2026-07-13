"""Coverage anchor for the split boot run handler module, plus the runs registrar facade."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.domain.lifecycle.run_steps import BootStepResult
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.console import console_evidence
from kdive.jobs.handlers.runs import boot as runs_boot
from kdive.jobs.handlers.runs import boot_evidence
from kdive.jobs.handlers.runs import registrar as runs
from kdive.jobs.handlers.runs import registrar as runs_registrar
from kdive.jobs.models import HandlerRegistry
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.lifecycle import Connector
from kdive.security.artifacts.artifact_search import (
    ArtifactSearchInputError,
    search_text,
)
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


def test_boot_handler_facade_and_leaf_console_patch_surface() -> None:
    assert runs.boot_handler is runs_boot.boot_handler
    assert console_evidence.console_log_path is not None
    assert console_evidence.read_console_log is not None


class _FakeRun:
    """Stand-in carrying the fields the boot handler reads (expected crash + run id)."""

    def __init__(self, expected_boot_failure: object) -> None:
        self.expected_boot_failure = expected_boot_failure
        self.id = uuid4()


_CONSOLE = b"line1\nkernel BUG at mm/slub.c:1\nline3\n"


def test_expected_crash_returns_matched_line_when_pattern_is_found() -> None:
    # The matched line (not just a bool) is returned so runs.get can surface *which* line
    # matched — the whole point of #840. The pattern is a substring of one line; the full
    # matched line text comes back.
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at"}))
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) == "kernel BUG at mm/slub.c:1"


def test_expected_crash_none_when_pattern_absent() -> None:
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "no-such-marker"}))
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) is None


def test_expected_crash_none_when_no_expected_failure_declared() -> None:
    run = cast(Run, _FakeRun(None))
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) is None


def test_expected_crash_none_for_non_console_crash_kind() -> None:
    run = cast(Run, _FakeRun({"kind": "exit_code", "pattern": "BUG at"}))
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) is None


def test_expected_crash_matches_resolved_panic_preset() -> None:
    # A persisted preset doc carries its resolved canonical pattern; the matcher treats a preset
    # kind identically to console_crash (ADR-0266).
    console = b"line1\nKernel panic - not syncing: Attempted to kill init!\nline3\n"
    run = cast(Run, _FakeRun({"kind": "panic", "pattern": "Kernel panic"}))
    matched = boot_evidence.expected_crash_matched_line(run, console)
    assert matched == "Kernel panic - not syncing: Attempted to kill init!"


def test_expected_crash_matches_resolved_oops_preset_el8_wording() -> None:
    console = b"prior\nBUG: unable to handle kernel paging request at 0000000000000010\nafter\n"
    run = cast(
        Run,
        _FakeRun(
            {
                "kind": "oops",
                "pattern": (
                    "Oops:|BUG: unable to handle page fault for address"
                    "|BUG: kernel NULL pointer dereference|BUG: unable to handle kernel"
                    "|kernel BUG at"
                ),
            }
        ),
    )
    matched = boot_evidence.expected_crash_matched_line(run, console)
    assert matched == "BUG: unable to handle kernel paging request at 0000000000000010"


def test_expected_crash_none_when_pattern_is_not_a_string() -> None:
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": 123}))
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) is None


def test_expected_crash_none_on_invalid_search_pattern() -> None:
    # A trailing '|' yields an empty term, so parse_literal_terms raises
    # ArtifactSearchInputError inside search_text. The handler must catch it and
    # fail closed (no matched line) rather than let it propagate out.
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at|"}))
    # Guard: the pattern truly drives search_text into the raising path.
    with pytest.raises(ArtifactSearchInputError):
        search_text(_CONSOLE, pattern="BUG at|", max_matches=1)
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) is None


def test_expected_crash_fails_closed_when_search_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even if search_text raises for some other reason, the except branch must
    # swallow it and return None — a mutant deleting the try/except is killed here.
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ArtifactSearchInputError("forced")

    monkeypatch.setattr(boot_evidence, "search_text", _boom)
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at"}))
    assert boot_evidence.expected_crash_matched_line(run, _CONSOLE) is None


def test_expected_crash_matched_line_is_redacted_at_source() -> None:
    # Redaction is by sourcing: the searched bytes are the already-redacted console, so a
    # secret the Redactor replaced upstream is the placeholder by the time the line is returned.
    redacted_console = b"line1\nkernel BUG at token=[REDACTED]\nline3\n"
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at"}))
    matched = boot_evidence.expected_crash_matched_line(run, redacted_console)
    assert matched == "kernel BUG at token=[REDACTED]"


def _ports() -> runs.RunHandlerPorts:
    return runs.RunHandlerPorts(
        resolver=cast(ProviderResolver, object()),
        secret_registry=cast(SecretRegistry, object()),
        artifact_store=cast(ObjectStore, "artifact-store"),
    )


def test_register_handlers_binds_each_run_kind_to_its_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The facade must bind exactly install/boot, each to its own leaf handler — a
    # mis-wired lambda (wrong kind, wrong handler) would let the worker dispatch a job to the
    # wrong run phase.
    calls: dict[str, tuple[object, object, dict[str, object]]] = {}

    async def _fake(label: str, conn: object, job: object, **kwargs: object) -> str:
        calls[label] = (conn, job, kwargs)
        return label

    monkeypatch.setattr(
        runs_registrar,
        "install_handler",
        lambda conn, job, **kw: _fake("install", conn, job, **kw),
    )
    monkeypatch.setattr(
        runs_registrar,
        "boot_handler",
        lambda conn, job, **kw: _fake("boot", conn, job, **kw),
    )

    registry = HandlerRegistry()
    ports = _ports()
    runs.register_handlers(registry, ports=ports)

    claimed = {JobKind.INSTALL, JobKind.BOOT}
    for kind in claimed:
        assert registry.get(kind) is not None
    # Every other JobKind must remain unclaimed by this facade — a mutant that
    # additionally registered some unrelated kind is caught here.
    for kind in JobKind:
        if kind not in claimed:
            assert registry.get(kind) is None, f"facade should not claim {kind}"

    conn = cast(AsyncConnection, object())
    job = cast(Job, object())

    def _dispatch(kind: JobKind) -> str | None:
        handler = registry.get(kind)
        assert handler is not None
        return asyncio.run(handler(conn, job))

    assert _dispatch(JobKind.INSTALL) == "install"
    assert _dispatch(JobKind.BOOT) == "boot"

    # Each lambda threads the shared conn/job plus the ports the leaf handler needs.
    assert calls["install"][0] is conn and calls["install"][1] is job
    assert calls["install"][2] == {"resolver": ports.resolver}
    assert calls["boot"][0] is conn and calls["boot"][1] is job
    assert calls["boot"][2] == {
        "resolver": ports.resolver,
        "secret_registry": ports.secret_registry,
        "artifact_store": ports.artifact_store,
    }


# --- crashed_halted_live recording (ADR-0233, #747) ----------------------------------------

_PANIC_CONSOLE = b"[ 1.45] Kernel panic - not syncing: VFS: Unable to mount root fs\n"

_PROFILE_DICT: dict[str, object] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
        }
    },
}


class _Pol:
    """Fake ProfilePolicy carrying just the predicates the recording path reads."""

    def __init__(self, *, gdbstub: bool, host_dump: bool, kdump: bool = False) -> None:
        self._gdbstub, self._host_dump, self._kdump = gdbstub, host_dump, kdump

    def gdbstub_provisioned(self, _profile: object) -> bool:
        return self._gdbstub

    def host_dump_provisioned(self, _profile: object) -> bool:
        return self._host_dump

    def capture_method(self, _profile: object) -> CaptureMethod:
        return CaptureMethod.KDUMP if self._kdump else CaptureMethod.CONSOLE


class _Connector:
    """Fake Connector: open_transport raises when the stub is unreachable."""

    def __init__(
        self,
        *,
        raises: bool,
        category: ErrorCategory = ErrorCategory.DEBUG_ATTACH_FAILURE,
    ) -> None:
        self._raises = raises
        self._category = category

    def open_transport(self, _system: object, _kind: object) -> object:
        if self._raises:
            raise CategorizedError("no stub", category=self._category)
        return object()

    def close_transport(self, _handle: object) -> None: ...


def _pol(*, gdbstub: bool, host_dump: bool) -> ProfilePolicy:
    return cast(ProfilePolicy, _Pol(gdbstub=gdbstub, host_dump=host_dump))


def test_available_capture_without_preserve() -> None:
    out = boot_evidence.available_capture(
        _pol(gdbstub=True, host_dump=False), cast(ProvisioningProfile, object())
    )
    assert out == ["gdbstub", "console"]


def test_available_capture_with_preserve() -> None:
    out = boot_evidence.available_capture(
        _pol(gdbstub=True, host_dump=True), cast(ProvisioningProfile, object())
    )
    assert out == ["gdbstub", "console", "host_dump"]


def test_inert_capture_empty_for_console_only_profile() -> None:
    out = boot_evidence.inert_capture(
        _pol(gdbstub=False, host_dump=False), cast(ProvisioningProfile, object())
    )
    assert out == []


def test_inert_capture_orders_gdbstub_host_dump_kdump() -> None:
    pol = cast(ProfilePolicy, _Pol(gdbstub=True, host_dump=True, kdump=True))
    out = boot_evidence.inert_capture(pol, cast(ProvisioningProfile, object()))
    assert out == ["gdbstub", "host_dump", "kdump"]


def test_inert_capture_kdump_only_when_crashkernel_set() -> None:
    pol = cast(ProfilePolicy, _Pol(gdbstub=False, host_dump=False, kdump=True))
    out = boot_evidence.inert_capture(pol, cast(ProvisioningProfile, object()))
    assert out == ["kdump"]


def test_gdbstub_reachable_true_when_open_succeeds() -> None:
    conn = cast(Connector, _Connector(raises=False))
    assert boot_evidence.gdbstub_reachable(conn, uuid4()) is True


def test_gdbstub_reachable_false_when_open_raises() -> None:
    conn = cast(Connector, _Connector(raises=True))
    assert boot_evidence.gdbstub_reachable(conn, uuid4()) is False


def test_gdbstub_reachable_preserves_transport_failure_category() -> None:
    conn = cast(Connector, _Connector(raises=True, category=ErrorCategory.TRANSPORT_FAILURE))
    with pytest.raises(CategorizedError) as exc:
        boot_evidence.gdbstub_reachable(conn, uuid4())
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


@dataclass
class _FakeSystem:
    provisioning_profile: dict[str, object]


def _record(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gdbstub: bool,
    host_dump: bool,
    console: bytes | None,
    reachable: bool,
    failure_category: ErrorCategory = ErrorCategory.DEBUG_ATTACH_FAILURE,
) -> tuple[BootStepResult | None, list[object]]:
    audits: list[object] = []

    async def _fake_get(_conn: object, _system_id: object) -> _FakeSystem:
        return _FakeSystem(_PROFILE_DICT)

    async def _fake_capture(*_a: object, **_k: object) -> object:
        if console is None:
            return None
        return boot_evidence.ConsoleArtifact(uuid4(), "tenant/console", console)

    async def _fake_audit(_conn: object, _ctx: object, run: object) -> None:
        audits.append(run)

    monkeypatch.setattr(boot_evidence.SYSTEMS, "get", _fake_get)
    monkeypatch.setattr(boot_evidence, "_capture_console_artifact", _fake_capture)
    monkeypatch.setattr(boot_evidence, "record_boot_audit", _fake_audit)

    async def _run() -> BootStepResult | None:
        return await boot_evidence.record_crash_halted_live(
            cast(AsyncConnection, object()),
            cast(RequestContext, object()),
            cast(Run, _FakeRun(None)),
            system_id=uuid4(),
            connector=cast(Connector, _Connector(raises=not reachable, category=failure_category)),
            profile_policy=cast(ProfilePolicy, _Pol(gdbstub=gdbstub, host_dump=host_dump)),
            secret_registry=cast(SecretRegistry, object()),
            artifact_store=cast(ObjectStore, object()),
            snapshotter=None,
            mark=0,
        )

    return asyncio.run(_run()), audits


def test_records_crashed_halted_live_on_panic_with_reachable_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, audits = _record(
        monkeypatch, gdbstub=True, host_dump=False, console=_PANIC_CONSOLE, reachable=True
    )
    assert result is not None
    assert result["boot_outcome"] == "crashed_halted_live"
    assert result["available_capture"] == ["gdbstub", "console"]
    assert len(audits) == 1  # the gate reversal is audited like the other outcomes


def test_available_capture_includes_host_dump_when_preserve_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _record(
        monkeypatch, gdbstub=True, host_dump=True, console=_PANIC_CONSOLE, reachable=True
    )
    assert result is not None
    assert result["available_capture"] == ["gdbstub", "console", "host_dump"]


def test_no_record_when_stub_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    result, audits = _record(
        monkeypatch, gdbstub=True, host_dump=False, console=_PANIC_CONSOLE, reachable=False
    )
    assert result is None and audits == []


def test_transport_failure_propagates_from_crashed_halted_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CategorizedError) as exc:
        _record(
            monkeypatch,
            gdbstub=True,
            host_dump=False,
            console=_PANIC_CONSOLE,
            reachable=False,
            failure_category=ErrorCategory.TRANSPORT_FAILURE,
        )
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_no_record_when_console_has_no_panic(monkeypatch: pytest.MonkeyPatch) -> None:
    # Panic signature, not the probe, is the crash signal: a reachable stub is not enough.
    result, audits = _record(
        monkeypatch, gdbstub=True, host_dump=False, console=b"[ 2.0] systemd up\n", reachable=True
    )
    assert result is None and audits == []


def test_no_record_when_gdbstub_not_provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    result, audits = _record(
        monkeypatch, gdbstub=False, host_dump=False, console=_PANIC_CONSOLE, reachable=True
    )
    assert result is None and audits == []


def _record_expected(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gdbstub: bool,
    host_dump: bool,
    kdump: bool,
    system_present: bool,
) -> tuple[BootStepResult | None, list[object]]:
    audits: list[object] = []

    async def _fake_get(_conn: object, _system_id: object) -> _FakeSystem | None:
        return _FakeSystem(_PROFILE_DICT) if system_present else None

    async def _fake_audit(_conn: object, _ctx: object, run: object) -> None:
        audits.append(run)

    monkeypatch.setattr(boot_evidence.SYSTEMS, "get", _fake_get)
    monkeypatch.setattr(boot_evidence, "record_boot_audit", _fake_audit)

    artifact = boot_evidence.ConsoleArtifact(uuid4(), "tenant/console", _PANIC_CONSOLE)

    async def _run() -> BootStepResult | None:
        return await boot_evidence.record_expected_crash(
            cast(AsyncConnection, object()),
            cast(RequestContext, object()),
            cast(Run, _FakeRun({"kind": "console_crash", "pattern": "panic"})),
            system_id=uuid4(),
            profile_policy=cast(
                ProfilePolicy, _Pol(gdbstub=gdbstub, host_dump=host_dump, kdump=kdump)
            ),
            artifact=artifact,
            matched_line="Kernel panic - not syncing: matched line",
        )

    return asyncio.run(_run()), audits


def test_record_expected_crash_discloses_console_and_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, audits = _record_expected(
        monkeypatch, gdbstub=True, host_dump=True, kdump=False, system_present=True
    )
    assert result is not None
    assert result["boot_outcome"] == "expected_crash_observed"
    assert result["expectation_matched"] is True
    assert result["available_capture"] == ["console"]
    assert result["inert_capture"] == ["gdbstub", "host_dump"]
    assert result["matched_line"] == "Kernel panic - not syncing: matched line"
    assert len(audits) == 1


def test_record_expected_crash_degrades_when_system_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _record_expected(
        monkeypatch, gdbstub=True, host_dump=True, kdump=True, system_present=False
    )
    assert result is not None
    assert result["available_capture"] == ["console"]
    assert result["inert_capture"] == []


def test_record_expected_crash_degrades_when_profile_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_profile: object) -> object:
        raise CategorizedError("bad profile", category=ErrorCategory.CONFIGURATION_ERROR)

    monkeypatch.setattr(boot_evidence.ProvisioningProfile, "parse", staticmethod(_raise))
    # System present + capture flags provisioned, but the profile fails to parse: the outcome is
    # still recorded (best-effort disclosure), inert set empty (ADR-0239).
    result, audits = _record_expected(
        monkeypatch, gdbstub=True, host_dump=True, kdump=True, system_present=True
    )
    assert result is not None
    assert result["boot_outcome"] == "expected_crash_observed"
    assert result["available_capture"] == ["console"]
    assert result["inert_capture"] == []
    assert len(audits) == 1


def test_local_console_artifact_is_per_run_immutable(migrated_url: str) -> None:
    # ADR-0235: two Runs against one System write distinct, immutable console rows; a same-Run
    # re-boot refreshes that Run's own row rather than inserting a duplicate, so an earlier Run's
    # evidence id never resolves to a later boot's bytes.
    system_id = uuid4()
    run_a, run_b = uuid4(), uuid4()

    def _stored(run_id: object, etag: str) -> StoredArtifact:
        key = f"local/systems/{system_id}/console-{run_id}"
        return StoredArtifact(key, etag, Sensitivity.REDACTED, "console")

    async def _seed(conn: AsyncConnection) -> None:
        # run_id is an FK (ADR-0279); both Runs must exist for their console rows to insert.
        resource_id, allocation_id, investigation_id = uuid4(), uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) VALUES "
            "(%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
            (resource_id,),
        )
        await conn.execute(
            "INSERT INTO allocations (id, resource_id, state, principal, project) "
            "VALUES (%s, %s, 'granted', 'p', 'proj')",
            (allocation_id, resource_id),
        )
        await conn.execute(
            "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, "
            "project) VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
            (system_id, allocation_id),
        )
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'p', 'proj', 't', 'open')",
            (investigation_id,),
        )
        for run_id in (run_a, run_b):
            await conn.execute(
                "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, "
                "build_profile, principal, project) "
                "VALUES (%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
                (run_id, investigation_id, system_id),
            )

    async def _run() -> tuple[boot_evidence.ConsoleArtifact, ...]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                await _seed(conn)
                a1 = await boot_evidence._upsert_console_artifact_row(
                    conn, system_id, run_a, _stored(run_a, "etag-a"), b"crash-A"
                )
                b1 = await boot_evidence._upsert_console_artifact_row(
                    conn, system_id, run_b, _stored(run_b, "etag-b"), b"boot-B"
                )
                a2 = await boot_evidence._upsert_console_artifact_row(
                    conn, system_id, run_a, _stored(run_a, "etag-a2"), b"crash-A"
                )
        return a1, b1, a2

    a1, b1, a2 = asyncio.run(_run())
    assert a1.id != b1.id  # distinct Runs -> distinct rows (no cross-Run overwrite)
    assert a2.id == a1.id  # same Run re-boot -> refreshes its own row, no duplicate
    assert a1.object_key.endswith(f"console-{run_a}")
    assert b1.object_key.endswith(f"console-{run_b}")


def test_mark_boot_window_local_is_zero_regardless_of_log_size(tmp_path, monkeypatch) -> None:
    # Local (no snapshotter): no slice is taken. libvirt renders the serial <log> append='off',
    # truncating it per power-cycle (ADR-0258), so the whole current file is this boot — the mark
    # is always 0 even when a prior boot left bytes on disk.
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"prior boot bytes\n")
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: log)

    assert asyncio.run(boot_evidence.mark_boot_window(system_id, None)) == 0


def test_mark_boot_window_remote_uses_snapshotter() -> None:
    class _Snap:
        async def mark_boot_window(self, system_id):
            return 7

        async def snapshot(self, conn, system_id, run_id, start_index=0):
            return None

    assert asyncio.run(boot_evidence.mark_boot_window(uuid4(), _Snap())) == 7


def test_mark_boot_window_degrades_to_zero_on_failure() -> None:
    class _Boom:
        async def mark_boot_window(self, system_id):
            raise RuntimeError("s3 down")

        async def snapshot(self, conn, system_id, run_id, start_index=0):
            return None

    # Best-effort: a mark-read failure must not propagate; it degrades to cumulative (0).
    assert asyncio.run(boot_evidence.mark_boot_window(uuid4(), _Boom())) == 0


def test_local_capture_excludes_prior_boot_panic_via_truncation(tmp_path, monkeypatch) -> None:
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: log)

    # Run B power-cycles the domain; libvirt's append='off' serial <log> truncates on start
    # (ADR-0258), so by capture time the per-System log holds ONLY this boot's bytes — Run A's
    # prior panic is gone from disk, not merely sliced off by an offset.
    log.write_bytes(b"[run B] booted clean READY\n")

    mark_b = asyncio.run(boot_evidence.mark_boot_window(system_id, None))
    assert mark_b == 0  # local takes no slice

    redacted_b = asyncio.run(boot_evidence.read_redacted_console(system_id, SecretRegistry()))

    assert redacted_b == b"[run B] booted clean READY\n"
    assert not boot_evidence.generic_panic_matches(redacted_b)
