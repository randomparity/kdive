# Full inventory export — `ops.export_systems_toml` (M2.7 sub-issue C, #640)

- **Epic:** #429 · **Depends on:** #638 (A, merged) · **ADR:** [ADR-0199](../../adr/0199-seed-once-runtime-authoritative-inventory.md) · **Extends:** [ADR-0115](../../adr/0115-declarative-cost-class-coefficients.md) §6 · **Milestone plan:** [runtime-mutable-inventory](../plans/2026-06-20-runtime-mutable-inventory.md)

## Goal

Serialize the **live** DB inventory (`image_catalog` + `resources` + `build_hosts` +
`cost_class_coefficients`) back into a single deterministic `systems.toml` document, exposed as a
read-only `PLATFORM_OPERATOR` MCP tool `ops.export_systems_toml`. This generalizes ADR-0115 §6's
cost-class-only `ops.export_cost_classes` to the whole inventory file, so an operator can capture
runtime state (durable adds/removes/modifies from sub-issue B) back into the version-controlled
file and reproduce the fleet on a fresh start.

## Field-persistence audit (task 1, done first)

For round-trip (`export → parse → reconcile → equal DB state`), each model field is either
**persisted** (a reconcile pass writes it to a column/jsonb, so the export reads it back) or
**file-only** (the provider reads it straight from the file at runtime; no DB column exists).
Verified against `reconcile_resources.py`, `reconcile_build_hosts.py`, `reconcile_images.py`,
`reconcile_coefficients.py`, and the schema (`0023`/`0027`/`0029`/`0030`/`0002`).

### `[[remote_libvirt]]` (`RemoteLibvirtInstance`)

| field | persisted? | read-back |
|---|---|---|
| `name` | yes | `resources.name` |
| `cost_class` | yes | `resources.cost_class` (column) |
| `concurrent_allocation_cap` | yes | `resources.capabilities->>'concurrent_allocation_cap'` |
| `pool` | yes | `resources.pool` |
| `uri` | yes | `resources.host_uri` |
| `vcpus` | yes | `resources.capabilities->>'vcpus'` |
| `memory_mb` | yes | `resources.capabilities->>'memory_mb'` |
| `gdb_addr` | **file-only** | — skeleton placeholder |
| `gdbstub_range` | **file-only** | — skeleton placeholder |
| `client_cert_ref` | **file-only** | — skeleton placeholder |
| `client_key_ref` | **file-only** | — skeleton placeholder |
| `ca_cert_ref` | **file-only** | — skeleton placeholder |
| `base_image` | **file-only** | — skeleton placeholder (validated at parse against `[[image]]`; never stored) |
| `shapes` | **file-only** | — skeleton placeholder (default `[]`) |

The seven file-only fields are emitted as **placeholders**: `uri`/`gdb_addr`/`gdbstub_range`/the
three TLS refs/`base_image` as obvious `REPLACE_ME` sentinels, `shapes` as an empty array. The
provider reads these from the file, so an unedited skeleton **must not** parse (they are required,
non-default model fields except `shapes`); the operator completes them before a fresh start. Note:
`uri` **is** persisted (`host_uri`), so the export emits the live value, not a placeholder — but
the six others plus `base_image` are placeholders.

### `[[build_host]]` (`BuildHostInstance`) — fully round-trips

| field | read-back |
|---|---|
| `name` | `build_hosts.name` |
| `kind` | `build_hosts.kind` |
| `base_image_volume` | `build_hosts.base_image_volume` (NULL for `local`) |
| `workspace_root` | `build_hosts.workspace_root` |
| `max_concurrent` | `build_hosts.max_concurrent` |

Only `managed_by='config'` rows are exported. A config build host can only be `local` or
`ephemeral_libvirt` (the `ssh` kind is not config-expressible — `reconcile_build_hosts` warns and
skips it — so no config-owned `ssh` row exists to export).

### `[[image]]` (`ImageEntry`) — round-trips, with a known source-reconstruction rule

