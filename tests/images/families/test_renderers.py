"""Unit tests for the argv renderer that reproduces today's virt-customize bytes (ADR-0345)."""

from __future__ import annotations

from pathlib import Path

from kdive.images.families.renderers import (
    partition_steps,
    render_argv,
    render_firstboot_script,
    render_firstboot_unit,
)
from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    StageFile,
    Step,
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


def test_partition_separates_file_and_exec_ops() -> None:
    steps: list[Step] = [
        Mkdir("/d"),
        InstallPackages(("a",)),
        WriteFile("/f", "x"),
        RunCommand("y"),
    ]
    file_ops, exec_ops = partition_steps(steps)
    assert file_ops == [Mkdir("/d"), WriteFile("/f", "x")]
    assert exec_ops == [InstallPackages(("a",)), RunCommand("y")]


def test_firstboot_script_shape() -> None:
    script = render_firstboot_script(
        [InstallPackages(("drgn", "kexec-tools")), RunCommand("systemctl enable kdump.service")],
        console_device="hvc0",
        unit_name="kdive-customize.service",
        script_path="/usr/local/sbin/kdive-customize",
        ok_marker="kdive-customize-ok",
        fail_marker="kdive-customize-failed",
    )
    assert script.startswith("#!/bin/sh\nset -e\n")
    assert "trap 'echo kdive-customize-failed > /dev/hvc0" in script
    assert "dnf -y install drgn kexec-tools" in script
    assert "systemctl enable kdump.service" in script
    assert "rm -f /etc/systemd/system/kdive-customize.service" in script
    assert "multi-user.target.wants/kdive-customize.service" in script
    assert "/usr/local/sbin/kdive-customize" in script
    assert "trap - EXIT" in script
    assert script.rstrip().endswith("systemctl poweroff")
    assert "echo kdive-customize-ok > /dev/hvc0" in script
    # Durability: the success sync must precede the ok marker, so the orchestration's
    # force-destroy (fired on reading the marker) cannot truncate the customization writes.
    body = script.splitlines()
    sync_idx = body.index("sync")
    ok_idx = next(i for i, ln in enumerate(body) if ln == "echo kdive-customize-ok > /dev/hvc0")
    assert sync_idx < ok_idx


def test_firstboot_unit_orders_after_network_and_wants_multiuser() -> None:
    unit = render_firstboot_unit(script_path="/usr/local/sbin/kdive-customize")
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/local/sbin/kdive-customize" in unit
    assert "WantedBy=multi-user.target" in unit
