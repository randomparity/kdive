"""The per-System bootstrap key is wired into provision/reprovision/teardown (ADR-0289, #963).

`provision_handler`/`reprovision_handler` must ensure the System's bootstrap key **before**
the provision transaction (so a rollback inside that transaction never orphans an
overlay-injected key the DB no longer records) and pass a customizer carrying that key into
the provisioner. `teardown_handler` must delete the key row so a torn-down System leaves no
stale credential behind.

Follows the PROVEN in-repo pattern: sync `def test_(migrated_url)` + inner `async def _run()`
driven by `asyncio.run`, real `AsyncConnectionPool`, and a fake provisioner recording its
kwargs (mirrors `tests/adversarial/test_provider_state_races.py`'s `_TrackingProvisioner` and
`tests/mcp/systems_support.py`'s `provider_resolver`).
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.upload_manifest import (
    UploadManifestReplaceRequest,
    get_manifest,
    replace_manifest,
)
from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.payloads import ReprovisionPayload, SystemPayload
from kdive.jobs.provider_context import clear_provider_kind, take_provider_kind
from kdive.prereqs.system_bootstrap_key import ensure_system_bootstrap_key
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.security.audit import args_digest
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore, artifact_key
from tests.mcp.systems_support import PROVISIONING_PROFILE, provider_resolver

_RESOLVED_CPU: dict[str, Any] = {"model": "SapphireRapids", "arch": "x86_64"}

_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _RecordingProvisioner:
    """Records the id/profile/customizers/pubkey passed to provision + the resolved-cpu read."""

    def __init__(self, *, fail: bool = False, resolved_cpu: dict[str, Any] | None = None) -> None:
        self.fail = fail
        self._resolved_cpu = resolved_cpu
        self.recorded: dict[str, Any] = {}
        self.read_cpu_for: list[UUID] = []

    def provision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: tuple[Any, ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        self.recorded["system_id"] = system_id
        self.recorded["profile"] = profile
        self.recorded["overlay_customizers"] = overlay_customizers
        self.recorded["bootstrap_pubkey"] = bootstrap_pubkey
        if self.fail:
            raise CategorizedError(
                "simulated provision failure",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
        return f"kdive-{system_id}"

    def read_resolved_cpu(self, system_id: UUID) -> dict[str, Any] | None:
        self.read_cpu_for.append(system_id)
        return self._resolved_cpu

    def reprovision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: tuple[Any, ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        self.recorded["system_id"] = system_id
        self.recorded["profile"] = profile
        self.recorded["overlay_customizers"] = overlay_customizers
        self.recorded["bootstrap_pubkey"] = bootstrap_pubkey
        if self.fail:
            raise CategorizedError(
                "simulated reprovision failure",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
        return f"kdive-{system_id}"

    def teardown(self, domain_name: str) -> None:
        pass


async def _audit_rows(pool: AsyncConnectionPool, object_id: UUID) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT tool, object_kind, object_id, transition, args_digest, project "
            "FROM audit_log WHERE object_id = %s ORDER BY ts",
            (object_id,),
        )
        return list(await cur.fetchall())


async def _system(pool: AsyncConnectionPool, system_id: UUID) -> System:
    async with pool.connection() as conn:
        row = await SYSTEMS.get(conn, system_id)
    assert row is not None
    return row


async def _billing_started(pool: AsyncConnectionPool, allocation_id: UUID) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT active_started_at FROM allocations WHERE id = %s", (allocation_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0] is not None


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(
    pool: AsyncConnectionPool, state: SystemState, *, provisioning_profile: dict[str, Any]
) -> UUID:
    async with pool.connection() as conn:
        resource = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities={"vcpus": 64, "memory_mb": 65536},
                pool="default",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                resource_id=resource.id,
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=state,
                provisioning_profile=provisioning_profile,
            ),
        )
    return system.id


async def _key_row_count(pool: AsyncConnectionPool, system_id: UUID) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0]


def test_provision_handler_ensures_key_and_passes_one_customizer(migrated_url: str) -> None:
    clear_provider_kind()
    recorded_pubkeys: list[str | None] = []

    def _recording_customizer(pubkey: str) -> Any:
        recorded_pubkeys.append(pubkey)
        return lambda _path: None

    async def _run() -> dict[str, Any]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.PROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            prov = _RecordingProvisioner(resolved_cpu=_RESOLVED_CPU)
            resolver = provider_resolver(
                provisioner=prov, bootstrap_key_customizer=_recording_customizer
            )
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:provision",
                )
            async with pool.connection() as conn:
                result = await systems_handlers.provision_handler(conn, job, resolver=resolver)
            kind = take_provider_kind()
            count = await _key_row_count(pool, system_id)
            system = await _system(pool, system_id)
            async with pool.connection() as conn:
                pubkey = await ensure_system_bootstrap_key(
                    conn, system_id, secret_registry=SecretRegistry()
                )
            return {
                "result": result,
                "kind": kind,
                "count": count,
                "system": system,
                "pubkey": pubkey,
                "prov": prov,
                "billing": await _billing_started(pool, system.allocation_id),
                "audit": await _audit_rows(pool, system_id),
                "system_id": system_id,
            }

    out = asyncio.run(_run())
    system_id = out["system_id"]
    prov = out["prov"]
    assert len(prov.recorded["overlay_customizers"]) == 1
    assert out["count"] == 1
    assert out["pubkey"].startswith("ssh-ed25519 ")
    # The ensured public key is threaded to the provider as bootstrap_pubkey (ADR-0291): remote
    # injects it over the guest agent; local ignores it (uses the overlay customizer).
    assert prov.recorded["bootstrap_pubkey"] == out["pubkey"]
    # The overlay customizer is built from the ensured pubkey, not from None.
    assert recorded_pubkeys == [out["pubkey"]]
    # The provider is called with this System's id and the parsed (non-None) profile.
    assert prov.recorded["system_id"] == system_id
    assert isinstance(prov.recorded["profile"], ProvisioningProfile)
    # The System reaches READY with the provider-returned domain name and persisted resolved CPU.
    assert out["result"] == str(system_id)
    assert out["system"].state is SystemState.READY
    assert out["system"].domain_name == f"kdive-{system_id}"
    assert out["system"].resolved_cpu == _RESOLVED_CPU
    assert prov.read_cpu_for == [system_id]  # read with the real id, off the event loop
    assert out["kind"] == "local-libvirt"  # provider-kind tag set for worker telemetry
    assert out["billing"] is True  # allocation billing interval opened at READY
    # The ready transition is audited under systems.provision with the system-id args digest.
    assert out["audit"] == [
        (
            "systems.provision",
            "systems",
            system_id,
            "provisioning->ready",
            args_digest({"system_id": str(system_id)}),
            "proj",
        )
    ]


class _RootfsHeadStore:
    """A store whose ``head`` reports the pre-uploaded rootfs object (upload-window path)."""

    def __init__(self) -> None:
        self.head_keys: list[str] = []

    def head(self, key: str) -> HeadResult:
        self.head_keys.append(key)
        return HeadResult(size_bytes=1024, checksum_sha256=None, etag="rootfs-etag")


def test_provision_commits_uploaded_rootfs_artifact(migrated_url: str) -> None:
    """An upload-kind rootfs commits its write-once artifacts row at READY (ADR-0048 §6)."""
    upload_profile = copy.deepcopy(PROVISIONING_PROFILE)
    upload_profile["provider"]["local-libvirt"]["rootfs"] = {"kind": "upload"}

    async def _run() -> dict[str, Any]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.PROVISIONING, provisioning_profile=upload_profile
            )
            async with pool.connection() as conn:
                await replace_manifest(
                    conn,
                    UploadManifestReplaceRequest(
                        owner_kind="systems",
                        owner_id=system_id,
                        prefix=f"local/systems/{system_id}",
                        entries=(),
                        ttl=timedelta(hours=1),
                    ),
                )
            store = _RootfsHeadStore()
            resolver = provider_resolver(provisioner=_RecordingProvisioner())
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:provision",
                )
            async with pool.connection() as conn:
                result = await systems_handlers.provision_handler(
                    conn,
                    job,
                    resolver=resolver,
                    artifact_store=cast(ObjectStore, store),
                )
            async with pool.connection() as conn:
                manifest = await get_manifest(conn, "systems", system_id)
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT object_key, retention_class FROM artifacts "
                    "WHERE owner_kind = 'systems' AND owner_id = %s",
                    (system_id,),
                )
                rows = list(await cur.fetchall())
            return {
                "result": result,
                "rows": rows,
                "head_keys": store.head_keys,
                "sid": system_id,
                "manifest": manifest,
            }

    out = asyncio.run(_run())
    sid = out["sid"]
    rootfs_key = artifact_key("local", "systems", str(sid), "rootfs")
    # The commit reads the uploaded object's head under the exact rootfs key ...
    assert out["head_keys"] == [rootfs_key]
    # ... persists the write-once rootfs artifacts row for the System ...
    assert (rootfs_key, "rootfs") in out["rows"]
    # ... and deletes this System's upload manifest (owner_kind/owner_id threaded exactly).
    assert out["manifest"] is None
    assert out["result"] == str(sid)


def test_key_row_survives_provision_transaction_rollback(migrated_url: str) -> None:
    """A provisioner failure after the key was ensured+committed leaves the key row present.

    This is the load-bearing invariant (ADR-0289): the ensure runs in its own committed
    transaction *before* the main provision transaction, so a later failure/rollback never
    un-records a key the overlay may already trust.
    """

    async def _run() -> int:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.PROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            prov = _RecordingProvisioner(fail=True)
            resolver = provider_resolver(provisioner=prov)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:provision",
                )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await systems_handlers.provision_handler(conn, job, resolver=resolver)
            return await _key_row_count(pool, system_id)

    assert asyncio.run(_run()) == 1


def test_reprovision_handler_ensures_key_and_passes_one_customizer(migrated_url: str) -> None:
    clear_provider_kind()
    fingerprint = profile_digest(ProvisioningProfile.parse(PROVISIONING_PROFILE))

    async def _run() -> dict[str, Any]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.REPROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            prov = _RecordingProvisioner(resolved_cpu=_RESOLVED_CPU)
            resolver = provider_resolver(provisioner=prov)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.REPROVISION,
                    ReprovisionPayload(system_id=str(system_id), profile_digest=fingerprint),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:reprovision",
                )
            async with pool.connection() as conn:
                result = await systems_handlers.reprovision_handler(conn, job, resolver=resolver)
            kind = take_provider_kind()
            return {
                "result": result,
                "kind": kind,
                "count": await _key_row_count(pool, system_id),
                "system": await _system(pool, system_id),
                "prov": prov,
                "audit": await _audit_rows(pool, system_id),
                "system_id": system_id,
            }

    out = asyncio.run(_run())
    system_id = out["system_id"]
    assert len(out["prov"].recorded["overlay_customizers"]) == 1
    assert out["count"] == 1
    assert out["result"] == str(system_id)
    assert out["kind"] == "local-libvirt"
    assert out["system"].state is SystemState.READY
    assert out["system"].domain_name == f"kdive-{system_id}"
    # The recreated qcow2 stamps the new profile fingerprint (not None) at the READY boundary.
    assert out["system"].target_fingerprint == fingerprint
    # The local resolved CPU is persisted at READY (the binding kind threads into _persist).
    assert out["system"].resolved_cpu == _RESOLVED_CPU
    assert out["audit"] == [
        (
            "systems.reprovision",
            "systems",
            system_id,
            "reprovisioning->ready",
            args_digest({"system_id": str(system_id)}),
            "proj",
        )
    ]


def test_provision_failure_marks_system_failed_and_audits(migrated_url: str) -> None:
    async def _run() -> dict[str, Any]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.PROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            resolver = provider_resolver(provisioner=_RecordingProvisioner(fail=True))
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:provision",
                )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await systems_handlers.provision_handler(conn, job, resolver=resolver)
            return {
                "terminal": excinfo.value.terminal,
                "system": await _system(pool, system_id),
                "audit": await _audit_rows(pool, system_id),
                "system_id": system_id,
            }

    out = asyncio.run(_run())
    assert out["terminal"] is True  # a failed provision dead-letters at once
    assert out["system"].state is SystemState.FAILED
    assert out["audit"] == [
        (
            "systems.provision",
            "systems",
            out["system_id"],
            "provisioning->failed",
            args_digest({"system_id": str(out["system_id"])}),
            "proj",
        )
    ]


def test_reprovision_failure_marks_system_failed_and_audits(migrated_url: str) -> None:
    fingerprint = profile_digest(ProvisioningProfile.parse(PROVISIONING_PROFILE))

    async def _run() -> dict[str, Any]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.REPROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            resolver = provider_resolver(provisioner=_RecordingProvisioner(fail=True))
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.REPROVISION,
                    ReprovisionPayload(system_id=str(system_id), profile_digest=fingerprint),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:reprovision",
                )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await systems_handlers.reprovision_handler(conn, job, resolver=resolver)
            return {
                "terminal": excinfo.value.terminal,
                "system": await _system(pool, system_id),
                "audit": await _audit_rows(pool, system_id),
                "system_id": system_id,
            }

    out = asyncio.run(_run())
    assert out["terminal"] is True
    assert out["system"].state is SystemState.FAILED
    assert out["audit"] == [
        (
            "systems.reprovision",
            "systems",
            out["system_id"],
            "reprovisioning->failed",
            args_digest({"system_id": str(out["system_id"])}),
            "proj",
        )
    ]


def test_teardown_handler_deletes_key_row(migrated_url: str) -> None:
    async def _run() -> tuple[int, list[tuple[Any, ...]]]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.READY, provisioning_profile=PROVISIONING_PROFILE
            )
            async with pool.connection() as conn:
                await ensure_system_bootstrap_key(conn, system_id, secret_registry=SecretRegistry())
            prov = _RecordingProvisioner()
            resolver = provider_resolver(provisioner=prov)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.TEARDOWN,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:teardown",
                )
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, resolver=resolver)
            return await _key_row_count(pool, system_id), await _audit_rows(pool, system_id)

    count, audit = asyncio.run(_run())
    assert count == 0
    # Teardown audits the terminal transition under systems.teardown.
    assert len(audit) == 1
    tool, object_kind, _oid, transition, _digest, project = audit[0]
    assert tool == "systems.teardown"
    assert object_kind == "systems"
    assert transition == "ready->torn_down"
    assert project == "proj"


def test_teardown_handler_reclaims_pcap_directory(migrated_url, tmp_path, monkeypatch) -> None:
    async def _run() -> bool:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.READY, provisioning_profile=PROVISIONING_PROFILE
            )
            pcap_root = tmp_path / str(system_id)
            # Key the pcap dir on the id so a wrong-id lookup (a mutated arg) points elsewhere.
            monkeypatch.setattr(systems_handlers, "pcap_dir", lambda sid: tmp_path / str(sid))
            pcap_root.mkdir(parents=True)
            (pcap_root / "job.pcap").write_bytes(b"\xd4\xc3\xb2\xa1")
            resolver = provider_resolver(provisioner=_RecordingProvisioner())
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.TEARDOWN,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:teardown",
                )
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, resolver=resolver)
            return pcap_root.exists()

    assert asyncio.run(_run()) is False


def test_teardown_handler_pcap_reclaim_tolerates_absent_dir(
    migrated_url, tmp_path, monkeypatch
) -> None:
    async def _run() -> str | None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.READY, provisioning_profile=PROVISIONING_PROFILE
            )
            # pcap_dir points at a path that was never created — teardown must still succeed.
            monkeypatch.setattr(
                systems_handlers, "pcap_dir", lambda _sid: tmp_path / "absent" / str(system_id)
            )
            resolver = provider_resolver(provisioner=_RecordingProvisioner())
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.TEARDOWN,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:teardown",
                )
            async with pool.connection() as conn:
                return await systems_handlers.teardown_handler(conn, job, resolver=resolver)

    assert asyncio.run(_run()) is not None
