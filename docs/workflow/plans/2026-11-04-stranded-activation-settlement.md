# Stranded activation settlement plan

Goal: add the ADR-0816 settlement primitive for issue #3550.

Architecture: one forward migration holds a settlement table and three role-gated `SECURITY
DEFINER` functions. A repository wraps them, and one service function is the `platform_admin` entry.

Tech stack: Python 3.14, psycopg, PostgreSQL, pytest with disposable Postgres.

Expected implementation size: 620–760 changed lines (M) — migration, repository, service, real-DB tests and four window-list rows, from the file map below.

## Global Constraints

- Migration file is exactly `src/kdive/db/schema/0194_stranded_activation_settlement.sql`; ADR is `docs/adr/0816-stranded-activation-settlement.md`.
- Forward-only (ADR-0015). Ruff line length 100. Prose avoids "critical", "robust", "comprehensive", "elegant", "Sprint".
- No change to admission, routing, release or reconciler code. The only provider edit is one `accepted_function` entry in `host.py`.
- Deferred to #3551: routing, the lease-expiry lane, fences, the tool, refusal auditing. Deferred to #3552: runbook and live proof. Producing the observations and removing objects the authority holds has no owner yet; the campaign orchestrator holds that follow-up.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/db/schema/0194_stranded_activation_settlement.sql` | create | settlement table and functions (criteria 2, 3, 4) |
| `src/kdive/db/external_boot_activations.py` | modify | `SettlementStatus`, `settle_stranded`, `is_settled` (criterion 5) |
| `src/kdive/services/external_boot/settlement.py` | create | `platform_admin` and reason gate (criterion 4) |
| `tests/db/test_migration_0194_stranded_activation_settlement.py` | create | real-DB contracts (criteria 2, 3) |
| `tests/services/external_boot/test_settlement.py` | create | service gate |
| `src/kdive/providers/external_boot_authority/host.py` | modify | `accepted_function` entry for the authority recorder |
| `tests/db/test_migrate.py`, `test_migration_0091_*`, `test_migration_0102_*`, `test_migration_0115_*` | modify | window rows |
| `tests/db/test_worker_fence_authority.py`, `tests/db/test_external_boot_activation_migration.py` | modify | table inventories |

No caller migration and no obsolete path. Migration 0192 is consumed and unused; #3535 holds 0193. 0193 has landed on the base. Check each K by hand after any later base refresh. 0194 must stay above main's highest migration.

## Task 1: migration

Files: create the migration and its test; modify the four window-list tests.

Interfaces: later tasks call the three functions with the signatures below.

Verification:

- Mode: focused-test. Contract: each row of the spec's Settlement order and the role matrix. Case: `tests/db/test_migration_0194_stranded_activation_settlement.py`. Red: `function ... does not exist`. Green: `uv run python -m pytest tests/db/test_migration_0194_stranded_activation_settlement.py -q`, all passed.
- Mode: focused-test. Contract: migration window. Case: the four list tests. Red: list mismatch without the row. Green: `uv run python -m pytest tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py -q`.

Steps:

1. Write the test file with these cases, seeding through `_seed_case` from `tests.db.external_boot_authority_support`, then forcing the stranded shape: `UPDATE external_boot_activations SET state='preparing', recovery_point=NULL, materialization=NULL`, set the marked job `failed`, set `systems.domain_name`, insert a `pending` reservation, and add a running job whose payload is `{"system_id": ...}` for the seeded incarnation. `objects_unproven` sets a raw `materialization` carrying `artifacts`, kernel and modules refs. The applied case allocates a real authority row with `_allocate` before failing the job, and seeds one `remote_module_attempt_obligations` row. Call functions through `authority_role_dsns("kdive_server" | "kdive_worker" | "kdive_provider_authority")`.
   - `test_applied_settles_and_audits`: both records then settle gives `applied`; row is `abandoned`, `cleanup_complete`, reservation gone, release row exists, authority rows not open, obligations discharged, one audit row; replay gives `replayed` and still one audit row. The row validates through `validate_stored(ExternalBootActivation, row)` and the release through `ExternalBootReservationRelease`.
   - `test_authority_owned_system_cannot_settle`: insert an `authority_system_ownership` row directly for the seeded System, with both records present; settle gives `authority_owned` and nothing changes.
   - `test_one_owner_is_not_evidence_for_the_other`, parametrized: worker only gives `authority_evidence_missing`; authority only gives `worker_evidence_missing`.
   - `test_refusals_change_nothing`, parametrized over `not_found`, `not_preparing`, `system_ended`, `job_live`, `evidence_stale`, `authority_journal_moved`, `objects_unproven`, `reservation_missing`, each forcing its condition and asserting the status and an unchanged activation row.
   - `test_role_separation`: `kdive_server` gets `42501` on both recorders; `kdive_worker` on the authority recorder and settle; `kdive_provider_authority` on the worker recorder and settle; `kdive_reconciler` on all three.
   - `test_worker_record_requires_its_own_live_unmarked_job`, parametrized: wrong credential, expired lease, marked job, other System's job, wrong domain each give `superseded`.
   - `test_invalid_input_is_rejected`, parametrized: a blank reason, and an object of `vol:x`, `/x`, `a//b`, `a/../b`, `a"b` or a non-ASCII ref, each give `22023`.
   - `test_settled_is_readable_by_each_role`: `is_settled` false then true as `kdive_server`, `kdive_worker` and `kdive_reconciler`.
2. Run the green command; expect `does not exist`.
3. Create the migration:

