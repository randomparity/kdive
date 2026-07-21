# Streaming combined-tar fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream the S3 combined kernel tar straight into the tar extractor so it is never fully materialized as `bytes` or written to disk before extraction (#1351).

**Architecture:** Add an additive streaming read to `ObjectStore` (`get_artifact_stream`) that shares `get_artifact`'s GET-setup / error-mapping / metadata-parse via a new private `_open_get` helper, and returns a `StreamedArtifact` whose `reader` (an `io.RawIOBase` wrapping the boto body) maps mid-stream transport faults to the same typed error. Switch `extract_kernel_bundle` from a `Path` opened `r:gz` to an `IO[bytes]` opened `r|gz` stream mode, preserving every scan bound. The install path gains a `stream_kernel` seam so the combined tar never lands on disk.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; boto3 S3 client; `tarfile` stream mode.

## Global Constraints

- Spec: `docs/specs/2026-07-20-stream-tar-fetch-1351-design.md`. ADR: `docs/adr/0400-streaming-object-read-for-combined-tar-extract.md` (Accepted). Cite `ADR-0400` in the docstrings of every module this plan changes.
- Guardrails (run before each commit): `just lint` (ruff check + format), `just type` (`ty`, **whole tree** src+tests), `just test` (excludes `live_vm`). Full gate: `just ci`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only. Google-style docstrings on non-trivial public APIs. `ty` strict.
- Error taxonomy: `domain/errors.py` `ErrorCategory` — reuse the existing values (`STALE_HANDLE`, `INFRASTRUCTURE_FAILURE`, `CONFIGURATION_ERROR`); never invent strings.
- The `get_artifact` bytes API and its four callers (install initrd/vmlinux fetches, both `debug/introspect.py`, `crash_postmortem.py`) MUST be unchanged.
- Run a single test: `uv run python -m pytest <path>::<name> -q`.

## File Structure

- `src/kdive/artifacts/storage.py` — add `StreamedArtifact` value type (reader + sensitivity + retention_class).
- `src/kdive/store/objectstore.py` — add `_open_get` (shared), refactor `get_artifact` onto it, add `_StreamingBodyReader(io.RawIOBase)`, add `get_artifact_stream` context manager.
- `src/kdive/providers/local_libvirt/lifecycle/boot/kernel_bundle.py` — `extract_kernel_bundle`/`_scan_combined_tar` take `IO[bytes]`, open `r|gz`.
- `src/kdive/providers/local_libvirt/lifecycle/install.py` — `StreamFetch` type, `_ObjectStreamReader` protocol, `_stream_object`, `_real_stream`; installer `stream_kernel` seam; `_stage_install_artifacts` streams; `_delete_install_intermediates` drops `combined_tar`.
- Tests: `tests/store/test_objectstore.py`, `tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py`, `tests/providers/local_libvirt/test_install.py`.

---

### Task 1: `StreamedArtifact` value type

**Files:**
- Modify: `src/kdive/artifacts/storage.py` (near `FetchedArtifact`, ~line 128-133)

**Interfaces:**
- Produces: `StreamedArtifact(reader: IO[bytes], sensitivity: Sensitivity, retention_class: str)` — a `NamedTuple` paralleling `FetchedArtifact` with a reader instead of `bytes`.

- [ ] **Step 1: Add the type.** After `FetchedArtifact`, add (add `from typing import IO` if absent; `Sensitivity` is already imported for `FetchedArtifact`):

```python
class StreamedArtifact(NamedTuple):
    """A fetched object's streaming reader and the class read from its metadata.

    Parallels :class:`FetchedArtifact` but yields a forward-only reader instead of a
    fully-materialized ``bytes`` body, for callers that stream the object into a
    consumer (the install combined-tar extract, ADR-0400) rather than hold it in RAM.
    """

    reader: IO[bytes]
    sensitivity: Sensitivity
    retention_class: str
```