| field | read-back |
|---|---|
| `provider`/`name`/`arch`/`format`/`root_device`/`visibility`/`capabilities` | direct columns |
| `source` | reconstructed from `(object_key, volume, state)` |

Only `managed_by='config'` rows are exported (the reconcile-owned set; matches what the file
declares). `provenance`/`owner`/`expires_at`/`pending_since` are runtime-owned and not in the
model — not exported. Source reconstruction:

- `volume` set, `object_key` NULL → `staged` source (`volume` round-trips).
- `object_key` set → `s3` source (`object_key` + `digest` round-trip).
- `state='defined'`, both NULL → emitted as an `s3` source **skeleton** with a `REPLACE_ME`
  `object_key` placeholder and no digest.

The last case is the lossy one and is documented in the header comment. A `[[image]] source.kind =
"build"` declaration reconciles to a `state='defined'` row that stores **none** of its
`base`/`components` (verified: `reconcile_images._realize_build` writes only `state='defined'`,
all other columns NULL). A `build` source is therefore not faithfully reconstructable from DB
columns, and neither is an unrealized `s3` source. Both collapse to the same `defined` DB row.
**Round-trip equality is defined on DB state, not on the original file** (per the acceptance
criterion): re-parsing the exported `defined`-row skeleton (an `s3` source with a placeholder key)
and reconciling it yields the same `defined` row (the placeholder object HEADs absent → stays
`defined`). A `defined` config row is rare in practice — config images are normally `staged`
(operator volume) or registered `s3` — so the round-trip-faithful path covers the realistic fleet;
the `defined` placeholder keeps the export honest (it does not invent a base) and parseable. This
is called out in the header comment as a placeholder, exactly like the remote skeleton.

### `[[cost_class]]` (`CostClassEntry`) — fully round-trips

Reuses ADR-0115 §6's serializer (name-sorted `[[cost_class]]` blocks, `coeff` as a quoted exact
string). Every row in `cost_class_coefficients` is exported (no `managed_by` partition on that
table; the file is authoritative for declared classes and ops owns the rest — ADR-0115 §2).

## Honoring the override ledger (ADR-0199)

The export reads **live** rows, so a `detached` identity is automatically correct: `detached`
means the live row holds the operator's runtime-owned values and `managed_by` stays `config`, so
reading the row emits the runtime value (the desired behavior — capturing the runtime modify).

A `removed` identity is the one case that needs explicit handling: a `removed` config row that is
**cordoned-live** keeps `managed_by='config'` until the GC step deletes it once idle (ADR-0199), so
it is still in the `managed_by='config'` set the export would otherwise emit. The export must
**omit** any identity carrying a `removed` ledger entry, so the exported file matches the operator's
intent (the host is gone) and a fresh start does not resurrect it. The export queries
`inventory_overrides` (`lookup_many` per family) and filters out `removed` `(resource_kind, name)`
identities for resources and `removed` build-host names. `detached` entries are **not** filtered
(their live values are exactly what we want to capture).

## Determinism

Byte-identical output for a given DB state. Achieved by:

- A fixed section order: header comment, `[[image]]` (sorted by `(provider, name, arch)`),
  `[[remote_libvirt]]` (sorted by `name`), `[[local_libvirt]]` (sorted by `name`),
  `[[fault_inject]]` (sorted by `name`), `[[build_host]]` (sorted by `name`),
  `[[cost_class]]` (sorted by `name`, ADR-0115's serializer).
- A fixed key order within each block.
- No timestamps or other non-deterministic content in the body (the header comment is static
  text, no clock read).

`local_libvirt`/`fault_inject` resources are also config-owned and are exported when present
(discovery-owned `local_libvirt` rows with no config instance carry a derived name but
`managed_by='discovery'`, so they are excluded by the `managed_by='config'` filter). `local_libvirt`
config rows persist `name`/`cost_class`/`pool`/`concurrent_allocation_cap`/`host_uri`;
`fault_inject` persists those plus `vcpus`/`memory_mb`/`seed` (`seed` defaults to 0 and is
file-only — emitted as the default).

## TOML emission

No `tomli_w` dependency is available; the existing cost-class serializer hand-builds TOML strings.
This spec does the same with a small, well-tested emitter:

- Strings are emitted as TOML basic strings with full escaping (`\`, `"`, control chars, newline,
  tab) via a shared `_toml_str` helper. This closes the TOML-injection vector the existing
  `test_set_cost_class_coeff_rejects_toml_significant_name` guards for `set_cost_class_coeff`: a
  `name` or `host_uri` containing `"`/newline/`]` cannot break out of its value. (The reconcile
  loader already validates most identity fields, but the emitter must be safe regardless.)
- Integers and the `coeff` decimal string are emitted unquoted / quoted exactly as ADR-0115 does.
- Arrays (`capabilities`, `shapes`) are emitted as `["a", "b"]` with escaped elements; an empty
  array as `[]`.

## Public API of `serialize.py` (so sub-issue D can build on it)

```python
@dataclass(frozen=True)
class InventorySnapshot:
    images: tuple[ImageRow, ...]
    remote_libvirt: tuple[ResourceRow, ...]
    local_libvirt: tuple[ResourceRow, ...]
    fault_inject: tuple[ResourceRow, ...]
    build_hosts: tuple[BuildHostRow, ...]
    cost_classes: tuple[tuple[str, Decimal], ...]

async def read_inventory_snapshot(conn: AsyncConnection) -> InventorySnapshot: ...
    # reads live config-owned rows, honors the ledger (removed omitted, detached uses live values)

def serialize_inventory(snapshot: InventorySnapshot) -> str: ...
    # pure: snapshot -> deterministic systems.toml text (the function D persists)
```

The reader (DB I/O, ledger lookup) and the serializer (pure) are split so D can persist
`serialize_inventory(...)` output via its writeback adapter and can unit-test the serializer with a
hand-built snapshot.

## The tool (`ops.export_systems_toml` in `mcp/tools/ops/tuning.py`)

Read-only, `PLATFORM_OPERATOR` (gate via `require_platform_role`; denial audited iff the caller
holds ≥1 platform role, mirroring `export_cost_classes`). Returns the document as text in
`data["toml"]` (text output, no file write — writeback is sub-issue D). Audits the read to
`platform_audit_log` (scope `all-inventory`). Three-registration rule: the `register()` body, the
`exposure.py` `_PLAT_OP` map, and `test_tool_docs.py`.

## Acceptance

- Images/build_hosts/cost_classes and the identity/economic/sizing fields of resources round-trip
  (export → parse → reconcile → equal DB state) for the realized-source / fully-persisted cases.
- Byte-deterministic for a given DB state (two exports of the same state are identical).
- A `remote_libvirt` block is a skeleton naming every operator-supplied placeholder; an unedited
  skeleton does **not** parse (required file-only fields are placeholders).
- The round-trip test runs on the operator-**completed** file (placeholders filled).
- A `removed`-ledger identity is omitted; a `detached` identity is emitted with its live runtime
  values.

## Considered & rejected

- **Add a `tomli_w` dependency.** Rejected: a new dependency is attack surface and maintenance
  burden for a single serializer; the cost-class serializer already proves hand-rolled
  deterministic TOML works, and hand emission gives byte-determinism control without a sort-order
  surprise from a library.
- **Emit `build`-source images faithfully by storing the base in the DB.** Rejected: out of scope
  (a schema change to `image_catalog`), and the `defined`-row placeholder keeps round-trip DB
  equality (the only contract). The lossy case is documented, not hidden.
- **Persist the remote file-only fields to the DB so they round-trip.** Rejected by ADR-0199: the
  provider reads them from the file by design; persisting them duplicates the source of truth and
  would leak TLS-cert references into rows. The skeleton-placeholder contract is the ADR decision.
