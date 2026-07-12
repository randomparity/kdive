"""The ``kdive stage-volume`` operator command (ADR-0336).

Places a locally-built qcow2 onto a remote-libvirt host as a base-image volume **and** captures the
image's ``/boot/config-<ver>`` in the same step, so a remote ``staged`` image can offer its kernel
config just like a published one. It is the remote counterpart to the local staged-path capture:
remote has no build hook and kdive reaches the host only over ``qemu+tls``, so the operator running
this command — while the built qcow2 is still local — is the only controllable capture moment.

Order and failure semantics:

1. Resolve the target ``[[image]]`` catalog row (a ``staged`` volume row). **Fail fast** if it is
   absent — you cannot stage a volume for an image the catalog does not know; declare and reconcile
   the ``[[image]]`` first.
2. Probe ``/boot/config`` locally on the qcow2 (advisory — a probe miss just means no offer).
3. Upload the qcow2 into the host's storage pool. **Fatal** — the volume must land.
4. When a config was captured, upload it to the object store and set the row's
   ``kernel_config_key`` (advisory — the volume already landed, so a capture/attach miss leaves the
   image staged with no offer).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.provenance_probes import (
    DEFAULT_BOOT_ENTRIES_PROBE,
    DEFAULT_KERNEL_CONFIG_PROBE,
    BootEntriesProbeSeam,
    KernelConfigProbeSeam,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import baseline_kernel_names

_log = logging.getLogger(__name__)

_VMLINUZ_PREFIX = "vmlinuz-"


@dataclass(frozen=True, slots=True)
class _TargetRow:
    """The resolved staged-volume catalog row the command uploads into."""

    row_id: UUID
    volume: str


@dataclass(frozen=True, slots=True)
class StageVolumeDeps:
    """The injected seams the orchestration drives (env-wired by :func:`run_stage_volume`)."""

    find_row: Callable[[str, str, str], _TargetRow]
    capture_config: Callable[[Path], bytes | None]
    upload_volume: Callable[[str, Path], None]
    attach_config: Callable[[str, str, str, UUID, bytes], None]


def capture_kernel_config(
    qcow2: Path,
    *,
    boot_entries_probe: BootEntriesProbeSeam = DEFAULT_BOOT_ENTRIES_PROBE,
    kernel_config_probe: KernelConfigProbeSeam = DEFAULT_KERNEL_CONFIG_PROBE,
) -> bytes | None:
    """The image's ``/boot/config-<ver>`` bytes, or ``None`` — the same rule build-fs uses.

    Probes ``/boot``, and only when exactly one non-rescue kernel is present (an unambiguous default
    per ``baseline_kernel_names``) reads that version's config. Advisory: an unproduceable listing,
    zero/many kernels, an absent config, or a probe ``CategorizedError`` all degrade to ``None``.
    """
    try:
        entries = boot_entries_probe(qcow2)
    except CategorizedError:
        _log.warning("stage-volume: /boot listing failed for %s; no config captured", qcow2)
        return None
    if entries is None:
        return None
    kernels = baseline_kernel_names(entries)
    if len(kernels) != 1:
        return None
    version = kernels[0][len(_VMLINUZ_PREFIX) :]
    try:
        return kernel_config_probe(qcow2, version)
    except CategorizedError:
        _log.warning("stage-volume: kernel-config probe failed for %s; no config captured", version)
        return None


def stage_volume(provider: str, name: str, arch: str, qcow2: Path, deps: StageVolumeDeps) -> None:
    """Resolve the row (fail-fast), capture the config, upload the volume, attach the config.

    See the module docstring for the ordering and failure semantics. The volume upload is the only
    fatal step; the config capture and attach are advisory.
    """
    target = deps.find_row(provider, name, arch)
    config = deps.capture_config(qcow2)
    deps.upload_volume(target.volume, qcow2)
    if config is None:
        _log.info("stage-volume: no kernel config captured for %s/%s; volume staged", name, arch)
        return
    try:
        deps.attach_config(provider, name, arch, target.row_id, config)
    except CategorizedError:
        _log.warning(
            "stage-volume: config attach failed for %s/%s; volume staged with no offer",
            name,
            arch,
            exc_info=True,
        )


def add_stage_volume_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``stage-volume``: upload a built qcow2 to a remote-libvirt pool + capture config."""
    stage = sub.add_parser(
        "stage-volume",
        help="upload a built qcow2 to a remote-libvirt storage pool and capture its kernel config",
    )
    stage.add_argument("--provider", default="remote-libvirt", help="the target provider")
    stage.add_argument("--image", required=True, help="the declared [[image]] catalog name")
    stage.add_argument("--arch", default="x86_64", help="the image arch (default x86_64)")
    stage.add_argument(
        "--from", dest="source", required=True, help="the local built qcow2 to upload"
    )


def run_stage_volume(args: argparse.Namespace) -> None:
    """Wire the env-backed seams and run one ``stage-volume`` orchestration."""
    from kdive.images.rootfs.stage_volume_wiring import build_stage_volume_deps

    qcow2 = Path(args.source).resolve()
    if not qcow2.is_file():
        raise CategorizedError(
            f"stage-volume source qcow2 does not exist: {qcow2}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"source": str(qcow2)},
        )
    deps = build_stage_volume_deps(args.provider)
    stage_volume(args.provider, args.image, args.arch, qcow2, deps)
