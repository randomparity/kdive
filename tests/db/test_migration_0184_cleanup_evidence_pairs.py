"""The cleanup-evidence read admits three pairs and refuses resolve-conflict (#3432)."""

from __future__ import annotations

import psycopg
import pytest

from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.remote_module_cleanup_evidence_support import _derived, _read, _seed

_ADMITTED = [("recover", "recover"), ("release", "cleanup"), ("teardown", "teardown")]


@pytest.mark.parametrize(("purpose", "operation"), _ADMITTED)
def test_an_admitted_pair_passes_the_facts_check(
    migrated_url: str, authority_role_dsns: _RoleDsns, purpose: str, operation: str
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        row = _read(provider, seeded, identity, digest, purpose=purpose, operation=operation)

    assert row == (("open", seeded.recovery) if purpose == "release" else None)


def test_the_resolve_conflict_pair_is_refused_as_invalid_facts(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)

    with (
        psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="facts are invalid"),
    ):
        _read(
            provider,
            seeded,
            identity,
            digest,
            purpose="resolve-conflict",
            operation="resolve-conflict",
        )
