# Implementation plan — `ops.export_systems_toml` (M2.7 sub-issue C, #640)

Derived from [the spec](../specs/2026-06-20-export-systems-toml-640.md). Implements ADR-0199's
full-inventory export task. **No migration** (read-only tool; head stays 0046). TDD throughout:
failing test first, minimal impl, focused guardrails, refactor green.

## Conventions / guardrails (every commit)

- Python 3.14, `uv`. Absolute imports only. ≤100 lines/function, complexity ≤8, ≤100-char lines,
  Google-style docstrings on public APIs.
- Guardrails before each commit: `just lint`, `just type` (whole tree), focused
  `uv run python -m pytest <file> -q`. `just check-mermaid` fails locally (jsdom); it is a CI doc
  gate only — no mermaid in these docs, safe to skip locally.
- Every tool returns a `ToolResponse` with the most specific `ErrorCategory` on failure.
- Doc-style word ban (no "critical/comprehensive/robust/…") in comments/commits/docstrings.
- `detect-secrets` may flag the TLS-ref placeholder strings: use obvious `REPLACE_ME_*` sentinels
  (not realistic-looking secrets) so the scanner does not trip; if it still flags, add a
  `pragma: allowlist secret` with a one-line justification.

## Coordination (parallel sibling #639 / B)

#639 also edits `mcp/tools/ops/tuning.py` (it changes `set_host_capacity` to write a `detached`
ledger entry). To minimize conflict, **append** the new `export_systems_toml` handler and its
`register()` tool block at the end of the existing functions / the end of `register()` — do not
interleave with `set_host_capacity`. The `exposure.py` and `test_tool_docs.py` additions are
single-line map inserts (additive, low conflict). Note exactly what changed in tuning.py in the
final report.

## File map

| Path | Change |
|---|---|
| `src/kdive/inventory/serialize.py` (new) | `InventorySnapshot` + row dataclasses, `read_inventory_snapshot`, `serialize_inventory`, TOML emitter helpers |
| `src/kdive/mcp/tools/ops/tuning.py` | append `export_systems_toml` handler + `register()` tool block + constants |
| `src/kdive/mcp/exposure.py` | add `"ops.export_systems_toml": _PLAT_OP` |
| `tests/mcp/core/test_tool_docs.py` | add `"ops.export_systems_toml": (...,)` test-doc map entry |
| `tests/inventory/test_serialize.py` (new) | serializer unit + round-trip-via-model tests |
| `tests/mcp/ops/test_ops_tuning.py` | tool gating/audit/round-trip-through-reconcile tests |

---

## Task 1 — TOML emitter primitives (`serialize.py`, pure)

**Where it fits:** the deterministic, injection-safe string layer everything else builds on.

**Tests first** (`tests/inventory/test_serialize.py`):
- `_toml_str` escapes `"`, `\`, newline, tab, and a control char; a plain string round-trips
  through `tomllib.loads` to the original value.
- `_toml_str` on a string containing `"\ncoeff = \"9` cannot break out (parse the emitted
  `k = <escaped>` and assert the value equals the original, no injected key).
- `_toml_array(["a", 'b"c'])` emits `["a", "b\"c"]` and round-trips; `[]` for empty.
- `_toml_int(5)` → `5`.

**Impl:** `_toml_str(s) -> str` (TOML basic-string with full escaping), `_toml_array(items)`,
`_toml_int(n)`. Keep each tiny. Reuse the `coeff` quoting idiom from ADR-0115 for cost classes.

**Acceptance:** every emitted scalar parses back via `tomllib` to its input; no token can inject a
key. Guardrails green.

**Rollback:** delete `serialize.py` + its test (no other module imports it yet).

---

## Task 2 — Snapshot dataclasses + `serialize_inventory` (pure)

**Where it fits:** the pure model→TOML core; the function sub-issue D will persist.

**Tests first:** build an `InventorySnapshot` by hand (no DB) and assert `serialize_inventory`:
- emits sections in the fixed order (image, remote_libvirt, local_libvirt, fault_inject,
  build_host, cost_class), each block sorted by its key;
- is byte-identical across two calls on the same snapshot (determinism);
- omits a NULL `base_image_volume` (local build host) and a NULL `digest` (no `digest =` line);
- emits a remote skeleton with `REPLACE_ME_*` placeholders for gdb_addr/gdbstub_range/the three
  TLS refs and `base_image`, `shapes = []`, and the live `uri`/`vcpus`/`memory_mb`/`cost_class`/
  `pool`/`concurrent_allocation_cap`;
- emits `schema_version = 2` once at the top;
- emits the static header comment naming the values-only snapshot + the placeholders.

**Impl:**
- `InventorySnapshot` (frozen) + `ImageRow`, `ResourceRow`, `BuildHostRow` frozen dataclasses with
  exactly the fields the serializer needs (typed; `digest: str | None`, etc.).
- `serialize_inventory(snapshot) -> str`: header comment + `schema_version = 2` + per-section
  emit helpers (`_emit_image`, `_emit_remote`, `_emit_local`, `_emit_fault`, `_emit_build_host`,
  `_emit_cost_class`). Cost-class emission reuses the ADR-0115 idiom (name-sorted quoted coeff).
  Sort each collection before emitting. A NULL optional column is omitted, not blanked.
- The header comment is fixed text (no clock); document the remote skeleton + `defined`-image +
  `base_image` placeholders and the values-only nature.