- [ ] **Step 2: Verify import + lint.** Run: `just lint` and `just type`. Expected: clean.
- [ ] **Step 3: Commit.**

```bash
git add src/kdive/artifacts/storage.py
git commit -m "feat(store): add StreamedArtifact value type for streaming reads (#1351)"
```

---

### Task 2: `_open_get` refactor + streaming reader + `get_artifact_stream`

**Files:**
- Modify: `src/kdive/store/objectstore.py`
- Test: `tests/store/test_objectstore.py`

**Interfaces:**
- Consumes: `StreamedArtifact` (Task 1); `_infrastructure_error`, `_STALE_STATUSES`, `Sensitivity`, `CategorizedError`, `ErrorCategory` (existing in module).
- Produces:
  - `ObjectStore._open_get(key: str, etag: str | None) -> tuple[Any, Sensitivity, str]` — returns `(resp, sensitivity, retention_class)`; does the GET (adds `IfMatch` iff `etag is not None`), maps 404/412→`STALE_HANDLE` & other boto→`INFRASTRUCTURE_FAILURE`, parses metadata (absent/invalid→`INFRASTRUCTURE_FAILURE`).
  - `ObjectStore.get_artifact_stream(key: str, etag: str | None)` — `@contextmanager` yielding `StreamedArtifact`; closes the body on exit.
  - `_StreamingBodyReader(io.RawIOBase)` — wraps the boto body; `readinto` maps `(BotoCoreError, ClientError)` to `_infrastructure_error`.

- [ ] **Step 1: Write failing tests.** Add to `tests/store/test_objectstore.py`, reusing the existing fake-client patterns (mirror `test_get_artifact_*`):

```python
def test_get_artifact_stream_yields_body_and_metadata(...):
    # fake client returns a body over known bytes + valid Metadata
    with store.get_artifact_stream("k", None) as streamed:
        assert streamed.sensitivity == Sensitivity.INTERNAL
        assert streamed.retention_class == "standard"
        assert streamed.reader.read() == payload

def test_get_artifact_stream_short_reads_are_not_eof(...):
    # body.read(n) returns 1 byte at a time; full payload must reassemble
    with store.get_artifact_stream("k", None) as streamed:
        assert streamed.reader.read() == payload

def test_get_artifact_stream_404_raises_stale_handle(...): ...  # ClientError 404
def test_get_artifact_stream_412_raises_stale_handle(...): ...  # ClientError 412
def test_get_artifact_stream_non_stale_client_error_is_infrastructure_failure(...): ...
def test_get_artifact_stream_botocore_error_is_infrastructure_failure(...): ...
def test_get_artifact_stream_invalid_metadata_is_infrastructure_failure(...): ...

def test_get_artifact_stream_mid_read_error_maps_to_infrastructure_failure(...):
    # body.read raises BotoCoreError after the first chunk; reading the reader raises
    with store.get_artifact_stream("k", None) as streamed:
        with pytest.raises(CategorizedError) as exc:
            streamed.reader.read()
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["key"] == "k"
    assert "s3_error_code" in exc.value.details

def test_get_artifact_stream_with_etag_sends_if_match(...): ...   # asserts IfMatch='"e"'
def test_get_artifact_stream_none_etag_omits_if_match(...): ...
```

- [ ] **Step 2: Run to verify failure.** Run: `uv run python -m pytest tests/store/test_objectstore.py -k get_artifact_stream -q`. Expected: FAIL (`AttributeError: get_artifact_stream`).

- [ ] **Step 3: Implement.** Add imports at top: `import io`, `from collections.abc import Iterator`, `from contextlib import contextmanager`. Add the reader class near `_infrastructure_error`:

```python
class _StreamingBodyReader(io.RawIOBase):
    """A blocking ``RawIOBase`` over a boto ``StreamingBody`` that maps transport
    faults to the same typed infrastructure error the buffered read raises (ADR-0400).

    ``readinto`` returns the number of bytes copied (a short read is *not* EOF); it
    returns 0 only when the wrapped read returns ``b""`` (true end-of-stream). A
    mid-stream ``BotoCoreError``/``ClientError`` becomes a ``CategorizedError`` that
    propagates cleanly out through ``tarfile``'s stream/io buffering.
    """

    def __init__(self, body: Any, key: str) -> None:
        self._body = body
        self._key = key

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        try:
            chunk = self._body.read(len(buffer))
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("get_object", self._key, err) from err
        count = len(chunk)
        buffer[:count] = chunk
        return count
```

Extract `_open_get` from `get_artifact`'s pre-body logic (lines 177-202) as a method returning `(resp, sensitivity, retention_class)`, then rewrite `get_artifact` to call it:

```python
def _open_get(self, key: str, etag: str | None) -> tuple[Any, Sensitivity, str]:
    """Issue the GET and parse sensitivity metadata, shared by the buffered and
    streaming reads so their error taxonomy cannot drift (ADR-0400, refining ADR-0054)."""
    get_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
    if etag is not None:
        get_kwargs["IfMatch"] = f'"{etag}"'
    try:
        resp = self._client.get_object(**get_kwargs)
    except ClientError as err:
        status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in _STALE_STATUSES:
            raise CategorizedError(
                f"artifact {key!r} is gone or its etag no longer matches",
                category=ErrorCategory.STALE_HANDLE,
                details={"key": key, "http_status": status},
            ) from err
        raise _infrastructure_error("get_object", key, err) from err
    except BotoCoreError as err:
        raise _infrastructure_error("get_object", key, err) from err
    metadata = resp["Metadata"]
    try:
        sensitivity = Sensitivity(metadata["sensitivity"])
        retention_class = metadata["retention-class"]
    except (KeyError, ValueError) as err:
        raise CategorizedError(
            f"artifact {key!r} has absent or invalid sensitivity metadata",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"key": key},
        ) from err
    return resp, sensitivity, retention_class

def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact:
    # (keep the existing docstring verbatim)
    resp, sensitivity, retention_class = self._open_get(key, etag)
    try:
        data = resp["Body"].read()
    except (BotoCoreError, ClientError) as err:
        raise _infrastructure_error("get_object", key, err) from err
    return artifact_types.FetchedArtifact(data, sensitivity, retention_class)

@contextmanager
def get_artifact_stream(
    self, key: str, etag: str | None
) -> Iterator[artifact_types.StreamedArtifact]:
    """Yield a streaming reader over the object at ``key`` plus its sensitivity class.

    Same GET/error/metadata contract as :meth:`get_artifact` (they share
    ``_open_get``), but the body is not materialized: the yielded ``reader`` streams
    it and maps a mid-stream transport fault to ``INFRASTRUCTURE_FAILURE``. The body
    is closed on ``with``-exit, aborting a partially-read download. Async callers
    offload the whole ``with`` block via ``asyncio.to_thread`` (ADR-0400).
    """
    resp, sensitivity, retention_class = self._open_get(key, etag)
    body = resp["Body"]
    try:
        yield artifact_types.StreamedArtifact(
            _StreamingBodyReader(body, key), sensitivity, retention_class
        )
    finally:
        body.close()
```

- [ ] **Step 4: Run tests.** Run: `uv run python -m pytest tests/store/test_objectstore.py -q`. Expected: PASS (new + all existing `get_artifact` tests — proving the `_open_get` refactor is behavior-preserving). If `ty` flags the `readinto` override signature, annotate `buffer` to match the stdlib `WriteableBuffer` stub (positional-only) rather than widening return type.
- [ ] **Step 5: Guardrails + commit.** Run: `just lint && just type`. Then:

```bash
git add src/kdive/store/objectstore.py tests/store/test_objectstore.py
git commit -m "feat(store): add get_artifact_stream sharing _open_get with get_artifact (#1351)"
```

---

