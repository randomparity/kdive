"""Renderers that turn a family's typed customization ``Step``s into concrete build actions.

The argv renderer (:func:`render_argv`) reproduces the exact ``virt-customize`` argv the families
emitted before the one-list refactor (ADR-0345, reusing ADR-0251/0288): every ``Step`` maps to the
same flags today's ``virt-customize`` path consumes, so the ``virt_customize`` build lane is
byte-identical. ``StageFile`` stages its content to a host tempfile at render time (the caller
unlinks it via ``cleanup``), mirroring the old ``_staged_upload`` helper.

The firstboot renderer (:func:`partition_steps`, :func:`render_firstboot_script`,
:func:`render_firstboot_unit`) supports the boot-to-self-customize path (ADR-0345): a family's
``Step`` list is partitioned into offline file-ops (applied via guestfish before boot) and exec-ops
(collected into a firstboot script the guest runs on its own first boot). ``CUSTOMIZE_UNIT`` and
``CUSTOMIZE_SCRIPT_PATH`` are shared with the offline injector (``rootfs_build.py``) via
``customization_boot.py`` so the self-removal ``rm`` targets and the injector's write locations
never skew.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    StageFile,
    Step,
    UploadFile,
    WriteFile,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    CUSTOMIZE_SCRIPT_PATH,
    CUSTOMIZE_UNIT,
)

_FILE_OP_TYPES = (Mkdir, WriteFile, StageFile, UploadFile)
_EXEC_OP_TYPES = (InstallPackages, RunCommand)


def _stage_tempfile(content: str, cleanup: list[Path]) -> Path:
    """Write ``content`` to a delete-on-cleanup host tempfile and return its path."""
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(content)
        staged = Path(handle.name)
    cleanup.append(staged)
    return staged


def render_argv(steps: list[Step], *, cleanup: list[Path]) -> list[str]:
    """Render ``steps`` into a ``virt-customize`` argv fragment (ADR-0345, ADR-0251).

    Each step maps to the flags the pre-refactor families emitted, so the rendered argv is
    byte-identical to the historical ``virt-customize`` path. ``StageFile`` and ``UploadFile`` with
    a ``mode`` expand to two flags each.

    Args:
        steps: The ordered customization steps a family emitted for one rootfs.
        cleanup: Mutable list the renderer appends staged host tempfiles to; the caller unlinks
            them after ``virt-customize`` runs.
    """
    argv: list[str] = []
    for step in steps:
        match step:
            case Mkdir(path):
                argv += ["--mkdir", path]
            case WriteFile(path, content):
                argv += ["--write", f"{path}:{content}"]
            case StageFile(path, content):
                argv += ["--upload", f"{_stage_tempfile(content, cleanup)}:{path}"]
            case UploadFile(host_src, dest, mode):
                argv += ["--upload", f"{host_src}:{dest}"]
                if mode is not None:
                    argv += ["--run-command", f"chmod {mode} {dest}"]
            case InstallPackages(names):
                argv += ["--install", ",".join(names)]
            case RunCommand(sh):
                argv += ["--run-command", sh]
    return argv


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


def render_firstboot_script(
    exec_steps: list[Step],
    *,
    console_device: str,
    unit_name: str = CUSTOMIZE_UNIT,
    script_path: str = CUSTOMIZE_SCRIPT_PATH,
    ok_marker: str,
    fail_marker: str,
) -> str:
    """Render the ``/bin/sh`` firstboot script the guest runs to self-customize (ADR-0345).

    The script installs packages, runs commands, self-removes the firstboot unit (and its
    offline-created ``multi-user.target.wants`` symlink) plus itself, then reports completion via
    a console marker before powering off. The ``trap ... EXIT`` fires the fail marker on any
    non-zero exit (``set -e``) or early exit; the success path clears the trap before echoing ok.

    Args:
        exec_steps: The ``InstallPackages``/``RunCommand`` steps, in order (see
            :func:`partition_steps`).
        console_device: The guest console device (e.g. ``hvc0``) markers are echoed to.
        unit_name: The firstboot systemd unit's file name; must match the value
            ``inject_offline`` writes (``CUSTOMIZE_UNIT`` by default).
        script_path: This script's own guest path; must match the value ``inject_offline``
            writes (``CUSTOMIZE_SCRIPT_PATH`` by default).
        ok_marker: The console line echoed on success.
        fail_marker: The console line echoed on failure (via the ``EXIT`` trap).

    Returns:
        The complete script body, including the ``#!/bin/sh`` shebang.
    """
    lines = [
        "#!/bin/sh",
        "set -e",
        f"trap 'echo {fail_marker} > /dev/{console_device}; sync; systemctl poweroff' EXIT",
    ]
    for step in exec_steps:
        match step:
            case InstallPackages(names):
                lines.append(f"dnf -y install {' '.join(names)}")
            case RunCommand(sh):
                lines.append(sh)
    lines.append(
        f"rm -f /etc/systemd/system/{unit_name} "
        f"/etc/systemd/system/multi-user.target.wants/{unit_name} {script_path}"
    )
    lines += [
        "trap - EXIT",
        f"echo {ok_marker} > /dev/{console_device}",
        "sync",
        "systemctl poweroff",
    ]
    return "\n".join(lines) + "\n"


def render_firstboot_unit(*, script_path: str = CUSTOMIZE_SCRIPT_PATH) -> str:
    """Render the firstboot systemd unit body (ADR-0345).

    This unit is the boot-to-self-customize bootstrap: it has no in-guest ``systemctl enable``
    (that would run in the very firstboot it is trying to trigger), so ``inject_offline`` enables
    it offline via a guestfish symlink into ``multi-user.target.wants``.

    Args:
        script_path: The firstboot script's guest path, used as ``ExecStart``; must match
            ``render_firstboot_script``'s ``script_path`` (``CUSTOMIZE_SCRIPT_PATH`` by default).

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
        f"ExecStart={script_path}\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
