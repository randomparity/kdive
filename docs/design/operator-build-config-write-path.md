# Operator write-path for build-config fragments

- **Issue:** [#438](https://github.com/randomparity/kdive/issues/438)
- **ADR:** [`../adr/0119-operator-build-config-write-path.md`](../adr/0119-operator-build-config-write-path.md)
- **Status:** Draft

## Problem

Build-config fragments (today only the `kdump` kernel-config/cmdline policy) are baked into
the container image (`src/kdive/build_configs/data/kdump.config`) and reach the
`build_config_catalog` table **only** through the deploy-time seed (`seed_build_configs`,
called by `migrate()` via `_seed_build_configs_step`). The operator MCP surface is
read-only: `buildconfig.get` serves the bytes; there is no write tool. An operator who needs
a different kdump policy — or any new fragment — must rebuild and republish the image. The
policy is not under operator control (#438; #428 dev/operator-separation audit).

## Acceptance criteria (from #438)

1. An admin can publish/replace a build-config fragment through the MCP surface, without
   rebuilding the image.
2. The write is audited and admin-gated.
3. The packaged default remains the seed for a fresh install; an operator override is **not**
   clobbered by a later `migrate`.

## What already exists (verified in source)

| Building block | Where |
|---|---|
| `build_config_catalog` table (`name` PK, `object_key`, `sha256`, `description`) | `src/kdive/db/schema/0025_build_config_catalog.sql` |
| Seed publish + sha256-gated idempotent upsert | `src/kdive/build_configs/seed.py` (`seed_build_configs`) |
| Catalog repo (async + sync read, sha256 verify) | `src/kdive/build_configs/catalog.py` |
| Read tool `buildconfig.get` (auth-only, no project RBAC) | `src/kdive/mcp/tools/catalog/build_configs.py` |
| Reserved object key `system/build-configs/<name>/<name>.config` | `seed.py` via `put_artifact` |
| `platform_admin` gate + `platform_audit_log` write (break-glass precedent) | `src/kdive/mcp/tools/ops/breakglass.py`, `src/kdive/security/audit.py` (`record_platform`) |
| Shared platform-auth helpers (`actor_for`, `held_platform_roles`, `audit_platform_denial`) | `src/kdive/mcp/tools/_platform_auth.py` |
| Object-key component validation (`/`, control chars) | `src/kdive/artifacts/storage.py` (`validate_key_component`) |

So this is **one tool + one provenance column**, not a new subsystem.

## Design

### Tool: `buildconfig.set`

- **Gate:** `require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)`. The catalog is
  system-scoped (no project column; `buildconfig.get` skips project RBAC), so a project role
  has no project to gate on. On denial — *only* when the caller holds some platform role —
  `audit_platform_denial` writes a `platform_audit_log` row and the tool returns
  `AUTHORIZATION_DENIED`. A project-only token gets the same denial envelope with no audit row
  (the routine non-grant case).
- **Audit on success:** `audit.record_platform` writes one `platform_audit_log` row
  (`tool="buildconfig.set"`, `scope=name`, `args={name, sha256, bytes}`,
  `platform_role=held_platform_roles(ctx)`, `actor=actor_for(ctx)`). Only the one-way
  `args_digest` is stored — never the fragment content.
- **Inputs:** `name: str`, `content: str`, `description: str = ""`.
- **Validation (write boundary, before any object write):**
  - `name` matches `^[a-z0-9][a-z0-9_-]{0,63}$` → else `CONFIGURATION_ERROR`
    (`details={"field": "name"}`). Strictly stronger than `validate_key_component`, which the
    deterministic key build still applies as defence in depth.
  - `content` non-empty and ≤ `KDIVE_MAX_BUILD_CONFIG_BYTES` (UTF-8 byte length; default
    256 KiB) → else `CONFIGURATION_ERROR` (`details={"field": "content", ...}`).
  - `description` ≤ 1 KiB → else `CONFIGURATION_ERROR`.
- **Write (after validation), serialized per fragment name:** the whole write runs inside one
  `conn.transaction()` holding `advisory_xact_lock(conn, LockScope.BUILD_CONFIG, name)`, so two
  concurrent `buildconfig.set` calls for the same `name` cannot interleave the object PUT and
  the row write (which would leave the committed row's sha256 describing one writer's bytes and
  the object holding another's). `advisory_xact_lock` already accepts a `str` key (the
  `PROJECT` scope is string-keyed); a new `LockScope.BUILD_CONFIG` is keyed by the fragment
  name. `set` holds this one lock only, so the cross-scope total order is unaffected.
  1. `data = content.encode("utf-8")`; `sha256 = hashlib.sha256(data).hexdigest()`.
  2. `await asyncio.to_thread(store.put_artifact, ArtifactWriteRequest(tenant="system",
     owner_kind="build-configs", owner_id=name, name=f"{name}.config", data=data,
     sensitivity=Sensitivity.REDACTED, retention_class="build-config"))` — the same reserved
     key the seed uses; a re-set overwrites in place.
  3. Upsert the catalog row with the just-computed `sha256` and `source='operator'`, and write
     the `platform_audit_log` row — both on the same connection in the same transaction, so the
     row and its audit entry commit together.
- **Non-atomicity contract (object store is not transactional with Postgres):** the object PUT
  (step 2) lands before the transaction (step 3) commits. The per-name lock removes the
  *concurrent*-writer corruption, but a process/DB failure between a successful PUT and the row
  commit still leaves the object holding the new bytes while the unchanged row holds the old
  sha256. This is **fail-closed**: `buildconfig.get` and the build-path fetch both call
  `verify_bytes`, so they raise `INFRASTRUCTURE_FAILURE` on the mismatch rather than serving
  mismatched bytes. The remedy is a re-`set` (it re-PUTs and re-commits the matching sha); the
  prior bytes are not recoverable from the row. A `set` that returns an error therefore means
  "state unknown — re-`set` to converge," not "nothing changed."
- **Response:** `ToolResponse.success(name, "published",
  data={"name": name, "sha256": sha256, "bytes": len(data), "source": "operator"},
  suggested_next_actions=["buildconfig.get"])`. Content is not echoed.
- **Annotations:** `_docmeta.mutating()`, `meta={"maturity": "implemented"}`. Not destructive.

### Migration `0034`: `source` provenance column

```sql
ALTER TABLE build_config_catalog
    ADD COLUMN source text NOT NULL DEFAULT 'seed'
        CHECK (source IN ('seed', 'operator'));
```

Existing rows backfill to `source='seed'` (the seed owns everything published before this
change), so the migration is safe to apply over a populated table.

### Seed becomes source-aware

`seed_build_configs` today republishes whenever the stored sha256 differs from the packaged
bytes. New rule:

- Read the row's `(sha256, source)` for `name`. If the row exists, `source='seed'`, and its
  sha256 == packaged sha256 → skip (return 0; the unchanged fast-path, no object write).
- If the row exists and `source='operator'` → skip (return 0); the operator owns it.
- Otherwise publish the packaged bytes and run the **source-guarded** seed upsert.

The no-clobber invariant is **DB-enforced, not check-then-act**: the seed's upsert is

```sql
INSERT INTO build_config_catalog (name, object_key, sha256, description, source)
VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'seed')
ON CONFLICT (name) DO UPDATE SET
    object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256,
    description = EXCLUDED.description, source = 'seed', updated_at = now()
WHERE build_config_catalog.source = 'seed';
```

The `WHERE build_config_catalog.source = 'seed'` on the conflict path means the database
itself refuses to overwrite an `operator` row, closing the TOCTOU window where a live
`buildconfig.set` lands between the seed's pre-read and its upsert (e.g. a rolling redeploy
running `migrate` while the prior server still serves `set`). The pre-read is an optimization
(skip the object PUT when nothing changed), not the safety boundary.

So a fresh install seeds the default, a shipped packaged-fragment change still flows to
seed-owned rows on the next `migrate`, and an operator override is never clobbered.

### Repository changes (`catalog.py`)

- `BuildConfigEntry` gains a `source: str` field (read into both async/sync getters; the
  `_SELECT` adds `source`). The build-path fetch and `buildconfig.get` ignore it — they only
  need bytes + sha256 — but it is read so the seed can branch on it.
- **Two distinct writers, not one shared unconditional upsert:**
  - `upsert_operator_build_config(conn, name, object_key, sha256, description)` — the tool's
    writer: unconditional `ON CONFLICT (name) DO UPDATE` setting `source='operator'`. To avoid
    the empty-description clobber, `description` is written as
    `COALESCE(NULLIF(EXCLUDED.description, ''), build_config_catalog.description, '')`, so an
    operator who re-publishes bytes without a description preserves the prior (e.g. seed)
    description instead of blanking it.
  - `upsert_seed_build_config(conn, name, object_key, sha256, description)` — the seed's
    writer: the source-guarded statement above (`... WHERE source = 'seed'`).

## Failure modes and edges

| Input / condition | Result |
|---|---|
| Caller lacks `platform_admin`, holds a platform role | `AUTHORIZATION_DENIED` + `platform_audit_log` denial row |
| Caller lacks `platform_admin`, project-only token | `AUTHORIZATION_DENIED`, no audit row |
| `name` empty / bad charset / contains `..` | `CONFIGURATION_ERROR` (`field=name`), no object write |
| `content` empty | `CONFIGURATION_ERROR` (`field=content`) |
| `content` over the byte cap | `CONFIGURATION_ERROR` (`field=content`, `limit`, `actual`) |
| Object store unconfigured | `CONFIGURATION_ERROR` from `object_store_from_env` (same as `buildconfig.get`) |
| Process/DB failure after PUT, before row commit | Object holds new bytes, row holds old sha256; `get`/build fetch fail closed (`INFRASTRUCTURE_FAILURE` via `verify_bytes`); remedy is re-`set`. A `set` error means "state unknown — re-`set`" |
| Two concurrent `set` for the same name | Serialized by `advisory_xact_lock(BUILD_CONFIG, name)`; the second blocks until the first commits, so the committed sha256 always matches the bytes at the key |
| Re-set identical bytes | Idempotent: same object key overwritten, sha256 unchanged, `source` stays `operator` |
| `set` an operator override, then `migrate` | Seed reads `source='operator'`, skips; override survives |
| Packaged `kdump.config` changes, row is seed-owned, then `migrate` | Seed re-publishes; row refreshed |

## Test plan (behavior, not implementation)

Unit (driving `set_build_config` directly with an injected pool + object store + a
`RequestContext`, the `buildconfig.get` test convention):

- `platform_admin` set publishes bytes to the reserved key, upserts `source='operator'`,
  returns `published` + correct sha256/bytes; `buildconfig.get` then serves the new bytes.
- A second `set` replaces the bytes (new sha256 reflected by `get`).
- Non-`platform_admin` (platform role held) → `AUTHORIZATION_DENIED`, a `platform_audit_log`
  denial row exists, no object written.
- Project-only token → `AUTHORIZATION_DENIED`, no audit row.
- Bad `name` charset, empty `content`, over-cap `content` → `CONFIGURATION_ERROR`, no object
  written.
- A successful set writes exactly one `platform_audit_log` success row whose `args_digest`
  is not the plaintext content.
- Re-`set` of `kdump` with no `description` preserves the prior description (finding-4 guard).

Seed (driving `seed_build_configs`):

- After an operator `set` of `kdump`, `seed_build_configs` returns 0 and leaves the operator
  bytes in place (the migrate-clobber regression test for AC#3).
- A seed-owned row with drifted packaged bytes is re-published (returns 1).
- Fresh DB: first seed publishes and writes `source='seed'`.

Adversarial (`tests/adversarial/`):

- The DB-enforced guard: directly run the seed's source-guarded upsert against an
  `source='operator'` row and assert the row is unchanged (no Python pre-read in the test path),
  so the no-clobber boundary is proven at the SQL, not at the application read.
- Two concurrent `set` calls for the same name converge to a row whose sha256 matches the
  object bytes at the key (the per-name advisory lock holds).

Wiring/guard tests:

- `tests/db/test_migrate.py` applied-ID list includes `0034`.
- `tests/scripts/test_m2_portability_gate.py` frozenset includes `0034_*.sql`.
- `tests/mcp/core/test_tool_docs.py` tool→test map includes `buildconfig.set`.
- Generated tool-reference doc regenerated to list `buildconfig.set`.

## Out of scope

- A revert-to-default / `buildconfig.delete` tool (an operator re-`set`s packaged bytes to
  undo; no speculative surface for #438).
- Fixture/profile write-path (#439, the sibling read-only-only surface).
- Decoupling `migrate()`'s three fused seedings (#440).
