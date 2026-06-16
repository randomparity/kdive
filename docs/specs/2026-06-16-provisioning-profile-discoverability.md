# Spec — Provisioning-profile discoverability (#451)

- **Issue:** [#451](https://github.com/randomparity/kdive/issues/451) (work item A of epic #449)
- **ADR:** [`0124`](../adr/0124-provisioning-profile-discoverability.md)
- **Design:** [`mcp-onboarding-error-ergonomics.md`](../design/mcp-onboarding-error-ergonomics.md) §"Work item A"
- **Builds on:** [`0123`](../adr/0123-tool-error-detail-surfacing.md) (the `detail` + `errors` surfacing, merged)
- **Date:** 2026-06-16

## Problem

`systems.define`/`systems.provision`/`systems.reprovision` type the `profile` parameter as
`ProvisioningProfileInput = Mapping[str, object]` (`src/kdive/profiles/types.py:9`), so FastMCP
advertises a freeform `additionalProperties: true` blob. The real `ProvisioningProfile` schema
(`src/kdive/profiles/provisioning.py`) — required fields, the discriminated `rootfs`, the
per-provider sections, the `boot_method`↔provider pairing rule — is invisible on the MCP surface.
MCP-surface testing showed ten evidence-based profile attempts all rejected identically with no
schema to learn from. A new agent has no way to discover a valid profile.

## Spike results (both gating spikes PASS — typed-param branch is taken)

Run against `fastmcp==3.4.0` with the real `ProvisioningProfile` model:

1. **Interception** — a binding-time `pydantic_core.ValidationError` (FastMCP validates the typed
   model *before* the tool body, because `ProvisioningProfile` is `extra="forbid"`) **is catchable
   in `Middleware.on_call_tool`** around `call_next`. The call's arguments (including
   `allocation_id`/`system_id`) are available on `context.message.arguments`, and the tool name on
   `context.message.name`. So a boundary `ValidationError` can be re-enveloped as our
   `configuration_error` envelope.
2. **Client rendering** — the advertised input schema for `profile: ProvisioningProfile` contains
   **no `$ref`/`$defs`/`discriminator`** keys: FastMCP inlines the discriminated `rootfs` union as
   inline `oneOf` objects, and the top-level object advertises `additionalProperties: false`. The
   FastMCP 3.4.0 client renders it and a valid call round-trips (the `#404`/ADR-0113 *output*-schema
   recursion problem does not arise on this input — `ProvisioningProfile` is not self-recursive).

Because both pass, **no `json_schema_extra` fallback is needed**; the param is typed and validation
moves to the boundary, re-enveloped by middleware.

## Decision (per ADR-0124)

### 1. Type the `profile` parameter as `ProvisioningProfile`

`systems.define`, `systems.provision`, and `systems.reprovision` type `profile` as
`ProvisioningProfile` (not `ProvisioningProfileInput`), so FastMCP advertises the model's JSON
schema. The tool bodies already call `ProvisioningProfile.parse(...)` internally; that path stays
(it is also reached by the worker and is the authority for sizing reconciliation), but with a typed
param the well-formed model is bound at the boundary first.

**The registrar passes `dump_profile(profile)` (i.e. `model_dump(mode="json", by_alias=True,
exclude_none=True)`) — not `model_dump()` with defaults — into the existing handler.** This is
load-bearing: the downstream admission path runs `reconcile_profile_sizing(profile, sizing)` (which
reads top-level `vcpu`/`memory_mb`/`disk_gb` keys) and `ProvisioningProfile.parse(reconciled)`, and
`profile_digest` (the reprovision dedup key) is computed over the alias-keyed canonical dump. A
default `model_dump()` would emit Python field names (`local_libvirt_section`) instead of the
provider-section **aliases** (`local-libvirt`), which `parse()` rejects, and would change the stored
digest. `by_alias=True` reproduces exactly the mapping shape a raw-`Mapping` caller sent, so the
old and typed paths store byte-identical profiles and the same digest. A regression test asserts a
typed-param submission yields the same stored profile/digest as the equivalent raw-mapping
submission did.

Consequence (flagged, intended): extra keys a caller could previously send under
`additionalProperties: true` are now rejected (`extra="forbid"`). This tightens the contract; it is
the same rejection `parse()` already performed, just surfaced earlier.

### 2. Re-envelope the boundary `ValidationError` as `configuration_error`

