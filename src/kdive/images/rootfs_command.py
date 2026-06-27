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
from kdive.images.families import family_for
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


# The guest-contract capability tags each catalog image kind claims; the install package set is
# resolved from the (EL-major-aware) FamilyCustomizer, not duplicated here (ADR-0251, #823).
_KIND_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "debug": ("agent", "kdump", "drgn"),
    "build": ("agent", "build"),
}


def add_build_fs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `build-fs`: the operator's local-libvirt filesystem-image build."""
    build = sub.add_parser(
        "build-fs",
        help="build a local-libvirt kdive-ready filesystem qcow2 (debug guest or build host)",
    )
    build.add_argument("--image", required=True, help="rootfs catalog image name")
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
    build.add_argument(
        "--package",
        action="append",
        default=None,
        dest="packages",
        help="extra guest package (repeatable); defaults to the catalog image kind's package set",
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
    arch: str
    kind: str
    dest: str
    source_image_digest: str
    family: str


def _source_image_digest(source: RootfsSource) -> str:
    """Render the provenance ``source_image_digest`` for a resolved catalog base source."""
    if isinstance(source, CloudImageSource):
        return f"cloud-image:{source.url}@sha256:{source.sha256}"
    return f"virt-builder:{source.template}"


def _resolve_build_params(args: argparse.Namespace) -> _BuildParams:
    """Resolve the build identity from the catalog-authoritative ``--image`` value."""
    entry = resolve_rootfs_entry(args.image)
    return _BuildParams(
        name=entry.name,
        distro=entry.distro,
        releasever=entry.version,
        arch=entry.arch,
        kind=entry.kind,
        dest=args.dest or f"{_LOCAL_ROOTFS_DIR}/{entry.name}.qcow2",
        source_image_digest=_source_image_digest(entry.source),
        family=entry.family,
    )


def run_build_fs(args: argparse.Namespace) -> None:
    """Build a kdive-ready filesystem qcow2 via the local plane and move it to its destination."""
    params = _resolve_build_params(args)
    family = family_for(params.family)
    default_packages = family.packages(params.kind, params.distro, params.releasever)
    packages = tuple(args.packages) if args.packages else default_packages
    spec = RootfsBuildSpec(
        provider="local-libvirt",
        name=params.name,
        arch=params.arch,
        releasever=params.releasever,
        packages=packages,
        source_image_digest=params.source_image_digest,
        capabilities=_KIND_CAPABILITIES[params.kind],
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
