# ADR 0119 — Operator write-path for build-config fragments: `buildconfig.set` (#438)

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0096](0096-kdump-config-fragment-build-input.md)
  (the build-config catalog, reserved object key, and the read tool this extends),
  [ADR-0043](0043-platform-scoped-rbac-tier.md) (platform roles +
  `platform_audit_log`), [ADR-0062](0062-platform-operations.md) (the break-glass
  `platform_admin` + always-on platform-audit precedent this mirrors),
  [ADR-0019](0019-tool-response-envelope.md) (the `ToolResponse` envelope),
  [ADR-0113](0113-flat-tool-output-schema.md) (the flat advertised schema, unaffected).
- **Issue:** [#438](https://github.com/randomparity/kdive/issues/438).
- **Spec:** [`../design/operator-build-config-write-path.md`](../design/operator-build-config-write-path.md).

## Context

Build-config fragments (today only the `kdump` kernel-config/cmdline policy) ship baked
into the container image at `src/kdive/build_configs/data/kdump.config` and reach the
`build_config_catalog` only through the deploy-time seed (`seed_build_configs`, invoked
from `migrate()`). The MCP surface is read-only: `buildconfig.get` serves the bytes; there
is no `set`/`publish`/`replace`. An operator who needs a different kdump policy — or any new
fragment — must rebuild and republish the image (#438; the dev/operator-separation audit
#428 classed this a 🔴 gap: operational state with no operator write-path).

The catalog is **system-scoped**, not project-scoped: `buildconfig.get` deliberately skips
project RBAC ("a shared, operator-seeded catalog resource"), the reserved object key is
`system/build-configs/<name>/<name>.config` under tenant `system`, and there is no project
column on `build_config_catalog`. Any write-path must answer two questions the read-path
did not: **who may write**, and **how a write survives the next `migrate`**.

The seed's idempotency today is keyed on sha256 equality: it republishes whenever the
stored row's sha256 differs from the packaged bytes. That makes "an operator edited the
fragment" indistinguishable from "the packaged bytes drifted" — both look like a mismatch
and both trigger an overwrite. An operator override would therefore be clobbered by the
next `migrate`, violating the issue's third acceptance criterion.

## Decision

Add one admin MCP tool, `buildconfig.set`, and a provenance column that protects an
operator override from the seed.

### 1. `buildconfig.set` — `platform_admin`-gated, audited write

- **Gate:** `require_platform_role(ctx, PLATFORM_ADMIN)`. The catalog is system-scoped, so
  a *project* role (the route `accounting.set_budget` takes) has no project to gate on. A
  `platform_admin` write to a shared catalog mirrors the break-glass `ops.force_*` tools
  (ADR-0062): platform authority + an always-written `platform_audit_log` row.
- **Audit:** on success and on a platform-role-overreach denial, write a `platform_audit_log`
  row via `audit.record_platform` / the shared `audit_platform_denial` helper. There is no
  project-membership guard on that writer, which is exactly right for a project-less resource.
  The audited `args` carry `name`, `sha256`, and byte length — never the fragment content
  (the row stores only the one-way `args_digest`).
- **Inputs:** `name` (catalog key), `content` (fragment text), optional `description`.
- **Validation at the write boundary:**
  - `name` must match `^[a-z0-9][a-z0-9_-]{0,63}$`. The name folds into the object key, so a
    strict charset is enforced before it reaches `validate_key_component` (which blocks only
    `/` and control chars, not `..`/whitespace/case). Reject → `CONFIGURATION_ERROR`.
  - `content` is decoded/encoded as UTF-8 and capped at `KDIVE_MAX_BUILD_CONFIG_BYTES`
    (default 256 KiB; kernel-config fragments are a few KiB). Empty or over-cap →
    `CONFIGURATION_ERROR`.
- **Write:** `put_artifact` to the same reserved key the seed uses
  (`tenant=system`, `owner_kind=build-configs`, `owner_id=<name>`, `name=<name>.config`,
  `Sensitivity.REDACTED`, `retention_class=build-config`). The key is deterministic in
  `name`, so a re-set overwrites in place — no orphaned object. Then upsert the catalog row
  with `source='operator'`.
- **Response:** `ToolResponse.success(name, "published", data={name, sha256, bytes, source})`.
  `suggested_next_actions=["buildconfig.get"]`. Content is not echoed (the caller supplied it;
  `buildconfig.get` serves it back).

### 2. `source` provenance column protects an operator override

Migration `0034` adds to `build_config_catalog`:

```sql
source text NOT NULL DEFAULT 'seed' CHECK (source IN ('seed', 'operator'))
```

- `seed_build_configs` upserts a row **only** when it is absent or `source='seed'`. A row
  with `source='operator'` is left untouched: the seed reads the row's `source` (not just its
  sha256) and skips it. A fresh install still seeds the packaged default; a later `migrate`
  still propagates a *packaged* fragment change to seed-owned rows; an operator override is
  never clobbered.
- `buildconfig.set` writes `source='operator'`. Once an operator has overridden a fragment,
  it stays operator-owned until they overwrite it again (also `operator`).
- The existing `ON CONFLICT (name) DO UPDATE` upsert is split by writer: the seed's upsert
  carries `source='seed'` but is gated by a pre-read so it never overwrites an operator row;
  the tool's upsert carries `source='operator'` unconditionally.

The packaged default therefore remains the seed for a fresh install, and an operator
override is not clobbered by a later `migrate` (AC#3).

## Consequences

- One new tool (`buildconfig.set`), one additive migration (`0034`, nullable-safe via the
  `DEFAULT 'seed'` so existing rows backfill to seed-owned), one new env knob
  (`KDIVE_MAX_BUILD_CONFIG_BYTES`). No change to `buildconfig.get`, the build-path fetch, the
  reserved-key scheme, or the `ToolResponse`/flat-schema contracts.
- The seed gains a per-row `source` read before upsert; its sha256 fast-path is unchanged for
  seed-owned rows. The `build_config_catalog` migration + tool file are provider-agnostic core,
  so the M2 portability gate `ALLOWED_FILES` and its meta-test frozenset gain the new migration.
- `buildconfig.set` joins the reviewed mutating-admin set. It is `mutating`, not `destructive`
  (it publishes/replaces a config fragment; it tears nothing down), so it is *not* added to
  `DESTRUCTIVE_TOOLS`.
- An operator who wants to revert an override to the packaged default has no one-call path in
  this change; they re-`set` the packaged bytes (which `buildconfig.get` on a fresh deploy can
  supply) or, as a follow-up, a `buildconfig.delete`/`reset-to-seed` tool could clear the
  override. Out of scope for #438 (no speculative surface).

## Considered & rejected

- **Project-`admin` gate (literal reading of the issue's "like `accounting.set_budget`").**
  Rejected: `set_budget` gates on a *project* role and audits under a project, but
  `build_config_catalog` has no project. Gating on a project would force the operator to name
  an arbitrary project that has no bearing on the shared resource, and `audit.record`'s
  membership guard would reject the write outright. The system-scoped `platform_admin` +
  `platform_audit_log` path is the correct analogue and is already the precedent for
  cross-project/system mutation (ADR-0062).
- **No provenance column; seed only writes when the row is absent.** Rejected: it satisfies
  "don't clobber an operator override" but also stops a *packaged-fragment* update from ever
  reaching an existing seed-owned row, so a shipped fix to `kdump.config` would never apply on
  upgrade. `source` distinguishes "operator owns this" from "seed owns this and may be
  refreshed."
- **A boolean `operator_managed` flag instead of a `source` enum.** Rejected: an enum leaves
  room for a future provenance (e.g. `import`) without a second migration and reads
  self-documentingly in the row; the `CHECK` keeps it closed today.
- **Echo the stored content in the `set` response.** Rejected: the caller supplied it and
  `buildconfig.get` already serves it; echoing only inflates the envelope. `sha256` + byte
  length confirm what landed.
- **A separate `build_config_overrides` table layered over the seed table.** Rejected: two
  tables for one logical catalog complicate the read-path (`get`, the build fetch) for no
  gain; one table with a `source` column keeps a single lookup and a single reserved key.
