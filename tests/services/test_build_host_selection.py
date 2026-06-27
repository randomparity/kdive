"""Build-host selection policy tests."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.services.runs import build_host_selection

_RUN_ID = UUID("00000000-0000-0000-0000-00000000b017")


def _host(
    *,
    name: str = "worker-local",
    kind: BuildHostKind = BuildHostKind.LOCAL,
    enabled: bool = True,
    state: BuildHostState = BuildHostState.READY,
) -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-00000000b018"),
        name=name,
        kind=kind,
        address="builder.example" if kind is not BuildHostKind.LOCAL else None,
        ssh_credential_ref="ssh://builder" if kind is BuildHostKind.SSH else None,
        base_image_volume="base.qcow2" if kind is BuildHostKind.EPHEMERAL_LIBVIRT else None,
        workspace_root="/build",
        max_concurrent=1,
        enabled=enabled,
        state=state,
        toolchain_desc=None,
    )


def _profile(*, build_host: str | None = None, git: bool = False) -> ServerBuildProfile:
    source_ref: object = (
        {"git": {"remote": "https://example.invalid/linux.git", "ref": "v6.9"}}
        if git
        else "/src/linux"
    )
    return ServerBuildProfile.model_validate(
        {"schema_version": 1, "kernel_source_ref": source_ref, "build_host": build_host}
    )


def test_local_host_default_does_not_acquire_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        host = _host()
        acquired: list[BuildHost] = []
        lookups: list[str] = []

        async def get_by_name(_conn: AsyncConnection, name: str) -> BuildHost:
            lookups.append(name)
            return host

        monkeypatch.setattr(build_host_selection, "get_by_name", get_by_name)
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            lambda _conn, lease_host, _run_id: _record_async(acquired, lease_host, True),
        )

        selected = await build_host_selection.resolve_and_admit(
            cast(AsyncConnection, object()), _profile(), _RUN_ID
        )

        assert selected is host
        assert lookups == ["worker-local"]
        assert acquired == []

    asyncio.run(_run())


def test_remote_host_requires_git_source_and_acquires_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        host = _host(name="builder", kind=BuildHostKind.SSH)
        acquired: list[tuple[BuildHost, UUID]] = []

        async def get_by_name(_conn: AsyncConnection, name: str) -> BuildHost:
            assert name == "builder"
            return host

        async def acquire(_conn: AsyncConnection, lease_host: BuildHost, run_id: UUID) -> bool:
            acquired.append((lease_host, run_id))
            return True

        monkeypatch.setattr(build_host_selection, "get_by_name", get_by_name)
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            acquire,
        )

        selected = await build_host_selection.resolve_and_admit(
            cast(AsyncConnection, object()),
            _profile(build_host="builder", git=True),
            _RUN_ID,
        )

        assert selected is host
        assert acquired == [(host, _RUN_ID)]

    asyncio.run(_run())


def test_git_source_on_local_host_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-0162: a git kernel_source_ref is now admitted on the local host (allowlist is
    # enforced at build time on the worker, not here); no capacity lease for a local host.
    async def _run() -> None:
        host = _host()
        acquired: list[BuildHost] = []
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            lambda _conn, lease_host, _run_id: _record_async(acquired, lease_host, True),
        )

        selected = await build_host_selection.resolve_and_admit(
            cast(AsyncConnection, object()), _profile(git=True), _RUN_ID
        )

        assert selected is host
        assert acquired == []  # local builds take no lease

    asyncio.run(_run())


def test_warm_tree_on_remote_host_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        host = _host(name="builder", kind=BuildHostKind.SSH)
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                _profile(build_host="builder", git=False),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
        # The resolved host name flows through to the compatibility check's details.
        assert exc.value.details == {"build_host": "builder", "host_kind": "ssh"}

    asyncio.run(_run())


def test_missing_host_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(None))

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                _profile(build_host="missing"),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert str(exc.value) == "build host 'missing' not found"
        assert exc.value.details == {"build_host": "missing"}

    asyncio.run(_run())


def test_resolve_forwards_caller_connection_to_lookup_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's open-transaction connection must reach both the lookup and the lease.

    Passing anything else (e.g. ``None``) would run the lease outside the caller's
    RUN-locked transaction, so the resolver must forward the exact connection object it
    was given.
    """

    async def _run() -> None:
        conn = cast(AsyncConnection, object())
        host = _host(name="builder", kind=BuildHostKind.SSH)
        lookup_conns: list[object] = []
        lease_conns: list[object] = []

        async def get_by_name(conn_arg: AsyncConnection, name: str) -> BuildHost:
            lookup_conns.append(conn_arg)
            return host

        async def acquire(conn_arg: AsyncConnection, _host: BuildHost, _run_id: UUID) -> bool:
            lease_conns.append(conn_arg)
            return True

        monkeypatch.setattr(build_host_selection, "get_by_name", get_by_name)
        monkeypatch.setattr(build_host_selection, "try_acquire_lease", acquire)

        await build_host_selection.resolve_and_admit(
            conn, _profile(build_host="builder", git=True), _RUN_ID
        )

        assert lookup_conns == [conn]
        assert lease_conns == [conn]

    asyncio.run(_run())