### Task 3: `extract_kernel_bundle` consumes a reader in `r|gz` stream mode

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/boot/kernel_bundle.py`
- Test: `tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py`

**Interfaces:**
- Consumes: nothing new (pure signature change).
- Produces: `extract_kernel_bundle(source: IO[bytes], kernel_dest: Path, modules_dest: Path | None) -> bool` — opens `tarfile.open(fileobj=source, mode="r|gz")`; all bounds unchanged. Later tasks pass `StreamedArtifact.reader`.

- [ ] **Step 1: Update the existing tests to pass a reader.** In `test_kernel_bundle.py`, the helpers currently build a tar file on disk and call `extract_kernel_bundle(tar_path, ...)`. Change each call to open the built tar in binary and pass the handle, e.g.:

```python
with combined_tar.open("rb") as fh:
    found = extract_kernel_bundle(fh, kernel_dest, modules_dest)
```

Rework `test_extract_kernel_bundle_opens_the_combined_tar_once` to assert stream mode: patch `tarfile.open` and assert it is called once with `mode="r|gz"` and a `fileobj` (not a path arg). Keep `_boot_only_stops_at_the_boot_member` (patches `capped_tar_members`) — the early break is unchanged. Add:

```python
def test_extract_kernel_bundle_maps_mid_stream_reader_fault_to_infrastructure_failure():
    # A reader whose read() raises CategorizedError(INFRASTRUCTURE_FAILURE, {"key","s3_error_code"})
    # after emitting the gzip+tar header for a lib/modules member, driven through r|gz.
    class _FaultReader(io.RawIOBase):
        def __init__(self, prefix: bytes, key: str): ...
        def readinto(self, buf):
            # serve `prefix` bytes, then raise the store's mapped error
            ...
    with pytest.raises(CategorizedError) as exc:
        extract_kernel_bundle(_FaultReader(prefix, "k"), kernel_dest, modules_dest)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details.get("key") == "k"          # store's detail, NOT the extractor's generic one
    assert "s3_error_code" in exc.value.details
```

(The simplest robust fixture: build a real combined tar bytes buffer, take a valid prefix that includes the boot member header+data plus the start of a modules member, and have the reader emit that prefix then raise — proving the `CategorizedError` survives the `tarfile` `_Stream` stack, per spec AC 2.)

- [ ] **Step 2: Run to verify failure.** Run: `uv run python -m pytest "tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py" -q`. Expected: FAIL (signature/type mismatch and the new fault test).

- [ ] **Step 3: Implement the signature + mode change.** Change the top import to include `from typing import IO`. Then:

```python
def extract_kernel_bundle(source: IO[bytes], kernel_dest: Path, modules_dest: Path | None) -> bool:
    # (update docstring: reads from a streaming reader in one r|gz pass; ADR-0400/0399)
    modules_tmp = modules_dest.with_name(modules_dest.name + ".part") if modules_dest else None
    try:
        return _scan_combined_tar(source, kernel_dest, modules_dest, modules_tmp)
    except (OSError, tarfile.TarError) as exc:
        ...  # unchanged mapping; details already use kernel_dest/modules_dest, not a tar path
    finally:
        ...  # unchanged .part cleanup
```

In `_scan_combined_tar`, change the first parameter to `source: IO[bytes]` and the open:

```python
archive = stack.enter_context(tarfile.open(fileobj=source, mode="r|gz"))
```

Update the missing-boot `CategorizedError` details to drop `"tar": str(combined_tar)` (there is no on-disk tar path); keep `{"member": _KERNEL_BUNDLE_BOOT_MEMBER}`. Update the early-break comment from `"r:gz"` to `"r|gz"` and note that closing the reader after the break aborts the download.

- [ ] **Step 4: Run tests.** Run: `uv run python -m pytest "tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py" -q`. Expected: PASS (byte-identity for x86_64 + `./`-prefixed ppc64le members, member-count/oversize/missing-boot/`..`-skip bounds, boot-only early break, mid-stream fault).
- [ ] **Step 5: Guardrails + commit.** Run: `just lint && just type`. Then:

```bash
git add src/kdive/providers/local_libvirt/lifecycle/boot/kernel_bundle.py \
        tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py
