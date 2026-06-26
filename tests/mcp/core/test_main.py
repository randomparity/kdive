"""CLI argument parsing for `python -m kdive`."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from kdive.__main__ import _HTTP_KEEPALIVE_S, _server_uvicorn_config, build_parser
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput
from kdive.images.rootfs_command import run_build_fs


def _patch_plane(monkeypatch: pytest.MonkeyPatch, plane: object) -> None:
    """Replace the local rootfs build-plane factory with one returning ``plane``."""
    monkeypatch.setattr(
        "kdive.images.rootfs_command._build_local_rootfs_plane",
        lambda _workspace: plane,
    )


def test_server_subcommand_parses() -> None:
    args = build_parser().parse_args(["server"])
    assert args.command == "server"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_worker_subcommand_parses_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "worker"])
    assert args.command == "worker"
    assert args.log_level == "DEBUG"


def test_no_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_server_uvicorn_config_sets_explicit_keepalive() -> None:
    """The server passes an explicit uvicorn keepalive above the common 60s proxy idle default.

    Asserts the pure helper directly (ADR-0138): ``_run_server`` awaits ``app.run_async`` which
    blocks until the server stops, so the kwarg is built by this helper instead — testing the
    real value, not a forever-blocking mock.
    """
    assert _HTTP_KEEPALIVE_S == 65.0
    assert _server_uvicorn_config() == {"timeout_keep_alive": 65.0}


def test_build_fs_subcommand_parses_with_defaults() -> None:
    args = build_parser().parse_args(["build-fs"])
    assert args.command == "build-fs"
    assert args.kind == "debug"
    assert args.distro == "fedora"
    assert args.workspace == "/var/lib/kdive/build/images"
    assert args.name == "fedora-kdive-ready-43"
    assert args.arch == "x86_64"
    assert args.releasever == "43"
    # --dest defaults to None; the handler derives /var/lib/kdive/rootfs/local/<name>.qcow2 (or
    # the catalog name with --image) so an explicit --dest can override it.
    assert args.dest is None
    assert args.packages is None  # falls back to the --kind's package set in the handler


def test_build_fs_subcommand_rejects_an_unknown_kind() -> None:
    # --kind is constrained to the registered fs kinds; an unknown value is rejected at
    # parse time rather than passed through to a KeyError in the handler.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["build-fs", "--kind", "bogus"])


def test_build_fs_subcommand_collects_repeated_packages() -> None:
    args = build_parser().parse_args(
        ["build-fs", "--dest", "/tmp/out.qcow2", "--package", "drgn", "--package", "perf"]
    )
    assert args.dest == "/tmp/out.qcow2"
    assert args.packages == ["drgn", "perf"]


def test_run_build_fs_moves_plane_output_to_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build-fs` builds via the local plane and moves the qcow2 to ``--dest``."""
    produced = tmp_path / "plane-workspace" / "fedora-kdive-ready-43.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs = []
    seen_workspaces = []

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    # Capture the workspace the factory receives so a dropped/None argument is caught.
    monkeypatch.setattr(
        "kdive.images.rootfs_command._build_local_rootfs_plane",
        lambda workspace: seen_workspaces.append(workspace) or _FakePlane(),
    )

    # A nested, not-yet-existing workspace and dest exercise the parents=True mkdir.
    workspace = tmp_path / "ws" / "nested"
    dest = tmp_path / "rootfs" / "deep" / "out.qcow2"
    args = build_parser().parse_args(
        [
            "build-fs",
            "--name",
            "custom-name",
            "--arch",
            "aarch64",
            "--workspace",
            str(workspace),
            "--dest",
            str(dest),
            "--releasever",
            "42",
            "--package",
            "drgn",
        ]
    )
    run_build_fs(args)

    assert dest.read_bytes() == b"image-bytes"
    assert not produced.exists(), "the plane output is moved, not copied"
    assert oct(dest.stat().st_mode & 0o777) == "0o644"
    assert seen_workspaces == [workspace.resolve()]
    assert seen_specs and seen_specs[0].releasever == "42"
    assert seen_specs[0].packages == ("drgn",)
    assert seen_specs[0].provider == "local-libvirt"
    assert seen_specs[0].name == "custom-name"
    assert seen_specs[0].arch == "aarch64"


def test_run_build_fs_debug_kind_sets_debug_packages_and_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--kind debug` selects the crash/introspection package set and capabilities."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs = []

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    _patch_plane(monkeypatch, _FakePlane())
    args = build_parser().parse_args(
        [
            "build-fs",
            "--kind",
            "debug",
            "--workspace",
            str(tmp_path / "ws"),
            "--dest",
            str(tmp_path / "out.qcow2"),
        ]
    )
    run_build_fs(args)

    assert seen_specs[0].packages == (
        "drgn",
        "kexec-tools",
        "makedumpfile",
        "kdump-utils",
        "keyutils",
        "openssh-server",
    )
    assert seen_specs[0].capabilities == ("agent", "kdump", "drgn")
    assert seen_specs[0].distro == "fedora"
    assert seen_specs[0].source_image_digest == "virt-builder:fedora-43"


def test_run_build_fs_build_kind_sets_build_packages_and_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--kind build` selects the kernel-build-host toolchain and the ``build`` capability."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs = []

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    _patch_plane(monkeypatch, _FakePlane())
    args = build_parser().parse_args(
        [
            "build-fs",
            "--kind",
            "build",
            "--workspace",
            str(tmp_path / "ws"),
            "--dest",
            str(tmp_path / "out.qcow2"),
        ]
    )
    run_build_fs(args)

    assert "gcc" in seen_specs[0].packages and "make" in seen_specs[0].packages
    assert seen_specs[0].capabilities == ("agent", "build")