def test_disabled_host_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        host = _host(name="builder", kind=BuildHostKind.SSH, enabled=False)
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                _profile(build_host="builder", git=True),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert str(exc.value) == "build host 'builder' is not available"
        assert exc.value.details == {
            "build_host": "builder",
            "enabled": False,
            "state": BuildHostState.READY.value,
        }

    asyncio.run(_run())


def test_unreachable_but_enabled_host_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled host in the UNREACHABLE state is rejected (the ``or`` branch)."""

    async def _run() -> None:
        host = _host(
            name="builder",
            kind=BuildHostKind.SSH,
            enabled=True,
            state=BuildHostState.UNREACHABLE,
        )
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                _profile(build_host="builder", git=True),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert str(exc.value) == "build host 'builder' is not available"
        assert exc.value.details == {
            "build_host": "builder",
            "enabled": True,
            "state": BuildHostState.UNREACHABLE.value,
        }

    asyncio.run(_run())


def test_remote_host_at_capacity_is_capacity_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        host = _host(name="builder", kind=BuildHostKind.SSH)
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            lambda _conn, _host, _run_id: _async(False),
        )

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                _profile(build_host="builder", git=True),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.CAPACITY_EXHAUSTED
        assert str(exc.value) == "build host 'builder' is at capacity"
        assert exc.value.details == {"build_host": "builder"}

    asyncio.run(_run())


def test_compat_local_with_git_ok() -> None:
    # ADR-0162: a local host accepts a git source (the remote is gated by the build-time
    # allowlist), so the shared compatibility check no longer rejects local+git.
    assert (
        build_host_selection.check_source_kind_compatibility(
            host_kind=BuildHostKind.LOCAL, is_git=True, build_host="worker-local"
        )
        is None
    )


def test_compat_remote_with_warm_tree_raises_config_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        build_host_selection.check_source_kind_compatibility(
            host_kind=BuildHostKind.SSH, is_git=False, build_host="builder"
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "a remote build host requires a git kernel_source_ref"
    assert exc.value.details == {"build_host": "builder", "host_kind": "ssh"}


def test_compat_ephemeral_with_warm_tree_raises() -> None:
    with pytest.raises(CategorizedError) as exc:
        build_host_selection.check_source_kind_compatibility(
            host_kind=BuildHostKind.EPHEMERAL_LIBVIRT, is_git=False, build_host="builders-a"
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "a remote build host requires a git kernel_source_ref"
    assert exc.value.details == {"build_host": "builders-a", "host_kind": "ephemeral_libvirt"}


def test_compat_local_with_warm_tree_ok() -> None:
    assert (
        build_host_selection.check_source_kind_compatibility(
            host_kind=BuildHostKind.LOCAL, is_git=False, build_host="worker-local"
        )
        is None
    )


def test_compat_remote_with_git_ok() -> None:
    assert (
        build_host_selection.check_source_kind_compatibility(
            host_kind=BuildHostKind.SSH, is_git=True, build_host="builder"
        )
        is None
    )
    assert (
        build_host_selection.check_source_kind_compatibility(
            host_kind=BuildHostKind.EPHEMERAL_LIBVIRT, is_git=True, build_host="builders-a"
        )
        is None
    )


def test_accepted_source_kinds_matrix() -> None:
    from kdive.services.runs.build_host_selection import SourceKind, accepted_source_kinds

    # ADR-0162: a local host accepts both warm-tree and git (the local git-clone lane).
    assert accepted_source_kinds(BuildHostKind.LOCAL) == (SourceKind.WARM_TREE, SourceKind.GIT)
    assert accepted_source_kinds(BuildHostKind.SSH) == (SourceKind.GIT,)
    assert accepted_source_kinds(BuildHostKind.EPHEMERAL_LIBVIRT) == (SourceKind.GIT,)


@pytest.mark.parametrize("host_kind", list(BuildHostKind))
@pytest.mark.parametrize("is_git", [True, False])
def test_validator_agrees_with_accepted_source_kinds(
    host_kind: BuildHostKind, is_git: bool
) -> None:
    """The validator raises iff the submitted source kind is absent from the advertised set.

    This pins that ``check_source_kind_compatibility`` and ``accepted_source_kinds`` share
    one source of truth: the lane the read surfaces advertise is exactly the lane the
    validator enforces, for every ``BuildHostKind``.
    """
    from kdive.services.runs.build_host_selection import SourceKind, accepted_source_kinds

    submitted = SourceKind.GIT if is_git else SourceKind.WARM_TREE
    compatible = submitted in accepted_source_kinds(host_kind)

    if compatible:
        assert (
            build_host_selection.check_source_kind_compatibility(
                host_kind=host_kind, is_git=is_git, build_host="h"
            )
            is None
        )
    else:
        with pytest.raises(CategorizedError) as exc:
            build_host_selection.check_source_kind_compatibility(
                host_kind=host_kind, is_git=is_git, build_host="h"
            )
        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


async def _async[T](value: T) -> T:
    return value


async def _record_async[T](items: list[T], item: T, value: bool) -> bool:
    items.append(item)
    return value


def test_warm_tree_admission_rejects_empty_for_local() -> None:
    from kdive.db.build_host_policy import KERNEL_SRC_UNSET_DETAIL

    with pytest.raises(CategorizedError) as excinfo:
        build_host_selection.check_warm_tree_source_admission("", host_kind=BuildHostKind.LOCAL)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == KERNEL_SRC_UNSET_DETAIL


def test_warm_tree_admission_rejects_invalid_for_local() -> None:
    from kdive.db.build_host_policy import KERNEL_SRC_INVALID_DETAIL

    with pytest.raises(CategorizedError) as excinfo:
        build_host_selection.check_warm_tree_source_admission(
            "relative/path", host_kind=BuildHostKind.LOCAL
        )
    assert str(excinfo.value) == KERNEL_SRC_INVALID_DETAIL


def test_warm_tree_admission_admits_usable_local(tmp_path: object) -> None:
    build_host_selection.check_warm_tree_source_admission(
        str(tmp_path), host_kind=BuildHostKind.LOCAL
    )


@pytest.mark.parametrize("kind", [BuildHostKind.SSH, BuildHostKind.EPHEMERAL_LIBVIRT])
def test_warm_tree_admission_noop_for_non_local(kind: BuildHostKind) -> None:
    build_host_selection.check_warm_tree_source_admission("", host_kind=kind)


@pytest.mark.parametrize("kind", [BuildHostKind.LOCAL, BuildHostKind.SSH])
def test_build_host_resolves_local_and_ssh_always(kind: BuildHostKind) -> None:
    assert build_host_selection.build_host_resolves(kind, "anything", []) is True
    assert build_host_selection.build_host_resolves(kind, "anything", ["other"]) is True


def test_build_host_resolves_ephemeral_only_when_declared() -> None:
    eph = BuildHostKind.EPHEMERAL_LIBVIRT
    assert build_host_selection.build_host_resolves(eph, "ub24", ["ub24"]) is True
    assert build_host_selection.build_host_resolves(eph, "ub24", []) is False
    assert build_host_selection.build_host_resolves(eph, "ub24", ["other"]) is False