git commit -m "feat(install): extract_kernel_bundle streams a reader in r|gz mode (#1351)"
```

---

### Task 4: install path streams the kernel (no combined tar on disk)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/install.py`
- Test: `tests/providers/local_libvirt/test_install.py`

**Interfaces:**
- Consumes: `get_artifact_stream` (Task 2), `StreamedArtifact` (Task 1), `extract_kernel_bundle(source, ...)` (Task 3).
- Produces:
  - `type StreamFetch = Callable[[str], AbstractContextManager[StreamedArtifact]]`
  - `_ObjectStreamReader` protocol with `get_artifact_stream(key, etag) -> AbstractContextManager[StreamedArtifact]`
  - `_stream_object(store, ref) -> AbstractContextManager[StreamedArtifact]` (reads `etag=None`)
  - `_real_stream(ref)` (`# pragma: no cover - live_vm`)
  - `LocalLibvirtInstaller` / `LocalLibvirtInstall`: `stream_kernel: StreamFetch` replaces `fetch_kernel: Fetch`; `fetch_modules` defaults to `fetch_initrd`.

- [ ] **Step 1: Write failing tests.** In `test_install.py`, add a streaming fake store paralleling `_FakeStore`:

```python
class _FakeStreamStore:
    def __init__(self, payload: bytes, ...):
        self.recorded_etags: list[str | None] = []
        self._payload = payload
    @contextmanager
    def get_artifact_stream(self, key, etag):
        self.recorded_etags.append(etag)
        yield StreamedArtifact(io.BytesIO(self._payload), Sensitivity.INTERNAL, "standard")
```

Tests:
- `test_stream_object_reads_unconditionally_with_none_etag` — `_stream_object(store, ref)` opened → `store.recorded_etags == [None]`.
- `test_stream_object_propagates_store_error` — a store whose `get_artifact_stream` raises `CategorizedError(STALE_HANDLE)` propagates it out of `install`.
- `test_install_streams_kernel_and_writes_no_combined_tar` — after a successful streaming install, assert no `kernel.tar.gz` exists under staging **or** scratch, while `kernel` exists in staging.
- Update the extraction-via-install tests (`_skips_path_traversal_members`, `_normalizes_prefixed_members`) to feed the combined tar through the streaming fake.
- Replace `test_install_reclaims_the_redundant_combined_tar` with `test_install_never_writes_a_combined_tar` (its premise — reclaiming an on-disk combined tar — is gone). Keep `test_install_kdump_reclaims_the_repacked_modules_tar` and the scratch-routing tests (modules tar + vmlinux still stage to scratch).

- [ ] **Step 2: Run to verify failure.** Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement.** Add imports: `import io` (tests), `from contextlib import contextmanager`, `AbstractContextManager` from `contextlib`, `StreamedArtifact` from `kdive.artifacts.storage`. Add near `_stage_object`:

```python
class _ObjectStreamReader(Protocol):
    def get_artifact_stream(
        self, key: str, etag: str | None
    ) -> AbstractContextManager[StreamedArtifact]: ...


def _stream_object(store: _ObjectStreamReader, ref: str) -> AbstractContextManager[StreamedArtifact]:
    """Open a streaming read of the system-produced key ``ref`` (unconditional, ADR-0054/0400)."""
    return store.get_artifact_stream(ref, None)


def _real_stream(ref: str) -> AbstractContextManager[StreamedArtifact]:  # pragma: no cover - live_vm
    return _stream_object(object_store_from_env(), ref)
```

Add `type StreamFetch = Callable[[str], AbstractContextManager[StreamedArtifact]]` near the `Fetch` type. In `LocalLibvirtInstaller.__init__` and `LocalLibvirtInstall.__init__`, replace the `fetch_kernel: Fetch` param with `stream_kernel: StreamFetch`, store `self._stream_kernel = stream_kernel`, and change `self._fetch_modules = fetch_modules or fetch_kernel` → `... or fetch_initrd`. In `from_env`, replace `fetch_kernel=_real_fetch` with `stream_kernel=_real_stream`.

