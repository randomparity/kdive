# 0400 — Streaming object read for combined-tar extraction

Status: Accepted

- **Date:** 2026-07-20
- **Issue:** #1351 (streaming fetch-and-extract), deferred from #1350.
- **Refines:** [ADR-0054](0054-object-store-unconditional-read.md) (the
  unconditional `get_artifact(key, etag=None)` install/symbolization read) and
  [ADR-0399 Decision 4 / rejected alternative "Streaming
  fetch-and-extract"](0399-single-pass-kernel-bundle-and-scratch-staging.md).
- **Spec:** [`../specs/2026-07-20-stream-tar-fetch-1351-design.md`](../specs/2026-07-20-stream-tar-fetch-1351-design.md).

## Context

The install path materializes the whole combined kernel tar as `bytes` before it
extracts anything: `_stage_object` reads `store.get_artifact(ref, None).data`
(`FetchedArtifact.data`) and writes it to disk, then `extract_kernel_bundle`
re-opens that file with `tarfile.open(path, "r:gz")`. For a DWARF-bloated ~2 GB
tar the entire object is resident during the fetch and extraction cannot start
until the whole body has landed on disk.

ADR-0399 attacked the same bloat two other ways — one decompression pass instead
of two, and an opt-in `KDIVE_INSTALL_SCRATCH` root that can point the
intermediates at tmpfs. But tmpfs trades disk for RAM, and because the fetch
already buffers the whole object, a tmpfs scratch keeps those bytes resident for
the extract+inject window. ADR-0399 named streaming the genuine RAM-free win and
deferred it here (Decision 4 and its first rejected alternative), because it
reworks the shared `store.get_artifact` contract — sensitivity metadata,
redaction, error mapping — which warranted its own ADR.

Two constraints bound the decision. First, `get_artifact`'s `bytes` return is
still correct for its other callers — the two symbolization fetches
(`providers/*/debug/introspect.py`) and `crash_postmortem.py` want the whole
object in memory — so the streaming read must be additive, not a replacement.
Second, ADR-0054 rejected a *duplicate* read method (`fetch_object`) precisely
because the metadata-parse + error-mapping block would drift between two copies;
any streaming read must share that block, not fork it.

## Decision

**We will add an additive streaming read to `ObjectStore` that shares
`get_artifact`'s GET-setup, error-mapping, and metadata-parse logic, and have
`extract_kernel_bundle` consume it in `tarfile` stream mode so the combined tar is
never fully materialized as `bytes` or written to disk before extraction.**

1. **Shared `_open_get(key, etag)` helper.** Factor the GET-kwargs build (`IfMatch`
   iff `etag is not None`, ADR-0054), the `ClientError`/`BotoCoreError` mapping
   (404/412 → `STALE_HANDLE`, else `INFRASTRUCTURE_FAILURE`), and the
   `sensitivity`/`retention-class` metadata parse (absent/invalid →
   `INFRASTRUCTURE_FAILURE`) out of `get_artifact` into one private helper.
   `get_artifact` calls it and then reads the body into `bytes` — its signature,
   return type, and error contract are byte-for-byte unchanged. This directly
   honors ADR-0054's anti-drift reasoning: one read implementation, two tails.

2. **`get_artifact_stream(key, etag)` context manager.** A `@contextmanager` that
   calls `_open_get`, yields `StreamedArtifact(reader, sensitivity,
   retention_class)`, and closes `resp["Body"]` on exit — so a caller that stops
   early (the boot-only extract) aborts the underlying HTTP download. Async callers
   offload the whole `with` block via `asyncio.to_thread`, as with `get_artifact`.
   `StreamedArtifact` is a new `kdive.artifacts.storage` value type paralleling
   `FetchedArtifact` with a `reader: IO[bytes]` instead of `data: bytes`.

3. **A transport-error-mapping reader.** The yielded `reader` is a thin
   `io.RawIOBase` subclass wrapping `resp["Body"]` whose `readinto` catches
   `(BotoCoreError, ClientError)` and re-raises `_infrastructure_error("get_object",
   key, err)` — the same category and `{"key", "s3_error_code"}` detail shape
   `get_artifact`'s body-read `except` produces today. Because the body is read
   inside `tarfile`, this keeps the boto→`CategorizedError` mapping in the store
   layer where the boto types belong; the extractor stays decoupled from the
   transport and a `CategorizedError` propagates through `tarfile` unwrapped.

4. **`extract_kernel_bundle` reads a stream.** Change its first parameter from
   `combined_tar: Path` to `source: IO[bytes]` and open `tarfile.open(fileobj=source,
   mode="r|gz")` (forward-only stream mode) instead of `tarfile.open(path, "r:gz")`.
   Every scan bound is preserved unchanged — the member-count cap
   (`capped_tar_members`), `reject_oversize_member` on the boot member and the
   cumulative module tree, and the `..`-path skip all operate on `TarInfo`/read
   order, not random access. The extract-in-loop discipline (consume
   `extractfile(member)` before advancing) is already what the code does and is
   exactly what stream mode requires. The boot-only early `break` still stops at
   `boot/vmlinuz`; closing the reader after it now aborts the download instead of
   reading-to-discard. Only the install path calls this function, so the Path form
   is replaced, not kept (replace, don't deprecate).

5. **Install streams the kernel; other fetches unchanged.** Replace the kernel
   fetch seam `fetch_kernel: Fetch = Callable[[str, Path], None]` with
   `stream_kernel: StreamFetch = Callable[[str],
   AbstractContextManager[StreamedArtifact]]`, and stage via `with
   self._stream_kernel(ref) as streamed: extract_kernel_bundle(streamed.reader,
   ...)`. `_stream_object(store, ref)` is the testable core paralleling
   `_stage_object`, reading unconditionally (`etag=None`, ADR-0054) with the `None`
   pinned at the call site by a host-free test; `_real_stream` is the
   `# pragma: no cover - live_vm` `object_store_from_env()` wrapper. The initrd and
   debuginfo-`vmlinux` fetches keep the `Fetch` seam (`fetch_modules` now defaults
   to `fetch_initrd`). The combined tar is removed from the on-disk intermediates:
   `_delete_install_intermediates` drops its `combined_tar` parameter; the scratch
   dir, its separate-mount handling, and its reap are unchanged.

## Consequences

- The combined tar is never a whole `bytes` and never a disk file: peak install
  RAM drops by the tar size, and extraction begins at first byte. On a plain-boot
  install the boot-only early `break` now also aborts the download after
  `boot/vmlinuz`, saving the DWARF-bloated remainder's transfer.
- The tmpfs-scratch RAM concern ADR-0399 documented for the *combined tar*
  disappears; only the repacked modules tar and a debuginfo `vmlinux` still stage
  to scratch (both `reject_oversize_member`-bounded). ADR-0399's tmpfs guidance
  stays accurate for those, at much smaller size.
- `get_artifact` and its `bytes` callers are untouched; the store grows one method
  and one shared helper. The error taxonomy is identical across both reads because
  they share `_open_get` and the same `_infrastructure_error`.
- A new obligation: any future field added to the metadata contract must be added
  in `_open_get` so both reads carry it — the single point that makes drift
  impossible is also the single point to maintain.
- `extract_kernel_bundle`'s signature changes (Path → reader); its callers and
  tests move to passing a binary reader. No migration, no schema change, no
  config-doc regeneration (no new Setting).
- **Residual — the S3 GET connection is held across extract+repack.** The buffered
  read drained the body at network speed and then worked from RAM, so its
  connection closed before extraction. The streaming read keeps the `GetObject`
  HTTP connection open for as long as the extractor consumes the reader — on a
  modules-needed run that spans the whole member-by-member repack, whose pace is
  bounded by scratch-write throughput (ADR-0399 may route that scratch at a slow or
  near-full tmpfs). botocore applies a per-read timeout to a streaming body, so a
  scratch-write stall that delays the next read past that window trips a mid-stream
  `BotoCoreError` that the reader maps to `INFRASTRUCTURE_FAILURE` — a spurious
  install failure where the buffered path would have succeeded. This is an accepted
  residual: recovery is a new Run (ADR-0030 §2), the same as any install-step
  failure, and no bespoke timeout knob is added (it would be speculative). The
  botocore client timeouts are left at their configured defaults; if a slow-scratch
  repack proves to trip them in practice, the deliberate fix is raising the read
  timeout on the streamed GET, tracked separately rather than pre-built here.
- **Residual — streaming trades fetch-then-extract atomicity for interleaving.** The
  buffered flow received the whole tar (temp-then-rename) before any extraction ran,
  so extraction implied a fully-received object. The streaming flow interleaves the
  download with extraction: the boot member is still written to the durable staging
  `kernel` only after it is fully read (`_extract_boot_member` reads the whole member,
  then temp-then-renames — so the staging `kernel` is never a partial boot image), but
  a mid-stream fault *later* in a modules-needed run can leave that staging `kernel`
  behind from an install that ultimately failed. This is contained: the modules
  `.part` is cleaned in the `finally`, the install run-step is abandoned and the job
  dead-lettered, boot is a separate step that will not run on a failed install, and a
  retry re-fetches and clobbers (temp-then-rename). Whole-object checksum verification
  is intentionally absent on *both* reads (unchanged by this ADR); integrity rests on
  the run-step ledger + dead-letter + full re-fetch, not a per-object checksum.

## Considered & rejected

- **Do nothing / rely on `KDIVE_INSTALL_SCRATCH` tmpfs (ADR-0399).** Rejected as
  the terminal answer: tmpfs relocates the buffered bytes to RAM rather than
  eliminating them, and does nothing for time-to-first-byte. ADR-0399 itself named
  this the deferred, complementary win. Streaming is the RAM-free path; the tmpfs
  option remains valid for the still-staged modules tar.
- **Stream the GET body to a constant-memory scratch file, then extract from `Path`
  (unchanged extractor).** The tempting middle path: a `copyfileobj`-style chunked
  copy of the body to `scratch/kernel.tar.gz` drops the ~2 GB Python-heap `bytes`
  buffer with none of this ADR's surface — no `_open_get`/`get_artifact_stream`
  split, no `StreamedArtifact`, no `RawIOBase` reader, no `Path`→`IO[bytes]` change,
  and it *keeps* fetch-then-extract atomicity and closes the S3 connection before
  extraction (avoiding both residuals above). Rejected because it does not meet the
  primary goal under the configuration that motivates it: when the scratch is a
  tmpfs (ADR-0399's whole point), the copied `kernel.tar.gz` is **resident in RAM as
  a tmpfs file** — the exact whole-tar resident copy this issue exists to eliminate.
  Stream-to-file only moves the buffer from the Python heap to the filesystem; on a
  tmpfs scratch that is not a win at all, and on a disk scratch it still writes and
  re-reads the whole 2 GB. Full streaming is the only option that holds no
  whole-tar copy anywhere, and it alone earns the secondary wins (extraction begins
  at first byte; the boot-only early break aborts the rest of the download). The
  added surface is the honest price of the only design that satisfies the tmpfs
  case ADR-0399 opened.
- **A second, duplicate streaming method that re-implements the metadata parse and
  error mapping.** Rejected for the same reason ADR-0054 rejected `fetch_object`:
  two copies of the GET-setup + metadata + error-mapping block drift. The shared
  `_open_get` helper gives one implementation with a `bytes` tail and a stream tail.
- **Change `get_artifact` to return a stream (or a union) instead of `bytes`.**
  Rejected: the symbolization and postmortem callers want the whole object in
  memory; forcing a stream on them adds a `read()`-into-`bytes` dance at every
  call site and a wider return type for no benefit. The streaming read is additive.
- **Map mid-stream boto errors inside `extract_kernel_bundle` instead of in the
  reader.** Rejected: it couples the extractor to `botocore` exception types and
  duplicates the store's error taxonomy at a second site. Wrapping the body in the
  store keeps the boto→`CategorizedError` mapping in the one layer that imports
  boto, and the extractor sees only `CategorizedError`/`OSError`/`TarError`.
- **Add a hard total-bytes cap to the streaming reader.** Rejected as redundant:
  S3's `ContentLength` already bounds the compressed stream, and the decompressed
  content is bounded by the existing scan bounds (`capped_tar_members`,
  `reject_oversize_member`) now enforced on the forward pass. A third cap is
  belt-and-suspenders with a magic number to keep in sync.
- **Keep the Path-based `extract_kernel_bundle` alongside a new streaming one.**
  Rejected: only the install path calls it, and it now always has a reader. Two
  entry points to the same scan logic is exactly the drift ADR-0399's single-pass
  merge removed. Replace, don't deprecate.
- **Stream the boot member to disk in chunks too.** Out of scope: the boot member
  (`vmlinuz`) is small relative to the DWARF module tree and is already
  `reject_oversize_member`-bounded before its in-RAM read. The bloat is the
  `lib/modules/` tree inside the combined tar, which the streaming walk repacks
  member-by-member without holding the whole tree.
