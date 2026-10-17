# Vmcore retention rollout

`KDIVE_VMCORE_RETENTION_DAYS` is an optional positive integer number of days. Unset disables
retirement, including pending history drains. There is no numeric default. Set the same value
on server, worker and reconciler only after completing the baseline below and obtaining the
operator's activation authorization. This runbook describes the supported transition; it does
not authorize executing maintenance, credential changes or deletion.

## Evidence lifetime

A published raw/redacted pair becomes eligible only when its Run is `succeeded`, `failed` or
`canceled` and its Investigation is `closed` or `abandoned`. An open or active Investigation
protects evidence regardless of age. PostgreSQL time and the later of the pair's artifact
creation timestamps determine eligibility. Download protection, queued/running capture jobs,
and live write leases can postpone retirement. Eligibility is not a cleanup completion deadline.

Every new capture journals unique execution destinations before provider I/O. Local streamed
writes and guest presigned writes require conditional creation. Retirement first commits a
monotonic Run marker, then arbitrates each destination against delayed writes using a retained
zero-byte tombstone. History deletion addresses exact VersionIds. A pass considers at most
32 executions; each key permits at most 1,000 deletions and two version-list requests. Partial
or failed drains retain journal state and retry no sooner than 30 seconds later.

A retired execution retains its journal and two zero-byte fence objects. These are deliberate
storage costs, not remaining crash evidence. Generic upload cleanup excludes journal-owned keys
in every state. Do not manually delete tombstones, journal rows, or the baseline. The journal's
Run foreign key prevents deleting its owner; job IDs deliberately have no foreign key, so job
history collection does not erase destination ownership. No runtime role can delete journal or
baseline rows. This policy does not reclaim kernel builds or host dump volumes.

Affected reads expose `data.vmcore_retention`: `server_time`, `policy_enabled`, `eligible_at`,
`protected_until`, `retired_at`, and `protection_reason`. All timestamps are absolute UTC on
PostgreSQL's reference clock, scoped to one Run's pair. Authorized retired reads and capture
admission/replay fail with `not_found`, reason `vmcore_retired`, and `runs.get` as recovery.
Old artifact IDs cease to resolve after removal. Recapture cannot restore retired evidence.

Synchronous reads and analysis hold the existing Run lock. Cancellation drains the local I/O
thread before releasing that lock. Database session loss releases database protection and returns
`infrastructure_failure`; the service discards the result rather than claiming uninterrupted
analysis after authority loss. Retry the same read if evidence remains available.

For issued URLs, `download_expires_at` is the actual signed validity deadline and `server_time`
is the reference clock. Protection is persisted before returning the URL. This protects URL
validity, not completion of an external transfer after expiration. Request another URL through
`artifacts.fetch_raw` or `artifacts.get` while the evidence remains available.

## Deployment prerequisites

Apply migration `0176_vmcore_retention_write_fence.sql` through the normal migration owner.
Upgrade all app roles together before accepting captures. Old binaries use unconditional keys and
must not run against an activated retention deployment. Keep the policy unset during transition.

The bundled backend is the immutable image selected in `docker-compose.yml`, built from
SeaweedFS commit `d997fba1575583a89cf0cc50dc0150642286c86d`. It must run as the reviewed single
`weed mini` process containing the gateway, filer and volume service, with bucket versioning.
The conditional-write contract includes a second check at serialized publication after body
upload. An admission-only check is insufficient for an already accepted slow request.

The normal signed slow-body/tombstone proof has passed against the pinned versioned image.
The isolated backend publication-check fault proof remains required and unrun. Do not activate
retention until that proof and the deployment's conformance evidence are accepted. An external
backend requires equivalent publication semantics; AWS documentation alone does not establish
another provider's behavior. Native synthetic verification does not prove foreign-architecture
execution or an installed deployment.

## Fresh empty namespace

A fresh baseline requires an empty vmcore catalog and no historical vmcore keys or versions in
any supported namespace. Verify that no old producer can write to it, verify conditional
publication, and keep those observations in a private operator receipt. Fresh does not mean
"the database was reset" when the bucket still contains old objects. Unknown objects fail closed.

The receipt has `mode: "fresh"` and these Boolean attestations, each true only after verification:
`fresh_namespace_verified`, `conditional_publication_verified`, and `all_old_producers_stopped`.
Run the offline import below while app roles remain stopped. A fresh baseline on an external
store is supported only with the same verified conditional-publication contract.

## Finite legacy transition

This path is for supported existing protocol-4 deployments using the exact bundled single-process
backend. It preserves catalog-owned raw/redacted pairs and their original later-pair capture time.
It does not override other migrations' fresh-resource requirements or authorize discarding data.
External legacy stores require a separately reviewed backend-owner procedure; the importer refuses
them. Unknown identity inventory is a HOLD, not an alternative receipt mode.

1. Inventory every producer and every effective signing identity: app processes, workers,
   guests holding presigned PUTs, static configuration, dynamic IAM, environment credentials,
   additional gateways and replicas. Inspect the actual backend configuration, including
   `mini.options`, rather than assuming environment variables are the only identity source.
   Verify the exact image, single-process topology, persistent volumes and bucket versioning.
   Missing, empty or contradictory inventory blocks this procedure.
