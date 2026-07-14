"""Unit tests for the argv renderer that reproduces today's virt-customize bytes (ADR-0345)."""

from __future__ import annotations

from pathlib import Path

from kdive.images.families.renderers import render_argv
from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    StageFile,
    UploadFile,
    WriteFile,
)


def test_render_argv_maps_each_step() -> None:
    cleanup: list[Path] = []
    argv = render_argv(
        [
            Mkdir("/seed"),
            InstallPackages(("drgn", "kexec-tools")),
            RunCommand("systemctl enable kdump.service"),
            WriteFile("/etc/machine-id", "0a1b"),
            UploadFile(Path("/h/u.service"), "/etc/systemd/system/kdive-ready.service"),
        ],
        cleanup=cleanup,
    )
    assert argv == [
        "--mkdir",
        "/seed",
        "--install",
        "drgn,kexec-tools",
        "--run-command",
        "systemctl enable kdump.service",
        "--write",
        "/etc/machine-id:0a1b",
        "--upload",
        "/h/u.service:/etc/systemd/system/kdive-ready.service",
    ]


def test_stagefile_uploads_a_tempfile_with_content() -> None:
    cleanup: list[Path] = []
    argv = render_argv(
        [StageFile("/etc/cloud/x.cfg", "datasource_list: [ NoCloud ]\n")],
        cleanup=cleanup,
    )
    assert argv[0] == "--upload"
    src, _, dest = argv[1].partition(":")
    assert dest == "/etc/cloud/x.cfg"
    assert Path(src).read_text() == "datasource_list: [ NoCloud ]\n"
    assert cleanup == [Path(src)]


def test_uploadfile_mode_appends_chmod() -> None:
    argv = render_argv(
        [UploadFile(Path("/h/k"), "/usr/local/sbin/k", mode="0755")],
        cleanup=[],
    )
    assert argv == [
        "--upload",
        "/h/k:/usr/local/sbin/k",
        "--run-command",
        "chmod 0755 /usr/local/sbin/k",
    ]