```sql
-- 0194_stranded_activation_settlement.sql — #3550, ADR-0816 (2026-11-04).
-- Forward-only (ADR-0015). Ends a stranded fixed-worker `preparing` activation on independent
-- evidence from both owners. Each owner records only its own daemon's fact through its own role.
CREATE TABLE public.external_boot_activation_settlements (
    activation_id uuid PRIMARY KEY
        REFERENCES public.external_boot_activations (id) ON DELETE RESTRICT,
    system_id uuid NOT NULL REFERENCES public.systems (id) ON DELETE RESTRICT,
    authority_instance text,
    authority_domain_name text,
    authority_journal_sequence bigint,
    authority_journal_digest text,
    authority_composite_state text,
    authority_absent_objects text[],
    authority_recorded_at timestamptz,
    worker_incarnation text,
    worker_job_id uuid,
    worker_job_attempt integer,
    worker_domain_name text,
    worker_recorded_at timestamptz,
    principal text,
    reason text,
    settled_at timestamptz,
    CHECK ((authority_recorded_at IS NULL) = (authority_composite_state IS NULL)),
    CHECK ((authority_recorded_at IS NULL) = (authority_absent_objects IS NULL)),
    CHECK ((worker_recorded_at IS NULL) = (worker_domain_name IS NULL)),
    CHECK (settled_at IS NULL OR (
        authority_recorded_at IS NOT NULL AND worker_recorded_at IS NOT NULL
        AND principal IS NOT NULL
        AND reason IS NOT NULL AND octet_length(btrim(reason)) BETWEEN 1 AND 1024
    ))
);

CREATE FUNCTION public.record_stranded_activation_authority_absence(
    p_activation_id uuid, p_authority_instance text, p_domain_name text,
    p_composite_state text, p_absent_objects text[]
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_composite_state IS NULL OR p_composite_state !~ '^sha256:[0-9a-f]{64}$'
       OR p_absent_objects IS NULL OR array_position(p_absent_objects, NULL) IS NOT NULL
       OR EXISTS (SELECT 1 FROM unnest(p_absent_objects) AS ref
                  WHERE octet_length(ref) NOT BETWEEN 1 AND 1024
                     OR ref !~ '^[\x20-\x7e]+$' OR ref ~ '["\\:@]'
                     OR ref ~ '(^|/)\.{0,2}(/|$)') THEN
        RAISE EXCEPTION 'authority absence record is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_activation.system_id::text, 2125));
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = p_activation_id FOR UPDATE;
    IF v_activation.state <> 'preparing' OR NOT (
        EXISTS (SELECT 1 FROM public.external_boot_authorities AS authority
                WHERE authority.activation_id = p_activation_id
                  AND authority.authority_instance = p_authority_instance)
        OR EXISTS (SELECT 1 FROM public.jobs AS job
                   WHERE job.kind = 'boot'
                     AND job.payload #>> '{external_boot_authority_v1,activation_id}'
                         = p_activation_id::text
                     AND job.payload #>> '{external_boot_authority_v1,authority_instance}'
                         = p_authority_instance)
    ) THEN RETURN 'superseded'; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.systems AS system
                   WHERE system.id = v_activation.system_id
                     AND system.domain_name = p_domain_name) THEN
        RETURN 'superseded';
    END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE authority_instance = p_authority_instance AND system_id = v_activation.system_id;
    INSERT INTO public.external_boot_activation_settlements AS settlement (
        activation_id, system_id, authority_instance, authority_domain_name,
        authority_journal_sequence,
        authority_journal_digest, authority_composite_state, authority_absent_objects,
        authority_recorded_at
    ) VALUES (
        p_activation_id, v_activation.system_id, p_authority_instance, p_domain_name,
        v_head.sequence,
        v_head.digest, p_composite_state,
        ARRAY(SELECT DISTINCT ref FROM unnest(p_absent_objects) AS ref),
        clock_timestamp()
    ) ON CONFLICT (activation_id) DO UPDATE SET
        authority_instance = excluded.authority_instance,
        authority_domain_name = excluded.authority_domain_name,
        authority_journal_sequence = excluded.authority_journal_sequence,
        authority_journal_digest = excluded.authority_journal_digest,
        authority_composite_state = excluded.authority_composite_state,
        authority_absent_objects = excluded.authority_absent_objects,
        authority_recorded_at = excluded.authority_recorded_at
    WHERE settlement.settled_at IS NULL;
    RETURN 'applied';
END $$;

CREATE FUNCTION public.record_stranded_activation_worker_ownership(
    p_credential_hash bytea, p_job_id uuid, p_job_attempt integer, p_activation_id uuid,
    p_domain_name text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    IF v_incarnation IS NULL THEN RETURN 'superseded'; END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_activation.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = p_activation_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id = v_activation.system_id;
    IF v_activation.state <> 'preparing'
       OR (v_job.state = 'running' AND v_job.worker_id = v_incarnation
           AND v_job.attempt = p_job_attempt AND v_job.lease_expires_at > clock_timestamp()
           AND v_job.payload ->> 'system_id' = v_system.id::text
           AND NOT v_job.payload ?| ARRAY['external_boot_authority_v1', 'authority_system_v1']
           AND v_system.domain_name = p_domain_name) IS NOT TRUE THEN
        RETURN 'superseded';
    END IF;
    INSERT INTO public.external_boot_activation_settlements AS settlement (
        activation_id, system_id, worker_incarnation, worker_job_id, worker_job_attempt,
        worker_domain_name, worker_recorded_at
    ) VALUES (
        p_activation_id, v_system.id, v_incarnation, p_job_id, p_job_attempt, p_domain_name,
        clock_timestamp()
    ) ON CONFLICT (activation_id) DO UPDATE SET
        worker_incarnation = excluded.worker_incarnation,
        worker_job_id = excluded.worker_job_id,
        worker_job_attempt = excluded.worker_job_attempt,
        worker_domain_name = excluded.worker_domain_name,
        worker_recorded_at = excluded.worker_recorded_at
    WHERE settlement.settled_at IS NULL;
    RETURN 'applied';
END $$;

CREATE FUNCTION public.settle_stranded_external_boot_activation(
    p_activation_id uuid, p_principal text, p_agent_session text, p_actor text, p_reason text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_settlement public.external_boot_activation_settlements%ROWTYPE;
    v_reservation public.external_boot_reservations%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_known text[];
    v_refs text[];
    v_now text;
    v_release jsonb;
    v_release_identity text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member') THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_reason IS NULL OR octet_length(btrim(p_reason)) NOT BETWEEN 1 AND 1024
       OR p_principal IS NULL OR btrim(p_principal) = '' OR p_actor IS NULL THEN
        RAISE EXCEPTION 'settlement requires a principal and a reason' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_activation.system_id::text, 2125));
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = p_activation_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id = v_activation.system_id FOR UPDATE;
    SELECT * INTO v_settlement FROM public.external_boot_activation_settlements
    WHERE activation_id = p_activation_id FOR UPDATE;
    IF v_settlement.settled_at IS NOT NULL THEN RETURN 'replayed'; END IF;
    IF EXISTS (SELECT 1 FROM public.authority_system_ownership AS owner
               WHERE owner.system_id = v_system.id) THEN
        RETURN 'authority_owned';
    END IF;
    IF v_activation.state <> 'preparing' THEN RETURN 'not_preparing'; END IF;
    IF v_system.state IN ('torn_down', 'tearing_down') THEN RETURN 'system_ended'; END IF;
    IF EXISTS (SELECT 1 FROM public.jobs AS job
               WHERE job.state IN ('queued', 'running')
                 AND job.payload #>> '{external_boot_authority_v1,system_id}'
                     = v_system.id::text) THEN
        RETURN 'job_live';
    END IF;
    IF v_settlement.authority_recorded_at IS NULL THEN RETURN 'authority_evidence_missing'; END IF;
    IF v_settlement.worker_recorded_at IS NULL THEN RETURN 'worker_evidence_missing'; END IF;
    IF least(v_settlement.authority_recorded_at, v_settlement.worker_recorded_at)
       < clock_timestamp() - interval '1 hour' THEN
        RETURN 'evidence_stale';
    END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE authority_instance = v_settlement.authority_instance AND system_id = v_system.id
    FOR UPDATE;
    IF (v_head.sequence, v_head.digest) IS DISTINCT FROM
       (v_settlement.authority_journal_sequence, v_settlement.authority_journal_digest) THEN
        RETURN 'authority_journal_moved';
    END IF;
    SELECT * INTO v_reservation FROM public.external_boot_reservations
    WHERE activation_id = p_activation_id FOR UPDATE;
    IF NOT FOUND THEN RETURN 'reservation_missing'; END IF;
    v_known := ARRAY[
        v_activation.materialization #>> '{artifacts,kernel,ref}',
        v_activation.materialization #>> '{artifacts,modules,ref}',
        v_activation.materialization #>> '{artifacts,initrd,ref}',
        v_activation.materialization #>> '{artifacts,debuginfo,ref}',
        v_activation.recovery_point #>> '{recovery_ref,ref}',
        v_activation.pre_recovery_evidence #>> '{recovery_object,ref}'
    ];
    IF EXISTS (SELECT 1 FROM unnest(v_known) AS ref
               WHERE ref IS NOT NULL
                 AND ref <> ALL (v_settlement.authority_absent_objects)) THEN
        RETURN 'objects_unproven';
    END IF;
    v_refs := ARRAY(SELECT ref FROM unnest(v_settlement.authority_absent_objects) AS ref
                    ORDER BY convert_to(ref || '"', 'UTF8'));
    v_now := to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
    v_release := jsonb_build_object(
        'schema', 'external-boot-release-evidence-v1',
        'activation_id', p_activation_id::text, 'system_id', v_system.id::text,
        'store_identity', jsonb_build_object('ref', v_reservation.store_identity),
        'owner_key', jsonb_build_object('ref', v_reservation.owner_key),
        'reserved_bytes', v_reservation.reserved_bytes, 'enumeration_complete', true,
        'objects', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'absent', true, 'object', jsonb_build_object('ref', ref)) ORDER BY ordinal)
            FROM unnest(v_refs) WITH ORDINALITY AS listed (ref, ordinal)), '[]'::jsonb),
        'verified_at', v_now
    );
    v_release_identity := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-release-evidence-v1', 'UTF8') || decode('00', 'hex')
        || convert_to(public.canonical_external_boot_authority_json(v_release), 'UTF8')), 'hex');
    INSERT INTO public.external_boot_reservation_releases (
        activation_id, store_identity, owner_key, reserved_bytes, release_identity,
        release_evidence
    ) VALUES (
        p_activation_id, v_reservation.store_identity, v_reservation.owner_key,
        v_reservation.reserved_bytes, v_release_identity, v_release
    );
    DELETE FROM public.external_boot_reservations WHERE activation_id = p_activation_id;
    UPDATE public.external_boot_activations SET state = 'abandoned', cleanup_complete = true,
        terminal_evidence = jsonb_build_object(
            'schema', 'external-boot-terminal-evidence-v1',
            'activation_id', p_activation_id::text, 'system_id', v_system.id::text,
            'outcome', 'abandoned',
            'composite_state', v_settlement.authority_composite_state,
            'objects', coalesce((
                SELECT jsonb_agg(jsonb_build_object('ref', ref) ORDER BY ordinal)
                FROM unnest(v_refs) WITH ORDINALITY AS listed (ref, ordinal)), '[]'::jsonb),
            'observed_at', to_char(v_settlement.authority_recorded_at AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
        cleanup_evidence = jsonb_build_object(
            'schema', 'external-boot-cleanup-evidence-v1',
            'activation_id', p_activation_id::text, 'system_id', v_system.id::text,
            'release_identity', v_release_identity, 'mode', 'ordinary', 'completed_at', v_now)
    WHERE id = p_activation_id;
    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE activation_id = p_activation_id AND state IN ('allocating', 'current');
    UPDATE public.remote_module_attempt_obligations
    SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
    WHERE system_id = v_system.id AND mutation_discharged_at IS NULL;
    UPDATE public.external_boot_activation_settlements
    SET principal = p_principal, reason = btrim(p_reason), settled_at = clock_timestamp()
    WHERE activation_id = p_activation_id;
    INSERT INTO public.platform_audit_log (
        principal, agent_session, platform_role, tool, scope, args_digest, actor
    ) VALUES (
        p_principal, p_agent_session, 'platform_admin',
        'external_boot.settle_stranded_activation', 'settled:' || p_activation_id::text,
        encode(sha256(convert_to(public.canonical_external_boot_authority_json(
            jsonb_build_object('activation_id', p_activation_id::text, 'reason', btrim(p_reason))
        ), 'UTF8')), 'hex'),
        p_actor
    );
    RETURN 'applied';
END $$;

REVOKE ALL ON TABLE public.external_boot_activation_settlements
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT SELECT ON TABLE public.external_boot_activation_settlements
    TO kdive_server, kdive_worker, kdive_reconciler;
REVOKE ALL ON FUNCTION
    public.record_stranded_activation_authority_absence(uuid,text,text,text,text[]),
    public.record_stranded_activation_worker_ownership(bytea,uuid,integer,uuid,text),
    public.settle_stranded_external_boot_activation(uuid,text,text,text,text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.record_stranded_activation_authority_absence(uuid,text,text,text,text[])
    TO kdive_provider_authority;
GRANT EXECUTE ON FUNCTION
    public.record_stranded_activation_worker_ownership(bytea,uuid,integer,uuid,text)
    TO kdive_worker;
GRANT EXECUTE ON FUNCTION public.settle_stranded_external_boot_activation(uuid,text,text,text,text)
    TO kdive_server;
```