2. Obtain the separate maintenance authorization, including downtime and permanent credential
   revocation. Stop admission and every producer through the deployment's existing lifecycle
   gate. Isolate ingress and record actual process/container termination. Stop the complete old
   store process; establish that no surviving peer can commit writes to this bucket. A job lease
   expiry, successful PUT, URL expiry or rejected finalization does not prove request quiescence.
   Preserve the database and store volumes. Do not use `compose-down --volumes`.
3. Permanently revoke every old signer. Remove old identities from all effective static, dynamic
   and environment sources; configure new nonempty, non-sample credentials for the store and
   all app roles. Compose uses the paired `COMPOSE_S3_ACCESS_KEY` and `COMPOSE_S3_SECRET_KEY`
   variables, mapped to `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` on both sides.
   Do not restore an old key during rollback.
4. Restart only the preserved store. Verify its complete effective identity inventory again,
   authenticated reads and versioning with the new identity, rejection of old credentials and
   retained old presigned PUTs, and denial of anonymous writes. Use a separately owned disposable
   verification namespace for write probes. Record response classes and image/config identity
   without putting secrets or signed URLs in public records.
5. Keep app roles stopped. Apply the schema as migration owner and inventory catalog ownership,
   exact versions and all supported vmcore prefixes. Import only unambiguous known pairs.
   Missing siblings, mixed sensitivity, mismatched etags, malformed ownership, duplicates or
   unregistered vmcore history require explicit reconciliation before activation. Do not infer
   quiescence from object age. The importer makes no storage deletion or credential change.
6. Verify upgraded app binaries and conditional writes; start roles with the policy unset first.
   Obtain the separate activation authorization, then configure the same positive days value
   across all roles through the existing deployment restart path. Historical evidence can be
   eligible immediately. Monitor the named `vmcore_executions_retired_count` repair and failures.

Earlier Compose store entries named `S3_ACCESS_KEY` and `S3_SECRET_KEY` were ignored by the pinned
backend. Such deployments may have allowed anonymous writes. This transition does not supply an
anonymous-deployment exception: absent/unknown effective IAM inventory remains HOLD until an
operator establishes a supported, separately reviewed transition. Merely enabling authentication
now does not establish termination of earlier requests.

For a verified legacy transition, retain a private JSON receipt with `mode: "legacy_verified"`,
`backend: "bundled-single-process"`, the exact `backend_commit` above, and these Boolean
attestations: `all_old_producers_stopped`, `all_old_store_requests_terminated`,
`old_signers_permanently_revoked`, `identity_inventory_verified`, `anonymous_write_denied`,
`new_signer_verified`. Set each to true only from the corresponding retained observation. These
values are operator testimony, not automatic proof generated by the import function.

## Offline baseline import

Use the upgraded candidate's Python environment and migration-owner database credentials, with
app roles stopped. The store environment must select the verified bucket and new credentials.
The import function is an operator-only Python entry point; it is not an MCP tool or runtime
maintenance endpoint. The baseline table is writable only by its owner.

After the separately approved maintenance prerequisites, this invocation reads a private receipt:

```bash
export KDIVE_VMCORE_RECEIPT=/secure/operator/vmcore-receipt.json
uv run python - <<'PY'
import asyncio
import json
import os
from pathlib import Path

import psycopg
import kdive.config as config
from kdive.config.core_settings import DATABASE_URL
from kdive.db.vmcore_retention_migration import import_vmcore_baseline
from kdive.store.objectstore import object_store_from_env

async def main():
    receipt = json.loads(Path(os.environ["KDIVE_VMCORE_RECEIPT"]).read_text())
    async with await psycopg.AsyncConnection.connect(config.require(DATABASE_URL)) as conn:
        imported = await import_vmcore_baseline(conn, object_store_from_env(), receipt)
    print(f"imported {imported} vmcore pairs")

asyncio.run(main())
PY
```

`KDIVE_VMCORE_RECEIPT` here belongs only to the operator script, not the runtime settings registry.
Protect the receipt and its source evidence as private files. A successful retry with the same
baseline mode returns zero; a different mode or concurrent baseline completion is refused. Keep
the original receipt: a zero return does not re-prove the old maintenance observations.

## Failure and rollback

If baseline validation fails, keep the policy unset and preserve all objects. Investigate the
specific ownership, configuration or identity failure; never mark the baseline manually to bypass
it. If deletion fails, keep the retirement rows and fences intact and repair the store before
retry. Other reconciler repairs continue and the failure remains visible in the repair report.

Disabling the policy stops further retention drains but cannot restore deleted bytes or clear a
Run's retirement marker. Roll back only to a version that honors the journal, conditional writes,
retirement marker and read protections. Keep revoked credentials revoked and old producers stopped.
Restore evidence only through an independently approved recovery procedure; never remove a retained
fence to make an old URL writable again.
