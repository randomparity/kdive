# Plan — local-libvirt gdb-MI debuginfo resolver (#702)

Derived from [the spec](../../specs/2026-06-22-local-libvirt-gdbmi-debuginfo-resolver.md).
Anchor ADR: [ADR-0210](../../adr/0210-local-libvirt-live-debug-introspection.md) §1. No new ADR,
no migration, no schema change.

**Guardrails (run before every commit):** `just lint`, `just type`, `just test`. Full `just ci`
before push. Known trap: local `ty` may diverge from CI on live-dep imports (drgn/guestfs) — if a
type error is purely a live-dep import-resolution artifact and not in the changed code, note it and
rely on CI's type job; fix everything else to zero.

**Scope guard (parallel run):** only touch `src/kdive/providers/local_libvirt/debug/gdbmi.py`,
`src/kdive/db/artifact_queries.py`, and `tests/providers/local_libvirt/test_debug_gdbmi.py`. Make
**zero** edits to `composition.py` (sibling #703 may touch it). Do not touch tool maturity metadata.

The change is small and tightly coupled (one provider module + one DB query + its tests), so it is
implemented directly in-session with TDD, not fanned out to subagents.

---

## Task 1 — sync DB query: `debuginfo_ref_for_run_sync`

**Where it fits:** the resolver's `read_debuginfo_ref` seam needs the Run's `debuginfo_ref`
(`runs.debuginfo_ref`, set by the build plane) over a **sync** connection, because the attach seam
runs in `asyncio.to_thread` and owns no async pool (spec §2).

**Files:** `src/kdive/db/artifact_queries.py` (add a sync query beside the existing async
`raw_vmcore_key`).

**Implementation:**
- Add `from psycopg import Connection` (sync) alongside the existing `AsyncConnection` import.
- Add:
  ```python
  _DEBUGINFO_REF_SQL: LiteralString = "SELECT debuginfo_ref FROM runs WHERE id = %s"

  def debuginfo_ref_for_run_sync(conn: Connection, run_id: UUID) -> str | None:
      """Return the Run's published debuginfo (vmlinux) object key, or None.

      Sync because the gdb-MI attach seam runs off the event loop (asyncio.to_thread) and
      owns no async pool. None covers both an absent Run row and a row whose debuginfo_ref
      is NULL — the caller (the resolver) treats both as no_debuginfo.
      """
      with conn.cursor(row_factory=dict_row) as cur:
          cur.execute(_DEBUGINFO_REF_SQL, (run_id,))
          row = cur.fetchone()
      if row is None:
          return None
      ref = row["debuginfo_ref"]
      return str(ref) if isinstance(ref, str) and ref else None
  ```
- `run_id` is bound as a parameter (no interpolation). The caller passes a `UUID` (parsed from the
  handler's `str(session.run_id)`); psycopg adapts it.

**Acceptance:** module imports cleanly; `ty`/`lint` green. (Behavior is covered indirectly via the
resolver tests in Task 3 with a fake seam — this query's live DB read is `live_vm`/integration
territory and is not separately unit-tested here, matching how `raw_vmcore_key`'s SQL is exercised
through its callers.)

**Rollback:** delete the added query + import.

---

## Task 2 — `DebuginfoResolver` + real `default_attach_seam` wiring

**Where it fits:** replaces the stub `_resolve_debuginfo_ref` (the #702 defect) with a real,
unit-tested resolver, and wires `default_attach_seam` to materialize the vmlinux to a private path
before `GdbMiEngine().attach(...)` (spec §1, §2).

**Files:** `src/kdive/providers/local_libvirt/debug/gdbmi.py`.

**Implementation:**
1. Delete `_resolve_debuginfo_ref` (the stub) entirely — replace, don't deprecate.
2. Add a pure, non-live-gated resolver:
   ```python
   type _ReadDebuginfoRef = Callable[[str], str | None]
   type _FetchObject = Callable[[str], bytes]

   class DebuginfoResolver:
       """Resolve + materialize a Run's debuginfo (vmlinux) for the gdb-MI attach seam.

       Mirrors the Retrieve/introspect lookup split: the DB read and object-store fetch are
       injected seams, so the orchestration (ref lookup, the no_debuginfo error, the write) is
       unit-tested with fakes and only the IO seams are live.
       """

       def __init__(self, *, read_debuginfo_ref: _ReadDebuginfoRef, fetch_object: _FetchObject) -> None:
           self._read_debuginfo_ref = read_debuginfo_ref
           self._fetch_object = fetch_object

       def resolve(self, run_id: str, dest: Path) -> Path:
           ref = self._read_debuginfo_ref(run_id)
           if ref is None:
               raise CategorizedError(
                   "the Run has no published debuginfo object; build the kernel before attaching gdb",
                   category=ErrorCategory.CONFIGURATION_ERROR,
                   details={"run_id": run_id, "reason": "no_debuginfo"},
               )
           dest.write_bytes(self._fetch_object(ref))
           return dest
   ```
   - Order matters: `read_debuginfo_ref` first; on `None`, raise **before** any `fetch_object` call.
   - `resolve` writes to the `dest` it is handed; it never derives a path from `run_id`.
3. Add the real lazy sync DB seam:
   ```python
   def _real_read_debuginfo_ref(run_id: str) -> str | None:  # pragma: no cover - live_vm
       with psycopg.connect(config.require(DATABASE_URL)) as conn:
           return debuginfo_ref_for_run_sync(conn, UUID(run_id))
   ```
   - `UUID(run_id)` relies on the caller's guarantee that `run_id == str(session.run_id)` (a valid
     UUID the handler already produced, `ops.py:128`). A non-UUID `run_id` is a programming error,
     not an operational path, so `UUID()` is allowed to raise `ValueError` here — it cannot be
     reached through the live caller. State this in a one-line comment so a future caller change does
     not silently turn it into an uncaught `ValueError`. (The resolver itself never parses `run_id`;
     this conversion lives only in the live DB seam.)
4. Reuse the shared object fetch: import `default_fetch_object` from
   `kdive.providers.shared.debug_common.crash_postmortem` (the same seam introspect/crash use) — no
   third copy.
5. Rewrite `default_attach_seam` (`# pragma: no cover - live_vm`):
   ```python
   def default_attach_seam(*, host, port, run_id, transcript_path) -> GdbMiAttachment:
       staging_dir = Path(tempfile.mkdtemp(prefix="kdive-debuginfo-"))  # mode 0o700 default
       resolver = DebuginfoResolver(
           read_debuginfo_ref=_real_read_debuginfo_ref, fetch_object=default_fetch_object
       )
       try:
           vmlinux_path = resolver.resolve(run_id, staging_dir / "vmlinux")
           return GdbMiEngine().attach(
               host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
           )
       except BaseException:
           shutil.rmtree(staging_dir, ignore_errors=True)
           raise
   ```
   - `mkdtemp` already creates the dir `0o700` by default — no extra `chmod`. (Confirm in the code
     comment that this is the security property, not an accident.)
   - The `rmtree` runs on **any** failure of `resolve` **or** `attach`; the success path leaves the
     dir for the OS temp reaper (spec §2 — reclaiming it at reap is an out-of-scope follow-up).
   - Catch `BaseException` (not just `Exception`) so a `KeyboardInterrupt`/cancellation mid-attach
     still cleans the dir, then re-raise — the seam never swallows.
6. Imports to add: `shutil`, `psycopg`, `from uuid import UUID`, `from collections.abc import
   Callable`, `import kdive.config as config`, `from kdive.config.core_settings import DATABASE_URL`,
   `from kdive.db.artifact_queries import debuginfo_ref_for_run_sync`,
   `from kdive.providers.shared.debug_common.crash_postmortem import default_fetch_object`. Keep the
   existing `tempfile`, `Path`, `CategorizedError`/`ErrorCategory`, `GdbMiAttachment`, `GdbMiEngine`.
7. Update `__all__` to export `DebuginfoResolver` (so tests import the public class) and keep
   `GdbMiEngine`, `default_attach_seam`. Update the module docstring's "live_vm-gated debuginfo
   resolver" line to describe the new split (resolver is unit-tested; only the IO seams are live).

**Acceptance:** `lint`/`type` green; `DebuginfoResolver` importable from the module; no reference to
the deleted stub remains (grep `_resolve_debuginfo_ref` returns nothing in src).

**Rollback:** restore the stub and the old `default_attach_seam` (git revert the file).

---

## Task 3 — tests (TDD: after Task 1 lands, write tests, watch fail, implement Task 2)

**Where it fits:** proves the resolver orchestration + the no_debuginfo error contract with fakes;
replaces the obsolete stub test (spec acceptance criteria).

**Files:** `tests/providers/local_libvirt/test_debug_gdbmi.py`.

**Implementation:**
- **Delete** `test_debuginfo_resolver_default_raises_missing_dependency` (lines ~993–1000) and its
  section comment if now empty — the stub it asserts is gone. Confirm no other test references
  `_resolve_debuginfo_ref`.
- Add a small section `# --- debuginfo resolver -----` with a recording fake and three tests:
  1. `test_resolve_fetches_present_ref_to_dest`: `read_debuginfo_ref` returns `"local/runs/r1/vmlinux"`,
     `fetch_object` returns `b"ELFDATA"` and records its call args. Assert `resolve("r1", tmp_path /
     "vmlinux")` returns that path, the file's bytes == `b"ELFDATA"`, and `fetch_object` was called
     exactly once with `"local/runs/r1/vmlinux"`.
  2. `test_resolve_none_ref_raises_no_debuginfo_before_fetch`: `read_debuginfo_ref` returns `None`,
     `fetch_object` records calls. Assert `resolve` raises `CategorizedError`,
     `category is CONFIGURATION_ERROR`, `details == {"run_id": "r1", "reason": "no_debuginfo"}`,
     message == the exact string, and `fetch_object` was **never called** (recorded calls empty).
  3. `test_resolve_propagates_fetch_error`: `read_debuginfo_ref` returns a ref; `fetch_object` raises
     a `CategorizedError(INFRASTRUCTURE_FAILURE)`. Assert `resolve` re-raises the **same** error
     unchanged (identity or category+message), and `dest` was not created (or is empty).
  4. (Optional, pins the path-safety property) `test_resolve_writes_to_dest_not_run_id_derived_path`:
     pass a `dest = tmp_path / "custom-name"` and assert the bytes land at exactly that path — proving
     `resolve` writes where told and computes no `run_id`-derived path itself.
- Use `DebuginfoResolver` from `kdive.providers.local_libvirt.debug.gdbmi`.

**TDD sequence (and task ordering):** Task 2's module adds a top-level
`from kdive.db.artifact_queries import debuginfo_ref_for_run_sync`, so the resolver module will not
import until **Task 1 lands first**. Execute in task number order — **Task 1 (query) → Task 2
(resolver + seam) → Task 3 (tests)**:
1. Land Task 1 so `debuginfo_ref_for_run_sync` exists (the resolver module's import resolves).
2. Write these resolver tests against the not-yet-existing `DebuginfoResolver` → run, confirm they
   fail with `ImportError`/`AttributeError` (expected reason — the class is absent).
3. Implement Task 2's `DebuginfoResolver` + seam → rerun, confirm the resolver tests pass.
4. Run the full `test_debug_gdbmi.py` → green (the deleted stub test is gone; nothing references
   `_resolve_debuginfo_ref`).

Do **not** implement Task 2 before Task 1: with Task 2 in place and Task 1 absent, the module's
top-level import raises `ImportError` and the resolver tests cannot collect — that is a self-inflicted
ordering failure, not the clean red the TDD step expects.

**Acceptance:** the three (or four) resolver tests pass; the full pre-existing engine suite stays
green; `just test` green; no test asserts the old `MISSING_DEPENDENCY` "live_vm gate" message for
this seam.

**Rollback:** restore the deleted stub test; remove the new tests.

---

## Cross-cutting / finish

- Run full `just ci` before push (catches the doc-generation/architecture tests outside the touched
  dirs). The `debug.*` tool reference docs are **not** regenerated by this change (no maturity or
  schema change) — confirm `docs-check` stays green without a `just docs` run; if it flags a diff,
  investigate before pushing (it should not, since no tool metadata changed).
- Branch adversarial review (`/challenge --base main`) + `security-review` after green guardrails.
- **Flag to orchestrator (do not fix here):** (a) the identical remote stub
  `remote_libvirt/debug/gdbmi.py::_resolve_remote_debuginfo_ref`; (b) success-path staging-dir
  reclaim at session reap (needs a shared-dataclass/ops.py edit, out of scope); (c) build-id
  provenance between published vmlinux and the live kernel (scoped out, possible post-B6 follow-up).
- Do **not** merge; drive to green CI + CLEAN/MERGEABLE and hand off.
