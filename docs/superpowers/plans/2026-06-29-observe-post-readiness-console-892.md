# Post-readiness console observation (rotating part artifacts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the live console after boot readiness as append-only, redacted, gzip-compressed, ~64 KiB System-owned console *part* artifacts read through the existing `artifacts.{list,get,search_text}` surface — no new MCP tool.

**Architecture:** Each sealed part is a `REDACTED` System-owned `artifacts` row keyed `console-part-<gen>-<index>` (boot generation; zero-padded monotonic per-gen index). Local-libvirt capture is a reconciler-dispatched `console_rotate` worker job that reads the plaintext delta of the worker-host console file past a sidecar-tracked offset and feeds it through the collector's seam-overlap carry primitive (a held-back raw overlap emitted redacted with the next part), under a per-System advisory lock; idempotency comes from the sidecar (a crash-retry reproduces the same `(gen, index)` keys, registered insert-if-absent). Remote-libvirt's collector additionally writes a separate compressed part artifact per sealed part. `artifacts.get` inflates parts whose object metadata says `content_encoding=gzip`.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; Postgres (psycopg async), S3-compatible object store (boto3), libvirt; gzip (stdlib).

## Global Constraints

- Spec: `docs/specs/2026-06-29-observe-post-readiness-console-892.md`. ADR: `docs/adr/0273-observe-rotating-console-parts.md` (opens **Proposed**; flip to **Accepted** in the final task that cites it in `src/`, per `scripts/check_adr_status.py`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, **whole-tree** (`just type` covers `src` + `tests`).
- Absolute imports only (no relative). Google-style docstrings on non-trivial public APIs. Functions ≤100 lines, cyclomatic ≤8, ≤5 positional params.
- Per-commit guardrails (the CI-individual gates): `just lint`, `just type`, then the focused tests; before the first push run the full `just ci`.
- **Backend prerequisite (do this before Task 1):** the DB/store tests need disposable Postgres + MinIO. Bring them up with `just compose-up` and run those tests with `KDIVE_REQUIRE_DOCKER=1` so a missing backend **fails loudly** instead of skipping (a vacuous skip would let a broken store path merge green). Tasks 1, 3, 5, 6, 7, 8 contain such tests.
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

### Task 4: Pure rotation core — collector carry primitive, index keys, boot-id (THE load-bearing unit)

**Files:**
- Create: `src/kdive/providers/console_parts/rotation.py`
- Create: `tests/providers/console_parts/test_rotation.py`

This task holds the correctness the adversarial review hardened. **Do not hand-roll the seam
arithmetic** — extract the collector's proven carry mechanism (`_rotate`/`_carry`/`_redact`,
`src/kdive/providers/remote_libvirt/console/collector.py:213-241`) into a shared, pure primitive and
reuse it here (and in Task 8). The collector holds back the last `SEAM_OVERLAP` **raw** bytes in a
carry and emits them (redacted) prepended to the **next** part — so a secret straddling any boundary is
redacted contiguously and the overlap is emitted exactly once. There is **no** "redact a prefix and
drop its length" subtraction (that misaligns when a secret straddles the boundary). The local job is
stateless across invocations, so the carry is persisted in the sidecar (Task 5).

**Interfaces:**
- Produces:
  ```python
  SEAM_OVERLAP: int            # = the collector's seam window (its _seam_overlap default)
  ROTATION_THRESHOLD: int      # = DEFAULT_ROTATION_THRESHOLD (re-exported)

  @dataclass(frozen=True, slots=True)
  class RotationState:
      plaintext_offset: int     # plaintext bytes consumed from the file so far
      carry: bytes              # raw held-back overlap (<= ROTATION_THRESHOLD), emitted with next part
      next_index: int           # monotonic per-gen part index (key component; derived from prior runs)
      boot_gen: int
      boot_id: str | None

  @dataclass(frozen=True, slots=True)
  class SealedPart:
      gen: int
      index: int            # monotonic within the generation (key component)
      redacted: bytes       # redact(carry + raw_chunk); a contiguous redaction, never split

  @dataclass(frozen=True, slots=True)
  class RotationResult:
      parts: list[SealedPart]
      next_state: RotationState

  def part_object_name(gen: int, index: int) -> str   # "console-part-<gen>-<index:06d>"

  # Mirrors collector._rotate over a re-readable file delta. Pure: no I/O.
  def rotate(
      state: RotationState, file_bytes: bytes, boot_id: str,
      redact: Callable[[bytes], bytes],
  ) -> RotationResult
  ```
  Keying by a monotonic per-gen `index` (carried in the sidecar) — not a plaintext byte offset — because
  the carry mechanism means a part's logical start is not a clean file offset. Idempotency comes from the
  sidecar: a crash before the sidecar write leaves `plaintext_offset`/`next_index`/`carry` unchanged, so
  the retry reproduces the identical parts and re-`put`s the same `(gen, index)` keys (insert-if-absent,
  Task 6) — a no-op.

