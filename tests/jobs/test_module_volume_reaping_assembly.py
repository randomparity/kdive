"""Production registered module-reaping jobs with an instrumented libvirt boundary."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.assembly import ProcessAssembly
from kdive.db.repositories import JOBS
from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import JobKind, dispatch_lane_for_kind
from kdive.jobs.assembly import build_worker_handler_assembly, register_all_handlers
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.providers.assembly.composition import ProviderComposition
from kdive.providers.remote_libvirt.config import all_remote_configs
from kdive.reconciler.cleanup.provider_resources.module_volume_reaping import (
    enqueue_remote_module_volume_reap,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from kdive.store.assembly import ObjectStoreAssembly
from tests.support.object_store import INERT_OBJECT_STORE
from tests.support.worker_fence import register_worker

_REMOTE_INVENTORY = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
[[remote_libvirt]]
name = "plain"
uri = "qemu+tls://plain.example/system"
gdb_addr = "192.0.2.20"
gdbstub_range = "47000:47099"
client_cert_ref = "clientcert.pem"
client_key_ref = "clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "cacert.pem"
base_image = "base"
cost_class = "remote"
vcpus = 8
memory_mb = 16384
"""


@pytest.mark.parametrize("mixed", [False, True])
def test_registered_module_reap_job(migrated_url, tmp_path, monkeypatch, mixed):
    inventory = _REMOTE_INVENTORY
    if mixed:
        host = inventory.split("[[remote_libvirt]]", 1)[1].replace("plain", "eligible")
        inventory += (
            "\n[[remote_libvirt]]"
            + host
            + """
authority_instance = "authority-a"
authority_address = "192.0.2.20"
authority_port = 47001
authority_server_ca_ref = "authority-ca"
authority_client_cert_ref = "authority-cert"
authority_client_key_ref = "authority-key"  # pragma: allowlist secret - synthetic filename ref
authority_store_identity = "stores/authority-a"
authority_recovery_reserve_bytes = 4096
authority_recovery_max_bytes = 8192
"""
        )
    path = tmp_path / "systems.toml"
    path.write_text(inventory)
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    for name in ("clientcert.pem", "clientkey.pem", "cacert.pem"):
        (tmp_path / name).write_text("fixture-" + name)
    config.load()
    assert len(all_remote_configs()) == (2 if mixed else 1)
    opened, reads, pools = [], [], []
    original = FileRefBackend.resolve

    def resolve(self, ref):
        reads.append(ref)
        return original(self, ref)

    monkeypatch.setattr(FileRefBackend, "resolve", resolve)

    class Connection:
        def storagePoolLookupByName(self, name):
            pools.append(name)
            return self

        def refresh(self, flags=0):
            return None

        def listAllVolumes(self, flags=0):
            return []

        def close(self):
            return None

    def open_connection(uri):
        assert urlsplit(uri).hostname == "eligible.example"
        opened.append(uri)
        return Connection()

    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.reaping.module_volumes.open_libvirt_reaper", open_connection
    )

    async def scenario():
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            worker_id = "module-reap-" + uuid4().hex
            async with pool.connection() as conn:
                credential = await register_worker(conn, worker_id)
            secrets = SecretRegistry()
            process = ProcessAssembly(
                ObjectStoreAssembly(INERT_OBJECT_STORE),
                ProviderComposition(secret_registry=secrets, object_store=INERT_OBJECT_STORE),
            )
            assembly = build_worker_handler_assembly(
                process_assembly=process, incarnation_credential=credential, pool=pool
            )
            registry = HandlerRegistry()
            register_all_handlers(registry, assembly)
            worker = Worker(
                pool,
                registry,
                worker_id=worker_id,
                incarnation_credential=credential,
                secret_registry=secrets,
            )
            for _ in range(2):
                async with pool.connection() as conn:
                    assert await enqueue_remote_module_volume_reap(conn)
                job = await worker.run_once(
                    dispatch_lane_for_kind(JobKind.REMOTE_MODULE_VOLUME_REAP)
                )
                assert job is not None and job.kind is JobKind.REMOTE_MODULE_VOLUME_REAP
                async with pool.connection() as conn:
                    final = await JOBS.get(conn, job.id)
                assert final is not None and final.state is JobState.SUCCEEDED

    asyncio.run(scenario())
    assert len(opened) == len(pools) == (2 if mixed else 0)
    assert reads == (["clientcert.pem", "clientkey.pem", "cacert.pem"] * 2 if mixed else [])
