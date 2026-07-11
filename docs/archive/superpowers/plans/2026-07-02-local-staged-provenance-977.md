# Plan — Persist build provenance on the local staged-path reconcile flow (#977)

- **Spec:** `docs/superpowers/specs/2026-07-02-local-staged-provenance-977.md`
- **ADR:** [0296](../../adr/0296-local-staged-provenance.md)

Execution note: the tasks are tightly coupled (a shared sidecar module the writer and reader both
import), so they are implemented sequentially in one session, each TDD (failing test first). No
migration; the `provenance` column exists since 0023.

Guardrails to run before each commit (CI runs these individually): `just lint`, `just type`,
`just test`, and the doc gates `just docs-check`, `just adr-status-check`, `just docs-links`,
`just docs-paths`. Run the focused test module during TDD; run the full suite once before push.

## Task 1 — Shared sidecar module `kdive.images.staged_provenance`

**Fit:** the on-disk contract both `build-fs` (writer) and reconcile (reader) share. A single home
keeps the schema constant, path convention, and (de)serialization in one place.

**Files:** create `src/kdive/images/staged_provenance.py`; create
`tests/images/test_staged_provenance.py`.

**Public API:**
- `SIDECAR_SCHEMA = "kdive.staged-provenance.v1"`; `_SIDECAR_MAX_BYTES = 64 * 1024`.
- `sidecar_path(qcow2: Path) -> Path` → `Path(str(qcow2) + ".provenance.json")`.
- `write_sidecar(qcow2: Path, *, provenance: dict[str, object]) -> None` — atomic write
  (`NamedTemporaryFile` in the same dir + `os.replace`) of `{"schema", "provenance"}`. Raises
  `OSError` on I/O failure (the caller decides whether to degrade). No `digest` field (dropped —
  no in-system consumer; `staged-path` rows carry no digest per ADR-0228).
- `read_sidecar(qcow2: Path) -> dict[str, object] | None` — a **validated boundary**: returns the
  inner `provenance` dict only for a present sidecar that is ≤ `_SIDECAR_MAX_BYTES`, parses as a
  JSON object, has `schema == SIDECAR_SCHEMA`, and whose `provenance` is itself a JSON object.
  Returns `None` (never raises) for absent, unreadable, over-cap, non-JSON, non-object,
  wrong/missing-schema, or non-object-`provenance` sidecars. Logs a warning for a present-but-invalid
  sidecar (distinct from the silent absent case). The bound is a byte cap + object-shape check,
  **not** a per-key type allowlist, so a future provenance operand flows through unchanged.
  The cap is a **bounded read**: read at most `_SIDECAR_MAX_BYTES + 1` bytes and reject when the read
  exceeds `_SIDECAR_MAX_BYTES`, so an oversized sidecar never lands fully in memory.

**Tests (behavior + edges):**
- round-trip: `write_sidecar` then `read_sidecar` returns the exact provenance dict.
- `sidecar_path` appends `.provenance.json` without dropping `.qcow2`.
- absent sidecar → `read_sidecar` returns `None`, no warning.
- malformed JSON, JSON that is not an object, missing/unknown `schema`, `provenance` that is not a
  dict → each returns `None` (and warns for the present-but-invalid cases).
- over-cap: a sidecar larger than `_SIDECAR_MAX_BYTES` → `None` (+warn), rejected via the bounded
  read without reading the whole file into memory.
- a sidecar carrying an unknown extra provenance key → round-trips (future-operand freedom).
- `write_sidecar` is atomic: no `.provenance.json` left containing a partial document on success
  (assert content parses); a pre-existing sidecar is overwritten.

**Acceptance:** the module round-trips, every invalid-input branch (including over-cap) returns
`None` without raising, the writer is atomic, and an unknown extra key survives. `just lint type` clean.

## Task 2 — `build-fs` writes the sidecar

**Fit:** persists the provenance `run_build_fs` currently discards, beside the published qcow2.

**Files:** edit `src/kdive/images/rootfs_command.py`; edit `tests/images/test_rootfs_command.py`.

**Change:** in `run_build_fs`, after `_publish_rootfs(output, dest)`, call a new
`_write_provenance_sidecar(dest, output)` that calls
`staged_provenance.write_sidecar(dest, provenance=output.provenance)` inside a `try/except OSError`,
logging a warning (path + error) on failure and continuing. Do not change the `KDIVE_GUEST_IMAGE`
print or the success log.