- [ ] **Step 1: Write the failing tests** (behavior + edges):

```python
# tests/providers/console_parts/test_rotation.py
_ident = lambda b: b  # redaction stub (bytes->bytes) for non-secret tests
S0 = RotationState(plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None)

def test_seals_full_threshold_parts_indexed_monotonically():
    data = b"A" * (ROTATION_THRESHOLD * 2 + 10)
    r = rotate(S0, data, "id1", _ident)
    assert [(p.gen, p.index) for p in r.parts] == [(0, 0), (0, 1)]
    assert r.next_state.next_index == 2
    # the trailing < threshold remainder (plus the held-back overlap) is carried, not sealed
    assert len(r.next_state.carry) >= 10
    assert part_object_name(0, 1) == "console-part-0-000001"

def test_secret_split_across_internal_part_boundary_is_redacted():
    # a secret straddling the boundary BETWEEN two threshold parts within one rotate() call
    sensitive = b"ZZ-INTERNAL-BOUNDARY-MARKER-ZZ"
    data = b"a" * (ROTATION_THRESHOLD - 7) + sensitive + b"b" * ROTATION_THRESHOLD
    redact = lambda b: b.replace(sensitive, b"[REDACTED]")
    r = rotate(S0, data, "id1", redact)
    joined = b"".join(p.redacted for p in r.parts)
    assert sensitive not in joined and sensitive not in r.next_state.carry

def test_no_new_bytes_yields_no_parts_and_same_state():
    data = b"B" * ROTATION_THRESHOLD
    r1 = rotate(S0, data, "id1", _ident)
    r2 = rotate(r1.next_state, data, "id1", _ident)
    assert r2.parts == [] and r2.next_state.next_index == r1.next_state.next_index

def test_retry_same_delta_produces_same_keys_idempotent():
    data = b"C" * (ROTATION_THRESHOLD * 2 + 5)
    first = rotate(S0, data, "id1", _ident)
    # crash before sidecar write: re-run from the SAME (un-advanced) state
    retry = rotate(S0, data, "id1", _ident)
    assert [(p.gen, p.index) for p in first.parts] == [(p.gen, p.index) for p in retry.parts]

def test_boot_id_change_resets_and_bumps_generation():
    prior = RotationState(ROTATION_THRESHOLD * 3, b"leftover", 5, 0, "old")
    new = rotate(prior, b"D" * (ROTATION_THRESHOLD + 4), "new", _ident)
    assert new.next_state.boot_gen == 1 and new.next_state.boot_id == "new"
    assert new.next_state.next_index >= 1 and all(p.gen == 1 for p in new.parts)
    assert new.parts[0].index == 0  # new generation re-indexes from 0

def test_truncate_regrow_past_old_offset_detected_via_boot_id():
    # file already grew past old offset; size-only check would miss it, boot_id catches it
    prior = RotationState(ROTATION_THRESHOLD * 2, b"", 9, 0, "old")
    r = rotate(prior, b"E" * (ROTATION_THRESHOLD * 4), "new", _ident)
    assert r.next_state.boot_gen == 1 and r.parts[0].index == 0  # new boot's early console captured

def test_secret_split_across_job_boundary_is_redacted():
    sensitive = b"ZZ-REDACT-ME-MARKER-ZZ"
    full = b"x" * (ROTATION_THRESHOLD - 5) + sensitive + b"y" * (ROTATION_THRESHOLD)
    redact = lambda b: b.replace(sensitive, b"[REDACTED]")
    first = rotate(S0, full, "id1", redact)            # job 1 holds back the straddling region in carry
    second = rotate(first.next_state, full, "id1", redact)  # job 2 emits it, redacted
    joined = b"".join(p.redacted for p in first.parts + second.parts)
    assert sensitive not in joined  # never stored raw on either side of the seam
```

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/providers/console_parts/test_rotation.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `rotation.py` by extracting the collector's carry mechanism.** Lift
  `collector._rotate`/`_flush_tail`'s carry logic (`collector.py:213-241`) into a pure helper here and
  reuse it; **do not** invent prefix-subtraction arithmetic. Algorithm (pure, no I/O):
  1. **Detect new boot** (`_detect_new_boot`): if `boot_id != state.boot_id` **or**
     `len(file_bytes) < state.plaintext_offset` → fresh generation: `gen = (state.boot_gen + 1) if
     state.boot_id is not None else state.boot_gen`, `offset = 0`, `carry = b""`, `index = 0`. Else
     carry forward `gen`/`offset`/`carry`/`index` from `state`.
  2. **Process the unconsumed delta with the carry primitive:** `pending = state.carry +
     file_bytes[offset:]` (raw). Repeatedly, while `len(pending) >= ROTATION_THRESHOLD`: take
     `chunk = pending[:ROTATION_THRESHOLD]`; mirror the collector — keep the last `SEAM_OVERLAP` raw
     bytes as the next carry and emit the rest: `split = ROTATION_THRESHOLD - SEAM_OVERLAP`;
     `SealedPart(gen, index, redacted=redact(chunk[:split]))`; the carried-forward `chunk[split:]` is
     prepended to the remaining `pending[ROTATION_THRESHOLD:]` for the next iteration; `index += 1`.
     (The held-back `SEAM_OVERLAP` raw bytes are emitted, redacted, with the NEXT part — exactly the
     collector's `_carry` behavior — so a secret straddling any boundary is redacted contiguously and
     never emitted raw. No prefix is dropped.)
  3. The final `pending` remainder (`< ROTATION_THRESHOLD`, includes the last carry) is **not** sealed;
     it becomes `next_state.carry` (re-read/re-derived from the file next job — persisting it in the
     sidecar lets the stateless job reproduce the in-memory carry the collector keeps).
  4. `next_state = RotationState(plaintext_offset=len(file_bytes), carry=<remainder>, next_index=index,
     boot_gen=gen, boot_id=boot_id)`. `plaintext_offset` advances to the file end; the unemitted tail
     lives in `carry`, so nothing is double-read and the work per job is bounded by the new delta.
  Keep `rotate` ≤100 lines / complexity ≤8 via `_detect_new_boot` and a `_seal_with_carry` helper that
  is the lifted collector logic.

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
  ZERO = RotationState(plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None)
  def sidecar_object_name() -> str            # "console-rotation-state.json"
  def read_sidecar(store, tenant, system_id) -> RotationState   # absent/corrupt → ZERO
  def write_sidecar(store, tenant, system_id, state: RotationState) -> None
  ```
  Stored as a small JSON object (sensitivity `REDACTED` — the `carry` it holds is **already redacted**? No: `carry` is the raw held-back tail, which may contain an unredacted partial secret, so the sidecar object itself must be treated as sensitive. Store it with sensitivity `REDACTED` only after confirming carry never contains raw secret bytes — since `carry` IS raw, store the sidecar as a non-listed internal object and **base64-encode `carry`** in the JSON; do **not** register it as an `artifacts` row and ensure it is never returned by `artifacts.get`/`list` (it is not an artifacts row, so it is not). Its retention follows the System lifetime, removed at teardown, Task 9).

- [ ] **Step 1: Write the failing test** — write a `RotationState` with a non-empty `carry`, read it back equal; an absent sidecar reads `ZERO`; a corrupt JSON body reads `ZERO` (best-effort, never raises).

- [ ] **Step 2: Run to verify it fails** (needs Docker/MinIO).

- [ ] **Step 3: Implement** — `json.dumps`/`loads` of `{plaintext_offset, carry: base64(bytes), next_index, boot_gen, boot_id}`; put at the System's owner-prefixed sidecar key; `read` catches `CategorizedError`/`json`/`KeyError`/`binascii.Error` → `ZERO`.

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
- Produces: an async handler `async def handle_console_rotate(job, *, pool, secret_registry, artifact_store) -> None` registered in the worker registry. **Job-payload contract (defined here, populated by Task 7):** `job.payload["system_id"]: str` and `job.payload["boot_id"]: str` — the handler reads `boot_id` from the payload (it does not compute it; the reconciler stamps the boot-identity signal so the worker need not introspect libvirt). A missing/empty `boot_id` is treated as `""` (forces a generation reset on first sight, safe).

- [ ] **Step 1: Write the failing tests** (drive the handler directly with injected deps, the project's boundary):
  - Growing console file → after the job, the System has new `console-part-0-<index>` REDACTED artifact rows with `content_encoding=gzip` objects whose inflated bytes equal the redacted slices; the sidecar `plaintext_offset`/`next_index` advanced.
  - **Idempotent retry:** run the handler twice against the same file with the sidecar reset to the pre-run state between runs (simulating a crash before sidecar write) → no duplicate rows (same keys, insert-if-absent), no second copy of content.
  - **Best-effort:** `read_console_log` raising `CONFIGURATION_ERROR` (permission wall) → handler returns without raising and registers no parts.
  - **boot-id reset:** changing the boot identity → parts appear under `console-part-1-…`.

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/jobs/handlers/test_console_rotate.py -q` → FAIL.

