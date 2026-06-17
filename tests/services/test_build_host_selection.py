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
    # ADR-0158: a git kernel_source_ref is now admitted on the local host (allowlist is
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
        assert exc.value.details == {"build_host": "missing"}

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

    asyncio.run(_run())


def test_compat_local_with_git_ok() -> None:
    # ADR-0158: a local host accepts a git source (the remote is gated by the build-time
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


async def _async[T](value: T) -> T:
    return value


async def _record_async[T](items: list[T], item: T, value: bool) -> bool:
    items.append(item)
    return value
