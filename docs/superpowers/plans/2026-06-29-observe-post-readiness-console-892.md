# Post-readiness console observation (rotating part artifacts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the live console after boot readiness as append-only, redacted, gzip-compressed, ~64 KiB System-owned console *part* artifacts read through the existing `artifacts.{list,get,search_text}` surface — no new MCP tool.

**Architecture:** Each sealed part is a `REDACTED` System-owned `artifacts` row keyed `console-part-<gen>-<start>` (boot generation; zero-padded plaintext start offset). Local-libvirt capture is a reconciler-dispatched `console_rotate` worker job that reads the plaintext delta of the worker-host console file past a sidecar-tracked offset (re-reading a seam-overlap window for redaction), seals parts, and is idempotent by offset-derived key under a per-System advisory lock. Remote-libvirt's collector additionally writes a separate compressed part artifact per sealed part. `artifacts.get` inflates parts whose object metadata says `content_encoding=gzip`.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; Postgres (psycopg async), S3-compatible object store (boto3), libvirt; gzip (stdlib).

## Global Constraints

- Spec: `docs/specs/2026-06-29-observe-post-readiness-console-892.md`. ADR: `docs/adr/0273-observe-rotating-console-parts.md` (opens **Proposed**; flip to **Accepted** in the final task that cites it in `src/`, per `scripts/check_adr_status.py`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, **whole-tree** (`just type` covers `src` + `tests`).
- Absolute imports only (no relative). Google-style docstrings on non-trivial public APIs. Functions ≤100 lines, cyclomatic ≤8, ≤5 positional params.
- Per-commit guardrails (the CI-individual gates): `just lint`, `just type`, then the focused tests; before the first push run the full `just ci`.
- Doc-style: plain factual prose; never "critical"/"robust"/"comprehensive"/"elegant"; "Milestone" not "Sprint".
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Redaction runs on plaintext before storage (`security/secrets/redaction.py`, `Redactor(registry=...).redact_text`). Never store raw guest console bytes.
- Rotation threshold constant: reuse `DEFAULT_ROTATION_THRESHOLD = 64 * 1024` from `src/kdive/providers/remote_libvirt/console/collector.py` (re-export to a shared location in Task 4 rather than duplicating the literal).
- `console_rotate` is **not** a destructive job kind (do not add it to `DESTRUCTIVE_JOB_KINDS`).

---

### Task 1: Object-store `content_encoding` user-metadata (write + head)

**Files:**
- Modify: `src/kdive/store/objectstore.py` (`put_artifact`/`put_stream` `Metadata=` dicts ~lines 100-102, 141-143; `HeadResult` class ~line 222 area; `head()` metadata read)
- Modify: `src/kdive/artifacts/storage.py` (`ArtifactWriteRequest`: add optional `content_encoding: str | None = None`)
- Test: `tests/store/test_objectstore_content_encoding.py`

**Interfaces:**
- Produces: `ArtifactWriteRequest(..., content_encoding: str | None = None)`; object `head()` result exposes `content_encoding: str | None`; `put_artifact` writes a `content-encoding` user-metadata entry when set.

- [ ] **Step 1: Write the failing test** — round-trip a `content_encoding="gzip"` put and assert `head().content_encoding == "gzip"`, and that an unset put yields `None`.

```python
# tests/store/test_objectstore_content_encoding.py
def test_put_records_content_encoding_and_head_reads_it(object_store):
    req = ArtifactWriteRequest(
        tenant="t", owner_kind="systems", owner_id=str(uuid4()),
        name="console-part-0-000000", data=b"x", sensitivity=Sensitivity.REDACTED,
        retention_class="evidence", content_encoding="gzip",
    )
    stored = object_store.put_artifact(req)
    head = object_store.head(stored.key)
    assert head is not None and head.content_encoding == "gzip"

def test_put_without_content_encoding_heads_none(object_store):
    req = ArtifactWriteRequest(
        tenant="t", owner_kind="systems", owner_id=str(uuid4()),
        name="dmesg-redacted", data=b"x", sensitivity=Sensitivity.REDACTED,
        retention_class="evidence",
    )
    stored = object_store.put_artifact(req)
    head = object_store.head(stored.key)
    assert head is not None and head.content_encoding is None
```

- [ ] **Step 2: Run test to verify it fails** — `uv run python -m pytest tests/store/test_objectstore_content_encoding.py -q` → FAIL (`content_encoding` unknown / `HeadResult` has no such field). These store tests need Docker/MinIO; if the suite skips locally without it, run with `KDIVE_REQUIRE_DOCKER=1` on a host with the compose backends (`just compose-up`).

- [ ] **Step 3: Implement** — add `content_encoding: str | None = None` to `ArtifactWriteRequest`; in `put_artifact`/`put_stream`, conditionally add `"content-encoding": request.content_encoding` to the `Metadata=` dict when set; add `content_encoding: str | None` to `HeadResult` and read `metadata.get("content-encoding")` in `head()`. Keep the existing `sensitivity`/`retention-class` keys unchanged.

- [ ] **Step 4: Run test to verify it passes** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/store/objectstore.py src/kdive/artifacts/storage.py tests/store/test_objectstore_content_encoding.py
git commit -m "feat(892): record content_encoding object metadata + head read"
```

---

### Task 2: `artifacts.get` decompress-on-read (metadata-driven)

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/reads.py` (`_artifact_content`, ~lines 290-328)
- Test: `tests/mcp/tools/catalog/artifacts/test_reads_gzip.py`

**Interfaces:**
- Consumes: Task 1's `head().content_encoding`.
- Produces: `_artifact_content` inflates the fetched object bytes when `head.content_encoding == "gzip"`, before windowing; a non-gzip object is unchanged.

- [ ] **Step 1: Write the failing test** — store a gzip-compressed REDACTED object whose plaintext is `b"hello world\n" * 1000`, with `content_encoding=gzip`; assert `artifacts_get` returns inflated plaintext in `data["content"]` windowed correctly and `next_offset` reflects the *plaintext* length, not the compressed length. Add a second test: a non-gzip artifact reads byte-identically (regression guard).

```python
def test_artifacts_get_inflates_gzip_part(...):
    plaintext = b"hello world\n" * 1000
    # store gzip.compress(plaintext) at a REDACTED row with content_encoding=gzip
    resp = await artifacts_get(pool, ctx, artifact_id=row_id, byte_offset=0, max_bytes=64)
    assert resp.structured_content["data"]["content"] == plaintext[:64].decode()
    assert resp.structured_content["data"]["content_truncated"] is True
    assert resp.structured_content["data"]["next_offset"] == 64  # plaintext offset
```

- [ ] **Step 2: Run to verify it fails** — `uv run python -m pytest tests/mcp/tools/catalog/artifacts/test_reads_gzip.py -q` → FAIL (content is raw gzip bytes / wrong size).

- [ ] **Step 3: Implement** — in `_artifact_content`, after the `head` REDACTED gate and the `get_artifact` fetch, if `head.content_encoding == "gzip"` set `fetched_bytes = gzip.decompress(fetched.data)` and compute `size_bytes`/windowing against the inflated bytes (the `download_uri` still serves the compressed object — document that in the docstring). Detection is strictly `head.content_encoding`, never the key. Guard `gzip.BadGzipFile` → degrade to `content_unavailable="decode_error"` (best-effort, never raise).

- [ ] **Step 4: Run to verify it passes** — same command → PASS; also run `uv run python -m pytest tests/mcp/tools/catalog/artifacts -q` to confirm non-gzip reads unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/artifacts/reads.py tests/mcp/tools/catalog/artifacts/test_reads_gzip.py
git commit -m "feat(892): artifacts.get inflates gzip parts via content_encoding"
```

---

### Task 3: `JobKind.CONSOLE_ROTATE` + migration 0053

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py` (add `CONSOLE_ROTATE = "console_rotate"` to `JobKind`)
- Create: `src/kdive/db/schema/0053_console_rotate_job_kind.sql`
- Test: `tests/db/test_migration_0053_console_rotate.py`

**Interfaces:**
- Produces: `JobKind.CONSOLE_ROTATE`; the `jobs_kind_check` constraint admits `'console_rotate'`.

- [ ] **Step 1: Write the failing test** — assert a `jobs` insert with `kind='console_rotate'` is accepted after migration (and that `JobKind.CONSOLE_ROTATE.value == "console_rotate"`). Pattern after the existing job-kind migration tests.

- [ ] **Step 2: Run to verify it fails** — `uv run python -m pytest tests/db/test_migration_0053_console_rotate.py -q` (needs Docker/`KDIVE_REQUIRE_DOCKER=1`) → FAIL (constraint rejects).

- [ ] **Step 3: Implement** — copy the shape of `src/kdive/db/schema/0052_authorize_ssh_key_job_kind.sql`: `ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;` then re-add it `CHECK (kind IN (...existing..., 'console_rotate'))`. Header comment: "Additive to 0051/0052 (forward-only, ADR-0015). Widens jobs.kind for the internal console_rotate job (ADR-0273)." Add the enum member.

- [ ] **Step 4: Run to verify it passes** — same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/operations/jobs.py src/kdive/db/schema/0053_console_rotate_job_kind.sql tests/db/test_migration_0053_console_rotate.py
git commit -m "feat(892): add console_rotate job kind + migration 0053"
```

---

### Task 4: Pure rotation core — offset-derived parts, seam re-read, boot-id (THE load-bearing unit)

**Files:**
- Create: `src/kdive/providers/console_parts/rotation.py`
- Create: `tests/providers/console_parts/test_rotation.py`

This task holds the correctness the adversarial review hardened. Implement it as **pure functions** over inputs (sidecar state, the full plaintext file bytes, a `boot_id`, a `redact` callable) returning the parts to seal and the next sidecar state — no I/O, so it is exhaustively unit-testable.

**Interfaces:**
- Produces:
  ```python
  SEAM_OVERLAP: int            # >= the collector's seam window; e.g. 4096
  ROTATION_THRESHOLD: int      # = DEFAULT_ROTATION_THRESHOLD (re-exported)

  @dataclass(frozen=True, slots=True)
  class RotationState:
      plaintext_offset: int
      boot_gen: int
      boot_id: str | None

  @dataclass(frozen=True, slots=True)
  class SealedPart:
      gen: int
      start: int            # plaintext start offset (key component)
      redacted: bytes       # <= ROTATION_THRESHOLD

  @dataclass(frozen=True, slots=True)
  class RotationResult:
      parts: list[SealedPart]
      next_state: RotationState

  def part_object_name(gen: int, start: int) -> str   # "console-part-<gen>-<start:012d>"

  def rotate(
      state: RotationState, file_bytes: bytes, boot_id: str,
      redact: Callable[[str], str],
  ) -> RotationResult
  ```

- [ ] **Step 1: Write the failing tests** (behavior + edges):

```python
# tests/providers/console_parts/test_rotation.py
def _ident(s): return s  # redaction stub for non-secret tests

def test_seals_full_threshold_parts_keyed_by_plaintext_offset():
    data = b"A" * (ROTATION_THRESHOLD * 2 + 10)
    r = rotate(RotationState(0, 0, "id1"), data, "id1", _ident)
    assert [(p.gen, p.start, len(p.redacted)) for p in r.parts] == [
        (0, 0, ROTATION_THRESHOLD), (0, ROTATION_THRESHOLD, ROTATION_THRESHOLD)]
    assert r.next_state.plaintext_offset == ROTATION_THRESHOLD * 2  # 10-byte tail unsealed
    assert part_object_name(0, ROTATION_THRESHOLD) == f"console-part-0-{ROTATION_THRESHOLD:012d}"

def test_no_new_bytes_yields_no_parts_and_same_offset():
    data = b"B" * ROTATION_THRESHOLD
    r1 = rotate(RotationState(0, 0, "id1"), data, "id1", _ident)
    r2 = rotate(r1.next_state, data, "id1", _ident)
    assert r2.parts == [] and r2.next_state.plaintext_offset == ROTATION_THRESHOLD

def test_retry_same_delta_produces_same_keys_idempotent():
    data = b"C" * (ROTATION_THRESHOLD + 5)
    first = rotate(RotationState(0, 0, "id1"), data, "id1", _ident)
    # simulate crash before sidecar write: re-run from the SAME (un-advanced) state
    retry = rotate(RotationState(0, 0, "id1"), data, "id1", _ident)
    assert [(p.gen, p.start) for p in first.parts] == [(p.gen, p.start) for p in retry.parts]

def test_boot_id_change_resets_offset_and_bumps_generation():
    # offset already advanced into a prior boot; new boot_id, file is the NEW (short) boot
    new = rotate(RotationState(ROTATION_THRESHOLD * 3, 0, "old"), b"D" * 10, "new", _ident)
    assert new.next_state.boot_gen == 1 and new.next_state.boot_id == "new"
    assert new.next_state.plaintext_offset in (0, 10)  # started fresh at gen 1
    assert all(p.gen == 1 for p in new.parts)

def test_truncate_regrow_past_old_offset_detected_via_boot_id():
    # file already grew past old offset, size-only check would miss it; boot_id catches it
    big_new = b"E" * (ROTATION_THRESHOLD * 4)
    r = rotate(RotationState(ROTATION_THRESHOLD * 2, 0, "old"), big_new, "new", _ident)
    assert r.next_state.boot_gen == 1
    assert r.parts[0].start == 0  # new boot's early console captured, not skipped

def test_seam_overlap_reread_redacts_marker_split_across_job_boundary():
    sensitive = "ZZ-REDACT-ME-MARKER-ZZ"  # stands in for a registered secret value
    # first job consumes up to a point that splits the marker across the offset
    full = b"x" * (ROTATION_THRESHOLD - 5) + sensitive.encode() + b"y" * 200
    redact = lambda s: s.replace(sensitive, "[REDACTED]")
    first = rotate(RotationState(0, 0, "id1"), full, "id1", redact)
    second = rotate(first.next_state, full, "id1", redact)
    joined = b"".join(p.redacted for p in first.parts + second.parts)
    assert sensitive.encode() not in joined  # never stored raw on either side of the seam

def test_advance_is_plaintext_not_redacted_size():
    # redaction shrinks bytes; offset must advance by plaintext consumed
    marker = "ZZREDACTMEZZ"
    data = marker.encode() * 6000  # > threshold of plaintext
    redact = lambda s: s.replace(marker, "[R]")
    r = rotate(RotationState(0, 0, "id1"), data, "id1", redact)
    # offset advanced by plaintext bytes sealed (multiple of threshold), not the shorter redacted len
    assert r.next_state.plaintext_offset % ROTATION_THRESHOLD == 0
    assert r.next_state.plaintext_offset > sum(len(p.redacted) for p in r.parts)
```

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/providers/console_parts/test_rotation.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `rotation.py`.** Algorithm (pure, no I/O):
  1. If `boot_id != state.boot_id` **or** `len(file_bytes) < state.plaintext_offset`: new generation — `gen = state.boot_gen + 1` (or `state.boot_gen` when `state.boot_id is None`, the first-ever run keeps gen 0), reset `offset = 0`.
  2. Read window: `win_start = max(0, offset - SEAM_OVERLAP)`; `window = file_bytes[win_start:]`. Redact `window.decode("utf-8","replace")` → encode back. Map the redacted bytes that correspond to plaintext `>= offset` by redacting the prefix `file_bytes[win_start:offset]` separately to compute how many redacted bytes to drop (the overlap is redaction context only). **Simpler, exact approach:** redact `file_bytes[win_start:]` and `file_bytes[win_start:offset]` separately; the emitted bytes are `redact(window)` with the `len(redact(prefix))`-byte prefix removed. (This keeps the seam join intact while never re-emitting consumed bytes.)
  3. Slice the eligible redacted bytes into `ROTATION_THRESHOLD`-sized parts; the final sub-threshold remainder is **not** sealed (tail). Each sealed part's `start` is its plaintext start offset (track plaintext consumed alongside emitted redacted bytes — seal a part each time `THRESHOLD` *plaintext* bytes are consumed, redacting that plaintext slice; this makes `start` exact and `advance` plaintext-based, satisfying `test_advance_is_plaintext_not_redacted_size`). Prefer this plaintext-sliced formulation over redacted-byte slicing.
  4. `next_state = RotationState(offset + plaintext_sealed, gen, boot_id)`.
  Keep `rotate` ≤100 lines / complexity ≤8 by extracting `_detect_new_boot` and `_seal_plaintext_slices` helpers.

- [ ] **Step 4: Run to verify they pass** — same command → PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/console_parts/rotation.py tests/providers/console_parts/test_rotation.py
git commit -m "feat(892): pure console-rotation core (offset keys, seam, boot-id)"
```

---

### Task 5: Rotation sidecar (object-store read/write)

**Files:**
- Create: `src/kdive/providers/console_parts/sidecar.py`
- Test: `tests/providers/console_parts/test_sidecar.py`

**Interfaces:**
- Consumes: `RotationState` (Task 4), the object store.
- Produces:
  ```python
  def sidecar_object_name() -> str            # "console-rotation-state.json"
  def read_sidecar(store, tenant, system_id) -> RotationState   # absent → RotationState(0, 0, None)
  def write_sidecar(store, tenant, system_id, state: RotationState) -> None
  ```
  Stored as a small JSON object (sensitivity `REDACTED` — it holds no console bytes, only ints + an opaque id; mark REDACTED so it never trips the redaction gate if ever listed, but it is **not** registered as an `artifacts` row).

- [ ] **Step 1: Write the failing test** — write a `RotationState`, read it back equal; an absent sidecar reads `RotationState(0, 0, None)`; a corrupt JSON body reads the zero state (best-effort, never raises).

- [ ] **Step 2: Run to verify it fails** (needs Docker/MinIO).

- [ ] **Step 3: Implement** — `json.dumps`/`loads` of `{plaintext_offset, boot_gen, boot_id}`; put at the System's owner-prefixed key; `read` catches `CategorizedError`/`json`/`KeyError` → zero state.

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/console_parts/sidecar.py tests/providers/console_parts/test_sidecar.py
git commit -m "feat(892): object-store rotation-state sidecar"
```

---

### Task 6: `console_rotate` worker job handler (local)

**Files:**
- Create: `src/kdive/jobs/handlers/console_rotate.py`
- Modify: `src/kdive/mcp/worker_registration.py` (register the handler for `JobKind.CONSOLE_ROTATE`)
- Test: `tests/jobs/handlers/test_console_rotate.py`

**Interfaces:**
- Consumes: Task 4 `rotate`, Task 5 sidecar, `read_console_log`/`console_log_path` (`providers/shared/runtime_paths.py`), `Redactor(registry=...)`, `register_artifact_row`, `advisory_xact_lock` (`db/locks.py`), Task 1 `content_encoding`.
- Produces: an async handler `async def handle_console_rotate(job, *, pool, secret_registry, artifact_store) -> None` registered in the worker registry.

- [ ] **Step 1: Write the failing tests** (drive the handler directly with injected deps, the project's boundary):
  - Growing console file → after the job, the System has new `console-part-0-<start>` REDACTED artifact rows with `content_encoding=gzip` objects whose inflated bytes equal the redacted plaintext slices; the sidecar offset advanced.
  - **Idempotent retry:** run the handler twice against the same file with the sidecar reset to the pre-run state between runs (simulating a crash before sidecar write) → no duplicate rows (same keys, insert-if-absent), no second copy of content.
  - **Best-effort:** `read_console_log` raising `CONFIGURATION_ERROR` (permission wall) → handler returns without raising and registers no parts.
  - **boot-id reset:** changing the boot identity → parts appear under `console-part-1-…`.

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/jobs/handlers/test_console_rotate.py -q` → FAIL.

- [ ] **Step 3: Implement.** The handler, holding the per-System advisory lock via `advisory_xact_lock(conn, scope="system", key=system_id)`:
  1. `state = read_sidecar(store, tenant, system_id)`.
  2. Read `file_bytes` via `asyncio.to_thread(read_console_log, console_log_path(system_id))` inside `try/except CategorizedError` → on raise: log once, return.
  3. Compute `boot_id` (Task 7 supplies the source; for the unit test inject it). `result = rotate(state, file_bytes, boot_id, Redactor(registry=secret_registry).redact_text)`.
  4. For each `SealedPart`: `gzip.compress(part.redacted)` → `put_artifact(ArtifactWriteRequest(..., name=part_object_name(gen,start), sensitivity=REDACTED, retention_class="evidence", content_encoding="gzip"))`; then **insert-if-absent** the `register_artifact_row(stored, owner_kind="systems", owner_id=system_id)` row (skip when a row already exists for the object key — reuse the `_existing_console_row` pattern from `boot_evidence.py`). Commit the rows.
  5. After the rows commit, `write_sidecar(store, tenant, system_id, result.next_state)`.
  Register in `worker_registration.py` next to the other run/system handlers, passing `secret_registry` + `optional_upload_store`.

- [ ] **Step 4: Run to verify they pass**; then `uv run python -m pytest tests/jobs -q`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/handlers/console_rotate.py src/kdive/mcp/worker_registration.py tests/jobs/handlers/test_console_rotate.py
git commit -m "feat(892): console_rotate worker handler (local rotation)"
```

---

### Task 7: Reconciler dispatch of `console_rotate` for live local Systems

**Files:**
- Create: `src/kdive/reconciler/repairs/console_rotation.py`
- Modify: `src/kdive/reconciler/loop.py` (add the sweep to the repair catalog ~lines 110-135, 388-392)
- Test: `tests/reconciler/test_console_rotation_sweep.py`

**Interfaces:**
- Consumes: the reconciler conn; the `systems`/`jobs` repositories.
- Produces: `async def sweep_console_rotation(report, conn, _guard) -> int` matching the repair-catalog callable shape; enqueues one `console_rotate` job per **live local-libvirt** System (`ready`/booted, not torn down, provider local-libvirt) that has **no** pending/running `console_rotate` job (dedup ≤1 in flight). Defines the `boot_id` source (e.g. the libvirt domain's start identity / a stat of the console file's inode+mtime) and stamps it into the job payload.

- [ ] **Step 1: Write the failing tests:**
  - Two live local Systems with no in-flight rotation → two `console_rotate` jobs enqueued.
  - A System with a pending `console_rotate` job → **no** second job (dedup).
  - A System whose most recent Run is `succeeded` but the System is still `ready` → **still** enqueued (the #892 case; liveness keyed on System, not Run).
  - A remote-libvirt System or a torn-down System → no job.

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/reconciler/test_console_rotation_sweep.py -q` → FAIL.

- [ ] **Step 3: Implement** the sweep; select live local Systems, left-anti-join in-flight `console_rotate` jobs, enqueue. Add it to the repair catalog so `reconcile_once` runs it. Keep the enqueue idempotent under the sweep's own re-run.

- [ ] **Step 4: Run to verify they pass**; then `uv run python -m pytest tests/reconciler -q`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/reconciler/repairs/console_rotation.py src/kdive/reconciler/loop.py tests/reconciler/test_console_rotation_sweep.py
git commit -m "feat(892): reconciler dispatches console_rotate for live local systems"
```

---

### Task 8: Remote-libvirt dual-write of a compressed part artifact

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/console/collector.py` (the seal/`put_part` path)
- Modify: `src/kdive/providers/remote_libvirt/console/wiring.py` (DB conn + store wiring for the registered copy)
- Test: `tests/providers/remote_libvirt/console/test_collector_part_artifact.py`

**Interfaces:**
- Consumes: Task 1 `content_encoding`, Task 4 `part_object_name` (gen `0` for remote), `register_artifact_row`.
- Produces: on each sealed part, a separate gzip-compressed `console-part-0-<start>` artifact + row; the internal `console-parts-<n>` object and `finalize()` assembly are unchanged.

- [ ] **Step 1: Write the failing tests:**
  - On seal, a `console-part-0-<start>` REDACTED artifact row exists whose inflated object equals the part's redacted bytes.
  - **Regression guard:** `finalize()` still concatenates the internal `console-parts-<n>` objects raw and the assembled `console-<run>` evidence is byte-identical to the pre-change behavior (assert against a fixture of the existing assembly).

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/providers/remote_libvirt/console -q` → FAIL.

- [ ] **Step 3: Implement** — at the seal point, after `put_part` (internal, unchanged), additionally `put_artifact` the gzip-compressed copy with `content_encoding=gzip` and register its row on the reconciler conn. `<start>` is the byte offset in the assembled stream (track the running total of part lengths). Do **not** modify `put_part`/`read_part`/`finalize`.

- [ ] **Step 4: Run to verify they pass**; then `uv run python -m pytest tests/providers/remote_libvirt -q`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/remote_libvirt/console/collector.py src/kdive/providers/remote_libvirt/console/wiring.py tests/providers/remote_libvirt/console/test_collector_part_artifact.py
git commit -m "feat(892): remote collector registers separate compressed part artifact"
```

---

### Task 9: `live_vm` end-to-end proof (gated, operator-run)

**Files:**
- Create: `tests/integration/test_console_parts_live.py` (marked `live_vm`)

**Interfaces:** drives a real local-libvirt System with a post-readiness workload.

- [ ] **Step 1: Write the gated test** — `@pytest.mark.live_vm`: provision a local System, start a workload that emits >64 KiB of console after `kdive-ready` while the Run is `succeeded`; trigger reconciler sweeps; assert `artifacts.list(system_id)` grows new `console-part-<gen>-<start>` rows and `artifacts.get` on the newest shows a line emitted after the `kdive-ready` marker that the frozen `console-<run>` evidence does not contain. **Do not** un-gate or weaken the `live_vm` marker.

- [ ] **Step 2: Confirm it is collected but skipped by default** — `uv run python -m pytest tests/integration/test_console_parts_live.py -q` → SKIPPED (no `live_vm`); `just test` excludes it. Run live separately on the KVM host (`just test-live`) and record the result in the PR body.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_console_parts_live.py
git commit -m "test(892): live_vm proof for post-readiness console parts"
```

---

### Task 10: Flip ADR-0273 to Accepted (citation/ratification commit)

**Files:**
- Modify: `docs/adr/0273-observe-rotating-console-parts.md` (Status: Accepted)
- Modify: `docs/adr/README.md` (row status Proposed → Accepted)

**Interfaces:** none (ratification).

- [ ] **Step 1: Verify src cites ADR-0273** — the implementing modules (Task 4-8 docstrings) cite `ADR-0273`; `python3 scripts/check_adr_status.py` would now FAIL on shipped-but-Proposed drift. Confirm: `python3 scripts/check_adr_status.py` → currently failing (or will once citations land).

- [ ] **Step 2: Flip status** — set ADR `Status: Accepted` and the README row to `Accepted`.

- [ ] **Step 3: Verify the guard passes** — `python3 scripts/check_adr_status.py` → "no shipped-but-Proposed drift".

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0273-observe-rotating-console-parts.md docs/adr/README.md
git commit -m "docs(892): ratify ADR-0273 (Accepted)"
```

---

## Self-Review

**Spec coverage:** R1 (offset-derived key) → Task 4; R2 (64 KiB) → Task 4; R3 (redaction + seam re-read) → Task 4/6; R4 (decompress-on-read metadata) → Task 1/2; R5 (remote separate compressed artifact) → Task 8; R6/R6a/R6c (delta read, sidecar, lock+idempotency) → Task 4/5/6; R6b (boot-id power-cycle) → Task 4/7; R7 (best-effort catch) → Task 6; R8/R8a (observation surface, per-artifact search caveat) → existing tools (no code) + verified in Task 9; R9 (per-Run evidence untouched) → Task 8 regression test; R10 (migration 0053) → Task 3. All requirements map to a task.

**Placeholder scan:** the only deliberately deferred detail is the concrete `boot_id` source, which Task 7 must choose (libvirt domain start identity or console-file inode+mtime) and Task 4 consumes as an opaque string — flagged, not a silent TODO. No "handle edge cases"/"TBD" steps.

**Type consistency:** `RotationState`/`SealedPart`/`RotationResult`/`part_object_name`/`rotate` (Task 4) are consumed unchanged by Tasks 5/6/8; `content_encoding` (Task 1) is consumed by Tasks 2/6/8; `JobKind.CONSOLE_ROTATE` (Task 3) by Tasks 6/7.

**Ordering:** Tasks 1→2 (metadata before reader), 3 before 6/7 (job kind before handler/dispatch), 4/5 before 6 (core before handler), 6 before 7 (handler before dispatch), 8 independent of 6/7 (remote), 9 after 6/7, 10 last (ratify after citations land). Each task is independently testable and commits green.
