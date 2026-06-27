"""Build provenance capture on both clone paths (#778).

A successful build records WHAT was actually built — ``{remote, ref, resolved_commit, build_host}``
for a git source (remote userinfo-stripped), best-effort ``{label, resolved_commit?}`` for a
warm tree — onto ``BuildOutput.build_provenance``. Capture is best-effort and must never fail the
build.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.observability.build_telemetry import BuildPhaseRecorder
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.ports.build import (
    Builder,
    TransportCapableBuilder,
)
from kdive.providers.ports.build_transport import BuildTransport, CommandResult
from kdive.providers.shared.build_host.dispatch import (
    BuildHostDispatchRequest,
    BuildHostTransportFactory,
    _build_over_transport_session,
    run_build_on_host,
)
from kdive.providers.shared.build_host.transports.transport_seams import transport_git_checkout
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN_ID = UUID("00000000-0000-0000-0000-0000000000d1")
_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # pragma: allowlist secret

_CREDENTIALED_REMOTE = "https://u:tok@git.example/linux.git"  # pragma: allowlist secret
_GIT_PROFILE = {
    "schema_version": 1,
    "kernel_source_ref": {"git": {"remote": _CREDENTIALED_REMOTE, "ref": "v6.9"}},
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
}
_WARM_PROFILE = {
    "schema_version": 1,
    "kernel_source_ref": "linux-6.9",
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
}


def _git_parsed() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_GIT_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def _warm_parsed() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_WARM_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def _request(
    builder: object,
    host: BuildHost,
    parsed: ServerBuildProfile,
    *,
    kernel_src: str = "",
) -> BuildHostDispatchRequest:
    return BuildHostDispatchRequest(
        builder=cast(Builder, builder),
        host=host,
        run_id=_RUN_ID,
        parsed=parsed,
        secret_registry=SecretRegistry(),
        kernel_src=kernel_src,
    )


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
        toolchain_desc=None,
    )


def _ephemeral_host() -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-0000000000e1"),
        name="buildhost-eph",
        kind=BuildHostKind.EPHEMERAL_LIBVIRT,
        address=None,
        ssh_credential_ref=None,
        base_image_volume="kdive-build-base.qcow2",
        workspace_root="/build",
        max_concurrent=2,
        enabled=True,
        state=BuildHostState.READY,
        toolchain_desc=None,
    )


# ---------------------------------------------------------------------------
# transport_git_checkout returns explicit provenance
# ---------------------------------------------------------------------------


class _CloneTransport:
    """A no-op transport whose ``clone`` returns a canned SHA; merge-config steps all succeed.

    ``run`` reports success for every command (defconfig, merge_config.sh) and ``write_bytes`` is a
    no-op, so a checkout with no patch_ref runs end-to-end over this fake with no real subprocess.
    """

    def clone(self, remote: str, ref: str, dest: str) -> str:
        return _SHA

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    def write_bytes(self, path: str, data: bytes) -> None:
        return None


def test_transport_git_checkout_returns_stripped_provenance() -> None:
    checkout = transport_git_checkout(
        cast(BuildTransport, _CloneTransport()),
        _CREDENTIALED_REMOTE,
        "v6.9",
        SecretRegistry(),
    )
    provenance = checkout(_RUN_ID, _git_parsed(), Path("/ws"), b"FRAG=y\n")

    # The credentialed remote is userinfo-stripped before entering provenance.
    assert provenance is not None
    assert provenance.remote == "https://git.example/linux.git"
    assert provenance.ref == "v6.9"
    assert provenance.resolved_commit == _SHA


def test_transport_git_checkout_without_sink_does_not_raise() -> None:
    checkout = transport_git_checkout(
        cast(BuildTransport, _CloneTransport()),
        "https://git.example/linux.git",
        "v6.9",
        SecretRegistry(),
    )
    # No sink supplied: the checkout still completes (capture is optional).
    checkout(_RUN_ID, _git_parsed(), Path("/ws"), b"FRAG=y\n")


# ---------------------------------------------------------------------------
# Dispatch (git path): _build_over_transport_session attaches full provenance
# ---------------------------------------------------------------------------


class _FakeTransport:
    """No-op transport stand-in for the dispatch session (build never really runs)."""


class _SinkRecordingBuilder:
    """A transport-capable builder whose bound build returns explicit clone provenance."""

    def __init__(self, *, write_sink: bool = True) -> None:
        self.write_sink = write_sink

    def over_transport(self, transport: object, **_kw: object) -> _SinkRecordingBuilder:
        return self

    def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
        output = BuildOutput(kernel_ref="k", debuginfo_ref="d", build_id="b")
        if not self.write_sink:
            return output
        return output._replace(
            build_provenance={
                "remote": "https://git.example/linux.git",
                "ref": "v6.9",
                "resolved_commit": _SHA,
            }
        )


def _factory_for(ctx_transport: _FakeTransport) -> BuildHostTransportFactory:
    class _Ctx:
        def __enter__(self) -> _FakeTransport:
            return ctx_transport

        def __exit__(self, *a: object) -> None:
            return None

    def _factory(_host: BuildHost, _reg: SecretRegistry, _run_id: UUID, _source: object) -> _Ctx:
        return _Ctx()

    return cast(BuildHostTransportFactory, _factory)


def test_transport_session_attaches_build_host_to_provenance() -> None:
    builder = _SinkRecordingBuilder()
    host = _ephemeral_host()
    out = _build_over_transport_session(
        cast(TransportCapableBuilder, builder),
        _factory_for(_FakeTransport()),
        host=host,
        run_id=_RUN_ID,
        parsed=_git_parsed(),
        source=None,
        secret_registry=SecretRegistry(),
        recorder=BuildPhaseRecorder.disabled(),
    )
    assert out.build_provenance == {
        "remote": "https://git.example/linux.git",
        "ref": "v6.9",
        "resolved_commit": _SHA,
        "build_host": host.name,
    }


def test_transport_session_no_provenance_when_checkout_records_nothing() -> None:
    # A build that never populates the sink yields a None provenance, not an empty dict.
    builder = _SinkRecordingBuilder(write_sink=False)
    out = _build_over_transport_session(
        cast(TransportCapableBuilder, builder),
        _factory_for(_FakeTransport()),
        host=_ephemeral_host(),
        run_id=_RUN_ID,
        parsed=_git_parsed(),
        source=None,
        secret_registry=SecretRegistry(),
        recorder=BuildPhaseRecorder.disabled(),
    )
    assert out.build_provenance is None


# ---------------------------------------------------------------------------
# Worker-local git lane (ADR-0162): full {remote, ref, resolved_commit, build_host}
# ---------------------------------------------------------------------------


class _LocalGitBuilder:
    """A worker-local builder whose ``build`` fills a clone sink, then attaches it (#778).

    Mirrors the real provider contract: the checkout seam (``clone_tree``) records the clone's
    userinfo-stripped ``{remote, ref, resolved_commit}`` into a sink the builder closes over, and
    ``build`` attaches ``dict(sink)`` onto the returned ``BuildOutput`` when the sink is non-empty.
    """

    def __init__(self, sink: dict[str, str], *, fill: bool = True) -> None:
        self._sink = sink
        self._fill = fill

    def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
        if self._fill:
            # The remote arrives userinfo-stripped (strip_userinfo runs inside clone_tree).
            self._sink["remote"] = "https://git.example/linux.git"
            self._sink["ref"] = "v6.9"
            self._sink["resolved_commit"] = _SHA
        out = BuildOutput(kernel_ref="k", debuginfo_ref="d", build_id="b")
        if self._sink:
            out = out._replace(build_provenance=dict(self._sink))
        return out


def test_local_git_build_records_full_stripped_provenance() -> None:
    sink: dict[str, str] = {}
    builder = _LocalGitBuilder(sink)
    host = _local_host()
    out = asyncio.run(
        run_build_on_host(
            _request(cast(TransportCapableBuilder, builder), host, _git_parsed()),
        )
    )
    # The dispatch LOCAL git branch appends build_host to the clone-filled provenance, and the
    # credentialed remote never leaks (it was userinfo-stripped inside the clone seam).
    assert out.build_provenance == {
        "remote": "https://git.example/linux.git",
        "ref": "v6.9",
        "resolved_commit": _SHA,
        "build_host": host.name,
    }


def test_local_git_build_no_provenance_when_clone_records_nothing() -> None:
    # The clone seam recorded nothing (best-effort fill failed): provenance stays None, not {}.
    sink: dict[str, str] = {}
    builder = _LocalGitBuilder(sink, fill=False)
    out = asyncio.run(
        run_build_on_host(
            _request(cast(TransportCapableBuilder, builder), _local_host(), _git_parsed()),
        )
    )
    assert out.build_provenance is None


# ---------------------------------------------------------------------------
# Warm-tree path: best-effort {label, resolved_commit?}, never fails the build
# ---------------------------------------------------------------------------


class _WarmBuilder:
    def __init__(self) -> None:
        self.called = False

    def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
        self.called = True
        return BuildOutput(kernel_ref="k", debuginfo_ref="d", build_id="b")


def _git_init_commit(tree: Path) -> str:
    import subprocess

    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / "f").write_text("x")
    subprocess.run(["git", "-C", str(tree), "add", "."], check=True, env={**env})
    subprocess.run(["git", "-C", str(tree), "commit", "-q", "-m", "c"], check=True, env={**env})
    return subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_warm_tree_records_label_and_resolved_commit(tmp_path: Path) -> None:
    sha = _git_init_commit(tmp_path)
    builder = _WarmBuilder()
    out = asyncio.run(
        run_build_on_host(
            _request(
                cast(TransportCapableBuilder, builder),
                _local_host(),
                _warm_parsed(),
                kernel_src=str(tmp_path),
            ),
        )
    )
    assert builder.called is True
    assert out.build_provenance == {"label": "linux-6.9", "resolved_commit": sha}


def test_warm_tree_non_git_source_degrades_to_label_only(tmp_path: Path) -> None:
    # A staged tree that is not a git repo: rev-parse fails, provenance degrades to {label}.
    builder = _WarmBuilder()
    out = asyncio.run(
        run_build_on_host(
            _request(
                cast(TransportCapableBuilder, builder),
                _local_host(),
                _warm_parsed(),
                kernel_src=str(tmp_path),
            ),
        )
    )
    assert builder.called is True
    assert out.build_provenance == {"label": "linux-6.9"}