Rewrite `_stage_install_artifacts` to stream (drop `combined_tar`):

```python
def _stage_install_artifacts(self, request, staging_dir, scratch_dir):
    modules_tar = scratch_dir / "modules.tar.gz"
    vmlinux = scratch_dir / "vmlinux"
    try:
        kernel_path = staging_dir / "kernel"
        needs_modules = request.method in KDUMP_FAMILY or request.debuginfo_ref is not None
        with self._stream_kernel(request.kernel_ref) as streamed:
            modules_found = extract_kernel_bundle(
                streamed.reader, kernel_path, modules_tar if needs_modules else None
            )
        initrd_path = self._stage_initrd(request, staging_dir)
        modules_injected = False
        if modules_found:
            self._inject_built_modules(
                request.system_id, modules_tar, kernel_path, request.debuginfo_ref, vmlinux
            )
            modules_injected = True
        return _StagedInstallArtifacts(kernel_path, initrd_path, modules_injected)
    finally:
        self._delete_install_intermediates(modules_tar, vmlinux)
```

Change `_delete_install_intermediates(combined_tar, modules_tar, vmlinux)` → `(modules_tar, vmlinux)` and its loop/comment (the combined tar is no longer an on-disk intermediate). Update the `install` and `_stage_install_artifacts` docstrings/comments that describe fetching the combined tar to disk.

- [ ] **Step 4: Run tests.** Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -q`. Expected: PASS.
- [ ] **Step 5: Guardrails + commit.** Run: `just lint && just type`. Then:

```bash
git add src/kdive/providers/local_libvirt/lifecycle/install.py tests/providers/local_libvirt/test_install.py
git commit -m "feat(install): stream the combined tar via a stream_kernel seam (#1351)"
```

---

### Task 5: Full-suite guardrail sweep

**Files:** none (verification only).

- [ ] **Step 1: Grep for stale `fetch_kernel` / Path-based `extract_kernel_bundle` callers.** Run: `rg -n "fetch_kernel|extract_kernel_bundle" src tests`. Expected: no remaining `fetch_kernel` seam; every `extract_kernel_bundle` call passes a reader.
- [ ] **Step 2: Run the full gate.** Run: `just ci`. Expected: green (lint, `ty` whole-tree, lint-shell, lint-workflows, check-mermaid, test). Note the known macOS-local `mapfile` doc-script skips + `/var` rootfs test are benign locally and green on Linux CI (project memory); the doc guards `adr-status-check` must pass.
- [ ] **Step 3: If any guardrail is red, fix and re-run before proceeding.** Commit any fixup with a `fix(install): ...` subject.

## Self-Review

- **Spec coverage:** AC1 (round-trip + short-read) → Task 2; AC2 (error-mapping parity + end-to-end mid-stream fault + `s3_error_code` class) → Tasks 2 & 3; AC3 (`get_artifact` unchanged) → Task 2 step 4 (existing tests pass on the refactor); AC4 (byte-identity + repack in `r|gz`) → Task 3; AC5 (all bounds on streaming path) → Task 3; AC6 (install streams, no `kernel.tar.gz`, `etag=None`, mid-stream category propagates) → Task 4; AC7 (`just ci` green) → Task 5.
- **Placeholder scan:** the `_FaultReader`/`_FakeStreamStore` bodies are sketched with `...`; the implementer fills the trivial buffering. Every load-bearing signature and the four production edits are shown in full.
- **Type consistency:** `get_artifact_stream(key, etag)` and `StreamedArtifact(reader, sensitivity, retention_class)` are used identically in Tasks 2 and 4; `extract_kernel_bundle(source, kernel_dest, modules_dest)` is consistent across Tasks 3 and 4.