A new `ProfileBindingMiddleware` wraps `on_call_tool`. **Registration order is pinned:** it is added
**after** `TelemetryMiddleware` and `DenialAuditMiddleware` (so it is the *innermost* of the three,
adjacent to argument binding). This matters because `TelemetryMiddleware.on_call_tool` re-raises any
exception and records it as `outcome="error"`; by sitting inside it, `ProfileBindingMiddleware`
converts the binding `ValidationError` into a returned envelope (not a raised exception), so
telemetry sees the call as a normal completion, not an error — matching how a body-rejected bad
profile (which returns an envelope today) is already counted. It catches a `pydantic.ValidationError`
raised during binding **only for the typed-profile tools** (a fixed name set: `systems.define`,
`systems.provision`, `systems.reprovision`) and returns
`ToolResponse.failure_from_error(object_id, CategorizedError("invalid provisioning profile", CONFIGURATION_ERROR, details={"errors": exc.errors(...)}))`
— the **same** `CONFIGURATION_ERROR` + `detail` + sanitized `errors` shape `ProvisioningProfile.parse()`
already produces, reusing ADR-0123's `safe_error_details`. `object_id` is the call's
`allocation_id`/`system_id` argument when present, else the tool name. The middleware re-raises any
other exception unchanged (so role/membership denials still route through `DenialAuditMiddleware`,
and a non-profile tool's binding error is untouched). It is scoped to the fixed tool-name set so it
is **not** a project-wide boundary-validation rewrite (explicitly out of scope per the design doc).

The `errors()` are extracted with `include_url=False, include_input=False, include_context=False`
(mirroring `provisioning.py:281`), so no submitted value echoes; `safe_error_details` bounds the
list to 20 entries and strips to `{loc, msg, type}`.

### 3. Add `systems.profile_examples` (read-only, auth-only, no gate, no audit)

Modeled on `projects.list` (ADR-0117): a plain authenticated read — `current_context()` enforces a
valid token as defence-in-depth, no platform/project gate, no audit. It returns one
`ToolResponse` item per **configured provider**, each carrying a ready-to-edit example profile dict
in `data["profile"]`, chained via `suggested_next_actions = ["systems.define", "allocations.request"]`.

**Source = the `systems.toml` inventory only** (`load_inventory_optional`, the ADR-0112 source of
truth). The tool is a pure projection of the inventory file — it opens no DB connection. "Configured
provider" = a provider with at least one declared instance in the inventory
(`[[local_libvirt]]`/`[[remote_libvirt]]`/`[[fault_inject]]`); when no inventory file is present
(the gitignored pre-config state), the tool emits the built-in default example set (one per provider
kind) with marked placeholders.

**Reference resolution per provider:**

| Provider | Example shape | When a real ref exists | When it does not |
|---|---|---|---|
| `local-libvirt` | `boot_method: direct-kernel`; `local-libvirt` section with a `rootfs` | `catalog` ref naming a `PUBLIC` `local-libvirt` `[[image]]` | **`local` ref** with placeholder absolute path (`/REPLACE_ME/rootfs.img`) + `note` |
| `remote-libvirt` | `boot_method: disk-image`; `remote-libvirt` section with `base_image_volume` | the instance's `base_image` (a declared `[[image]]` name) | placeholder string `"REPLACE_ME-base-image-volume"` + `note` |
| `fault-inject` | `boot_method: direct-kernel`; `fault-inject` section (**no rootfs**) | — (section carries only `destructive_ops`/`capture_method`) | n/a (no reference to fill) |

The shapes are dictated by the model: `LibvirtProfile` requires a discriminated `rootfs`;
`FaultInjectProfile` has **no** `rootfs` field (`provisioning.py:127`), so a fault-inject example
must omit it or it fails `extra="forbid"`; `RemoteLibvirtProfile` has no rootfs and pairs with
`boot_method: disk-image` (the pairing validator, `provisioning.py:248`). `direct-kernel` is the
only legal `boot_method` for the two non-remote providers (disk-image forces remote).

**Why local-libvirt falls back to a `local` rootfs, not a placeholder `catalog` name (load-bearing
for the "policy-valid" guarantee).** `validate_profile_for_provider` → the local policy →
`validate_rootfs_reference` rejects a `catalog` name **not declared** in `systems.toml` when an
inventory file is present (`provisioning.py:393` / `_catalog_name_declared` returns `False` and
raises). So a placeholder `catalog` name would *fail* provider policy in exactly the case it is used
(a configured local-libvirt provider with no public image). A `local` rootfs ref is **not**
inventory-checked (`validate_rootfs_reference` only validates `catalog` kinds) and `local` is an
accepted source (`composition.py:50`); its only constraint is an absolute `path`
(`references.py:50`), which the placeholder satisfies. So every emitted example — real-ref or
placeholder — passes `parse()` + `validate_profile_for_provider()` unconditionally. When a `catalog`
ref *is* used, it names a real declared image, so it passes too.

**Data contract / no-leak invariant (this tool is *not* `projects.list`).** The projection reads
**only** non-sensitive inventory identifiers: provider name, and a `PUBLIC`-visibility `[[image]]`
name (or, for remote, the `base_image` name, which is itself a declared `[[image]]`). It **never**
reads or emits the `[[remote_libvirt]]` `uri`, `gdb_addr`, `gdbstub_range`, or any `*_cert_ref`
secret-ref name. Inventory `[[image]]` entries with `visibility = private` are **excluded** — only
`PUBLIC` images can name a reference, so no operator-private image name reaches the wire. Because the
tool reads only the operator-declared inventory file (not DB project-private images) and filters to
`PUBLIC`, the no-leak invariant holds by construction: there is no tenant-scoped data path to leak.

DB-backed, project-private catalog images (ADR-0124's "shown only to its owning project" clause) are
**deferred**: surfacing them would add a pool dependency and a project-scoped query to what is
otherwise a pure inventory projection, for marginal onboarding benefit (an agent can already
`images.list` its project's images, and `systems.profile_examples` is a "what shape is valid here"
discovery aid, not an image picker). This is a deliberate tightening of ADR-0124's contract toward
its strictly-safe subset, not a weakening: every acceptance criterion still holds (the leak test,
AC4, is *easier* to satisfy because the only data path is `PUBLIC` inventory), and the ADR's central
guarantee — "never the `uri`/`gdb_addr`/secret-ref, and one tenant's private names never reach
another" — is preserved a fortiori because no private data path exists. ADR-0124 is `Proposed`
(per the M2 convention that onboarding ADRs stay Proposed post-merge); this spec narrows its
implementation scope rather than amending the decision text, and records the deferral so a later
reviewer sees it as chosen, not missed.

**`object_id` on the re-enveloped boundary error.** When the middleware re-envelopes a binding
`ValidationError`, `object_id` is the call's `allocation_id`/`system_id` string argument if present
(matching the body-path `_config_error(allocation_id)` shape so the same bad call has a consistent
`object_id` whether the profile is rejected at the boundary or in the body), else the tool name.

**Validity obligation.** Every emitted example — real-ref **or** placeholder — must pass
`ProvisioningProfile.parse()` + `validate_profile_for_provider()` (the schema+policy layer, not the
allocation-scoped admission path), with **no further edits**. (The remote-libvirt placeholder
`base_image_volume` is a plain non-empty string the model accepts as-is; it is not provisionable
until the operator stages that volume, but it parses and passes policy.) The examples carry concrete
sizing (`vcpu`/`memory_mb`/`disk_gb`) so they parse standalone.

The validity test must reckon with a file-vs-doc coupling: `validate_rootfs_reference` re-loads the
inventory from the `KDIVE_SYSTEMS_TOML` path via `config.get`, **not** from the in-memory `doc` the
builder used. So the test writes one temp `systems.toml`, points `KDIVE_SYSTEMS_TOML` at it, and
drives **both** the builder and the validator off that same file — otherwise a `catalog` example
built from the file's image would be re-validated against a *different* (or absent) inventory and
the test would not exercise the real path.

## Acceptance (each has a test)

1. `systems.define`'s advertised input schema is no longer `additionalProperties: true` — it is the
   typed `ProvisioningProfile` object schema (`additionalProperties: false`).
2. A malformed profile sent to `systems.define` returns the `configuration_error` envelope (with
   `detail` and an `errors` field-path list), not a raw FastMCP `ToolError`.
3. `systems.profile_examples` returns a valid example per configured provider; each example (with
   placeholders resolved) round-trips through `ProvisioningProfile.parse()` +
   `validate_profile_for_provider()` without a `configuration_error`.
4. A leak test asserts no example contains a `uri`, `gdb_addr`, `gdbstub_range`, or a `*_cert_ref`
   name, and that a `private`-visibility inventory image name never appears.
5. `tests/mcp/core/test_tool_docs.py` tool→test map includes `systems.profile_examples`; the
   generated tool reference lists it (regenerated via `just docs`).

## Out of scope

- Project-wide boundary-validation middleware (the re-envelope is scoped to the three typed-profile
  tools).
- DB-backed project-private catalog images in examples (deferred; inventory `PUBLIC` only).
- Versioning or persistence of profiles (examples, not stored templates).
- The `json_schema_extra` fallback (not needed — both spikes passed).
- No DB migration.
