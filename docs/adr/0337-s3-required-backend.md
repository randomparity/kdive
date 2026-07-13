# ADR 0337 — S3 object storage is a required backend (retire the no-S3 lane)

- **Status:** Accepted
- **Date:** 2026-07-12
- **Deciders:** kdive maintainers
- **Supersedes (in part):** the no-S3 accommodation of [ADR-0228](0228-local-staged-path-catalog-source.md) and [ADR-0336](0336-staged-kernel-config-offer.md)

## Context

kdive treats an S3-compatible object store as *optional*. The `KDIVE_S3_*`
settings carry no `required_when`, so `config.validate()` passes without them
(`config/core_settings.py:120-141`), and a family of code paths branches on "is
the object store configured?" and degrades — the "no-S3 lane".

Two facts make that lane a liability rather than a feature:

1. **Every shipped deployment already runs an object store.** `docker-compose.yml`,
   the Helm chart (`deploy/helm/kdive/templates/demo/minio.yaml`), and
   `scripts/live-stack` all provision MinIO and export `KDIVE_S3_*`. Object
   storage is load-bearing for vmcore retrieval, debuginfo staging, console
   parts, and artifact egress.

2. **The readiness probe already hard-requires it.** Both `build_server_checks`
   and `build_worker_checks` unconditionally add a `minio` check that calls
   `object_store_factory().ping()` (`health/server_checks.py:41-47`,
   `health/worker_checks.py:39-45`). In a no-S3 deployment
   `object_store_from_env()` raises a `configuration_error`, so the check fails
   and the long-running server/worker/reconciler **never report ready**.

The result is a latent contradiction: config validation says S3 is optional, the
health probe says it is mandatory. A no-S3 deployment passes `config.validate()`
then silently never becomes ready — a "looks configured, is not" failure. The
only surfaces that genuinely run today without S3 are the one-shot
`reconcile-systems` CLI and the local-libvirt staged-path provision seam.

The no-S3 lane also imposes a recurring tax: each new agent-facing byte-egress
feature must either build a parallel local-egress mechanism or degrade without
S3. #1132 (the kernel-config offer) is the immediate case — its clean delivery is
a presigned S3 URL, and an inline/DB fallback purely for no-S3 is
over-engineering (ADR-0336 documented the limitation and deferred to this ADR).

## Decision

**Ratify S3 as a required, assumed backend.** A no-S3 deployment is no longer
supported. Config validation and the readiness probe are made to agree, and the
no-S3 accommodation branches are removed so the object store is non-optional end
to end.

1. **Fail fast at config validation.** `KDIVE_S3_ENDPOINT_URL` and
   `KDIVE_S3_BUCKET` become `required_when=_always` for the
   `server`/`worker`/`reconciler` processes. `KDIVE_S3_REGION` keeps its
   `us-east-1` default (never missing). The failure moves from the silent
   readiness hang to `config.validate()`.

2. **Collapse the optional-store assembly.** `store/assembly.py` returns a
   non-optional `ObjectStore`. `optional_object_store`, `s3_env_is_absent`,
   `_required_store_error`, `_S3_OPTIONAL_ENV_NAMES`, and the `RequiredObjectStore`
   alias are removed; the three identical role fields collapse to one non-optional
   store; the image-build store drops its captured-error arm;
   `request_time_store_factory` stays. The duplicate absence policy in
   `processes/reconciler.py` (`optional_reconciler_object_store`) is removed.

3. **Remove the no-S3 degradation branches** (audit class `(a)`): the deferred
   `_unconfigured_image_build_handler`, the `_AbsentImageStore` reconcile
   fallback, the `if store is None` raise/skip/no-op branches in
   `jobs/handlers/systems.py`, `.../control/diagnostic_sysrq.py`,
   `.../runs/boot_evidence.py`, `.../console/console_rotate.py`,
   `reconciler/loop.py` (inventory + GC pass gates),
   `mcp/tools/ops/images/{upload,registrar}.py`, and the `store_unconfigured`
   sentinel in `mcp/tools/catalog/artifacts/reads.py`.

4. **Keep the staged-path store-free resolution** (audit class `(b)`).
   ADR-0228's `staged-path` and remote `staged` volume rootfs still resolve from
   a host-local file with no object store touched — a cost optimization (avoid
   round-tripping a multi-GB rootfs through S3), not a no-S3 mode. Only the
   "no-S3 lane" *wording* in `rootfs_catalog_fetch.py` and `images/rootfs/fetch.py`
   changes to "staged-path avoids the object store".

5. **Keep genuine error handling** (audit class `(c)` and transient-fault
   handling): `services/runs/complete_build.py` chunked-store gating (`None` means
   "no chunks"), the `retrieve.py` lazy-init of a required store,
   `materialize.py`'s unwired-lane guard, and the fail-open transient-store-error
   handling in `kernel_config/fetch.py` and the `artifacts`/`raw_fetch` read tools
   (a live store *outage* still degrades gracefully; only the *unconfigured* case
   is removed).

6. **Documentation.** Operator docs state S3 as required; the Helm chart no longer
   ships an empty `KDIVE_S3_ENDPOINT_URL` default that would break under the new
   requirement; ADRs 0228/0336 are superseded in part (append-only — not edited).

## Consequences

- Config validation and the readiness probe finally agree: a misconfigured
  deployment fails at `config.validate()` with a message naming the missing S3
  settings, instead of passing validation and silently never becoming ready.
- The object store is a non-`None` `ObjectStore` end to end; the type change
  forces the compiler/`ty` to surface any remaining `is None` reader, so a missed
  branch is a type error, not a latent runtime path.
- New byte-egress features may assume an object store exists — no parallel
  local-egress mechanism, no no-S3 degrade. #1132's offer path is unblocked.
- Staged-path provisioning is unaffected: it never touched the store and still
  does not.
- A live store *outage* still degrades read/advisory paths gracefully; only the
  *unconfigured* deployment mode is removed.
- Cost: config change + wiring/branch removal + doc. No migration, no schema
  change, no new tool or response field.

## Considered & rejected

- **Keep the no-S3 lane, fix only the config/probe contradiction** (make the
  probe conditional on S3 being configured). Preserves a mode no shipped
  deployment uses, keeps the recurring byte-egress tax, and leaves the object
  store optional-typed so every new feature must re-handle `None`. Rejected: the
  lane's cost is ongoing and its only live users (reconcile-systems CLI,
  staged-path) do not need it — staged-path is store-free by design, not by mode.
- **Ratify in the ADR now, defer all code removal to follow-ups** (the issue's
  original framing). Leaves a ratified-but-unenforced limbo where the "optional"
  typing and dead branches persist indefinitely, and `git bisect` cannot pin the
  behavior change to one commit. The issue owner elected to do the removal now;
  rejected in favor of a single coherent change.
- **Remove the staged-path store-free branch too** (full uniformity — everything
  through S3). Round-trips a multi-GB host-local rootfs through the object store
  for no benefit, the exact cost ADR-0228 exists to avoid. Rejected: store-free
  staged-path resolution is a cost optimization independent of whether S3 is
  configured.
- **Make S3 required via the readiness probe only** (no config change). Keeps the
  silent-until-not-ready failure shape for the window between process start and
  first probe, and does not help the one-shot CLI. Rejected: `config.validate()`
  is the earliest and clearest gate.
