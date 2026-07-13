# S3 object storage as a required backend (retire the no-S3 lane)

- **Issue:** #1133
- **ADR:** [0337](../adr/0337-s3-required-backend.md)
- **Status:** Draft
- **Date:** 2026-07-12

## Problem

kdive treats an S3-compatible object store as *optional*: the `KDIVE_S3_*`
settings carry no `required_when`, so `config.validate()` passes without them
(`config/core_settings.py:120-141`). A whole family of code paths branches on
"is the object store configured?" and degrades, skips, or fails when it is not —
the "no-S3 lane". Yet every shipped deployment already provisions one
(`docker-compose.yml`, `deploy/helm/kdive/`, `scripts/live-stack`), object
storage is load-bearing for vmcore retrieval, debuginfo staging, console parts,
and artifact egress, and the readiness probe (`health/server_checks.py:41-47`,
`health/worker_checks.py:39-45`) *already* hard-requires it: both server and
worker unconditionally add a `minio` check that calls `object_store_factory().
ping()`. In a no-S3 deployment `object_store_from_env()` raises a
`configuration_error`, so those checks fail and the long-running processes never
report ready.

The result is a latent contradiction: config validation says S3 is optional, the
health probe says it is mandatory. A no-S3 deployment passes `config.validate()`
then silently never becomes ready — the worst failure shape (looks configured,
is not). The only surfaces that genuinely run store-free today are the
in-process reconcile pass (the `reconcile-systems` MCP tool substitutes an
`_AbsentImageStore` when `image_store is None`) and the local-libvirt
staged-path provision seam. The `reconcile-systems` **CLI** already
hard-requires S3 — it exits non-zero with a friendly message when the store is
absent, so it does not run without one.

Every new agent-facing byte-egress feature pays a recurring tax: it must either
build a parallel local-egress mechanism or degrade in no-S3 mode. #1132 (the
kernel-config offer) is the immediate example — its clean delivery is a presigned
S3 URL, and an inline/DB fallback purely for no-S3 is over-engineering.

## Goal

Ratify S3 as a required, assumed backend; make config validation and the health
probe agree; and remove the no-S3 accommodation branches so new features can
assume an object store exists. This PR both **ratifies** (ADR + operator docs)
and **implements** (config change + branch removal), per the issue owner's
decision to do the removal now rather than as a separate follow-up.

## Non-goals

- **The staged-path store-free resolution is kept.** ADR-0228's `staged-path`
  (and remote `staged` volume) rootfs resolve from a host-local file with no
  object store touched. That is a *cost optimization* — do not round-trip a
  multi-GB rootfs through S3 — not a no-S3 deployment mode. It survives; only its
  "no-S3 lane" *wording* changes to "staged-path avoids the object store". The
  factory is only ever invoked on the `s3` branch, which with S3 mandated always
  resolves.
- **No new object-store feature.** No presigned-URL, retention, or egress
  behavior changes.
- **No new migration.** This is config + wiring + doc; no schema change.

## Approach

### 1. Make S3 required at config validation (fail fast on absent *and* empty)

`KDIVE_S3_ENDPOINT_URL` and `KDIVE_S3_BUCKET` gain `required_when=_always`
**and** a non-empty `parse` (`config/core_settings.py`). `required_when=_always`
alone is insufficient: the registry's presence check is
`s.name in env or s.default is not None` (`config/registry.py:164`), so a
present-but-empty `KDIVE_S3_ENDPOINT_URL=""` counts as "present" and passes
validation, yet `object_store_from_env` rejects it (`if not endpoint_url: raise`).
The Helm configmap renders `KDIVE_S3_ENDPOINT_URL` unconditionally
(`configmap.yaml:36`), empty on the external-backend path with no override — the
exact present-but-empty vector. A non-empty parse (strip
whitespace, then raise `ValueError` on a blank result) makes `config.validate()`
reject empty **and whitespace-only** values via its malformed-value path, so
absent, empty, and blank all fail fast at the earliest point rather than at the
silent readiness-probe hang. `KDIVE_S3_REGION` keeps its `us-east-1` default and `_str`
parse (it is never "missing" and `object_store_from_env` falls back on blank).

