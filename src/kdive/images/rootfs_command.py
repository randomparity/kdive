"""CLI assembly for the local `build-fs` filesystem-image build command."""

from __future__ import annotations

import argparse
import logging
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildPlane, RootfsBuildSpec
from kdive.images.rootfs_kinds import RootfsImageKind
from kdive.images.rootfs_specs import catalog_rootfs_build
from kdive.images.staged_provenance import write_sidecar
from kdive.providers.assembly.composition import build_local_rootfs_build_plane

_log = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_LOCAL_ROOTFS_DIR = "/var/lib/kdive/rootfs/local"


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
    kind: RootfsImageKind
    dest: str
    spec: RootfsBuildSpec


def _resolve_build_params(args: argparse.Namespace) -> _BuildParams:
    """Resolve the build identity from the catalog-authoritative ``--image`` value."""
    build = catalog_rootfs_build(
        "local-libvirt",
        args.image,
        packages=tuple(args.packages) if args.packages else (),
    )
    return _BuildParams(
        name=build.spec.name,
        kind=_kind_for_capabilities(build.spec.capabilities),
        dest=args.dest or f"{_LOCAL_ROOTFS_DIR}/{build.spec.name}.qcow2",
        spec=build.spec,
    )


def run_build_fs(args: argparse.Namespace) -> None:
    """Build a kdive-ready filesystem qcow2 via the local plane and move it to its destination."""
    params = _resolve_build_params(args)
    workspace = Path(args.workspace).resolve()
    _ensure_workspace_writable(workspace)
    plane = _build_local_rootfs_plane(workspace)
    output: RootfsBuildOutput = plane.build(params.spec)
    dest = Path(params.dest).resolve()
    _publish_rootfs(output, dest)
    _write_provenance_sidecar(dest, output)
    _log.info(
        "built %s rootfs %s digest=%s; set KDIVE_GUEST_IMAGE to this path",
        params.kind,
        dest,
        output.digest,
    )
    print(f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest))}")


def _write_provenance_sidecar(dest: Path, output: RootfsBuildOutput) -> None:
    """Record the build's provenance beside the published qcow2 for the reconcile (#977, ADR-0296).

    Advisory: the qcow2 is the primary artifact, so a sidecar failure is logged and swallowed rather
    than failing the build — the row simply reads ``unverified`` until the next build, as it does
    today. Both an I/O failure (``OSError``) and a non-JSON-serializable provenance
    (``TypeError``/``ValueError`` from ``json.dumps``) are treated as advisory.
    """
    try:
        write_sidecar(dest, provenance=output.provenance)
    except OSError, TypeError, ValueError:
        _log.warning("could not write provenance sidecar for %s; skipping", dest, exc_info=True)


def _kind_for_capabilities(capabilities: tuple[Capability, ...]) -> RootfsImageKind:
    """Return the operator-facing image kind label for logging."""
    if Capability.BUILD in capabilities:
        return "build"
    return "debug"
