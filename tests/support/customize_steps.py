"""Test-side views over a family's customization ``Step`` list (ADR-0345).

The families emit pure-data steps; the build plane partitions them into offline guestfish
file-ops and the in-guest firstboot script. These helpers let a test assert on the *plan* — what
gets baked, what runs, in what order — without rendering either side.
"""

from __future__ import annotations

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


def rendered(steps: list[Step]) -> str:
    """A space-joined, one-fragment-per-step text of the plan, for substring assertions."""
    parts: list[str] = []
    for step in steps:
        match step:
            case Mkdir(path):
                parts.append(f"mkdir {path}")
            case WriteFile(path, content) | StageFile(path, content):
                parts.append(f"write {path} {content}")
            case UploadFile(host_src, dest, mode):
                parts.append(f"upload {host_src} {dest}")
                if mode is not None:
                    parts.append(f"chmod {mode} {dest}")
            case InstallPackages(names):
                parts.append(f"install {' '.join(names)}")
            case RunCommand(sh):
                parts.append(sh)
    return " ".join(parts)


def commands(steps: list[Step]) -> list[str]:
    """The ``RunCommand`` shell lines, in order."""
    return [step.sh for step in steps if isinstance(step, RunCommand)]


def installed(steps: list[Step]) -> list[str]:
    """Every package name any ``InstallPackages`` step installs, in order."""
    return [name for step in steps if isinstance(step, InstallPackages) for name in step.names]


def baked_paths(steps: list[Step]) -> set[str]:
    """Guest paths the plan *creates* (write / stage / upload destinations).

    Only file-creating destinations count — not a path merely referenced by a ``RunCommand`` —
    because the guest-contract validator does an exact ``exists <path>``.
    """
    created: set[str] = set()
    for step in steps:
        match step:
            case WriteFile(path, _) | StageFile(path, _):
                created.add(path)
            case UploadFile(_, dest, _):
                created.add(dest)
    return created


def baked_contents(steps: list[Step]) -> dict[str, str]:
    """Guest path → text the plan writes there (an ``UploadFile`` reads its host source)."""
    contents: dict[str, str] = {}
    for step in steps:
        match step:
            case WriteFile(path, content) | StageFile(path, content):
                contents[path] = content
            case UploadFile(host_src, dest, _):
                contents[dest] = Path(host_src).read_text()
    return contents


def upload_source(steps: list[Step], dest: str) -> Path:
    """The host source of the ``UploadFile`` targeting ``dest``; fails if there is none."""
    for step in steps:
        if isinstance(step, UploadFile) and step.dest == dest:
            return step.host_src
    raise AssertionError(f"no UploadFile targets {dest}: {steps}")