- [ ] **Step 3: Implement.** The handler, holding the per-System advisory lock via `advisory_xact_lock(conn, scope="system", key=system_id)`:
  1. `state = read_sidecar(store, tenant, system_id)`.
  2. Read `file_bytes` via `asyncio.to_thread(read_console_log, console_log_path(system_id))` inside `try/except CategorizedError` → on raise: log once, return.
  3. Read `boot_id = job.payload.get("boot_id", "")` (the reconciler stamped it, per the payload contract above). `result = rotate(state, file_bytes, boot_id, Redactor(registry=secret_registry).redact_text)`.
  4. For each `SealedPart`: `gzip.compress(part.redacted)` → `put_artifact(ArtifactWriteRequest(..., name=part_object_name(part.gen, part.index), sensitivity=REDACTED, retention_class="evidence", content_encoding="gzip"))`; then **insert-if-absent** the `register_artifact_row(stored, owner_kind="systems", owner_id=system_id)` row (skip when a row already exists for the object key — reuse the `_existing_console_row` pattern from `boot_evidence.py`). Commit the rows.
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
- Produces: `async def sweep_console_rotation(report, conn, _guard) -> int` matching the repair-catalog callable shape; enqueues one `console_rotate` job per **live local-libvirt** System (`ready`/booted, not torn down, provider local-libvirt) that has **no** pending/running `console_rotate` job (best-effort dedup — see note). Stamps `job.payload = {"system_id": ..., "boot_id": ...}` per Task 6's contract. The `boot_id` source is a per-boot signal independent of console size — use the console file's `os.stat` identity `f"{st_dev}:{st_ino}:{int(st_mtime)}"` (changes when libvirt truncates/recreates the log on power-cycle); a libvirt domain start-time is an acceptable alternative if exposed.

