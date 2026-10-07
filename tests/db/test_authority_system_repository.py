"""Real-Postgres round trip of the authority System repository (ADR-0623)."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.profiles.provisioning import ProvisioningProfile, dump_profile, profile_digest
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    AuthoritySystemJournalPhase,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemObservationV1,
    AuthoritySystemOperation,
    AuthoritySystemTakeoverRequestV1,
    make_authority_system_record,
)
from kdive.providers.system_authority.repository import (
    AuthoritySystemJournalHead,
    DatabaseAuthoritySystemRepository,
)
from tests.db.external_boot_authority_support import _RoleDsns

_BOOTSTRAP_KEY = "ssh-ed25519 YWFhYQ== kdive-system"
_ROOT_DIGEST = "sha256:" + "b" * 64
_QUIESCENCE = "sha256:" + "d" * 64


def _repository(dsn: str) -> DatabaseAuthoritySystemRepository:
    @asynccontextmanager
    async def connections():
        connection = await psycopg.AsyncConnection.connect(dsn)
        try:
            yield connection
        finally:
            await connection.close()

    return DatabaseAuthoritySystemRepository(connections)


def _seed_provision(
    migrated_url: str, worker: str, credential: bytes, profile: ProvisioningProfile
) -> dict[str, UUID]:
    ids = {name: uuid4() for name in ("resource", "allocation", "system", "image", "job")}
    root = RootSpecV1(
        architecture="x86_64",
        root="/dev/vda1",
        arguments=("root=/dev/vda1",),
        authority="stage-inspection",
        source={"kind": "staged-image", "identity": _ROOT_DIGEST},
    )
    profile_identity = "sha256:" + profile_digest(profile)
    marker = {
        "schema": "authority-system-marker-v1",
        "system_id": str(ids["system"]),
        "allocation_id": str(ids["allocation"]),
        "resource_id": str(ids["resource"]),
        "provider_kind": "local-libvirt",
        "resource_name": "host-repo",
        "authority_instance": "auth-repo",
        "profile_identity": profile_identity,
        "root_identity": _ROOT_DIGEST,
        "operation": "provision",
        "operation_identity": "provision-repo",
    }
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) VALUES "
            "(%s,'local-libvirt','host-repo','default','standard','available','qemu:///system')",
            (ids["resource"],),
        )
        conn.execute(
            "INSERT INTO allocations (id,resource_id,state,principal,project) "
            "VALUES (%s,%s,'active','p','proj')",
            (ids["allocation"], ids["resource"]),
        )
        conn.execute(
            "INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project) "
            "VALUES (%s,%s,'provisioning',%s,'p','proj')",
            (ids["system"], ids["allocation"], Jsonb(dump_profile(profile))),
        )
        conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id,source_image_id,project,architecture,image_digest,root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,%s)",
            (
                ids["system"],
                ids["image"],
                _ROOT_DIGEST,
                Jsonb(root.model_dump(mode="json", by_alias=True)),
            ),
        )
        conn.execute(
            "INSERT INTO system_bootstrap_keys (system_id,private_key,public_key) "
            "VALUES (%s,'private',%s)",
            (ids["system"], _BOOTSTRAP_KEY),
        )
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash) "
            "VALUES (%s,'docker','{}',4,%s)",
            (worker, credential),
        )
        conn.execute(
            "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
            "payload,authorizing,dedup_key) VALUES "
            "(%s,'provision','running',1,3,%s,clock_timestamp()+interval '5 minutes',%s,%s,%s)",
            (
                ids["job"],
                worker,
                Jsonb({"authority_system_v1": marker}),
                Jsonb({"principal": "p", "agent_session": "repo-session", "project": "proj"}),
                f"repo-{ids['job']}",
            ),
        )
        conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity) VALUES "
            "(%s,%s,%s,'local-libvirt','host-repo','auth-repo',%s,%s)",
            (ids["system"], ids["allocation"], ids["resource"], profile_identity, _ROOT_DIGEST),
        )
        conn.commit()
    return ids


def _profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "linux-test",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/base.qcow2"}
                }
            },
        }
    )


def _assert_stored_head(head: AuthoritySystemJournalHead, digest: str | None) -> None:
    assert head.record is not None
    assert head.digest == digest
    assert head.sequence == head.record.sequence
    assert head.phase is head.record.phase


@pytest.mark.anyio
async def test_repository_resolves_journal_heads_it_stored(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    profile = _profile()
    worker, credential, attempt_id = f"docker:repo-{uuid4()}", b"r" * 32, uuid4()
    ids = _seed_provision(migrated_url, worker, credential, profile)
    with psycopg.connect(authority_role_dsns("kdive_worker")) as worker_conn:
        allocated = worker_conn.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (credential, ids["job"], attempt_id),
        ).fetchone()
        assert allocated is not None and allocated[0] == "allocated"
        authority_id, generation, operation_digest = allocated[1:]
        worker_conn.commit()
    takeover = AuthoritySystemTakeoverRequestV1(
        system_id=ids["system"],
        allocation_id=ids["allocation"],
        resource_id=ids["resource"],
        provider_kind="local-libvirt",
        resource_name="host-repo",
        authority_instance="auth-repo",
        profile_identity="sha256:" + profile_digest(profile),
        root_identity=_ROOT_DIGEST,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-repo",
        authority_id=authority_id,
        generation=generation,
        attempt_id=attempt_id,
        operation_digest=operation_digest,
        bootstrap_identity="sha256:" + hashlib.sha256(_BOOTSTRAP_KEY.encode()).hexdigest(),
    )
    repository = _repository(authority_role_dsns("kdive_provider_authority"))

    resolved = await repository.resolve_allocating(worker, takeover)
    assert resolved is not None and resolved.head.record is None
    assert resolved.snapshot.root_spec.root == "/dev/vda1"
    sequence, digest = 0, GENESIS_DIGEST
    for phase in (
        AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
        AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED,
    ):
        advanced = await repository.advance_head(
            worker,
            takeover,
            expected_sequence=sequence,
            expected_digest=digest,
            record=make_authority_system_record(
                takeover, sequence=sequence + 1, previous_digest=digest, phase=phase
            ),
        )
        assert advanced.status == "advanced" and advanced.sequence is not None
        sequence, digest = advanced.sequence, str(advanced.digest)
    resolved = await repository.resolve_allocating(worker, takeover)
    assert resolved is not None
    _assert_stored_head(resolved.head, digest)

    with psycopg.connect(authority_role_dsns("kdive_worker")) as worker_conn:
        ack = worker_conn.execute(
            "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,1,%s,%s,%s,2,%s,%s)",
            (credential, ids["job"], authority_id, generation, attempt_id, digest, _QUIESCENCE),
        ).fetchone()
        assert ack is not None and ack[0] == "acknowledged"
        worker_conn.commit()
    ack_sequence, ack_digest = sequence, digest
    mutation = AuthoritySystemMutationRequestV1.model_validate(
        takeover.model_dump(mode="python", by_alias=True)
    )
    for phase, observation in (
        (AuthoritySystemJournalPhase.ADMITTED, None),
        (AuthoritySystemJournalPhase.MUTATION_STARTED, None),
        (AuthoritySystemJournalPhase.PROVIDER_RETURNED, None),
        (
            AuthoritySystemJournalPhase.OBSERVED,
            AuthoritySystemObservationV1(category="owned", composite_state="sha256:" + "e" * 64),
        ),
    ):
        current = await repository.resolve_current(worker, mutation, ack_sequence, ack_digest)
        assert current is not None and current.head.sequence == sequence
        advanced = await repository.advance_head(
            worker,
            mutation,
            expected_sequence=sequence,
            expected_digest=digest,
            record=make_authority_system_record(
                mutation,
                sequence=sequence + 1,
                previous_digest=digest,
                phase=phase,
                observation=observation,
            ),
        )
        assert advanced.status == "advanced" and advanced.sequence is not None
        sequence, digest = advanced.sequence, str(advanced.digest)
    current = await repository.resolve_current(worker, mutation, ack_sequence, ack_digest)
    assert current is not None
    _assert_stored_head(current.head, digest)
    assert current.head.record is not None
    assert current.head.record.observation == observation
