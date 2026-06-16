# ADR 0120 — Operator override for local-libvirt fixture profiles via file/ConfigMap (#439)

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0065](0065-provider-component-references.md)
  (the provider fixture catalog + `KDIVE_FIXTURE_CATALOG_PATH` override this formalizes),
  [ADR-0112](0112-systems-inventory-config.md) (the `systems.toml` operator-owned-file
  ConfigMap pattern this mirrors, and the move of the rootfs/image half of the fixture
  catalog into the DB), [ADR-0088](0088-deployment-packaging.md) (local-libvirt is
  a venv-on-a-libvirt-host provider, not containerized), [ADR-0019](0019-tool-response-envelope.md)
  (the `ToolResponse` envelope), [ADR-0113](0113-flat-tool-output-schema.md) (the flat
  advertised schema, unaffected).
- **Contrasts with:** [ADR-0119](0119-operator-build-config-write-path.md) — the sibling
  build-config write-path. Both close a read-only-only operator surface (#428 audit), but
  reach opposite mechanisms because the two artifacts are consumed differently (see Context).
- **Issue:** [#439](https://github.com/randomparity/kdive/issues/439).
- **Spec:** [`../design/operator-fixture-profile-write-path.md`](../design/operator-fixture-profile-write-path.md).

## Context

A local-libvirt **fixture profile** is the build-time kernel-config/cmdline **validation
policy** a built kernel is checked against — `requires.config` (CONFIG_* a kernel must set),
`requires.cmdline` (`required_tokens` + `protected_prefixes`), and `requires.rootfs`
(format/root_device/capabilities). It is keyed by `(provider, name, arch)` — e.g.
`console-ready_x86_64` — and is **provider-scoped shared policy**: non-secret, not bound to
any System, no per-System state.

The packaged default profile ships baked into the image as a YAML literal in
`src/kdive/admin/default_fixtures.py` (`LOCAL_LIBVIRT_FIXTURES`) and reaches disk through the
`install-fixtures` CLI (`python -m kdive install-fixtures --dest …`). The operator MCP surface
is read-only — `fixtures.list` reports the rootfs `image_catalog` rows (ADR-0112), and there
is no tool that reads, validates, or authors a profile. An operator who needs a different
profile/cmdline policy must edit packaged bytes or rebuild (#439; the #428 dev/operator
audit classed this a read-only-only gap, sibling to the build-config gap ADR-0119 closed).

**Why this is not the build-config write-path (ADR-0119).** The two artifacts are consumed
by opposite seams:

| | build-config fragment (ADR-0119) | fixture profile (this ADR) |
|---|---|---|
| Stored as | DB `build_config_catalog` row + object-store bytes | YAML files on disk |
| Read at runtime via | `buildconfig.get` (MCP) + object-store fetch in the build | `load_fixture_catalog()` reading disk |
| Pre-existing override seam | none — the only path was a DB seed | `KDIVE_FIXTURE_CATALOG_PATH` (env → loader path) |
| Survives `migrate`/redeploy | needed a `source` provenance column to stop the seed clobbering an operator edit | the image never writes the override path — survives by construction |

A build-config fragment had no file seam, so ADR-0119 had to add an MCP write tool that puts
bytes into the object store and upserts a DB row, plus a provenance column so the deploy seed
would not clobber the operator's edit. A fixture profile is the opposite: it is already read
from a **disk path** the operator can already point elsewhere (`KDIVE_FIXTURE_CATALOG_PATH`,
ADR-0065), exactly the way `systems.toml` is an operator-owned file read via
`KDIVE_SYSTEMS_TOML` (ADR-0112). Inventing a DB/object-store catalog and refactoring
`load_fixture_catalog()` from disk to DB would add a persistence subsystem to solve a clobber
problem the file seam does not have.

**What the seam is missing.** The override path exists in code but is not a usable operator
feature: (1) it is undocumented as *the* override mechanism; (2) it is not wired into the
Helm chart the way `systems.toml` is (no ConfigMap mount); (3) a malformed operator catalog
surfaces only deep inside a build as an opaque `INFRASTRUCTURE_FAILURE` from
`load_fixture_catalog()`, with no operator-facing way to validate a candidate catalog before
relying on it. The containerized image ships no fixture catalog at all (the final Dockerfile
stage copies only `/app/src`; local-libvirt is venv-on-host, ADR-0088), so a containerized
fixture-profile consumer (a remote/external build that references a profile's `requires`)
has no catalog unless one is mounted.

The domain/libvirt **XML** is a separate plane and out of scope: it is generated per System
by `render_domain_xml(system_id, profile, …)` from the System's **provisioning profile**
(`profiles.provisioning.ProvisioningProfile`: `domain_xml_params`, rootfs source,
crashkernel, gdbstub) plus per-System runtime state. It is System-bound and ephemeral, never
read from the fixture catalog, and its operator knobs already arrive via `systems.toml`.

## Decision

Formalize the existing file override as the operator write-path, mirror it into Helm the way
`systems.toml` is, and add one read-only MCP tool to validate a candidate catalog. No DB
table, object-store key, migration, or write-MCP-tool.

### 1. `KDIVE_FIXTURE_CATALOG_PATH` is the operator override (documented, server-readable)

The loader (`load_fixture_catalog()` → `fixture_catalog_path_from_env()`) already honors
`KDIVE_FIXTURE_CATALOG_PATH`, falling back to the packaged source-tree default. An operator
points it at a directory holding their own `manifest.yaml` + profile YAML — authored from
scratch or copied from the packaged default via `python -m kdive install-fixtures --dest
<their-dir>` and edited. The image never writes that directory, so a redeploy does not clobber
it (the no-clobber invariant ADR-0119 needed a `source` column for is structural here).

`FIXTURE_CATALOG_PATH`'s process scope widens from `{worker, reconciler}` to add `server`,
because the new validate tool reads it in the server process; the config reference regenerates
to match (the `config-docs-check`/`config-guard` gates enforce the registry↔doc↔reader
consistency).

### 2. `fixtures.validate` — read-only validation of the resolved catalog

A new MCP tool `fixtures.validate` loads the catalog at the resolved path and reports either
the profiles it exposes or a categorized error:

- **Auth model:** a valid token only — no project RBAC and no platform gate, matching
  `fixtures.list`/`buildconfig.get` (a shared, operator-seeded catalog resource). It reads
  only non-secret policy metadata the operator themselves supplied. The echoed resolved `path`
  is non-secret deployment info already public in the chart's `values.yaml`
  (`fixtures.mountPath`), and is the diagnostic signal the tool exists to give; no file
  contents are echoed.
- **Success:** `ToolResponse.success("fixtures", "valid", data={path, profiles:[{provider,
  name, arch}]})`. `suggested_next_actions=["fixtures.list"]`. (No `rootfs_count` — the
  manifest rootfs list is empty post-ADR-0112, so it would be a permanently-zero field.)
- **Failure:** an absent/unreadable/malformed catalog returns
  `CONFIGURATION_ERROR` (the operator's supplied config is wrong — the operator-facing
  re-categorization of the loader's internal `INFRASTRUCTURE_FAILURE`), carrying the resolved
  `path` and a bounded reason (exception type name + path, never the raw exception text or file
  body — a YAML/validation error can quote the offending document line). This is the fail-fast
  the build path lacks: the operator runs `fixtures.validate` after mounting/overriding and
  learns the catalog is good (and which profiles it advertises) before a build depends on it.
- **Scope:** the verdict attests the **server process's** resolved catalog. The profile
  consumers span processes — the build-host config read runs in the worker — so on the
  venv-on-host deployment (independent per-process env) the operator must set
  `KDIVE_FIXTURE_CATALOG_PATH` identically across server/worker/reconciler; the k8s ConfigMap
  mounts on every pod and gives this for free.
- **No bytes echoed beyond the `(provider, name, arch)` identity triples** — the validate
  result is presence metadata, the same shape `fixtures.list` already returns for rootfs.

### 3. Helm `fixtures` ConfigMap mount (clones the `systems.toml` pattern)

The chart gains a `fixtures` block mirroring `systems`:

```yaml
fixtures:
  configMapName: ""          # operator creates a ConfigMap holding manifest.yaml + profiles/*
  mountPath: /etc/kdive/fixtures
```

When `fixtures.configMapName` is set, the chart mounts it on the server/worker/reconciler
pods (not the migrate job — `migrate()` does not read the fixture catalog, unlike
`systems.toml` which it reconciles) and sets `KDIVE_FIXTURE_CATALOG_PATH=<mountPath>`, as
`kdive.systemsEnv`/`systemsVolume`/`systemsVolumeMount` do for `systems.toml`. Unset is inert
(the container then has no fixture catalog, which is the honest status quo — local-libvirt is
not containerized, ADR-0088). Operators own the ConfigMap through their normal k8s/GitOps
flow.

The ConfigMap catalog must use a **flat layout**: a ConfigMap `data` key cannot contain `/`,
and a plain ConfigMap volume mount writes flat files, but `load_fixture_catalog()` reads each
`manifest.profiles` entry as `path / <entry>`. The packaged default uses a `profiles/`
subdirectory entry, which a flat mount cannot reproduce, so the operator's ConfigMap holds
`manifest.yaml` plus each profile YAML as a top-level key with the manifest listing **bare
filenames**. The chart uses a plain `configMap` volume (the generic chart cannot enumerate an
operator-authored ConfigMap's keys for per-key `items` path mapping). The `profiles/` subdir is
a venv-on-host `install-fixtures` convention only. This is a layout constraint documented for
operators, not a loader change.

## Consequences

- One new read-only tool (`fixtures.validate`), one widened config-process scope
  (`FIXTURE_CATALOG_PATH` += `server`), one new Helm block (`fixtures`), and operator docs.
  No DB table, no migration, no object-store key, no write-MCP-tool, no change to
  `load_fixture_catalog()`'s disk-read contract.
- The two read-only-only gaps the #428 audit named are now resolved **consistently in
  principle** — each is operator-owned and survives redeploy — while using the seam that
  matches how the artifact is consumed: build-config bytes over the object-store/DB path they
  are already fetched through (ADR-0119), fixture profiles over the disk path they are already
  read through (this ADR). "Consistent" is at the operator-experience level (an operator can
  override without rebuilding, and the override is not clobbered), not an identical mechanism.
- An operator override is not authorable from inside an agent turn the way `buildconfig.set`
  is; it is an operator file/ConfigMap edit. This is deliberate: a fixture profile is deploy
  policy, owned like `systems.toml`, not per-turn authored state. `fixtures.validate` gives an
  agent a read/verify surface for it.
- The container image still ships no default fixture catalog; the ConfigMap is how a
  containerized consumer supplies one. Wiring that fix into the image is out of scope (it
  would contradict ADR-0088's "local-libvirt is venv-on-host"); the latent gap is documented,
  not silently papered over.

## Considered & rejected

- **An MCP write tool with a DB/object-store profile catalog (parallel `profile.set`,
  mirroring ADR-0119's mechanism exactly).** Rejected: it requires inventing a profile DB
  table + object-store storage + deploy seed + provenance column, and refactoring
  `load_fixture_catalog()` from a disk read to a DB read — a new persistence subsystem to
  solve a redeploy-clobber problem the file seam does not have (the image never writes the
  override path). The acceptance-criterion "consistent with the build-config write-path" is
  met at the principle level (operator-owned, survives redeploy); each gap rightly uses the
  seam matching its consumption path.
- **A mutating MCP tool that writes the ConfigMap/override file from the server.** Rejected:
  the server runs non-root and, in k8s, has no RBAC to the Kubernetes API; folding k8s-API or
  host-filesystem writes into the MCP server breaks the "state of record is Postgres + object
  store" model and the non-root container contract. Operators own the ConfigMap via their
  normal deploy flow, exactly as they own `systems.toml`.
- **Per-System / domain-XML override.** Out of scope: the domain XML is generated per System
  from the provisioning profile and is not a fixture; its operator knobs already arrive via
  `systems.toml`. A request to make `domain_xml_params` more reachable is a separate concern,
  not this write-path.
- **Baking the fixture catalog into the container image so the default path resolves.**
  Rejected: it contradicts ADR-0088 (local-libvirt is venv-on-host, not containerized) and
  would ship a local-libvirt-only artifact into a remote/fault-inject image. The ConfigMap
  mount is the supported way to put a catalog in a container.
