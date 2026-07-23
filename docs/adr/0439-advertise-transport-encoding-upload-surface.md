# ADR 0439 — Advertise the transport-encoding upload surface; remote parity deferred to #1433

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0437](0437-transport-encoding-canonical-object-model.md) (the transport-encoding
  model, the per-owner `accepts_encoding` capability, and the shared `strip_gzip_to_writer` decode
  utility), [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md) (the rootfs consumer that
  strips the encoding on the local-libvirt lane — the live-proven consumer this ADR now advertises).
- **Part of:** epic #1508 (transparent transport-encoding for agent-uploaded objects); this is
  Sub 3 (#1511), the final sub — agent-surface advertisement + the remote-parity decision.

## Context

ADR-0437 (Sub 1) landed the generic model and validator: an agent can declare `encoding: "gzip"`
plus an `uncompressed_size`, the declaration validator enforces it (`uncompressed_size` required,
unknown codec rejected, `encoding`+`chunks` rejected, per-owner `accepts_encoding` + `uncompressed_cap`),
and the `upload_manifests` JSONB persists it. ADR-0438 (Sub 2) wired the rootfs consumer: the
local-libvirt uploaded-rootfs fetch reads the declared encoding from the manifest and strips a gzip
transport object streaming into the staged qcow2 base, bomb-bound and magic-checked. Both merged and
are **live-proven** (a gzipped 6.0 GiB rootfs, 364 MiB compressed, provisioned to `ready`).

Both subs deliberately left the `encoding`/`uncompressed_size` fields **unadvertised** in the
agent-facing tool schema (ADR-0438 closing note: "`encoding` stays unadvertised until Sub 3
(#1511)"). This was the epic's ordering guard — advertising a field before its consumer exists would
invite an agent to declare a gzip upload that then dead-ends at provision. With the consumer merged
and proven, the guard is satisfied and the surface can be advertised.

The epic also names **remote-libvirt parity** (Requirement 9): the remote lane should decompress on
the remote host with identical semantics. But remote-libvirt accepts **no `ROOTFS` component source
at all** today — `remote_libvirt/composition.py:_component_sources()` maps only `CONFIG`/`KERNEL`/
`PATCH`/`VMLINUX`; the rootfs is a fixed operator-staged base image. Adding a remote uploaded-rootfs
fetch is exactly the scope of **#1433** ("Component source: ROOTFS base image on remote-libvirt"),
which is `status:blocked`. There is no remote consumer to wire the encoding strip into.

## Decision

### 1. Advertise the encoding surface on the systems upload tool only

`create_system_upload` (systems, `accepts_encoding=True`) advertises the two optional fields in the
declaration item JSON Schema (`json_schema_extra`), the `artifacts` `Field` description, the tool
wrapper docstring, and a third worked example (a single-PUT gzip declaration). The constraints stated
match the validator exactly: gzip only; single-PUT only (no `chunks`); `uncompressed_size` **required**
with `encoding`; the canonical (decompressed) object is capped at 50 GiB.

`create_run_upload` (runs, `accepts_encoding=False`) does **not** advertise the fields — the validator
rejects a non-identity `encoding` on the run lane, so advertising it would invite a rejected
declaration. The run tool's docstring states, in one line, that transport encoding is a systems-only
(rootfs) surface. The declaration item schema therefore becomes **per-owner**: a base schema for runs
and a systems schema that adds the two encoding properties.

The generated tool reference (`docs/guide/reference/artifacts.md`, `just docs`), the committed
`kdivectl` verb descriptors (`just cli-verbs`), and the hand-written toolset guide
(`docs/guide/toolsets/artifacts.md`) are regenerated/updated from these source edits, so the
advertisement can never drift from the validator.

### 2. Remote parity is deferred to #1433; the shared utility is confirmed provider-agnostic

No speculative remote wiring is added (repo rule: no speculative features). Instead this ADR records
that the decode utility is already provider-agnostic and ready for #1433 to adopt:

- `strip_gzip_to_writer(store: RangedReadStore, request: StripDecodeRequest, writer: IO[bytes])`
  lives in `src/kdive/artifacts/transport_encoding.py` and imports nothing from any provider. It
  takes a narrow `RangedReadStore` (one `get_range` method) and any binary writer, so a remote
  consumer streaming into a libvirt volume stream (or a remote host file) reuses it unchanged.
- The declared `(encoding, uncompressed_size)` is a plain systems-manifest fact read via
  `get_manifest_sync(conn, "systems", id)` — not a local-libvirt concept — so #1433's remote fetch
  reads it the same way ADR-0438's local fetch does.

When #1433 lands a remote `ROOTFS` component source, it consumes the *same* utility with the *same*
semantics (streaming, bomb-bound, qcow2 magic), decompressing on the remote host. That is tracked as
a #1433 obligation, not built here.

## Consequences

- Agents discover the encoding surface and its constraints from the tool schema and guides; the
  headline >5 GiB-uncompressed rootfs case is now self-service-discoverable, not tribal knowledge.
- The run and system declaration schemas diverge by two properties. This is honest — the surfaces
  genuinely differ (runs rejects encoding) — at the cost of a per-owner item schema instead of one
  shared constant.
- Remote-libvirt still cannot consume any uploaded rootfs (encoded or not); that gap is #1433's, and
  the epic's Requirement 9 is satisfied at the utility-contract level (provider-agnostic, ready to
  adopt) rather than by shipping an unreachable remote code path.

## Alternatives considered

- **Advertise encoding on both upload tools.** Rejected: runs rejects a non-identity encoding at
  declaration, so advertising it there invites a guaranteed rejection.
- **Build the remote strip now.** Rejected: there is no remote uploaded-rootfs path to wire it into
  (#1433, blocked). Shipping the decode call with no caller is dead speculative code.
- **Keep one shared declaration schema.** Rejected: it would advertise encoding to the run lane that
  rejects it. A per-owner schema keeps the advertisement truthful.
