"""Renderers that turn a family's typed customization ``Step``s into the customization boot.

:func:`partition_steps` splits a family's ``Step`` list into offline file-ops (applied via
guestfish before boot) and exec-ops; :func:`render_firstboot_script` collects the exec-ops into
the firstboot script the guest runs on its own first boot, and :func:`render_firstboot_unit`
renders the systemd unit that runs it (ADR-0345). The unit name and script path are passed in by
the provider-layer caller (``rootfs_build.py``, which shares the ``CUSTOMIZE_UNIT`` /
``CUSTOMIZE_SCRIPT_PATH`` constants with the offline injector) so the self-removal ``rm`` targets
and the injector's write locations never skew — this module stays in the ``images`` layer and does
not depend on the provider.
"""

from __future__ import annotations

from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    StageFile,
    Step,
    UploadFile,
    WriteFile,
)

_FILE_OP_TYPES = (Mkdir, WriteFile, StageFile, UploadFile)


def partition_steps(steps: list[Step]) -> tuple[list[Step], list[Step]]:
    """Split ``steps`` into offline file-ops and in-guest exec-ops, order preserved (ADR-0345).

    File-ops (``Mkdir``/``WriteFile``/``StageFile``/``UploadFile``) are applied offline via
    guestfish before boot; exec-ops (``InstallPackages``/``RunCommand``) are collected into the
    firstboot script the guest runs on its own first boot.

    Args:
        steps: The ordered customization steps a family emitted for one rootfs.

    Returns:
        A ``(file_ops, exec_ops)`` tuple, each preserving the original relative order.
    """
    file_ops: list[Step] = []
    exec_ops: list[Step] = []
    for step in steps:
        if isinstance(step, _FILE_OP_TYPES):
            file_ops.append(step)
        else:
            exec_ops.append(step)
    return file_ops, exec_ops


# The firstboot script captures the customization steps' output to a guest file, then dumps that
# file to the serial console — instead of pointing the steps' stdout straight at ``/dev/<console>``.
# Under load the serial console is a lossy, error-prone sink: a Python program (dnf) that writes a
# large volume directly to the serial *tty* fails its final stdout flush at interpreter shutdown
# and exits ``120`` (a benign flush error, not a transaction failure), and even ``tee``/``cat``
# writes to the tty can return an error. Deriving the ok/failed verdict from a serial write
# therefore false-fails a customization that actually succeeded (#1174, live-diagnosed: a Rocky 9
# ``dnf -y install epel-release`` that installs cleanly still exits 120 when its stdout is the
# console). ``/run`` is tmpfs, so the capture file never persists into the sealed image.
_CUSTOMIZE_LOG_PATH = "/run/kdive-customize.log"


