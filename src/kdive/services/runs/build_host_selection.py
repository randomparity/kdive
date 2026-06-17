"""Build-host selection and capacity admission for the ``runs.build`` tool boundary.

Resolves a :class:`~kdive.profiles.build.ServerBuildProfile`'s ``build_host`` name to a
live :class:`~kdive.db.build_hosts.BuildHost` row, validates it is available and
compatible with the profile's kernel-source provenance, and acquires one capacity lease
under the ``BUILD_HOST`` advisory lock so the lease and the subsequent ``BUILD`` job
enqueue commit atomically.

The caller must already hold an open transaction and the ``RUN`` advisory lock; this
function takes the ``BUILD_HOST`` lock inside that transaction (``RUN → BUILD_HOST`` in
the global lock order).
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.build_hosts import (
    BuildHost,
    BuildHostKind,
    BuildHostState,
    get_by_name,
    try_acquire_lease,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile, is_git_source


class SourceKind(StrEnum):
    """The two ``kernel_source_ref`` provenances a build host can accept (ADR-0099 §5).

    ``WARM_TREE`` is a bare string (warm-tree / URI provenance); ``GIT`` is a
    ``{"git": {"remote": ..., "ref": ...}}`` object. The string values are the public
    tokens surfaced on ``build_hosts.list`` and ``runs.profile_examples`` (ADR-0159).
    """

    WARM_TREE = "warm-tree"
    GIT = "git"


def accepted_source_kinds(host_kind: BuildHostKind) -> tuple[SourceKind, ...]:
    """Return the ``kernel_source_ref`` kinds a build host of this kind accepts.

    Single source of truth for the ADR-0099 §5 fail-closed matrix: ``LOCAL`` accepts a
    warm-tree string only; ``SSH`` / ``EPHEMERAL_LIBVIRT`` accept a git ref only. Both
    the create/build compatibility check (:func:`check_source_kind_compatibility`) and
    the ``build_hosts.list`` / ``runs.profile_examples`` discovery surfaces (ADR-0159)
    derive from this one function, so the advertised lane can never drift from the
    enforced one.

    Args:
        host_kind: The build host's transport kind.

    Returns:
        The accepted :class:`SourceKind` values for ``host_kind``.
    """
    if host_kind is BuildHostKind.LOCAL:
        return (SourceKind.WARM_TREE,)
    return (SourceKind.GIT,)


def check_source_kind_compatibility(
    *, host_kind: BuildHostKind, is_git: bool, build_host: str
) -> None:
    """Reject a build host whose transport kind is incompatible with the source provenance.

    Consumes :func:`accepted_source_kinds` (the single source of truth for the
    ADR-0099 §5 matrix), shared by the ``runs.create`` create-time check and the
    ``runs.build`` admission backstop (``resolve_and_admit``): a ``local`` host accepts
    a warm-tree string only; an ``ssh`` / ``ephemeral_libvirt`` host accepts a git ref
    only.

    Args:
        host_kind: The resolved build host's transport kind.
        is_git: Whether the profile's ``kernel_source_ref`` is git provenance
            (``is_git_source(profile)``).
        build_host: The resolved host name, carried into the error details.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the host kind and source kind are
            incompatible. The message and ``details`` are stable across both call sites,
            so a create-time and a build-time rejection match for the same host.
    """
    submitted = SourceKind.GIT if is_git else SourceKind.WARM_TREE
    if submitted in accepted_source_kinds(host_kind):
        return
    if host_kind is BuildHostKind.LOCAL:
        raise CategorizedError(
            "a local build host requires a warm-tree kernel_source_ref, not a git ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": build_host, "host_kind": host_kind.value},
        )
    raise CategorizedError(
        "a remote build host requires a git kernel_source_ref",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"build_host": build_host, "host_kind": host_kind.value},
    )


async def resolve_and_admit(
    conn: AsyncConnection,
    parsed_profile: ServerBuildProfile,
    run_id: UUID,
) -> BuildHost:
    """Resolve the build host for a server-lane Run and admit it under capacity.

    Caller MUST already hold an open transaction + the RUN advisory lock; this takes
    the BUILD_HOST lock (after RUN in the global order) and inserts the lease so the
    lease + the BUILD-job enqueue commit atomically.

    For ``kind='local'`` hosts no lease row is inserted (local builds are single-slot
    by convention, not tracked in ``build_host_leases``).

    Args:
        conn: An async psycopg connection with an open transaction. The RUN advisory
            lock must already be held on this connection.
        parsed_profile: The validated server-build profile for the Run being admitted.
        run_id: The Run's primary key, used as the lease owner.

    Returns:
        The resolved :class:`~kdive.db.build_hosts.BuildHost`.

    Raises:
        CategorizedError: ``NOT_FOUND`` when the named host is absent from the catalog;
            ``CONFIGURATION_ERROR`` when the host is disabled, unreachable, or its
            transport kind is incompatible with the profile's kernel-source provenance;
            ``CAPACITY_EXHAUSTED`` when the host exists and is available but all
            concurrent-build slots are occupied.
    """
    name = parsed_profile.build_host or "worker-local"
    host = await get_by_name(conn, name)
    if host is None:
        raise CategorizedError(
            f"build host '{name}' not found",
            category=ErrorCategory.NOT_FOUND,
            details={"build_host": name},
        )

    if not host.enabled or host.state is BuildHostState.UNREACHABLE:
        raise CategorizedError(
            f"build host '{name}' is not available",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": name, "enabled": host.enabled, "state": host.state.value},
        )

    check_source_kind_compatibility(
        host_kind=host.kind, is_git=is_git_source(parsed_profile), build_host=name
    )

    if host.kind is not BuildHostKind.LOCAL:
        ok = await try_acquire_lease(conn, host, run_id)
        if not ok:
            raise CategorizedError(
                f"build host '{name}' is at capacity",
                category=ErrorCategory.CAPACITY_EXHAUSTED,
                details={"build_host": name},
            )

    return host
