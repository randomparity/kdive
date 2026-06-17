"""Build-host workspace checkout, config merge, patch, and sync helpers."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - all calls use fixed argv and no shell
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import UUID

from kdive.build_artifacts.validation import patch_target_paths, snapshot_file_bytes
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitSourceRef, ServerBuildProfile, git_source_of
from kdive.providers.shared.build_host.config import resolve_local_ref
from kdive.providers.shared.build_host.execution import (
    MAKE_TIMEOUT_S,
    build_failure,
    launch_failure,
    run_make_target,
    workspace_failure,
)
from kdive.providers.shared.build_host.git_source import remote_allowed, validate_git_arg
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

STDERR_TAIL = 2000
GIT_APPLY_TIMEOUT_S = 120
GIT_CLONE_TIMEOUT_S = 10 * 60
RSYNC_TIMEOUT_S = 10 * 60

# Closed ambient escape hatches so the local-build allowlist bounds the actual connection,
# not just the submitted URL string (ADR-0157): no redirect-follow off the allowlisted host,
# no system/global gitconfig insteadOf rewrite, and only the three vetted transports.
_GIT_HARDENED_FLAGS = [
    "-c",
    "http.followRedirects=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.ssh.allow=always",
    "-c",
    "protocol.git.allow=always",
]
_GIT_HARDENED_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_TERMINAL_PROMPT": "0",
}

# A warm-tree build resolves its source from the worker-process ``KDIVE_KERNEL_SRC``
# env, which the server cannot see at admission time, so a misconfigured source can
# only surface here. The failure strings name the three ways forward — the operator
# warm-tree staging step (and its doc), the local git lane (a structured
# ``kernel_source_ref`` whose remote the operator allowlists), and the git lane on a
# registered remote build host — so the caller can self-correct from the error alone.
_BUILD_LANE_GUIDANCE = (
    "Either stage a kernel source tree on the build worker and set KDIVE_KERNEL_SRC to "
    "its absolute path (see docs/operating/build-source-staging.md), or submit a git "
    'build profile instead — a structured kernel_source_ref {"git": {"remote": ..., '
    '"ref": ...}} either on the local host once the operator allowlists its remote via '
    "KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST, or on a registered remote build host "
    "(build_hosts.register_ssh / build_hosts.register_ephemeral_libvirt)."
)
KERNEL_SRC_UNSET_DETAIL = (
    "This warm-tree build has no kernel source: KDIVE_KERNEL_SRC is not set on the "
    f"build worker. {_BUILD_LANE_GUIDANCE}"
)
KERNEL_SRC_INVALID_DETAIL = (
    "KDIVE_KERNEL_SRC is set on the build worker but is not an absolute path to an "
    f"existing kernel source tree. {_BUILD_LANE_GUIDANCE}"
)

type Checkout = Callable[[UUID, ServerBuildProfile, Path, bytes], None]


def make_checkout(
    kernel_src: str, secret_registry: SecretRegistry, *, allowlist: Sequence[str] = ()
) -> Checkout:
    """Create the default checkout seam (warm tree or, for a git source, an allowlisted clone).

    Args:
        kernel_src: The warm kernel source tree path for the warm-tree lane.
        secret_registry: Registry used to redact secrets out of error details.
        allowlist: Remotes the local git-clone lane may clone (ADR-0157); empty disables it.
    """

    def _checkout(
        run_id: UUID, profile: ServerBuildProfile, workspace: Path, fragment_bytes: bytes
    ) -> None:
        real_checkout(
            kernel_src,
            profile,
            workspace,
            fragment_bytes,
            run_id=run_id,
            secret_registry=secret_registry,
            allowlist=allowlist,
        )

    return _checkout


def real_checkout(
    kernel_src: str,
    profile: ServerBuildProfile,
    workspace: Path,
    fragment_bytes: bytes,
    *,
    run_id: UUID,
    secret_registry: SecretRegistry,
    allowlist: Sequence[str] = (),
) -> None:
    """Materialize a per-run workspace, merge config, and apply an optional patch.

    Dispatches on the profile's source provenance: a git ``kernel_source_ref`` clones the
    allowlisted remote (ADR-0157), a bare string mirrors the warm tree.
    """
    git_source = git_source_of(profile)
    if git_source is not None:
        clone_tree(
            git_source,
            workspace,
            allowlist,
            run_id=run_id,
            secret_registry=secret_registry,
        )
    else:
        sync_tree(kernel_src, workspace, secret_registry)
    merge_config(fragment_bytes, workspace, run_id)
    if profile.patch_ref is not None:
        apply_patch(profile.patch_ref, workspace, secret_registry)


def _run_git(
    args: list[str], *, cwd: Path | None, run_id: UUID
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with hardened flags/env: no redirect-follow, no ambient config, vetted protos."""
    try:
        return subprocess.run(
            ["git", *_GIT_HARDENED_FLAGS, *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_CLONE_TIMEOUT_S,
            env={**os.environ, **_GIT_HARDENED_ENV, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise build_failure("a git clone step exceeded the build timeout", run_id) from exc
    except OSError as exc:
        raise launch_failure("git", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def clone_tree(
    source: GitSourceRef,
    workspace: Path,
    allowlist: Sequence[str],
    *,
    run_id: UUID,
    secret_registry: SecretRegistry,
) -> None:
    """Clone ``source.remote`` at ``source.ref`` into a clean ``workspace`` (ADR-0157).

    The remote is allowlist-gated (deny by default) and the clone uses the same
    init+shallow-fetch+verify+checkout recipe as the remote transport (ADR-0154). ``ref`` must
    be a server-advertised tag or branch; a bare commit SHA is not guaranteed fetchable
    shallowly and surfaces as the ``git fetch`` failure below.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe/disallowed remote-ref (incl. an
            empty allowlist, which means the operator has not enabled local git builds), a
            failed ``git fetch``, or a failed ``git checkout``; ``MISSING_DEPENDENCY`` if git is
            absent; ``INFRASTRUCTURE_FAILURE`` for a failed ``git init`` or workspace mkdir;
            ``TRANSPORT_FAILURE`` when the fetch reported success but produced no ``FETCH_HEAD``.
    """
    validate_git_arg(source.remote, "remote")
    validate_git_arg(source.ref, "ref")
    if not allowlist:
        raise CategorizedError(
            "local git builds are disabled: the operator has not set "
            "KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST (see docs/operating/build-source-staging.md)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if not remote_allowed(source.remote, allowlist):
        raise CategorizedError(
            "the git remote is not on KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST "
            "(see docs/operating/build-source-staging.md)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to clone a kernel source", category=ErrorCategory.MISSING_DEPENDENCY
        )
    shutil.rmtree(workspace, ignore_errors=True)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise workspace_failure("mkdir", "build_workspace", exc) from exc
    init = _run_git(["init", str(workspace)], cwd=None, run_id=run_id)
    if init.returncode != 0:
        raise CategorizedError(
            "git init failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": redacted_tail(init.stderr, secret_registry)},
        )
    fetch = _run_git(
        ["-C", str(workspace), "fetch", "--depth", "1", source.remote, source.ref],
        cwd=None,
        run_id=run_id,
    )
    if fetch.returncode != 0:
        raise CategorizedError(
            "git fetch failed",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(fetch.stderr, secret_registry)},
        )
    verify = _run_git(
        ["-C", str(workspace), "rev-parse", "--verify", "--quiet", "FETCH_HEAD"],
        cwd=None,
        run_id=run_id,
    )
    if verify.returncode != 0:
        raise CategorizedError(
            "git fetch produced no FETCH_HEAD (the fetch did not complete)",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"stderr": redacted_tail(fetch.stderr, secret_registry)},
        )
    checkout = _run_git(["-C", str(workspace), "checkout", "FETCH_HEAD"], cwd=None, run_id=run_id)
    if checkout.returncode != 0:
        raise CategorizedError(
            "git checkout FETCH_HEAD failed",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(checkout.stderr, secret_registry)},
        )


def merge_config(fragment_bytes: bytes, workspace: Path, run_id: UUID) -> None:  # pragma: no cover
    """Run base defconfig, merge the kdump fragment, and leave olddefconfig to the caller."""
    if run_make_target(workspace, ["defconfig"], "make defconfig") != 0:
        raise build_failure("make defconfig exited non-zero", run_id)
    fragment_path = workspace / "kdump.config.fragment"
    try:
        fragment_path.write_bytes(fragment_bytes)
    except OSError as exc:
        raise workspace_failure("write", "kdump.config.fragment", exc) from exc
    try:
        merge = subprocess.run(
            ["scripts/kconfig/merge_config.sh", "-m", ".config", str(fragment_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=MAKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise build_failure("merge_config.sh -m exceeded the build timeout", run_id) from exc
    except OSError as exc:
        raise launch_failure(
            "merge_config.sh", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
        ) from exc
    if merge.returncode != 0:
        raise build_failure("merge_config.sh -m exited non-zero", run_id)


def redacted_tail(text: str, secret_registry: SecretRegistry | None = None) -> str:
    """Redact known secrets and key/value pairs, then return the trailing stderr slice."""
    secret_registry = secret_registry or SecretRegistry()
    return Redactor(registry=secret_registry).redact_text(text)[-STDERR_TAIL:]


def apply_patch(
    patch_ref: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Apply the resolved patch ref to the workspace tree with no-op guards."""
    patch = resolve_local_ref(patch_ref, kind="patch_ref")
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to apply a build patch",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        patch_text = patch.read_text(errors="replace")
    except OSError as exc:
        raise CategorizedError(
            "patch_ref could not be read",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"kind": "patch_ref", "path": str(patch), "error": type(exc).__name__},
        ) from exc
    targets = patch_target_paths(patch_text, strip=1)
    before = {rel: snapshot_file_bytes(workspace / rel) for rel in targets}
    try:
        result = subprocess.run(
            ["git", "apply", "-p1", "-v", "--", str(patch)],
            cwd=workspace,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=GIT_APPLY_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "patch_ref does not apply within the timeout",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"timeout_s": GIT_APPLY_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "patch_ref does not apply against the kernel tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
    if any(line.startswith("Skipped patch ") for line in result.stderr.splitlines()):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but skipped one or "
            "more files as already applied (the build workspace has no .git, so git fell "
            "back to context matching)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
    if targets and all(snapshot_file_bytes(workspace / rel) == before[rel] for rel in targets):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but left the kernel "
            "tree unchanged (the build workspace has no .git, so git fell back to context "
            "matching and treated the patch as already applied)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"targets": sorted(str(rel) for rel in targets)},
        )


def sync_tree(
    kernel_src: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Mirror the warm kernel source tree into ``workspace`` with ``rsync -a --delete``."""
    if not kernel_src.strip():
        raise CategorizedError(
            KERNEL_SRC_UNSET_DETAIL,
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    source = Path(kernel_src)
    if not source.is_absolute() or source == source.parent or not source.is_dir():
        raise CategorizedError(
            KERNEL_SRC_INVALID_DETAIL,
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if shutil.which("rsync") is None:
        raise CategorizedError(
            "rsync is required to materialize the warm kernel tree",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise workspace_failure("mkdir", "build_workspace", exc) from exc
    try:
        result = subprocess.run(
            ["rsync", "-a", "--delete", "--", f"{source}/", f"{workspace}/"],
            capture_output=True,
            text=True,
            timeout=RSYNC_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "rsync exceeded the workspace sync timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": RSYNC_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("rsync", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "rsync failed to materialize the workspace tree",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