### 2. Collapse the optional-store assembly

`store/assembly.py` currently returns `ObjectStore | None` and encodes the
absence-vs-partial policy (`optional_object_store`, `s3_env_is_absent`,
`_required_store_error`, `RequiredObjectStore`). With S3 required these become
dead:

- `build_object_store_assembly` constructs the store once (raising via
  `object_store_from_env` if — impossibly, post-validation — unconfigured).
- The `ObjectStoreAssembly` fields become non-`None` `ObjectStore`. The three
  role fields (`optional_upload_store`, `optional_image_store`,
  `optional_ops_image_store`) always referenced the same object; they collapse to
  a single non-optional store field. `required_image_build_store` drops its
  `CategorizedError` arm. `request_time_store_factory` stays (request-time lazy
  construction is unaffected).
- `optional_object_store`, `s3_env_is_absent`, `_required_store_error`,
  `_S3_OPTIONAL_ENV_NAMES`, and the `RequiredObjectStore` alias are removed.
- The duplicate absence policy in `processes/reconciler.py`
  (`optional_reconciler_object_store`) is removed; the reconciler requires the
  store. This function is also imported and called by the `reconcile-systems`
  CLI (`__main__.py:29,225`), so `__main__.py` must change too (see step 5).

### 3. Remove the `if store is None` degradation branches

Every branch classified `(a)` in the #1133 audit (a pure no-S3 accommodation)
is removed so the store type is non-optional end to end:

| site | today's no-S3 behavior | after |
|------|------------------------|-------|
| `jobs/assembly.py` `_unconfigured_image_build_handler` | deferred config-error stub | image-build handler always registered with a real store |
| `jobs/handlers/systems.py` `_commit_uploaded_rootfs` | raise config_error | store always present |
| `jobs/handlers/systems.py` teardown reclaim | skip reclaim | always reclaim |
| `jobs/handlers/control/diagnostic_sysrq.py` | raise config_error | store always present |
| `jobs/handlers/runs/boot_evidence.py` | skip capture (return None) | always capture |
| `jobs/handlers/console/console_rotate.py` | no-op + warn | store always present |
| `reconciler/loop.py` inventory/gc pass gates | pass not scheduled | passes always scheduled |
| `mcp/tools/ops/images/upload.py` | `_config_error` | store always present |
| `mcp/tools/ops/images/registrar.py` prune | `_config_error` | store always present |
| `mcp/tools/ops/reconcile/reconcile_systems.py` `_AbsentImageStore` | no-op fallback | store always present |
| `mcp/tools/catalog/artifacts/reads.py` `store_unconfigured` degrade | `content_unavailable` | removed (see below) |

### 4. Keep genuine error handling; distinguish it from no-S3 tolerance

Some sites wrap store access in `try/except CategorizedError` to survive
*transient* store faults, not to support no-S3. These are **kept** but tightened
so they no longer encode "unconfigured":

- `kernel_config/fetch.py` `load_effective_config` — fail-open advisory read;
  kept (defends the vmcore/install-arming gate against transient store errors).
- `mcp/tools/catalog/artifacts/reads.py` / `kernel_config.py` / `raw_fetch.py` —
  keep infra-error handling for a live store outage; drop the `store_unconfigured`
  sentinel that only encoded the no-S3 case.

Sites classified `(c)` (not no-S3 accommodations at all) are **untouched**:
`services/runs/complete_build.py` chunked-store gating (`None` means "no chunks",
not "no S3"), the `retrieve.py` lazy-init of a *required* store, and
`providers/local_libvirt/lifecycle/rootfs/materialize.py`'s unwired-lane guard.

### 5. `reconcile-systems` CLI keeps a clean error

The one-shot CLI (`__main__.py` `_handle_reconcile_systems`) currently calls the
removed `optional_reconciler_object_store` and exits cleanly when the store is
`None`. Rewrite it to call `object_store_from_env()` directly and catch
`CategorizedError`, printing the actionable message and exiting non-zero, and
drop the `optional_reconciler_object_store` import (`__main__.py:29`). This is
error handling, not no-S3 tolerance — with S3 required, `config.validate()` /
`object_store_from_env` are the fail-fast gate.

