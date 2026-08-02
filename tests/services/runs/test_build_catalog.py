"""Service contracts for immutable investigation build generations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.artifacts.storage import HeadResult
from kdive.domain.capacity.state import InvestigationState, RunState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Run
from kdive.services.runs.build_catalog import (
    BuildPublication,
    canonical_build_document,
    parse_build_ref,
    publish_or_reuse_build,
    resolve_build,
)
from kdive.services.runs.steps import BuildStepResult
from tests.clock import STORE_MTIME
from tests.db import conftest as _db_conftest  # noqa: F401

_DT = datetime(2026, 8, 1, tzinfo=UTC)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _seed_investigation(conn: psycopg.AsyncConnection, investigation_id: UUID) -> None:
    await conn.execute(
        "INSERT INTO investigations (id, title, state, principal, project) "
        "VALUES (%s, 'catalog test', %s, 'alice', 'proj')",
        (investigation_id, InvestigationState.ACTIVE.value),
    )


def _run(investigation_id: UUID, *, build_profile: dict[str, object] | None = None) -> Run:
    return Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        investigation_id=investigation_id,
        target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.CREATED,
        build_profile=build_profile or {"arch": "x86_64", "config": ["A=y"]},
        principal="alice",
        project="proj",
    )


def _result() -> BuildStepResult:
    return BuildStepResult(
        kernel_ref="runs/source/kernel",
        debuginfo_ref="runs/source/vmlinux",
        initrd_ref="runs/source/initrd",
        build_id="build-id",
        cmdline="console=ttyS0",
        build_provenance={"ref": "v6.10", "dirty": False},
    )


def _heads() -> dict[str, HeadResult]:
    return {
        "runs/source/vmlinux": HeadResult(
            20, "vmlinux-sha", "vmlinux-etag", STORE_MTIME, "vmlinux-version"
        ),
        "runs/source/initrd": HeadResult(
            10, "initrd-sha", "initrd-etag", STORE_MTIME, "initrd-version"
        ),
        "runs/source/kernel": HeadResult(
            5, "kernel-sha", "kernel-etag", STORE_MTIME, "kernel-version"
        ),
    }


def test_canonical_document_is_deterministic_and_head_order_independent() -> None:
    run = _run(uuid4(), build_profile={"z": [2, 1], "a": "x"})
    result = _result()
    first = canonical_build_document(run, result, _heads())
    second = canonical_build_document(run, result, dict(reversed(_heads().items())))

    assert first == second
    artifacts = first["artifacts"]
    assert isinstance(artifacts, dict)
    assert artifacts["kernel"] == {"checksum_sha256": "kernel-sha"}


def test_publication_uses_fixed_compact_sorted_content_digest(migrated_url: str) -> None:
    async def exercise() -> None:
        investigation_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await _seed_investigation(conn, investigation_id)
            first = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id, build_profile={"z": [2, 1], "a": "x"}),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )
            second = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id, build_profile={"a": "x", "z": [2, 1]}),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )

            assert (
                first.build.content_digest
                == (
                    "94a31a9ad2cfbcc85125943687d3892b28ced464e8cd256ddd028a0cec9e386a"  # pragma: allowlist secret  # noqa: E501
                )
            )
            assert second.created is False
            assert second.build == first.build

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 64,
        f"{'A' * 64}.72e5033f-d02a-43e5-8307-993206b5e292",
        f"{'a' * 63}.bb55e678-8773-4bf7-bf01-452f68350bda",
        f"{'a' * 64}.not-a-uuid",
        f"{'a' * 64}.6E477264-3602-4545-826A-3900AC82765D",
    ],
)
def test_parse_build_ref_rejects_malformed_references(value: str) -> None:
    with pytest.raises(ValueError, match="build_ref"):
        parse_build_ref(value)


def test_parse_build_ref_returns_digest_and_generation() -> None:
    generation = uuid4()
    digest, parsed_generation = parse_build_ref(f"{'a' * 64}.{generation}")
    assert digest == "a" * 64
    assert parsed_generation == generation


def test_active_unexpired_publication_converges_on_existing_generation(migrated_url: str) -> None:
    async def exercise() -> None:
        investigation_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await _seed_investigation(conn, investigation_id)
            first = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )
            second = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )

            assert isinstance(first, BuildPublication)
            assert first.created is True
            assert second.created is False
            assert second.build == first.build
            resolved = await resolve_build(conn, investigation_id, first.build.build_ref)
            assert resolved == first.build

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("state", "expires_delta"),
    [("active", timedelta(seconds=-1)), ("reclaiming", timedelta(days=7))],
)
def test_expired_or_reclaiming_digest_mints_new_generation(
    migrated_url: str, state: str, expires_delta: timedelta
) -> None:
    async def exercise() -> None:
        investigation_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await _seed_investigation(conn, investigation_id)
            first = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )
            await conn.execute(
                "UPDATE investigation_builds SET state = %s, expires_at = now() + %s "
                "WHERE investigation_id = %s AND generation = %s",
                (state, expires_delta, investigation_id, first.build.generation),
            )
            second = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )
            assert second.created is True
            assert second.build.generation != first.build.generation

    asyncio.run(exercise())


def test_matching_digest_with_different_canonical_document_fails_loudly(migrated_url: str) -> None:
    async def exercise() -> None:
        investigation_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await _seed_investigation(conn, investigation_id)
            first = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=7),
            )
            await conn.execute(
                "UPDATE investigation_builds SET canonical_document = '{\"tampered\":true}'::jsonb "
                "WHERE investigation_id = %s AND generation = %s",
                (investigation_id, first.build.generation),
            )
            with pytest.raises(RuntimeError, match="canonical"):
                await publish_or_reuse_build(
                    conn,
                    run=_run(investigation_id),
                    result=_result(),
                    heads=_heads(),
                    retention=timedelta(days=7),
                )

    asyncio.run(exercise())


def test_publication_uses_postgres_clock_and_stores_generation_scoped_versions(
    migrated_url: str,
) -> None:
    async def exercise() -> None:
        investigation_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await _seed_investigation(conn, investigation_id)
            before = await conn.execute("SELECT now()")
            before_row = await before.fetchone()
            assert before_row is not None
            publication = await publish_or_reuse_build(
                conn,
                run=_run(investigation_id),
                result=_result(),
                heads=_heads(),
                retention=timedelta(days=3),
            )
            after = await conn.execute("SELECT now()")
            after_row = await after.fetchone()
            assert after_row is not None

            assert before_row[0] + timedelta(days=3) <= publication.build.expires_at
            assert publication.build.expires_at <= after_row[0] + timedelta(days=3)
            assert publication.build.artifacts == {
                "initrd": {"key": "runs/source/initrd", "version_id": "initrd-version"},
                "kernel": {"key": "runs/source/kernel", "version_id": "kernel-version"},
                "vmlinux": {"key": "runs/source/vmlinux", "version_id": "vmlinux-version"},
            }

    asyncio.run(exercise())
