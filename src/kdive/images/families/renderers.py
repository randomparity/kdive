"""Renderers that turn a family's typed customization ``Step``s into concrete build actions.

The argv renderer (:func:`render_argv`) reproduces the exact ``virt-customize`` argv the families
emitted before the one-list refactor (ADR-0345, reusing ADR-0251/0288): every ``Step`` maps to the
same flags today's ``virt-customize`` path consumes, so the ``virt_customize`` build lane is
byte-identical. ``StageFile`` stages its content to a host tempfile at render time (the caller
unlinks it via ``cleanup``), mirroring the old ``_staged_upload`` helper.
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
