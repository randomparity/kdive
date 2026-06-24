"""The combined kernel+modules install bundle, shared by both build planes (ADR-0081/0101).

Both `remote_libvirt` and `local_libvirt` publish a single ``kernel`` artifact: one
gzip-compressed tar holding ``boot/vmlinuz`` (the renamed ``arch/x86/boot/bzImage``) and the
``lib/modules/<ver>/…`` tree, excluding the ``build``/``source`` back-reference symlinks
``make modules_install`` plants (absolute worker paths that must not enter the in-guest extract).
This module owns the one packaging implementation so the two providers cannot drift on the
artifact's bytes (#766 converges local-libvirt onto this shape).

The worker-local seam (:func:`local_kernel_bundle`) packages the bundle in memory as
:class:`ArtifactBytes` for a single PUT. The transport-backed seam
(:func:`transport_kernel_bundle`) tars the bundle ON the build host and returns an
:class:`ArtifactRemoteFile` so the publish step uploads it via a presigned PUT without the
worker reading the bytes (ADR-0099/0101).
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.build_transport import BuildTransport
from kdive.providers.shared.build_host.publishing.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    ArtifactSource,
)
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S

# The back-reference symlinks make modules_install plants in /lib/modules/<ver>/; they point at
# absolute paths in the worker's build tree and must not enter the in-guest bundle.
_MODULE_BACKREF_LINKS = frozenset({"build", "source"})
_BUNDLE_NAME = "kdive-bundle.tar.gz"
_BUNDLE_TAR_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S

type MakeKernelBundle = Callable[[Path, Path], ArtifactSource]


def make_kernel_bundle_bytes(workspace: Path, mod_root: Path) -> bytes:
    """Package ``boot/vmlinuz`` + ``lib/modules/<ver>/…`` into one gzip-compressed tar (bytes).

    The bzImage is renamed to ``boot/vmlinuz`` and every real file under the staging tree's
    ``lib/modules`` is added under a ``lib/modules/…`` arcname; the ``build``/``source``
    back-reference symlinks are excluded so the in-guest extract carries no dangling links. The
    whole object is held in memory for the single PUT, kept small by gzip (ADR-0081).

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if the bzImage is absent (a zero-exit make can leave
            no image) or a module file vanishes mid-pack — surfaced as a typed error rather than a
            bare ``OSError`` escaping the provider error contract.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bundle_member(tar, workspace / "arch/x86/boot/bzImage", "boot/vmlinuz", "bzImage")
        modules_root = mod_root / "lib" / "modules"
        for path in build_bundle_member_dirs(modules_root):
            arcname = "lib/modules/" + str(path.relative_to(modules_root))
            _add_bundle_member(tar, path, arcname, "module bundle", recursive=False)
    return buf.getvalue()


def build_bundle_member_dirs(modules_root: Path) -> list[Path]:
    """Sorted paths under ``modules_root``, dropping the absolute back-reference symlinks."""
    members: list[Path] = []
    for path in sorted(modules_root.rglob("*")):
        if path.is_symlink() and path.name in _MODULE_BACKREF_LINKS:
            continue
        members.append(path)
    return members


def _add_bundle_member(
    tar: tarfile.TarFile, path: Path, arcname: str, output: str, *, recursive: bool = True
) -> None:
    try:
        tar.add(path, arcname=arcname, recursive=recursive)
    except OSError as exc:
        raise CategorizedError(
            "kernel bundle could not be packaged",
            category=ErrorCategory.BUILD_FAILURE,
            details={"output": output},
        ) from exc


def local_kernel_bundle(workspace: Path, mod_root: Path) -> ArtifactSource:
    """Worker-local bundle seam: package the combined bundle in memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(make_kernel_bundle_bytes(workspace, mod_root))


def transport_kernel_bundle(t: BuildTransport) -> MakeKernelBundle:
    """Return a :data:`MakeKernelBundle` that tars the combined bundle ON the build host (ADR-0099).

    The returned seam runs one ``tar`` over the transport that renames ``arch/x86/boot/bzImage``
    to ``boot/vmlinuz`` and stores the staged ``lib/modules`` tree, excluding the ``build`` and
    ``source`` back-reference symlinks (the same exclusion :func:`make_kernel_bundle_bytes`
    applies in memory). The archive stays on the host; an :class:`ArtifactRemoteFile` referencing
    it is returned so the publish step uploads it via a presigned PUT without the worker reading
    its bytes.

    Args:
        t: The build transport to run ``tar`` through.

    Returns:
        A ``(workspace, mod_root) -> ArtifactRemoteFile`` matching :data:`MakeKernelBundle`.
    """

    def _make(workspace: Path, mod_root: Path) -> ArtifactSource:
        bundle_path = str(workspace / _BUNDLE_NAME)
        argv = [
            "tar",
            "-czf",
            bundle_path,
            "--exclude=*/build",
            "--exclude=*/source",
            "--transform=s|^arch/x86/boot/bzImage$|boot/vmlinuz|",
            "-C",
            str(workspace),
            "arch/x86/boot/bzImage",
            "-C",
            str(mod_root),
            "lib/modules",
        ]
        result = t.run(argv, cwd=str(workspace), timeout_s=_BUNDLE_TAR_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                "tar failed to package the kernel bundle on the build host",
                category=ErrorCategory.BUILD_FAILURE,
                details={"output": "module bundle", "stderr": result.stderr[-512:]},
            )
        return ArtifactRemoteFile(path=bundle_path, transport=t)

    return _make


__all__ = [
    "ArtifactBytes",
    "ArtifactRemoteFile",
    "ArtifactSource",
    "MakeKernelBundle",
    "build_bundle_member_dirs",
    "local_kernel_bundle",
    "make_kernel_bundle_bytes",
    "transport_kernel_bundle",
]
