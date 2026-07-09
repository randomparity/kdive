# Operator override for local-libvirt fixture profiles

> **Superseded (2026-07-09).** The server-build lane that read a profile's `requires` was removed by
> [ADR-0316](../adr/0316-remove-server-build-lane.md); the `requires` data shape itself
> (`ProfileRequirements` / `ConfigRequirements` / `CmdlineRequirements`) was removed by
> [ADR-0319](../adr/0319-remove-dead-profile-requirements-buildhost-vestiges.md) (#1055). A fixture
> profile now carries only `(provider, name, arch)`; the consumer references below no longer exist.
> Retained for historical context only.

- **Issue:** [#439](https://github.com/randomparity/kdive/issues/439)
- **ADR:** [`../adr/0120-operator-fixture-profile-write-path.md`](../adr/0120-operator-fixture-profile-write-path.md)
- **Status:** Draft (superseded — see banner)

## Problem

A local-libvirt **fixture profile** is the build-time kernel-config/cmdline **validation
policy** a built kernel is checked against — keyed `(provider, name, arch)` (e.g.
`console-ready_x86_64`), carrying `requires.config` (CONFIG_* a kernel must set),
`requires.cmdline` (`required_tokens` + `protected_prefixes`), and `requires.rootfs`
(format/root_device/capabilities). It is provider-scoped shared policy: non-secret, not bound
to any System, no per-System state.

The packaged default ships baked into the image as a YAML literal
(`src/kdive/admin/default_fixtures.py`, `LOCAL_LIBVIRT_FIXTURES`) and reaches disk via the
`install-fixtures` CLI. The operator MCP surface is read-only and does not even expose a
profile: `fixtures.list` reports the rootfs `image_catalog` rows (ADR-0112), not the profile
policy. An operator who needs a different profile/cmdline policy must edit packaged bytes or
rebuild (#439; #428 dev/operator audit). This is the sibling of the build-config gap closed by
ADR-0119, but the artifact is consumed by a different seam, so the answer differs (see below).

## Acceptance criteria (from #439)

1. An operator can supply/override the local-libvirt profile policy without rebuilding the
   image.
2. The mechanism is consistent with how the build-config fragment write-path is resolved —
   satisfied at the operator-experience level: operator-owned, survives redeploy, with a
   read/verify surface. The *mechanism* differs by design (file vs DB) because the artifacts
   are consumed differently; ADR-0120 records why.

## What already exists (verified in source)

| Building block | Where |
|---|---|
| Fixture catalog models (`FixtureManifest`, `ProfileCatalogEntry`) | `src/kdive/components/catalog.py` |
| Disk loader + env override `KDIVE_FIXTURE_CATALOG_PATH` → source-tree default | `src/kdive/components/catalog.py` (`load_fixture_catalog`, `fixture_catalog_path_from_env`) |
| Packaged default profile + manifest (the bytes `install-fixtures` writes) | `src/kdive/admin/default_fixtures.py` (`LOCAL_LIBVIRT_FIXTURES`) |
| `install-fixtures` CLI (writes the packaged bundle to `--dest`) | `src/kdive/__main__.py`, `src/kdive/admin/bootstrap.py` (`install_fixtures`) |
| The two profile consumers (`requires` is read here) | `src/kdive/mcp/tools/lifecycle/runs/build.py` (`_external_config_requirements`), `src/kdive/providers/shared/build_host/config.py` |
| Read tool `fixtures.list` (auth-only, no project RBAC) — the registration pattern to mirror | `src/kdive/mcp/tools/catalog/fixtures.py` |
| `FIXTURE_CATALOG_PATH` config setting (process scope `{worker, reconciler}`) | `src/kdive/config/core_settings.py` |
| Helm `systems.toml` ConfigMap mount (`systemsEnv`/`systemsVolume`/`systemsVolumeMount`) — the pattern to clone | `deploy/helm/kdive/templates/_helpers.tpl`, `deploy/helm/kdive/values.yaml` |

So this is **one read-only tool + one config-scope widening + one Helm block + docs**, not a
new persistence subsystem.

## Design

### 1. Formalize `KDIVE_FIXTURE_CATALOG_PATH` as the override (venv-on-host)

No code change to the loader — it already resolves `KDIVE_FIXTURE_CATALOG_PATH` and falls back
to the packaged source-tree default. The work is documentation + making it server-readable:

- Document the operator workflow: point `KDIVE_FIXTURE_CATALOG_PATH` at an operator-owned
  directory holding `manifest.yaml` + the referenced profile YAML; seed it from the packaged
  default with `python -m kdive install-fixtures --dest <dir>` and edit, or author from
  scratch. The image never writes that directory, so a redeploy does not clobber it.
- Add `server` to `FIXTURE_CATALOG_PATH`'s process scope (a **new** frozenset
  `{server, worker, reconciler}` — do not mutate the shared `_DISCOVERY` literal, which other
  settings reference). This is a config-reference/manifest accuracy change, not a functional
  gate: `config.get` does not check process membership at read time (`registry.py`), so the
  server's `fixtures.validate` read works regardless; `Registry.validate(process)` only
  pre-parses settings already in the process set and would silently skip a server-scoped read,
  not reject it. The reason to declare `server` is that the server genuinely reads the setting
  and the generated config reference (gated by `config-docs-check`) should say so. Regenerate
  with `just config-docs`.

### 2. `fixtures.validate` MCP read tool

A new tool that loads the catalog at the resolved path and reports its profiles or a
categorized error. Contract:

- **Name / annotations:** `fixtures.validate`, `read_only()`, `meta={"maturity": "implemented"}`.
- **Auth:** `current_context()` only (valid token), no project RBAC, no platform gate —
  matches `fixtures.list`/`buildconfig.get`. The response echoes the resolved catalog `path`,
  which is non-secret deployment info already public in the chart's `values.yaml`
  (`fixtures.mountPath` defaults to `/etc/kdive/fixtures`) — not host topology a tenant could
  not already read from the chart — and it is the diagnostic signal the tool exists to provide
  (did my override take effect?). No file contents are echoed.
- **Success:** `ToolResponse.success("fixtures", "valid", data={"path": str, "profiles":
  [{"provider","name","arch"}]}, suggested_next_actions=["fixtures.list"])`. Profiles sorted
  `(provider, name, arch)` for deterministic output. The output is the profile identity triples
  only — the same presence shape `fixtures.list` returns for rootfs. (No `rootfs_count`: the
  manifest's rootfs list is empty post-ADR-0112, so it would be a permanently-zero field that
  reads as "broken" to an operator.)
- **Failure:** any loader error (absent dir, unreadable file, malformed/`extra="forbid"`
  validation failure, schema mismatch) → `ToolResponse.failure("fixtures",
  CONFIGURATION_ERROR, suggested_next_actions=["fixtures.validate"], data={"path": str,
  "reason": str})`. The loader's internal `INFRASTRUCTURE_FAILURE` is re-categorized to
  `CONFIGURATION_ERROR` at this boundary because the operator's supplied config is the wrong
  thing, not the infrastructure.
- **Bounded reason, no raw content:** the `reason` is a fixed safe form — the exception type
  name plus the resolved `path` — never the raw exception text or file body (a YAML/validation
  error can quote the offending document line). This bounds disclosure at construction; it does
  not rely on the worker redaction registry, which a plain server read does not populate
  (secrets resolve at the worker boundary). Only `(provider, name, arch)` identity triples and
  this bounded reason are echoed.
- **Registration:** new `register` in `src/kdive/mcp/tools/catalog/fixtures.py` (same module as
  `fixtures.list`), already wired through `_PLANE_REGISTRARS` via the catalog registrar.

This is the fail-fast the build path lacks: today a malformed override surfaces only deep in a
build as an opaque `INFRASTRUCTURE_FAILURE`. An operator now runs `fixtures.validate` after
overriding/mounting and learns the catalog is good — and which profiles it advertises — before
a build depends on it. **Scope of the guarantee:** `fixtures.validate` attests the **server
process's** resolved catalog. The profile consumers span processes —
`runs/build.py:_external_config_requirements` reads it in the server, but
`providers/shared/build_host/config.py:load_profile_config_requirements` reads it in the
**worker** (build jobs). In k8s the same ConfigMap mounts on every pod (§3), so the views
match; in the venv-on-host deployment each process reads its own `KDIVE_FIXTURE_CATALOG_PATH`,
so the operator must set it identically for server + worker + reconciler — a 'valid' verdict on
the server does not prove the worker sees the same bytes. The docs (§4) state this requirement
explicitly.

### 3. Helm `fixtures` ConfigMap mount (clone `systems.toml`)

Add a `fixtures` block to `values.yaml` mirroring `systems`, and `fixturesEnv` /
`fixturesVolume` / `fixturesVolumeMount` helpers mirroring the `systems*` helpers, wired into
the server/worker/reconciler deployments **only** — not the migrate job. (The systems.toml
ConfigMap is on the migrate job because `migrate()` reconciles `systems.toml`; `migrate()`'s
steps — `apply_migrations` + `_reconcile_inventory_images` + `_seed_build_configs_step` — never
call `load_fixture_catalog`, so a fixtures mount there is dead wiring that would imply migrate
consumes fixtures.) The block:

```yaml
fixtures:
  configMapName: ""          # operator ConfigMap: manifest.yaml + each profile YAML as a
                             # flat top-level key (see the flat-layout constraint below)
  mountPath: /etc/kdive/fixtures
```

When `fixtures.configMapName` is set, the chart mounts it and sets
`KDIVE_FIXTURE_CATALOG_PATH=<mountPath>`. Unset is inert. The container ships no default
fixture catalog (local-libvirt is venv-on-host, ADR-0088); the ConfigMap is how a containerized
fixture-profile consumer (a remote/external build referencing a profile's `requires`) supplies
one. The chart-render test (`tests/helm/`) gains coverage that an unset `fixtures` emits no
volume/env and a set one emits both.

**The ConfigMap catalog must use a flat layout** (a hard constraint, not a convention). A
Kubernetes ConfigMap `data` key cannot contain `/` (keys match `[-._a-zA-Z0-9]+`), and a plain
ConfigMap volume mount writes one flat file per key into `mountPath`. The packaged default
references its profile through a **subdirectory** (`manifest.profiles:
["profiles/console-ready_x86_64.yaml"]`, written under a `profiles/` dir by
`install-fixtures`), and `load_fixture_catalog()` reads it by joining that path onto the
catalog root (`path / manifest_path` → `<mount>/profiles/console-ready_x86_64.yaml`). A flat
ConfigMap mount yields `<mount>/manifest.yaml` and `<mount>/console-ready_x86_64.yaml` with **no
`profiles/` subdir**, so a verbatim copy of the packaged default into a ConfigMap fails the
profile read. The loader joins `manifest.profiles` onto the root, so the fix is layout, not
code: the operator's ConfigMap holds `manifest.yaml` plus each profile YAML as a **top-level
key**, and the manifest lists **bare filenames** (`profiles: ["console-ready_x86_64.yaml"]`).
The chart mounts the ConfigMap with a plain `configMap` volume (no per-key `items` path
mapping, which the generic chart cannot enumerate for an operator-authored ConfigMap). The
operating docs (§4) show a flat-layout ConfigMap example and call out that the `profiles/`
subdir is a venv-on-host (`install-fixtures`) convention only. (The venv-on-host override keeps
the subdir layout, because `install-fixtures` creates the directory; only the ConfigMap path
requires flattening.)

### 4. Documentation

- The operator override workflow lands in the operating docs alongside the `systems.toml`
  instructions, covering: (a) venv-on-host — `install-fixtures --dest <dir>` then
  `KDIVE_FIXTURE_CATALOG_PATH=<dir>`, set identically for **every** process that loads the
  catalog (server + worker + reconciler), since each reads its own env; (b) k8s — a
  **flat-layout** ConfigMap example (manifest.yaml + each profile YAML as a top-level key,
  manifest referencing bare filenames) and the `fixtures.configMapName` value; (c)
  `fixtures.validate` as the post-override check, with its server-scope caveat stated.
- `KDIVE_FIXTURE_CATALOG_PATH`'s config-reference row updates to reflect the added `server`
  process and its role as the operator override (regenerated, not hand-edited).

## Failure modes & edge cases

- **No catalog at the resolved path** (the container default, or a typo'd
  `KDIVE_FIXTURE_CATALOG_PATH`): `fixtures.validate` → `CONFIGURATION_ERROR` naming the resolved
  `path`. This is informative, not a crash — it tells the operator they have not supplied a
  catalog.
- **Malformed `manifest.yaml` or a profile referenced by the manifest but missing/invalid:**
  `load_fixture_catalog` raises; `fixtures.validate` reports `CONFIGURATION_ERROR` with the
  redacted reason. The build path's behavior is unchanged (still fails), but the operator can
  now catch it pre-build.
- **Empty profile list** (a valid manifest with `profiles: []`): `valid`, `profiles: []` — a
  legitimate, if unusual, catalog. Not an error.
- **Redeploy with an operator override in place:** the override path/ConfigMap is operator-owned
  and never written by the image or `migrate`, so it persists across redeploys with no
  provenance guard (contrast ADR-0119's `source` column, which was needed only because the
  build-config seed *did* write the same key).
- **Concurrency:** the catalog is read-only at runtime; `fixtures.validate` and the build-path
  reads are independent disk reads with no write, so no locking is involved.

## Out of scope

- The libvirt **domain XML** and the **provisioning profile**
  (`profiles.provisioning.ProvisioningProfile`: `domain_xml_params`, rootfs source,
  crashkernel, gdbstub). The XML is generated per System and is not a fixture; its operator
  knobs arrive via `systems.toml`. Making `domain_xml_params` more reachable is a separate
  concern.
- Baking a default fixture catalog into the container image (contradicts ADR-0088).
- A mutating MCP `profile.set` writing a DB/object-store catalog (ADR-0120 "Considered &
  rejected").
