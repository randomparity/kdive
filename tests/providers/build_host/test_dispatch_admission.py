"""run_build_on_host admits a LOCAL warm-tree build only when KDIVE_KERNEL_SRC is usable."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_host_policy import KERNEL_SRC_UNSET_DETAIL
from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.domain.build_phase import BuildPhase
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.build_telemetry import BuildPhaseRecorder
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.ports import TransportCapableBuilder
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host.dispatch import (
    BuildHostTransportFactory,
    _build_over_transport_session,
    run_build_on_host,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN_ID = UUID("00000000-0000-0000-0000-0000000000d1")

_WARM_PROFILE = {
    "schema_version": 1,
    "kernel_source_ref": "linux-6.9",
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
}

_GIT_PROFILE = {
    "schema_version": 1,
    "kernel_source_ref": {"git": {"remote": "https://git.example/linux.git", "ref": "v6.9"}},
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

    def build(self, run_id: UUID, profile: ServerBuildProfile, **_: object) -> BuildOutput:
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


def test_usable_kernel_src_runs_builder(tmp_path: object) -> None:
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


class _ThreadRecordingBuilder:
    """A transport-capable builder that records the thread ``build()`` runs on.

    Advertising ``over_transport`` makes ``run_build_on_host`` treat it as remote-capable
    (the ``TransportCapableBuilder`` structural check), and ``build`` records its thread so the
    test can assert the build did not run on the event-loop thread.
    """

    def __init__(self) -> None:
        self.build_thread: threading.Thread | None = None

    def over_transport(self, transport: object, **_kw: object) -> _ThreadRecordingBuilder:
        return self

    def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
        self.build_thread = threading.current_thread()
        return BuildOutput(kernel_ref="k", debuginfo_ref="d", build_id="b")


def _ephemeral_host() -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-0000000000e1"),
        name="eph",
        kind=BuildHostKind.EPHEMERAL_LIBVIRT,
        address=None,
        ssh_credential_ref=None,
        base_image_volume="kdive-build-base.qcow2",
        workspace_root="/build",
        max_concurrent=2,
        enabled=True,
        state=BuildHostState.READY,
    )


def _git_parsed() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_GIT_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def test_transport_session_runs_off_event_loop_thread() -> None:
    """The whole transport session (factory __enter__, build, __exit__) runs off the loop thread.

    The ephemeral-libvirt factory's __enter__ provisions a VM and blocks for minutes on
    synchronous readiness waits; __exit__ tears it down. If any of that runs on the asyncio loop
    thread it freezes the worker's /livez heartbeat ticker and aux server, and the kubelet
    SIGKILLs the worker mid-build (#583, ADR-0181). asyncio.run runs the loop on this (the test's)
    thread, so the session's threads must all differ from it.
    """
    enter_thread: list[threading.Thread] = []
    exit_thread: list[threading.Thread] = []

    @contextmanager
    def _factory(
        _host: BuildHost, _registry: SecretRegistry, _run_id: UUID, _source: object
    ) -> Iterator[BuildTransport]:
        enter_thread.append(threading.current_thread())
        try:
            yield cast(BuildTransport, _FakeTransport())
        finally:
            exit_thread.append(threading.current_thread())

    builder = _ThreadRecordingBuilder()
    loop_thread = threading.current_thread()
    out = asyncio.run(
        run_build_on_host(
            builder,
            _ephemeral_host(),
            _RUN_ID,
            _git_parsed(),
            secret_registry=SecretRegistry(),
            kernel_src="",
            transport_factories={BuildHostKind.EPHEMERAL_LIBVIRT: _factory},
        )
    )

    assert out.kernel_ref == "k"
    assert len(enter_thread) == 1
    assert len(exit_thread) == 1
    assert builder.build_thread is not None
    assert enter_thread[0] is not loop_thread, "factory __enter__ ran on the event-loop thread"
    assert builder.build_thread is not loop_thread, "build ran on the event-loop thread"
    assert exit_thread[0] is not loop_thread, "factory __exit__ teardown ran on the loop thread"


class _FakeTransport:
    """A no-op transport stand-in; never used for real ssh or a build VM in this test."""


def test_local_git_build_skips_warm_tree_admission() -> None:
    # ADR-0162: a LOCAL git build clones its allowlisted remote and never reads
    # KDIVE_KERNEL_SRC, so an empty kernel_src must NOT block it (the allowlist is enforced
    # inside the builder's clone_tree, not by the warm-tree admission).
    git_profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": {
                "git": {"remote": "https://github.com/myorg/linux", "ref": "v6.9"}
            },
            "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
        }
    )
    assert isinstance(git_profile, ServerBuildProfile)
    builder = _RecordingBuilder()
    out = asyncio.run(
        run_build_on_host(
            builder,
            _local_host(),
            _RUN_ID,
            git_profile,
            secret_registry=SecretRegistry(),
            kernel_src="",
        )
    )
    assert builder.called is True
    assert out.kernel_ref == "k"


# ---------------------------------------------------------------------------
# PROVISION phase telemetry: _build_over_transport_session (ADR-0191 G1)
# ---------------------------------------------------------------------------


@dataclass
class _FakeTransportCtx:
    """A fake transport context manager that records __enter__ and __exit__ calls."""

    transport: _FakeTransport = field(default_factory=_FakeTransport)
    entered: bool = False
    exited: bool = False

    def __enter__(self) -> _FakeTransport:
        self.entered = True
        return self.transport

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.exited = True


def _provision_points(reader: InMemoryMetricReader) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "kdive.build.phase.duration":
                    out.extend(
                        p
                        for p in m.data.data_points
                        if (p.attributes or {}).get("build_phase") == BuildPhase.PROVISION.value
                    )
    return out


def _git_profile() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_GIT_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def _ephemeral_host() -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-0000000000e1"),
        name="eph",
        kind=BuildHostKind.EPHEMERAL_LIBVIRT,
        address=None,
        ssh_credential_ref=None,
        base_image_volume="kdive-build-base.qcow2",
        workspace_root="/build",
        max_concurrent=2,
        enabled=True,
        state=BuildHostState.READY,
    )


def test_provision_phase_point_emitted_on_happy_path() -> None:
    """_build_over_transport_session emits a PROVISION point with outcome=ok when all succeeds."""
    reader = InMemoryMetricReader()
    recorder = BuildPhaseRecorder(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    ctx = _FakeTransportCtx()

    def _factory(
        _host: BuildHost, _registry: SecretRegistry, _run_id: UUID, _source: object
    ) -> _FakeTransportCtx:
        return ctx

    builder = _ThreadRecordingBuilder()
    host = _ephemeral_host()
    profile = _git_profile()

    _build_over_transport_session(
        builder,
        cast(BuildHostTransportFactory, _factory),
        host=host,
        run_id=_RUN_ID,
        parsed=profile,
        source=None,
        secret_registry=SecretRegistry(),
        recorder=recorder,
    )

    pts = _provision_points(reader)
    assert pts, "No provision phase point recorded"
    assert pts[0].attributes["outcome"] == "ok"
    assert ctx.entered is True
    assert ctx.exited is True


def test_provision_phase_point_recorded_and_exit_called_when_build_body_raises() -> None:
    """When the build body raises, the PROVISION point is still emitted and __exit__ is called."""
    reader = InMemoryMetricReader()
    recorder = BuildPhaseRecorder(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    ctx = _FakeTransportCtx()

    def _factory(
        _host: BuildHost, _registry: SecretRegistry, _run_id: UUID, _source: object
    ) -> _FakeTransportCtx:
        return ctx

    class _FailingBuilder:
        def over_transport(self, transport: object, **_kw: object) -> _FailingBuilder:
            return self

        def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
            raise RuntimeError("build body exploded")

    host = _ephemeral_host()
    profile = _git_profile()

    with pytest.raises(RuntimeError, match="build body exploded"):
        _build_over_transport_session(
            cast(TransportCapableBuilder, _FailingBuilder()),
            cast(BuildHostTransportFactory, _factory),
            host=host,
            run_id=_RUN_ID,
            parsed=profile,
            source=None,
            secret_registry=SecretRegistry(),
            recorder=recorder,
        )

    # Provision succeeded (enter returned ok), so the provision point has outcome=ok.
    pts = _provision_points(reader)
    assert pts, "No provision phase point recorded after build-body failure"
    assert pts[0].attributes["outcome"] == "ok"
    # Transport teardown must have been called even though the build raised.
    assert ctx.exited is True