4. Add the row `("0194", "0194_stranded_activation_settlement.sql")` to the end of each of the four lists and raise K by one (`-89` to `-90`, `-85` to `-86` twice, `-72` to `-73`, with #3535's 0193 row already present).
5. Add `'public.record_stranded_activation_authority_absence(uuid,text,text,text,text[])'::regprocedure` to `accepted_function` in `host.py`, add `external_boot_activation_settlements` to the `external_boot_%` table set in `test_external_boot_activation_migration.py`, and give it its own `SELECT`-only entry in `test_worker_fence_authority.py`, for the worker and the reconciler.
6. Run both green commands plus `uv run python -m pytest tests/db/test_worker_fence_authority.py tests/db/test_external_boot_activation_migration.py tests/db/test_connected_authority_acceptance.py tests/db/test_external_boot_authority_head_inventory_migration.py -q`. Commit `feat(db): add stranded activation settlement primitive`.

## Task 2: repository and service

Files: modify `src/kdive/db/external_boot_activations.py`; create `src/kdive/services/external_boot/settlement.py` and `tests/services/external_boot/test_settlement.py`.

Interfaces: consumes the three functions. #3551 calls `settle_stranded_activation` and `ExternalBootActivationRepository.is_settled`.

Verification:

- Mode: focused-test. Contract: the service refuses without `platform_admin` before any database call, refuses a blank reason, and maps a refusal to `CategorizedError` with `details["reason"]`. Case: `tests/services/external_boot/test_settlement.py`. Red: import error. Green: `uv run python -m pytest tests/services/external_boot/test_settlement.py -q`.

Steps:

1. Write the tests with `RequestContext` values and a stub repository for the gate cases, plus one real-DB case using the Task 1 seed for `applied`.
2. Run them; expect the import error.
3. In the repository module add `SettlementStatus(StrEnum)`, one member per status in the spec's Settlement order plus `APPLIED = "applied"` and `REPLAYED = "replayed"`, and export it. Add to `ExternalBootActivationRepository`, each a keyword-only wrapper over one `SELECT public.<function>(...)`, raising `RuntimeError` on no row:

```python
async def settle_stranded(
    self,
    conn,
    *,
    activation_id: UUID,
    principal: str,
    agent_session: str | None,
    actor: str,
    reason: str,
) -> SettlementStatus: ...
async def is_settled(self, conn, activation_id: UUID) -> bool: ...
```

`is_settled` selects the row with `settled_at IS NOT NULL`.

4. Create the service:

```python
"""The `platform_admin` entry to stranded activation settlement (ADR-0816)."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.external_boot_activations import (
    ExternalBootActivationRepository,
    SettlementStatus,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, require_platform_role

_REPOSITORY = ExternalBootActivationRepository()


async def settle_stranded_activation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID, actor: str, reason: str
) -> SettlementStatus:
    """Settle one stranded activation, or raise naming the closed refusal reason.

    The caller owns denial auditing, as the break-glass tools do; the settlement function writes
    the success audit row in its own transaction.
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    if not reason.strip():
        raise CategorizedError(
            "settling a stranded activation requires a non-blank reason",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "reason_required"},
        )
    status = await _REPOSITORY.settle_stranded(
        conn,
        activation_id=activation_id,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        actor=actor,
        reason=reason,
    )
    if status in {SettlementStatus.APPLIED, SettlementStatus.REPLAYED}:
        return status
    raise CategorizedError(
        f"activation {activation_id} was not settled: {status.value}",
        category=(
            ErrorCategory.NOT_FOUND
            if status is SettlementStatus.NOT_FOUND
            else ErrorCategory.CONFLICT
        ),
        details={"reason": status.value},
        terminal=True,
    )
```

5. Run the green command, then `just lint` and `just type`. Commit `feat(external-boot): add the settlement service entry`.

Rollback: forward-only; a defect takes a new migration. No production caller exists until #3551.
