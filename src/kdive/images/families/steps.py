"""Typed customization ``Step`` value objects shared by every rootfs family (ADR-0345, #1147).

A :class:`FamilyCustomizer` emits one ordered list of these ``Step``s describing how to
customize a rootfs. Two renderers consume the same list: an argv renderer (the
``virt-customize`` path) and an offline-injector + firstboot renderer (the boot-to-self-customize
path). Steps are pure data — no rendering or execution logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Mkdir:
    """Create an empty directory at ``path``."""

    path: str


@dataclass(frozen=True, slots=True)
class WriteFile:
    """Write literal ``content`` to ``path``."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class StageFile:
    """Write literal ``content`` to ``path`` via a staged host tempfile upload."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class UploadFile:
    """Upload the host file at ``host_src`` to ``dest``, optionally chmod'd to ``mode``."""

    host_src: Path
    dest: str
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class InstallPackages:
    """Install the guest packages named in ``names``."""

    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunCommand:
    """Run the shell command ``sh`` in the guest."""

    sh: str


type Step = Mkdir | WriteFile | StageFile | UploadFile | InstallPackages | RunCommand