### 6. Docs

- Add ADR-0337 and its README index row.
- Operator docs (`docs/operating/*`) already list S3 among required backends;
  make the requirement explicit and remove any "optional" framing.
- **No Helm chart change is needed.** The demo path already derives a working
  endpoint: `kdive.s3Endpoint` (`_helpers.tpl:37-49`) returns
  `http://<fullname>-minio:9000` when `bundledBackends` is set with no override,
  so making S3 required does not break the demo deploy. The empty
  `KDIVE_S3_ENDPOINT_URL` default (`values.yaml:32`) is the external-backend
  path's intentional operator-supplied value; aliasing it to the demo service
  would inject a nonexistent in-cluster reference into production installs. With
  the step-1 non-empty rejection, an external install that omits the endpoint
  fails fast at `config.validate()` — the desired behavior.
- Reword the "no-S3 lane" *code comments* in `rootfs_catalog_fetch.py` and
  `images/rootfs/fetch.py` to "staged-path avoids the object store". Accepted
  ADRs (0228, 0336) are append-only and are superseded, not edited, by 0337.

## Success criteria (falsifiable)

1. `config.validate()` for `server`/`worker`/`reconciler` fails with a
   `configuration_error` naming `KDIVE_S3_ENDPOINT_URL`/`KDIVE_S3_BUCKET` when
   they are **unset**, **present-but-empty** (`=""`), and **whitespace-only**
   (`="  "`) — three new test cases.
2. `rg -n 'optional_object_store|s3_env_is_absent|_AbsentImageStore|optional_reconciler_object_store|_unconfigured_image_build_handler|store_unconfigured|RequiredObjectStore|_S3_OPTIONAL_ENV_NAMES|_required_store_error|optional_upload_store|optional_image_store|optional_ops_image_store' src/ tests/`
   returns nothing — the full set of deleted symbols (not only the five headline
   ones), scoped to `tests/` too where they are referenced by unit tests that
   must be updated. This grep is the removal backstop; criterion 3 (type check)
   only forces removal of `ObjectStore | None` declarations and cannot catch a
   lingering sentinel, alias, or helper.
3. The `ObjectStoreAssembly` store field(s) and every handler/reconciler store
   parameter are typed `ObjectStore`, not `ObjectStore | None`; `ty check` passes.
   (Caveat: `ty` surfaces removed *fields/attributes*
   (`unresolved-attribute`/`unknown-argument`) but does **not** flag a dead
   `if store is None` guard left on a narrowed param — verified against ty 0.0.53.
   Deleting each branch is manual discipline; the plan's Task 11 residual-`is None`
   grep is the real backstop for leftover dead guards, alongside the removed-symbol
   grep in criterion 2.)
4. Staged-path rootfs resolution still succeeds with the store never touched
   (existing `test_sync_fetch_staged_path_returns_validated_path_without_store`
   still passes, its "no-S3" naming reworded).
5. A live store *outage* (not "unconfigured") still degrades `artifacts.get`/
   `find` to a content-unavailable envelope without a false `match_found`
   (retained-behavior test kept).
6. `just ci` is green (lint, type, lint-shell, lint-workflows, check-mermaid,
   test), including the doc-style guard on the new ADR/spec.

## Risks

- **Missed / leftover-dead branch.** A store-`None` branch reachable only in
  `live_vm`/`live_stack` (skipped in CI), or a dead `if store is None` guard left
  behind after narrowing a param, could survive. `ty` does **not** flag such a
  dead guard, and the removed-symbol grep (criterion 2) does not match a generic
  `is None`. Mitigation: the audit enumerated all sites; the plan's Task 11 runs a
  residual-`is None` grep over the touched trees and eyeballs each hit against the
  deliberately-kept class-(b)/(c) list; branch deletion is per-task discipline.
- **Present-but-empty / whitespace-only S3 config.** Covered by the step-1
  strip-then-reject parse; criterion-1 tests exercise the `=""` and `="  "` cases,
  not only the unset case.
- **`reconcile-systems` UX regression.** Losing the friendly "set KDIVE_S3_*"
  message. Mitigation: keep the CLI's `CategorizedError` catch (step 5).
