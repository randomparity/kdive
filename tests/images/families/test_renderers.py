"""Unit tests for the customization-boot renderers (ADR-0345)."""

from __future__ import annotations

from kdive.images.families.renderers import (
    partition_steps,
    render_firstboot_script,
    render_firstboot_unit,
)
from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    Step,
    WriteFile,
)


def _script(exec_steps: list[Step], *, install_command: str, console_device: str = "hvc0") -> str:
    return render_firstboot_script(
        exec_steps,
        install_command=install_command,
        console_device=console_device,
        unit_name="kdive-customize.service",
        script_path="/usr/local/sbin/kdive-customize",
        ok_marker="kdive-customize-ok",
        fail_marker="kdive-customize-failed",
    )


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
    script = _script(
        [InstallPackages(("drgn", "kexec-tools")), RunCommand("systemctl enable kdump.service")],
        install_command="dnf -y install",
    )
    assert script.startswith("#!/bin/sh\n")
    assert "console=/dev/hvc0" in script
    assert "dnf -y install drgn kexec-tools" in script
    assert "systemctl enable kdump.service" in script
    assert "rm -f /etc/systemd/system/kdive-customize.service" in script
    assert "multi-user.target.wants/kdive-customize.service" in script
    assert "/usr/local/sbin/kdive-customize" in script
    assert script.rstrip().endswith("systemctl poweroff")
    assert 'echo kdive-customize-ok > "$console"' in script
    assert 'echo kdive-customize-failed > "$console"' in script


def test_firstboot_script_uses_the_family_install_command() -> None:
    """The package manager is the family's, not a hardcoded dnf (#1167).

    The debian family installs with a non-interactive ``apt-get``; its index refresh is a
    preceding ``RunCommand`` the family emits, so it lands in the script in step order.
    """
    apt = "DEBIAN_FRONTEND=noninteractive apt-get -y install"
    script = _script(
        [RunCommand("apt-get update"), InstallPackages(("kdump-tools", "python3-drgn"))],
        install_command=apt,
    )
    body = script.splitlines()
    assert f"{apt} kdump-tools python3-drgn" in body
    assert body.index("apt-get update") < body.index(f"{apt} kdump-tools python3-drgn")
    assert "dnf" not in script


def test_firstboot_script_derives_verdict_from_captured_status_not_a_serial_write() -> None:
    """The verdict is the captured exit status, and the steps run in a ``set -e`` subshell (#1174).

    A large volume written straight to the serial *tty* can spuriously fail (a Python program
    exits 120 on the failed shutdown-flush; even ``cat`` can error), so the steps' output is
    captured to a plain file — which honours dnf's real exit code — and the ok/failed marker is
    chosen from that captured ``$rc``. A serial-write error must never flip a good build to failed.
    """
    script = _script(
        [InstallPackages(("drgn",))], install_command="dnf -y install", console_device="ttyS0"
    )
    # The exec steps DO NOT write straight to the serial tty (that path false-fails), and the
    # verdict marker is gated on the captured subshell status, not on a serial write succeeding.
    assert "exec > /dev/ttyS0" not in script
    assert "( set -e" in script
    assert '> "$log" 2>&1' in script
    assert "rc=$?" in script
    assert 'if [ "$rc" -eq 0 ]; then' in script
    # Failure evidence still reaches the console, best-effort — a serial-write error is swallowed
    # so it cannot change the verdict.
    assert 'cat "$log" > "$console" 2>/dev/null || true' in script
    # The capture file lives on tmpfs so it never persists into the sealed, published image.
    assert "log=/run/" in script


def test_firstboot_script_syncs_before_the_ok_marker() -> None:
    """Durability: the success sync must precede the ok marker (ADR-0345).

    The orchestration force-destroys the domain the instant a poll reads the marker, so a marker
    emitted before the flush completes would let the destroy truncate the customization writes.
    """
    script = _script([InstallPackages(("drgn",))], install_command="dnf -y install")
    body = script.splitlines()
    sync_idx = body.index("sync")
    ok_idx = next(i for i, ln in enumerate(body) if ln == 'echo kdive-customize-ok > "$console"')
    assert sync_idx < ok_idx


def test_firstboot_unit_orders_after_network_and_wants_multiuser() -> None:
    unit = render_firstboot_unit(script_path="/usr/local/sbin/kdive-customize")
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/local/sbin/kdive-customize" in unit
    assert "WantedBy=multi-user.target" in unit


def test_firstboot_unit_disables_the_systemd_start_timeout() -> None:
    """The oneshot must set ``TimeoutStartSec=infinity`` (#1152).

    A customization that installs packages can easily exceed systemd's default 90s
    ``DefaultTimeoutStartSec`` (a slow dnf under TCG, or a large native install); without this,
    systemd SIGTERMs the service mid-install, the script's EXIT trap fires the ``-failed`` marker,
    and the build fails for a reason unrelated to the customization. The host orchestration's
    TCG-scaled window is the authoritative deadline, so the unit must not impose a shorter one.
    """
    unit = render_firstboot_unit(script_path="/usr/local/sbin/kdive-customize")
    assert "TimeoutStartSec=infinity" in unit
