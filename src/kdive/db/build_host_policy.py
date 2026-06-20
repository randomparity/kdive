"""Build-host source policy shared by admission, diagnostics, and workspace execution."""

from __future__ import annotations

from pathlib import Path

from kdive.db.build_hosts import BuildHostKind
from kdive.domain.errors import CategorizedError, ErrorCategory

# A warm-tree build resolves its source from the worker-process ``KDIVE_KERNEL_SRC``
# env, which the server cannot see at admission time, so a misconfigured source can
# only surface here. The failure strings name the three ways forward — the operator
# warm-tree staging step (and its doc), the local git lane (a structured
# ``kernel_source_ref`` whose remote the operator allowlists), and the git lane on a
# registered remote build host — so the caller can self-correct from the error alone.
_BUILD_LANE_GUIDANCE = (
    "Either stage a kernel source tree on the build worker and set KDIVE_KERNEL_SRC to "
    "its absolute path (see resource://kdive/docs/operating/build-source-staging.md), or "
    "submit a git "
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


def warm_tree_source_error(kernel_src: str) -> str | None:
    """Return the offending message for an unusable warm-tree source, or ``None``.

    The single definition of the warm-tree ``KDIVE_KERNEL_SRC`` rule, shared by
    workspace sync (the build-time backstop), the admission helper, and diagnostics
    (ADR-0161). Empty/whitespace reads as "unset"; a present value that is not an
    absolute path to an existing directory reads as "invalid".
    """
    if not kernel_src.strip():
        return KERNEL_SRC_UNSET_DETAIL
    source = Path(kernel_src)
    if not source.is_absolute() or source == source.parent or not source.is_dir():
        return KERNEL_SRC_INVALID_DETAIL
    return None


def check_warm_tree_source_admission(kernel_src: str, *, host_kind: BuildHostKind) -> None:
    """Reject a LOCAL warm-tree build whose ``KDIVE_KERNEL_SRC`` is unset or unusable.

    A no-op for any non-``LOCAL`` host kind (git/remote lanes never read
    ``KDIVE_KERNEL_SRC``). For a ``LOCAL`` host this applies the same predicate workspace
    sync applies and raises the identical detail string, so an admission rejection matches
    the build-time backstop.
    """
    if host_kind is not BuildHostKind.LOCAL:
        return
    detail = warm_tree_source_error(kernel_src)
    if detail is not None:
        raise CategorizedError(detail, category=ErrorCategory.CONFIGURATION_ERROR)
