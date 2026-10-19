"""Exact-byte inspected remote staging (ADR-0336, ADR-0776).

Root inspection and digest persistence are mandatory after a fresh upload. Kernel-config
capture/attachment remains advisory. A private self-contained qcow2 snapshot binds inspection,
hash and upload to the same bytes; catalog facts describe stage-time identity only.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
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
    RootBootProbeSeam,
)
from kdive.images.rootfs.baseline import VMLINUZ_PREFIX, baseline_kernel_names

_log = logging.getLogger(__name__)
_CHUNK_BYTES = 1 << 20
# Dirty, corrupt, compression and extended-L2 bits do not introduce external bytes.
_SELF_CONTAINED_FEATURES = 0b11011


@dataclass(frozen=True, slots=True)
class _TargetRow:
    """The resolved staged-volume catalog row the command uploads into."""

    row_id: UUID
    volume: str
    state: str = "registered"
    publication_attempt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class StageVolumeDeps:
    """The injected seams the stage-volume CLI composition provides to the orchestration."""

    find_row: Callable[[str, str, str], _TargetRow]
    capture_config: Callable[[Path], bytes | None]
    upload_volume: Callable[[str, Path], None]
    attach_config: Callable[[str, str, str, UUID, bytes], None]
    inspect_root: RootBootProbeSeam
    persist_root: Callable[[str, str, str, _TargetRow, str, dict[str, object]], None]


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
    version = kernels[0][len(VMLINUZ_PREFIX) :]
    try:
        return kernel_config_probe(qcow2, version)
    except CategorizedError:
        _log.warning("stage-volume: kernel-config probe failed for %s; no config captured", version)
        return None


def _snapshot_digest(source: Path, snapshot: Path) -> str:
    """Copy with bounded memory and refuse external qcow2 bytes before inspection.

    Header offsets/features follow https://www.qemu.org/docs/master/interop/qcow2.html.
    The inspector owns the remaining qcow2/filesystem validity checks.
    """
    try:
        with source.open("rb") as incoming, snapshot.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=_CHUNK_BYTES)
        snapshot.chmod(0o400)
        with snapshot.open("rb") as handle:
            header = handle.read(104)
            version = int.from_bytes(header[4:8], "big")
            features = int.from_bytes(header[72:80], "big") if version == 3 else 0
            if (
                header[:4] != b"QFI\xfb"
                or version not in (2, 3)
                or len(header) < (72 if version == 2 else 104)
                or int.from_bytes(header[8:16], "big") != 0
                or features & ~_SELF_CONTAINED_FEATURES
            ):
                raise CategorizedError(
                    "stage-volume requires a self-contained qcow2 v2/v3 without backing or "
                    "external data files; flatten the image locally and retry with a fresh volume",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                )
            handle.seek(0)
            digest = hashlib.sha256()
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError as exc:
        raise CategorizedError(
            "stage-volume could not snapshot/hash the image; check source access and temporary "
            "disk space for one qcow2 copy, then retry",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc


def stage_volume(provider: str, name: str, arch: str, qcow2: Path, deps: StageVolumeDeps) -> None:
    """Inspect a private snapshot, fresh-upload it, then commit matching root facts.

    Inspection/upload/root persistence failures are fatal; config remains advisory.
    A post-upload persistence failure requires operator cleanup or a fresh target.
    """
    target = deps.find_row(provider, name, arch)
    if arch != "x86_64" and arch != "ppc64le":
        raise CategorizedError(
            "stage-volume architecture must be x86_64 or ppc64le",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    with tempfile.TemporaryDirectory(prefix="kdive-stage-volume-") as directory:
        snapshot = Path(directory) / "image.qcow2"
        digest = _snapshot_digest(qcow2, snapshot)
        root = deps.inspect_root(snapshot, arch, digest)
        if (
            root.architecture != arch
            or root.authority != "stage-inspection"
            or root.source.kind != "staged-image"
            or root.source.identity != digest
        ):
            raise CategorizedError(
                "stage-volume root inspection does not match the snapshot; rebuild and retry",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        config = deps.capture_config(snapshot)
        deps.upload_volume(target.volume, snapshot)
        deps.persist_root(
            provider, name, arch, target, digest, root.model_dump(mode="json", by_alias=True)
        )
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
