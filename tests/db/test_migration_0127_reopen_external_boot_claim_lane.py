"""Migration 0127 reopens the worker claim path for authority-marked payloads (#2201).

0122_external_boot_authority.sql closed both the claim path and the generic finalization
path against payloads carrying ``external_boot_authority_v1``. 0127 reverses only the claim
half. The finalizer half must survive, because ``commit_external_boot_authority_result``
finalizes the ``jobs`` row itself under SECURITY DEFINER: reopening the generic finalizers
would give an authority-marked job two competing terminalization paths, one of them outside
that commit.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb

from tests.db.external_boot_authority_support import (
    _ALLOCATE_SIGNATURE,
    _COMMIT_SIGNATURE,
    _apply_version,
    _RoleDsns,
    _seed_case,
)
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)

_CLAIM_FUNCTIONS = (
    "public.claim_worker_job(text,bytea,interval,text[])",
    "public.count_claimable_worker_jobs(text[])",
)
_FINALIZER_FUNCTIONS = (
    "public.complete_worker_job(uuid,bytea,integer,text)",
    "public.fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)",
)
_EXTERNAL_BOOT_TABLES = (
    "external_boot_activations",
    "external_boot_reservations",
    "external_boot_recovery_attempts",
    "external_boot_reservation_releases",
)
_MARKER = "external_boot_authority_v1"


def _definition(conn: psycopg.Connection, signature: str) -> str:
    row = conn.execute("SELECT pg_get_functiondef(%s::regprocedure)", (signature,)).fetchone()
    assert row is not None
    return str(row[0])


def _queue(conn: psycopg.Connection, job_id: object) -> None:
    conn.execute(
        "UPDATE jobs SET state='queued', attempt=0, worker_id=NULL, lease_expires_at=NULL, "
        "heartbeat_at=NULL WHERE id=%s",
        (job_id,),
    )


def test_claim_functions_no_longer_exclude_authority_marked_payloads(
    migrated_url: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        for signature in _CLAIM_FUNCTIONS:
            assert _MARKER not in _definition(conn, signature), signature


def test_generic_finalizers_still_exclude_authority_marked_payloads(
    migrated_url: str,
) -> None:
    """The fence 0127 must not reopen.

    The claim-side and finalizer-side exclusions differ only by an alias
    (``j.payload`` vs ``payload``), so a reversal that matched the bare marker token
    would remove both. This is the only assertion that catches that.
    """
    with psycopg.connect(migrated_url) as conn:
        for signature in _FINALIZER_FUNCTIONS:
            definition = _definition(conn, signature)
            assert f"AND NOT (payload ? '{_MARKER}')" in definition, signature


def test_rewritten_claim_functions_keep_their_security_attributes(migrated_url: str) -> None:
    """The rewrite must not drop SECURITY DEFINER or the empty search_path.

    0127 rebuilds both bodies from ``pg_get_functiondef`` and re-executes them. Losing
    ``SET search_path = ''`` on a SECURITY DEFINER function would be a search_path injection
    hole, and it would be invisible to every behavioural test in this module.
    """
    with psycopg.connect(migrated_url) as conn:
        for signature in _CLAIM_FUNCTIONS:
            assert conn.execute(
                "SELECT prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure",
                (signature,),
            ).fetchone() == (True, ['search_path=""']), signature


def test_claim_worker_job_authority_gate_survives_an_accidental_grant(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """The in-body authority gate must survive 0127 rebuilding the function.

    Only the EXECUTE ACL is asserted elsewhere; the ``pg_has_role`` gate is the layer that
    still holds if a grant is ever added by accident. ``count_claimable_worker_jobs`` has
    this guard in ``tests/db/test_worker_fence_authority.py``; ``claim_worker_job`` had no
    equivalent, and 0127 redefines its body.

    Granting first is what makes this test bite: without the grant the call fails with
    ``permission denied for function`` from the ACL layer, never reaching the gate.

    The grant is revoked in ``finally``: the ``migrated_url`` reset restores table data, not
    function ACLs, so leaving it would leak into every later test in this worker.
    """
    server_login = authority_role_dsns.logins["kdive_server"]
    grant = SQL(
        "GRANT EXECUTE ON FUNCTION public.claim_worker_job(text,bytea,interval,text[]) TO {}"
    ).format(Identifier(server_login))
    revoke = SQL(
        "REVOKE EXECUTE ON FUNCTION public.claim_worker_job(text,bytea,interval,text[]) FROM {}"
    ).format(Identifier(server_login))
    with psycopg.connect(migrated_url, autocommit=True) as owner:
        owner.execute(grant)
        try:
            with (
                psycopg.connect(authority_role_dsns("kdive_server"), autocommit=True) as server,
                pytest.raises(
                    psycopg.errors.InsufficientPrivilege,
                    match="worker authority is required",
                ),
            ):
                server.execute(
                    "SELECT id FROM public.claim_worker_job(%s, %s, interval '5 minutes', "
                    "ARRAY['default'])",
                    ("w-not-a-worker", b"x" * 32),
                )
        finally:
            owner.execute(revoke)


def test_worker_claims_and_counts_an_authority_marked_job(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="r")
        _queue(conn, case.job_id)
        assert conn.execute(
            "SELECT payload ? %s FROM jobs WHERE id=%s", (_MARKER, case.job_id)
        ).fetchone() == (True,)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            "SELECT count_claimable_worker_jobs(ARRAY['default'])"
        ).fetchone() == (1,)
        assert worker.execute(
            "SELECT id FROM claim_worker_job(%s, %s, interval '5 minutes', ARRAY['default'])",
            (case.worker_id, case.credential),
        ).fetchone() == (case.job_id,)


def test_generic_finalizers_still_refuse_an_authority_marked_job(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="s")
        _queue(conn, case.job_id)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            "SELECT id FROM claim_worker_job(%s, %s, interval '5 minutes', ARRAY['default'])",
            (case.worker_id, case.credential),
        ).fetchone() == (case.job_id,)
        assert (
            worker.execute(
                "SELECT state FROM complete_worker_job(%s, %s, 1, 'unused')",
                (case.job_id, case.credential),
            ).fetchone()
            is None
        )
        assert (
            worker.execute(
                "SELECT state FROM fail_worker_job(%s, %s, 1, 'provider_error', %s, true)",
                (case.job_id, case.credential, Jsonb({})),
            ).fetchone()
            is None
        )
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=%s", (case.job_id,)).fetchone() == (
            "running",
        )


def test_reapplying_the_migration_raises_on_an_already_reversed_database(
    migrated_url: str,
) -> None:
    with (
        psycopg.connect(migrated_url) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="unexpected source shape"),
    ):
        _apply_version(conn, "0127")


def test_grants_and_authority_vocabulary_are_unchanged(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    granted = {"kdive_worker"}
    others = {"kdive_server", "kdive_reconciler", "kdive_provider_authority"}
    with psycopg.connect(migrated_url) as conn:
        for signature in (_ALLOCATE_SIGNATURE, _COMMIT_SIGNATURE):
            for role in granted | others:
                login = authority_role_dsns.logins[role]
                assert conn.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (login, signature)
                ).fetchone() == ((role in granted),), (signature, role)
        worker_login = authority_role_dsns.logins["kdive_worker"]
        for table in _EXTERNAL_BOOT_TABLES:
            assert conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT'), "
                "has_table_privilege(%s, %s, 'UPDATE')",
                (worker_login, table, worker_login, table),
            ).fetchone() == (True, False), table
        purposes = conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname IN ('external_boot_authority_purpose', "
            "'external_boot_authority_audit_purpose') ORDER BY conname"
        ).fetchall()
        assert len(purposes) == 2
        for _, definition in purposes:
            for purpose in ("activate", "recover", "resolve-conflict", "release", "teardown"):
                assert f"'{purpose}'" in definition, definition
