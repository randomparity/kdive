# Spec — Remote-libvirt parity & agent-surface docs for uploaded-object encoding (#1511)

Epic #1508 Sub 3 (final). ADR: [ADR-0439](../adr/0439-advertise-transport-encoding-upload-surface.md).

## Problem

Subs 1 (#1509, ADR-0437) and 2 (#1510, ADR-0438) made the transport `encoding`/`uncompressed_size`
upload surface **functional and live-proven** (a gzipped 6.0 GiB rootfs provisioned to `ready`) but
deliberately left it **unadvertised** in the agent-facing tool schema — the epic's ordering guard, so
no agent declares a gzip upload before its consumer exists. That guard is now satisfied. Two open
threads remain:

- **A — Advertisement.** The fields are invisible to agents: the tool schema, `Field` descriptions,
  tool docstrings, and guides describe only `{name, sha256, size_bytes, chunks}`.
- **B — Remote parity (epic Req 9).** The remote-libvirt lane should decompress on the remote host
  with identical semantics. But remote-libvirt accepts no `ROOTFS` component source at all today; a
  remote uploaded-rootfs path is #1433 (`status:blocked`).

## Part A — Advertise the encoding surface (in scope)

### Behavior

Only `create_system_upload` gains the advertisement (systems `accepts_encoding=True`).
`create_run_upload` does not (runs rejects a non-identity encoding at declaration).

Advertised constraints (matching the ADR-0437 validator exactly):

- `encoding` optional, `"gzip"` only (`"identity"`/omitted = no encoding).
- `uncompressed_size` **required** when `encoding` is present; the canonical object's size in bytes.
- Single-PUT only — `encoding` cannot be combined with `chunks`.
- The canonical (decompressed) object is capped at 50 GiB (the systems `uncompressed_cap`).
- Rootfs (systems) is the only consumer today; the run lane rejects `encoding`.

### Changes

1. `src/kdive/mcp/tools/catalog/artifacts/uploads.py`
   - Split the shared `UPLOAD_DECLARATION_ITEM_SCHEMA` into the base (runs) schema and a
     `SYSTEM_UPLOAD_DECLARATION_ITEM_SCHEMA` that adds `encoding` + `uncompressed_size` properties.
   - Add a third `SYSTEM_DECLARATION_EXAMPLES` item: a single-PUT gzip declaration with
     `encoding`/`uncompressed_size`.
2. `src/kdive/mcp/tools/catalog/artifacts/registrar.py`
   - Parameterize `_declaration_schema_extra(examples, *, item_schema)`.
   - `create_system_upload`: pass the systems item schema; extend the `Field` description and the
     wrapper docstring to describe the encoding surface + constraints.
   - `create_run_upload`: keep the base schema; one docstring line that transport encoding is a
     systems-only (rootfs) surface.
3. `docs/guide/toolsets/artifacts.md` (hand-written): add an "uploading a large rootfs" note on the
   gzip transport encoding and its constraints.
4. Regenerate: `just docs` (→ `docs/guide/reference/artifacts.md`), `just cli-verbs`
   (→ `_generated_verbs.py`), plus `doc-constants`/`rbac-matrix`/`resources-docs` as the check demands.

### Tests

- `_validate_encoding` behavior is already covered by Sub 1's tests; no validator change here.
- Add/extend a registrar/schema test asserting the systems declaration schema advertises
  `encoding`/`uncompressed_size` and the run schema does **not** (drift guard for the per-owner split).

## Part B — Remote parity (deferred to #1433)

Remote-libvirt has no uploaded-rootfs consumer to strip into (`_component_sources()` accepts no
`ROOTFS`). No speculative remote wiring is added. The ADR records that the shared
`strip_gzip_to_writer` + `RangedReadStore` contract is provider-agnostic (lives in
`src/kdive/artifacts/transport_encoding.py`, no provider imports) and that #1433 adopts it unchanged
when it lands the remote `ROOTFS` source. Epic Req 9 is satisfied at the contract level; the remote
code path is a #1433 obligation.

## Out of scope

- Remote uploaded-rootfs wiring (#1433, blocked).
- Any validator/consumer behavior change (Subs 1/2 own it).
- Other codecs, multipart >5 GiB, storage-cost accounting (epic non-goals / separate follow-ups).
