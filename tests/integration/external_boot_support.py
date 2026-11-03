"""Shared seeding and provider stand-ins for the connected external-boot tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, LiteralString
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

import kdive.config as config_registry
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import LocalObservedState
from kdive.worker_lifecycle.authority_store import CURRENT_WORKER_FENCE_PROTOCOL
from tests.jobs.handlers.external_boot.seeding import AUTHORITY_INSTANCE
from tests.mcp.lifecycle import runs_support

CREDENTIAL = SecretStr("external-boot-e2e-incarnation-credential")


async def register_incarnation(pool: AsyncConnectionPool, worker_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES "
            "(%s, 'local', '{}'::jsonb, %s, sha256(convert_to(%s, 'UTF8'))) "
            "ON CONFLICT (incarnation) DO NOTHING",
            (worker_id, CURRENT_WORKER_FENCE_PROTOCOL, CREDENTIAL.get_secret_value()),
        )


async def fetch_one(
    conn: AsyncConnection, sql: LiteralString, args: tuple[Any, ...]
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, args)
        row = await cur.fetchone()
    assert row is not None
    return dict(row)


DIGEST = "sha256:" + "1" * 64


class PreparingProvider(FaultInjectExternalBoot):
    """Record the worker-owned phases and reopen its prepared recovery point."""

    def __init__(self) -> None:
        super().__init__()
        self.phases: list[str] = []
        self._points: dict[str, Any] = {}
        self._active: set[str] = set()

    def execute_preparation(self, request: Any) -> Any:
        self.phases.append(request.phase)
        return super().execute_preparation(request)

    def prepare(self, materialization: Any, binding: Any, authority: Any) -> Any:
        point = super().prepare(materialization, binding, authority)
        self._points[binding.activation_id] = point
        return point

    def recovery_point(self, binding: Any, authority: Any) -> Any:
        del authority
        return self._points[binding.activation_id]

    def observe_state(self, binding: Any, authority: Any) -> LocalObservedState:
        del authority
        point = self._points[binding.activation_id]
        state = point.target_state if binding.activation_id in self._active else point.source_state
        return LocalObservedState(
            definition=state.definition,
            modules=state.modules,
            active=binding.activation_id in self._active,
        )

    def activate(self, recovery: Any, authority: Any, *, local_timing: Any = None) -> None:
        self.phases.append("activate")
        super().activate(recovery, authority)
        self._active.add(recovery.binding.activation_id)


async def seed_public_external_boot(
    pool: AsyncConnectionPool, *, initrd_state: Literal["present", "null", "missing"] = "present"
) -> tuple[str, str]:
    with_initrd = initrd_state == "present"
    system_id = await runs_support.seed_system(pool)
    investigation_id = await runs_support.seed_investigation(pool)
    run_id = str(uuid4())
    generation = uuid4()
    build_ref = f"{'b' * 64}.{generation}"
    evidence = {
        "schema": "external-boot-evidence-v1",
        "architecture": "x86_64",
        "bundle_sha256": DIGEST,
        "initrd": {"sha256": DIGEST, "size_bytes": 1024} if with_initrd else None,
        "archive_member_count": 3,
        "archive_uncompressed_bytes": 4096,
        "vmlinuz_sha256": DIGEST,
        "vmlinuz_size_bytes": 2048,
        "decoded_kernel_size_bytes": 4096,
        "elf_metadata_bytes": 512,
        "gnu_build_id_size_bytes": 8,
        "release": "6.9.0-kdive",
        "module_source_manifest": DIGEST,
        "module_member_count": 2,
        "module_uncompressed_bytes": 64,
    }
    if initrd_state == "missing":
        del evidence["initrd"]
    root_spec = {
        "schema": "root-spec-v1",
        "architecture": "x86_64",
        "root": "UUID=authority-root",
        "arguments": ["root=UUID=authority-root", "rootfstype=xfs"],
        "authority": "stage-inspection",
        "source": {"kind": "staged-image", "identity": DIGEST},
    }
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO investigation_builds "
            "(investigation_id, generation, build_ref, content_digest, canonical_document, "
            "build_result, artifacts, target_kind, build_profile, state, expires_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,'local-libvirt',%s,'active',%s)",
            (
                investigation_id,
                generation,
                build_ref,
                "b" * 64,
                Jsonb({"version": 2, "external_boot_evidence": evidence}),
                Jsonb(
                    {
                        "kernel_ref": "builds/kernel.tar",
                        **({"initrd_ref": "builds/initrd.img"} if with_initrd else {}),
                    }
                ),
                Jsonb(
                    {
                        "kernel": {"version_id": "kernel-v1"},
                        **({"initrd": {"version_id": "initrd-v1"}} if with_initrd else {}),
                    }
                ),
                Jsonb({"schema_version": 1, "arch": "x86_64"}),
                datetime.now(UTC) + timedelta(days=1),
            ),
        )
        await conn.execute(
            "INSERT INTO runs "
            "(id, investigation_id, system_id, target_kind, state, build_profile, build_ref, "
            "principal, project) VALUES "
            "(%s,%s,%s,'local-libvirt','succeeded',%s,%s,'user-1','proj')",
            (run_id, investigation_id, system_id, Jsonb({"schema_version": 1}), build_ref),
        )
        await conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id, source_image_id, project, architecture, image_digest, root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,%s)",
            (system_id, uuid4(), DIGEST, Jsonb(root_spec)),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s,'install','succeeded','{}'::jsonb)",
            (run_id,),
        )
    return run_id, system_id


def configure_external_boot() -> None:
    config_registry.load(
        {
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": AUTHORITY_INSTANCE,
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY": "store/public-boot",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES": "4096",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES": "8192",
            "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES": "4096",
        }
    )