def render_firstboot_script(
    exec_steps: list[Step],
    *,
    install_command: str,
    console_device: str,
    unit_name: str,
    script_path: str,
    ok_marker: str,
    fail_marker: str,
) -> str:
    """Render the ``/bin/sh`` firstboot script the guest runs to self-customize (ADR-0345, #1174).

    The exec steps run in a ``( set -e … )`` subshell whose combined output is captured to a guest
    log file; the subshell aborts on the first failing step and its exit status is the **verdict**.
    The captured log is then best-effort dumped to the serial console (so a failed install or
    RunCommand's error still lands in the console log — the failure-evidence path, #1147), and a
    single ``ok``/``failed`` marker line is echoed to the console from the captured status. The
    verdict is never derived from a serial-console write, because writing a large volume to the
    serial *tty* can spuriously fail (a Python program exits 120 on the failed shutdown flush; even
    ``cat`` can error) and would false-fail an otherwise-good customization (#1174).

    Args:
        exec_steps: The ``InstallPackages``/``RunCommand`` steps, in order (see
            :func:`partition_steps`).
        install_command: The family's package-install command prefix
            (:attr:`~kdive.images.families.base.FamilyCustomizer.install_command`); each
            ``InstallPackages`` step renders as ``<install_command> <names…>``.
        console_device: The guest console device (e.g. ``hvc0``) the log + markers are echoed to.
        unit_name: The firstboot systemd unit's file name; must match the value
            ``inject_offline`` writes (the caller passes ``CUSTOMIZE_UNIT``).
        script_path: This script's own guest path; must match the value ``inject_offline``
            writes (the caller passes ``CUSTOMIZE_SCRIPT_PATH``).
        ok_marker: The console line echoed on success.
        fail_marker: The console line echoed on failure.

    Returns:
        The complete script body, including the ``#!/bin/sh`` shebang.
    """
    lines = [
        "#!/bin/sh",
        f"console=/dev/{console_device}",
        f"log={_CUSTOMIZE_LOG_PATH}",
        # A subshell so ``set -e`` (stop on the first failing step, and NOT leak to the marker
        # logic below) is confined here; its exit status is the customization verdict. Output is
        # captured to $log (a plain file honours the package manager's real exit code — the serial
        # tty does not).
        "( set -e",
    ]
    for step in exec_steps:
        match step:
            case InstallPackages(names):
                lines.append(f"{install_command} {' '.join(names)}")
            case RunCommand(sh):
                lines.append(sh)
    lines += [
        f"rm -f /etc/systemd/system/{unit_name} "
        f"/etc/systemd/system/multi-user.target.wants/{unit_name} {script_path}",
        ') > "$log" 2>&1',
        "rc=$?",
        # Best-effort: surface the captured output on the console for diagnosis. A serial-write
        # error here must never change the verdict, so it is swallowed (``|| true``).
        'cat "$log" > "$console" 2>/dev/null || true',
        # Order matters: sync BEFORE echoing the ok marker. The orchestration force-destroys the
        # domain the instant a poll reads the marker (10s cadence, slower under TCG), so a marker
        # emitted before the flush completes would let the destroy truncate the customization
        # writes (installed packages, version markers, the unit self-removal). Flushing first means
        # the host can only ever observe ok after every write is durable (ADR-0345).
        "sync",
        'if [ "$rc" -eq 0 ]; then',
        f'echo {ok_marker} > "$console"',
        "else",
        f'echo {fail_marker} > "$console"',
        "fi",
        "sync",
        "systemctl poweroff",
    ]
    return "\n".join(lines) + "\n"


def render_firstboot_unit(*, script_path: str) -> str:
    """Render the firstboot systemd unit body (ADR-0345).

    This unit is the boot-to-self-customize bootstrap: it has no in-guest ``systemctl enable``
    (that would run in the very firstboot it is trying to trigger), so ``inject_offline`` enables
    it offline via a guestfish symlink into ``multi-user.target.wants``.

    ``TimeoutStartSec=infinity`` disables systemd's default 90s ``DefaultTimeoutStartSec``: a
    customization that installs packages can exceed it (a slow package fetch under TCG, or a large
    native install), and a timeout would SIGTERM the service mid-install, fire the script's
    ``-failed`` marker, and fail the build for a reason unrelated to the customization (#1152). The
    host orchestration's TCG-scaled window is the authoritative deadline; the unit must not impose
    a shorter one. Deliberately not a large *finite* value matched to that window: the guest cannot
    know the host's config-driven, TCG-scaled deadline, so a finite guess would either re-introduce
    the false-fail (if too short) or duplicate the host bound (if too long). The trade-off is that a
    genuinely *hung* (non-exiting) customization self-reports no marker and instead surfaces as the
    host's ``BOOT_TIMEOUT`` when the window expires; the common failure (a step exits non-zero)
    still fires the ``-failed`` marker promptly (the script's ``set -e`` subshell aborts and its
    non-zero status selects the fail marker).

    Args:
        script_path: The firstboot script's guest path, used as ``ExecStart``; must match
            ``render_firstboot_script``'s ``script_path`` (the caller passes the shared constant).

    Returns:
        The unit file body.
    """
    return (
        "[Unit]\n"
        "Description=kdive one-shot build customization\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=infinity\n"
        f"ExecStart={script_path}\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