**Tests:**
- happy path: `run_build_fs` with a stubbed plane (returns a known `RootfsBuildOutput`) writes
  `<dest>.provenance.json` whose inner `provenance` equals `output.provenance`.
- sidecar-write failure (patch `write_sidecar` to raise `OSError`) → `run_build_fs` still returns
  normally, still prints `KDIVE_GUEST_IMAGE`, logs a warning (assert via `caplog`).

**Acceptance:** acceptance criterion 1 of the spec. Focused test module green; `just lint type`.

## Task 3 — Reconcile persists the sidecar provenance

**Fit:** the read side — carries the sidecar into the `provenance` column, change-detected.

**Files:** edit `src/kdive/inventory/reconcile/images.py`; edit the reconcile-images tests under
`tests/reconciler/` (or `tests/inventory/reconcile/` — match where the existing image-reconcile
tests live).

**Prerequisite (resolved):** `tests/inventory/test_layering.py` forbids only `inventory → kdive.mcp`;
`inventory → kdive.images` is allowed, and `kdive.images` does not import `kdive.inventory` (no
cycle). The new `staged_provenance` module uses stdlib only, so the import direction is clean.

**Changes:**
1. `_load_config_rows`: add `provenance` to the SELECT column list.
2. New `_resolve_staged_provenance(entry, row) -> dict[str, object]` (async, off-thread read via
   `asyncio.to_thread`): for a `StagedPathSource`, `read_sidecar(Path(source.path))` → the dict if
   not `None`, else the existing row provenance (and log at debug that the staged-path row got no
   sidecar); for every other source kind, the existing row provenance (`row["provenance"]` narrowed
   to a dict, or `{}` when `row is None`).
3. Thread the resolved provenance into realization. Return it alongside the realized fields — prefer
   a small `RealizedImage` dataclass replacing the current 6-tuple return of `_realize` (state,
   object_key, volume, path, digest, **provenance**), keeping `warning` as a separate return, so the
   already-long tuple does not grow to seven positional elements. Update `_realize`/`_realize_build`/
   `_realize_s3` and both callers.
4. `_create_entry`: add the `provenance` column to the INSERT (`Jsonb(provenance)`).
5. `_update_entry`: add `provenance` to the `realized` change-detection dict and the UPDATE
   (`Jsonb`). Comparison is dict `==` (jsonb round-trips to a dict).

**Tests (drive `reconcile_images` against a real disposable Postgres, the existing pattern; write a
real temp qcow2 path + sidecar file):**
- a `staged-path` entry whose path has a valid sidecar → the created row's `provenance` equals the
  sidecar dict; `images.describe`-level signal rendering (or a direct `render_direct_kernel_signal`/
  `render_kdump_signal` on the fetched entry) reports a confident status (criteria 2, 3).
- a `staged-path` entry with **no** sidecar → row `provenance` is `{}`, signals `unverified`
  (criterion 4).
- a `staged-path` row that already has provenance, sidecar now absent → provenance preserved, no
  spurious `updated` record (criteria 4, 6 — preserve-on-absence + no phantom drift).
- a rebuild changes the sidecar → next reconcile updates the row's provenance (criterion 6).
- a `build`/`s3` row with publish-written provenance → reconcile leaves `provenance` untouched
  (criterion 5).
- steady state (unchanged sidecar) → clean no-op (no `updated` record).

**Acceptance:** spec criteria 2–6. Focused reconcile tests green; `just lint type`.

## Task 4 — Docs + guardrail sweep

**Fit:** finish the job — regenerate any generated docs the change touches and run the full gate.

**Files:** none expected beyond the spec/ADR/plan already committed; run `just docs-check`,
`just config-docs-check`, `just resources-docs-check`, `just adr-status-check` and regenerate if any
report drift. No tool-surface change is expected, so `just docs-check` should be a no-op.

**Acceptance:** the full `just` gate is green locally before push.

## Rollback / cleanup

Pure additive: a new module, a sidecar write in `build-fs`, and provenance persistence in reconcile.
Reverting the branch removes the sidecar write and reconcile persistence; existing rows keep their
`{}`/publish-written provenance, and the honesty invariant means no row was ever made to lie. No
migration to roll back.