**Dedup is best-effort, not a guarantee.** Two concurrent reconciler passes can both observe no in-flight job and both enqueue. That is safe — Task 6's per-System advisory lock serializes execution and the sidecar-carried index keys (insert-if-absent) make a duplicate job a no-op — so correctness rests on Task 6, not on this dedup. The anti-join only reduces wasted duplicate jobs.

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
- Produces: on each sealed part, a separate gzip-compressed `console-part-0-<index>` artifact + row; the internal `console-parts-<n>` object and `finalize()` assembly are unchanged.

- [ ] **Step 1: Write the failing tests:**
  - On seal, a `console-part-0-<index>` REDACTED artifact row exists whose inflated object equals the part's redacted bytes.
  - **Regression guard:** `finalize()` still concatenates the internal `console-parts-<n>` objects raw and the assembled `console-<run>` evidence is byte-identical to the pre-change behavior (assert against a fixture of the existing assembly).

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/providers/remote_libvirt/console -q` → FAIL.

- [ ] **Step 3: Implement** — refactor the collector's `_rotate`/`_flush_tail` to call the shared seam primitive extracted in Task 4 (single source of truth for the carry logic), then at the seal point, after `put_part` (internal, unchanged), additionally `put_artifact` the gzip-compressed copy with `content_encoding=gzip` and register its row on the reconciler conn. `<index>` is the collector's existing monotonic part index (`_take_index`), gen `0` (remote has no power-cycle truncation). Do **not** modify `read_part`/`finalize`.

- [ ] **Step 4: Run to verify they pass**; then `uv run python -m pytest tests/providers/remote_libvirt -q`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/remote_libvirt/console/collector.py src/kdive/providers/remote_libvirt/console/wiring.py tests/providers/remote_libvirt/console/test_collector_part_artifact.py
git commit -m "feat(892): remote collector registers separate compressed part artifact"
```

---

### Task 9: Teardown/reprovision cleanup of console parts and the sidecar

**Files:**
- Modify: the local-libvirt teardown path that reclaims a System's host/object-store artifacts (locate via `rg -n "teardown" src/kdive/jobs/handlers/systems src/kdive/providers/local_libvirt` and the existing per-Run console/overlay reclaim) — extend it to delete the System's `console-part-*` objects+rows and the `console-rotation-state.json` sidecar.
- Test: `tests/jobs/handlers/test_console_rotate_teardown.py`

**Interfaces:**
- Consumes: Task 5 `sidecar_object_name`, Task 4 `part_object_name`, the System teardown handler.
- Produces: teardown removes all `console-part-<gen>-<index>` artifacts (objects + rows) and the sidecar for the System.