**Acceptance:** spec's determinism + skeleton + NULL-omission bullets. Parse the full output with
`tomllib` + `InventoryDoc.parse` after filling the remote placeholders → a valid doc. Guardrails
green.

**Rollback:** revert task-2 hunk of `serialize.py`.

---

## Task 3 — `read_inventory_snapshot` (DB read + ledger honoring)

**Where it fits:** the DB I/O half; turns live rows into an `InventorySnapshot`, honoring the ledger.

**Tests first** (in `tests/inventory/test_serialize.py`, using the `migrated_url` DB fixture):
- seed config rows (an `[[image]]` staged + registered-s3 + defined; a remote_libvirt resource via
  reconcile or direct insert; a local build host + an ephemeral one; cost classes) and assert the
  snapshot contains them with the right values read back from columns/jsonb;
- a `removed`-ledger resource identity and a `removed` build-host name are **omitted** from the
  snapshot;
- a `detached`-ledger resource is **present** with its live (runtime-modified) capability value;
- discovery-owned (`managed_by='discovery'`) and runtime-owned (`runtime`) rows are excluded;
- a private image (`managed_by='runtime'`) is excluded.

**Impl:** `read_inventory_snapshot(conn) -> InventorySnapshot`:
- SELECT `managed_by='config'` rows from `image_catalog`, `resources` (split by `kind`),
  `build_hosts`; SELECT all `cost_class_coefficients` (name-sorted).
- `lookup_many(conn, RESOURCE)` and `lookup_many(conn, BUILD_HOST)`; drop any `(kind, name)` /
  build-host name carrying a `removed` disposition. `detached` rows pass through unfiltered.
- Narrow jsonb cap numbers to `int`; reconstruct image `source` from `(object_key, volume, state)`
  per the spec rule. Read-back typing mirrors `_row_typing.RowTyper` (loud on a malformed row).

**Acceptance:** spec's ledger-honoring bullet (removed omitted, detached live). Guardrails green.

**Rollback:** revert task-3 hunk of `serialize.py`.

---

## Task 4 — `ops.export_systems_toml` tool + three registrations

**Where it fits:** the operator-facing read-only tool.

**Tests first** (`tests/mcp/ops/test_ops_tuning.py`, mirroring the `export_cost_classes` tests):
- no platform role → `authorization_denied`, no audit amplification;
- `platform_auditor` (holds a platform role, not operator) → denied **and** audited;
- operator → `ok`, `data["toml"]` is non-empty, audited exactly once (scope `all-inventory`);
- **round-trip through reconcile:** seed config inventory, export, fill the remote placeholders in
  the text (TLS/gdb + `base_image` set to an exported image name), `tomllib.loads` +
  `InventoryDoc.parse`, run the reconcile passes against a fresh-but-migrated DB (or re-run
  reconcile and assert no drift), and assert the resulting config rows equal the original DB state
  for images/build_hosts/cost_classes + the identity/economic/sizing resource fields;
- a `removed` resource is absent from the exported text;
- byte-determinism: two exports of the same state are identical.

**Impl:** append to `tuning.py`:
- constants `_EXPORT_SYSTEMS_TOOL = "ops.export_systems_toml"`, `_EXPORT_SYSTEMS_OBJECT_ID`,
  `_EXPORT_SYSTEMS_SCOPE = "all-inventory"`.
- `async def export_systems_toml(pool, ctx) -> ToolResponse`: gate on `PLATFORM_OPERATOR`
  (`audit_platform_denial` on denial); on a connection, `read_inventory_snapshot` +
  `serialize_inventory`; audit the read (`record_platform`, scope `all-inventory`); return
  `ToolResponse.success(..., data={"toml": text})`. Mirror `export_cost_classes` structure.
- a `register()` block: `@app.tool(name=_EXPORT_SYSTEMS_TOOL, annotations=_docmeta.read_only(),
  meta={"maturity": "implemented"})` wrapping `export_systems_toml(pool, current_context())`.

Then the **three registrations**:
1. `register()` block (above).
2. `exposure.py`: add `"ops.export_systems_toml": _PLAT_OP,` next to `ops.export_cost_classes`.
3. `test_tool_docs.py`: add `"ops.export_systems_toml": ("tests/mcp/ops/test_ops_tuning.py",),`.

**Acceptance:** issue #640 acceptance in full. Guardrails green, including the app-completeness and
tool-doc guards that fail outside touched dirs if a registration is missing.

**Rollback:** revert the tuning.py append + the two single-line map inserts + the test.

---

## Verification (before push)

- `just lint && just type` (whole tree) clean.
- Full focused suite: `tests/inventory/test_serialize.py`, `tests/mcp/ops/test_ops_tuning.py`,
  `tests/mcp/core/test_tool_docs.py`, plus the app-completeness test (`tests/mcp/core/test_app.py`).
- A full `just ci` equivalent locally where possible (skip `check-mermaid`/live markers); state any
  skipped gate in the PR body.

## Self-review

- **Spec coverage:** emitter (T1) → injection-safe determinism; pure serializer (T2) → determinism
  + skeleton + NULL omission; reader (T3) → persistence audit + ledger honoring; tool (T4) → gating
  + audit + round-trip + three registrations. Every spec acceptance bullet maps to a named test.
- **Placeholder scan:** no TBD/TODO; no migration (read-only) called out explicitly.
- **Consistency:** tool name `ops.export_systems_toml`, scope `all-inventory`, `serialize.py` API
  (`InventorySnapshot`/`read_inventory_snapshot`/`serialize_inventory`) match the spec verbatim.
