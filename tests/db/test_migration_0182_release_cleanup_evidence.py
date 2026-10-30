"""The cleanup-evidence read admits a derived release CLEANUP against the root release row."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.remote_module_cleanup_evidence_support import (
    _NONCE,
    _OTHER_DIGEST,
    _derived,
    _read,
    _refused,
    _seed,
)


def test_a_derived_release_cleanup_matches_the_root_release_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, seeded, identity, digest) == ("open", seeded.recovery)
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE remote_module_attempt_obligations SET reap_discharged_at = now() "
            "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
            (seeded.case.system_id, seeded.case.run_id, _NONCE),
        )
    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, seeded, identity, digest) == ("discharged", seeded.recovery)


@pytest.mark.parametrize(
    "override",
    [
        {"peer": "docker:another-worker"},
        {"authority_id": uuid4()},
        {"generation": 99},
        {"sequence": 2},
        {"ack_digest": "sha256:" + "a" * 64},
        {"system_id": uuid4()},
        {"activation_id": uuid4()},
        {"run_id": uuid4()},
        {"plan": "sha256:" + "b" * 64},
        {"provider_kind": "local-libvirt"},
        {"authority_instance": "foreign"},
        {"nonce": "1" * 32},
    ],
    ids=lambda value: next(iter(value)),
)
def test_a_derived_release_cleanup_still_needs_every_binding_field(
    migrated_url: str, authority_role_dsns: _RoleDsns, override: dict[str, object]
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest, **override)


def test_the_phase_identity_and_digest_must_be_the_derived_cleanup_pair(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)
    recover_identity, recover_digest = _derived(migrated_url, seeded, "recover")

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, recover_identity, recover_digest)
        assert _refused(provider, seeded, identity, recover_digest)
        assert _refused(provider, seeded, recover_identity, digest)
        assert _refused(provider, seeded, identity, _OTHER_DIGEST)
        assert _refused(provider, seeded, "sha256:" + "1" * 64, digest)
        assert _refused(provider, seeded, seeded.case.operation_identity, digest)
        assert _refused(
            provider, seeded, seeded.case.operation_identity, seeded.allocated.operation_digest
        )


def test_a_derived_release_cleanup_needs_a_live_current_release_authority(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)
    case = seeded.case

    def refused() -> bool:
        with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
            return _refused(provider, seeded, identity, digest)

    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (case.job_id,),
        )
    assert refused()
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at = now() + interval '5 minutes' WHERE id = %s",
            (case.job_id,),
        )
        admin.execute(
            "UPDATE external_boot_authorities SET state = 'retired', retired_at = now() "
            "WHERE id = %s",
            (seeded.allocated.authority_id,),
        )
    assert refused()


def test_a_release_root_that_is_not_the_release_operation_is_not_a_phase_root(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns, operation="fail")
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest)


@pytest.mark.parametrize("purpose", ["recover", "teardown"])
def test_the_other_admitted_pairs_never_accept_a_derived_request(
    migrated_url: str, authority_role_dsns: _RoleDsns, purpose: str
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns, purpose=purpose, operation=purpose)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest)
        assert _refused(provider, seeded, identity, digest, purpose=purpose, operation=purpose)


def test_a_release_recover_is_still_not_an_admitted_pair(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded, "recover")

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest, operation="recover")


def test_a_standalone_cleanup_root_still_matches_exactly(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns, purpose="release", operation="cleanup")

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(
            provider,
            seeded,
            seeded.case.operation_identity,
            seeded.allocated.operation_digest,
        ) == ("open", seeded.recovery)
        assert _refused(provider, seeded, seeded.case.operation_identity, _OTHER_DIGEST)
