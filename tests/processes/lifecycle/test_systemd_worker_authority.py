"""PostgreSQL authority binding for retained systemd worker slots."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.processes.lifecycle.systemd_worker_contract import SlotPhase
from kdive.processes.lifecycle.systemd_worker_lifecycle import (
    EvidenceRejected,
    PostgresAuthority,
)
from kdive.processes.lifecycle.systemd_worker_state import SlotState
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    IncarnationAuthenticationError,
    authenticate_worker_incarnation,
)
from tests.reconciler.conftest import connect

_BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
_INVOCATION_ID = "a" * 32


def _state(*, invocation_id: str = _INVOCATION_ID, credential_hash: str = "c" * 64) -> SlotState:
    generation = "b" * 32
    unit = "kdive-live-worker@1.service"
    return SlotState(
        schema=1,
        slot=1,
        unit=unit,
        generation=generation,
        incarnation=f"local-systemd:{unit}:{generation}",
        credential_hash=credential_hash,
        phase=SlotPhase.GATED,
        boot_id=_BOOT_ID,
        invocation_id=invocation_id,
    )


async def _configure_witness(connection: psycopg.AsyncConnection) -> None:
    await connection.execute("SET SESSION AUTHORIZATION kdive_lifecycle_witness")
    await connection.commit()


@asynccontextmanager
async def _witness_pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=1,
        open=False,
        configure=_configure_witness,
    )
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


def test_authority_registers_exact_local_binding(migrated_url: str) -> None:
    async def _run() -> None:
        credential = SecretStr("exact-local-systemd-credential")
        credential_hash = hashlib.sha256(credential.get_secret_value().encode()).digest()
        state = _state(credential_hash=credential_hash.hex())
        async with _witness_pool(migrated_url) as pool:
            authority = PostgresAuthority(pool)
            await authority.register(state, credential_hash)
        observer = await connect(migrated_url)
        try:
            row = await (
                await observer.execute(
                    "SELECT authority_kind, authority_binding, fence_protocol "
                    "FROM worker_incarnations WHERE incarnation = %s",
                    (state.incarnation,),
                )
            ).fetchone()
        finally:
            await observer.close()
        assert row == (
            "local",
            {
                "unit": state.unit,
                "generation": state.generation,
                "boot_id": state.boot_id,
                "invocation_id": state.invocation_id,
            },
            CURRENT_WORKER_FENCE_PROTOCOL,
        )
        worker = await connect(migrated_url)
        try:
            await worker.execute("SET SESSION AUTHORIZATION kdive_worker")
            authenticated = await authenticate_worker_incarnation(worker, credential)
            assert authenticated.incarnation == state.incarnation
            with pytest.raises(IncarnationAuthenticationError):
                await authenticate_worker_incarnation(worker, SecretStr("different-credential"))
        finally:
            await worker.close()

    asyncio.run(_run())


def test_authority_terminates_with_the_same_exact_binding(migrated_url: str) -> None:
    async def _run() -> None:
        state = _state()
        async with _witness_pool(migrated_url) as pool:
            authority = PostgresAuthority(pool)
            await authority.register(state, bytes.fromhex(state.credential_hash))
            await authority.terminate(state, "killed")
        observer = await connect(migrated_url)
        try:
            row = await (
                await observer.execute(
                    "SELECT authority_kind, authority_binding, state, outcome "
                    "FROM worker_incarnations WHERE incarnation = %s",
                    (state.incarnation,),
                )
            ).fetchone()
        finally:
            await observer.close()
        assert row == ("local", state.authority_binding(), "terminated", "killed")

    asyncio.run(_run())


@pytest.mark.parametrize("rejection", ["missing", "binding"])
def test_authority_rejects_false_termination_evidence(migrated_url: str, rejection: str) -> None:
    async def _run() -> None:
        state = _state()
        registered = state
        async with _witness_pool(migrated_url) as pool:
            authority = PostgresAuthority(pool)
            if rejection == "binding":
                await authority.register(state, bytes.fromhex(state.credential_hash))
                state = _state(invocation_id="d" * 32)
            with pytest.raises(EvidenceRejected, match="slot 1"):
                await authority.terminate(state, "killed")
        observer = await connect(migrated_url)
        try:
            row = await (
                await observer.execute(
                    "SELECT authority_binding, state, outcome FROM worker_incarnations "
                    "WHERE incarnation = %s",
                    (state.incarnation,),
                )
            ).fetchone()
        finally:
            await observer.close()
        expected = (registered.authority_binding(), "active", None)
        assert row == (None if rejection == "missing" else expected)

    asyncio.run(_run())
