"""CLI assembly for the local `build-fs` filesystem-image build command."""

from __future__ import annotations

import argparse
import logging
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families._fedora_customize import (
    DEFAULT_BUILD_FS_PACKAGES,
    DEFAULT_DEBUG_FS_PACKAGES,
)
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildPlane, RootfsBuildSpec
from kdive.images.rootfs_catalog import (
    CloudImageSource,
    RootfsSource,
    resolve_rootfs_entry,
)
from kdive.providers.assembly.composition import build_local_rootfs_build_plane

_log = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_LOCAL_ROOTFS_DIR = "/var/lib/kdive/rootfs/local"


@dataclass(frozen=True, slots=True)
class _FsKind:
    """The package set and guest-contract capabilities a ``--kind`` selects."""

    packages: tuple[str, ...]
    capabilities: tuple[str, ...]


_FS_KINDS: dict[str, _FsKind] = {
    "debug": _FsKind(DEFAULT_DEBUG_FS_PACKAGES, ("agent", "kdump", "drgn")),
    "build": _FsKind(DEFAULT_BUILD_FS_PACKAGES, ("agent", "build")),
}


def add_build_fs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `build-fs`: the operator's local-libvirt filesystem-image build."""
    build = sub.add_parser(
        "build-fs",
        help="build a local-libvirt kdive-ready filesystem qcow2 (debug guest or build host)",
    )
    build.add_argument(
        "--kind",
        choices=tuple(_FS_KINDS),
        default="debug",
        help="debug = guest crash/introspection rootfs; build = kernel-build-host toolchain image",
    )
    build.add_argument(
        "--image",
        default=None,
        help=(
            "rootfs catalog image name (e.g. fedora-kdive-ready-44); when given, name/distro/"
            "releasever/dest and the base source are resolved from the catalog"
        ),
    )
    build.add_argument(
        "--distro",
        default="fedora",
        help="base-OS family for the no-`--image` path (extensibility seam; implemented: fedora)",
    )
    build.add_argument(
        "--workspace",
        default=_DEFAULT_WORKSPACE,
        help=(
            f"build/publish workspace (default: {_DEFAULT_WORKSPACE}); point at a user-writable "
            "path to avoid a privileged mkdir of the root-owned default"
        ),
    )
    build.add_argument(
        "--dest",
        default=None,
        help=(
            "destination qcow2 path (the produced image is moved here); defaults to "
            "/var/lib/kdive/rootfs/local/<name>.qcow2 (the catalog name with --image)"
        ),
    )
    build.add_argument("--name", default="fedora-kdive-ready-43", help="catalog image name")
    build.add_argument("--arch", default="x86_64")
    build.add_argument("--releasever", default="43", help="release the image is built from")
    build.add_argument(
        "--package",
        action="append",
        default=None,
        dest="packages",
        help="extra guest package (repeatable); defaults to the --kind's package set",
    )


def _build_local_rootfs_plane(workspace: Path) -> RootfsBuildPlane:
    """Resolve the local-libvirt rootfs build plane via the composition seam (test seam)."""
    return build_local_rootfs_build_plane(workspace=workspace)


def _ensure_workspace_writable(workspace: Path) -> None:
    """Create ``workspace`` if absent and verify it is writable, else fail with a fix hint.

    Replaces a bare ``PermissionError`` traceback (the common first-run friction on the
    root-owned default) with an actionable message naming the directory and a command to make
    it writable.
    """
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".build-fs-probe-"):
            pass
    except OSError as exc:
        raise CategorizedError(
            f"build-fs workspace {workspace} is not writable; create it writable first, e.g. "
            f'`sudo install -d -o "$USER" {workspace}`, or pass a writable --workspace',
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"workspace": str(workspace), "error": type(exc).__name__},
        ) from exc


def _publish_rootfs(output: RootfsBuildOutput, dest: Path) -> None:
    """Move the built image to ``dest`` and map destination I/O failures."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(output.qcow2_path), str(dest))
        dest.chmod(0o644)
    except OSError as exc:
        raise CategorizedError(
            f"build-fs could not publish rootfs to {dest}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "dest": str(dest),
                "operation": "publish",
                "error": type(exc).__name__,
            },
        ) from exc


@dataclass(frozen=True, slots=True)
class _BuildParams:
    """The image identity and base-source provenance a ``build-fs`` invocation resolves to."""

    name: str
    distro: str
    releasever: str
    kind: str
    dest: str
    source_image_digest: str


def _source_image_digest(source: RootfsSource) -> str:
    """Render the provenance ``source_image_digest`` for a resolved catalog base source."""
    if isinstance(source, CloudImageSource):
        return f"cloud-image:{source.url}@sha256:{source.sha256}"
    return f"virt-builder:{source.template}"


def _resolve_build_params(args: argparse.Namespace) -> _BuildParams:
    """Resolve the build identity from ``--image`` (catalog-authoritative) or the CLI flags.

    With ``--image`` the catalog row owns ``name``/``distro``/``releasever``/``kind``/``dest`` and
    the base-source digest; without it the ``--distro``/``--releasever``/``--name``/``--dest``/
    ``--kind`` flags drive the legacy ``virt-builder:<distro>-<releasever>`` path.
    """
    if args.image is not None:
        entry = resolve_rootfs_entry(args.image)
        return _BuildParams(
            name=entry.name,
            distro=entry.distro,
            releasever=entry.version,
            kind=entry.kind,
            dest=args.dest or f"{_LOCAL_ROOTFS_DIR}/{entry.name}.qcow2",
            source_image_digest=_source_image_digest(entry.source),
        )
    return _BuildParams(
        name=args.name,
        distro=args.distro,
        releasever=args.releasever,
        kind=args.kind,
        dest=args.dest or f"{_LOCAL_ROOTFS_DIR}/{args.name}.qcow2",
        source_image_digest=f"virt-builder:{args.distro}-{args.releasever}",
    )


def run_build_fs(args: argparse.Namespace) -> None:
    """Build a kdive-ready filesystem qcow2 via the local plane and move it to its destination."""
    params = _resolve_build_params(args)
    kind = _FS_KINDS[params.kind]
    packages = tuple(args.packages) if args.packages else kind.packages
    spec = RootfsBuildSpec(
        provider="local-libvirt",
        name=params.name,
        arch=args.arch,
        releasever=params.releasever,
        packages=packages,
        source_image_digest=params.source_image_digest,
        capabilities=kind.capabilities,
        distro=params.distro,
    )
    workspace = Path(args.workspace).resolve()
    _ensure_workspace_writable(workspace)
    plane = _build_local_rootfs_plane(workspace)
    output: RootfsBuildOutput = plane.build(spec)
    dest = Path(params.dest).resolve()
    _publish_rootfs(output, dest)
    _log.info(
        "built %s rootfs %s digest=%s; set KDIVE_GUEST_IMAGE to this path",
        params.kind,
        dest,
        output.digest,
    )
    print(f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest))}")