def test_run_build_fs_default_path_synthesizes_distro_passthrough_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-`--image` path passes `--distro`/`--releasever` straight to a virt-builder digest."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs = []

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    _patch_plane(monkeypatch, _FakePlane())
    args = build_parser().parse_args(
        [
            "build-fs",
            "--distro",
            "rocky",
            "--releasever",
            "9",
            "--workspace",
            str(tmp_path / "ws"),
            "--dest",
            str(tmp_path / "out.qcow2"),
        ]
    )
    run_build_fs(args)
    assert seen_specs[0].distro == "rocky"
    assert seen_specs[0].source_image_digest == "virt-builder:rocky-9"


def test_run_build_fs_unwritable_workspace_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-writable workspace fails with an actionable message, not a bare PermissionError."""

    class _UnusedPlane:
        def build(self, spec: object) -> RootfsBuildOutput:  # pragma: no cover - never reached
            raise AssertionError("build must not run when the workspace is unwritable")

    _patch_plane(monkeypatch, _UnusedPlane())
    # The workspace dir already exists (so mkdir succeeds) but is read-only, so the write
    # probe inside it fails — this exercises the in-workspace probe, not just the mkdir.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace.chmod(0o500)
    try:
        args = build_parser().parse_args(
            ["build-fs", "--workspace", str(workspace), "--dest", str(tmp_path / "out.qcow2")]
        )
        with pytest.raises(CategorizedError) as caught:
            run_build_fs(args)
    finally:
        workspace.chmod(0o700)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "writable" in str(caught.value)
    assert caught.value.details == {
        "workspace": str(workspace.resolve()),
        "error": "PermissionError",
    }


def test_run_build_fs_reuses_an_existing_writable_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-present writable workspace is reused (exist_ok), not rejected."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    _patch_plane(monkeypatch, _FakePlane())
    workspace = tmp_path / "ws"
    workspace.mkdir()  # pre-existing and writable
    dest = tmp_path / "out.qcow2"
    args = build_parser().parse_args(
        ["build-fs", "--workspace", str(workspace), "--dest", str(dest)]
    )
    run_build_fs(args)
    assert dest.read_bytes() == b"image-bytes"


def test_run_build_fs_prints_eval_safe_export_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`build-fs` prints exactly one eval-safe export line to stdout on success."""
    produced = tmp_path / "plane-workspace" / "fedora-kdive-ready-43.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    _patch_plane(monkeypatch, _FakePlane())
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(
        ["build-fs", "--workspace", str(tmp_path / "ws"), "--dest", str(dest)]
    )
    run_build_fs(args)

    out = capsys.readouterr().out
    assert out == f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest.resolve()))}\n", (
        "stdout is exactly the eval-safe wiring line and nothing else"
    )
    assert "sha256:abc" not in out, "the digest summary stays on stderr, never on stdout"


def test_run_build_fs_export_line_round_trips_a_path_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --dest with a space is a single shlex-quoted token that round-trips to the path."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    _patch_plane(monkeypatch, _FakePlane())
    dest = tmp_path / "with space" / "out.qcow2"
    args = build_parser().parse_args(
        ["build-fs", "--workspace", str(tmp_path / "ws"), "--dest", str(dest)]
    )
    run_build_fs(args)

    out = capsys.readouterr().out.strip()
    assert out.startswith("export KDIVE_GUEST_IMAGE=")
    value = out[len("export KDIVE_GUEST_IMAGE=") :]
    assert shlex.split(value) == [str(dest.resolve())], "one token, round-trips to the path"


def test_run_build_fs_writes_nothing_to_stdout_on_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing build raises and prints no export line, so eval exports nothing."""

    class _FailingPlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            raise CategorizedError("build blew up", category=ErrorCategory.PROVISIONING_FAILURE)

    _patch_plane(monkeypatch, _FailingPlane())
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(
        ["build-fs", "--workspace", str(tmp_path / "ws"), "--dest", str(dest)]
    )
    with pytest.raises(CategorizedError):
        run_build_fs(args)
    assert capsys.readouterr().out == "", "no export line is printed when the build fails"


def test_run_build_fs_destination_publish_failure_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Destination publish I/O failures use the CLI error taxonomy."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    def _move(_src: str, _dest: str) -> str:
        raise PermissionError("destination unwritable")

    _patch_plane(monkeypatch, _FakePlane())
    monkeypatch.setattr("kdive.images.rootfs_command.shutil.move", _move)
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(
        ["build-fs", "--workspace", str(tmp_path / "ws"), "--dest", str(dest)]
    )
    with pytest.raises(CategorizedError) as caught:
        run_build_fs(args)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {
        "dest": str(dest.resolve()),
        "operation": "publish",
        "error": "PermissionError",
    }
    assert "could not publish" in str(caught.value)
    assert capsys.readouterr().out == ""