- [ ] **Step 1: Write the failing tests:**
  - After teardown of a System that had console parts, `artifacts.list(system_id)` returns no `console-part-*` rows and the sidecar object is absent.
  - **Reprovision starts fresh:** reusing the same `system_id`, the first rotation after reprovision begins a new series — verified by the `boot_id` change resetting offset/gen (a stale sidecar must not carry an old offset into the new boot). If teardown already removed the sidecar, this is automatic; assert no stale parts/sidecar survive a teardown→provision cycle.

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/jobs/handlers/test_console_rotate_teardown.py -q` → FAIL.

- [ ] **Step 3: Implement** — in the System teardown reclaim, enumerate and delete the System's `console-part-*` objects + artifact rows and the sidecar object. If the codebase already bulk-reclaims System-owned artifacts at teardown, confirm console parts are covered and add only the sidecar-object deletion; otherwise add the part reclaim. Keep it best-effort (a missing object is not an error).

- [ ] **Step 4: Run to verify they pass**; then `uv run python -m pytest tests/jobs -q`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/handlers/systems tests/jobs/handlers/test_console_rotate_teardown.py
git commit -m "feat(892): reclaim console parts + sidecar on teardown"
```

---

### Task 10: `live_vm` end-to-end proof (gated, operator-run)

**Files:**
- Create: `tests/integration/test_console_parts_live.py` (marked `live_vm`)

**Interfaces:** drives a real local-libvirt System with a post-readiness workload.

- [ ] **Step 1: Write the gated test** — `@pytest.mark.live_vm`: provision a local System, start a workload that emits >64 KiB of console after `kdive-ready` while the Run is `succeeded`; trigger reconciler sweeps; assert `artifacts.list(system_id)` grows new `console-part-<gen>-<index>` rows and `artifacts.get` on the newest shows a line emitted after the `kdive-ready` marker that the frozen `console-<run>` evidence does not contain. **Do not** un-gate or weaken the `live_vm` marker.

- [ ] **Step 2: Confirm it is collected but skipped by default** — `uv run python -m pytest tests/integration/test_console_parts_live.py -q` → SKIPPED (no `live_vm`); `just test` excludes it. Run live separately on the KVM host (`just test-live`) and record the result in the PR body.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_console_parts_live.py
git commit -m "test(892): live_vm proof for post-readiness console parts"
```

---

### Task 11: Flip ADR-0273 to Accepted (citation/ratification commit)

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

**Spec coverage:** R1 (sidecar-carried index key) → Task 4; R2 (per-part plaintext bound; redacted size may differ) → Task 4; R3 (redaction + seam overlap at every boundary) → Task 4/6; R4 (decompress-on-read metadata) → Task 1/2; R5 (remote separate compressed artifact) → Task 8; R6/R6a/R6c (delta read, sidecar, lock+idempotency) → Task 4/5/6; R6b (boot-id power-cycle) → Task 4/7; R7 (best-effort catch) → Task 6; R8/R8a (observation surface, per-artifact search caveat) → existing tools (no code) + verified in Task 10; R9 (per-Run evidence untouched) → Task 8 regression test; R10 (migration 0053) → Task 3; teardown/reprovision cleanup (R8a retention + no leak) → Task 9. All requirements map to a task.

**Placeholder scan:** `boot_id`'s concrete source is fixed in Task 7 (console-file `os.stat` identity) and carried in `job.payload["boot_id"]` per Task 6's contract; Task 4 consumes it as an opaque string. No "handle edge cases"/"TBD" steps.

**Type consistency:** `RotationState`(`plaintext_offset,carry,next_index,boot_gen,boot_id`)/`SealedPart`(`gen,index,redacted`)/`RotationResult`/`part_object_name(gen,index)`/`rotate` (Task 4) are consumed unchanged by Tasks 5/6/8/9; the seam-carry primitive extracted in Task 4 is reused by Task 8; `content_encoding` (Task 1) is consumed by Tasks 2/6/8; `JobKind.CONSOLE_ROTATE` (Task 3) by Tasks 6/7; `job.payload["boot_id"]` is defined in Task 6 and populated in Task 7.

**Ordering:** Tasks 1→2 (metadata before reader), 3 before 6/7 (job kind before handler/dispatch), 4/5 before 6 (core before handler), 6 before 7 (handler contract before dispatch populates it), 8 independent of 6/7 (remote), 9 after 6 (teardown reclaims what 6 creates), 10 after 6/7, 11 last (ratify after citations land). Each task is independently testable and commits green.
