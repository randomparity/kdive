# Stream the combined-tar fetch into the extractor (#1351)

Status: Draft
Issue: #1351
ADR: [0400](../adr/0400-streaming-object-read-for-combined-tar-extract.md)
Refines: [ADR-0054](../adr/0054-object-store-unconditional-read.md),
[ADR-0399](../adr/0399-single-pass-kernel-bundle-and-scratch-staging.md)

## Problem

The install path fully materializes the combined kernel tar in memory before
extraction. `store.get_artifact(ref, None)` returns the whole object as `bytes`
(`FetchedArtifact.data`), and `_stage_object` writes that buffer to disk before
`extract_kernel_bundle` opens the tar. For a DWARF-bloated ~2 GB tar this means
the full object is resident in RAM during the fetch, and extraction cannot begin
until the whole body has landed on disk.

ADR-0399 (#1350) merged the two decompression passes into one and added an opt-in
`KDIVE_INSTALL_SCRATCH` root so an operator can move the intermediates to tmpfs.
But tmpfs trades disk for RAM: because the fetch already materializes the whole
object, a tmpfs scratch holds those bytes resident for the extract+inject window
plus the repacked modules tar (up to a few GB, multiplied by concurrent
installs). The genuinely RAM-free win — deferred from #1350 to here with its own
ADR — is streaming.

## Goals

- The combined kernel tar is **never** fully materialized as `bytes` and **never**
  written to disk before extraction begins; the S3 body streams straight into the
  tar extractor.
- Every existing scan bound is preserved on the streaming path: the member-count
  cap, the oversize boot member, the cumulative module-tree size, and the
  `..`-path skip.
- The sensitivity-metadata, redaction, and error-mapping contract that
  `get_artifact` provides today is preserved for the streaming read: a 404/412
  maps to `STALE_HANDLE`, other client/transport faults (including mid-stream) map
  to `INFRASTRUCTURE_FAILURE`, absent/invalid sensitivity metadata maps to
  `INFRASTRUCTURE_FAILURE`, and the object's `sensitivity`/`retention_class` are
  surfaced to the caller.
- The existing bytes API (`get_artifact` → `FetchedArtifact`) is unchanged for its
  other callers (symbolization, crash postmortem), which legitimately want the
  whole object in memory.

## Non-goals

- **Changing `get_artifact`'s return type or removing it.** Four callers read
  `.data` and want the whole object (`install`'s initrd/vmlinux fetches, the two
  `introspect.py` symbolization fetches, `crash_postmortem.py`). The streaming read
  is an **additive** method; the bytes API stays.
- **Streaming the boot member itself.** `extract_kernel_bundle` reads the
  `boot/vmlinuz` member into RAM before the temp-then-rename write, bounded by
  `reject_oversize_member`. The bloat this issue targets is the DWARF-inflated
  `lib/modules/` tree inside the *combined* tar, not the single boot image; the
  boot member's in-RAM read is unchanged.
- **Streaming the initrd or the debuginfo `vmlinux` fetch.** Those remain
  fetch-to-disk via `_stage_object`; they are small relative to the combined tar
  and their disk staging is not the memory problem.
- **A ranged / resumable / retrying streaming reader.** A mid-stream transport
  fault fails the install with `INFRASTRUCTURE_FAILURE`, and the agent's recovery
  is a new Run (ADR-0030 §2), exactly as for the buffered fetch today. No retry or
  range logic is added.

## Design

### Part 1 — additive streaming read on the object store

`ObjectStore` gains a streaming read alongside `get_artifact`. To avoid the
metadata-parse + error-mapping drift ADR-0054 warned about when it rejected a
duplicate `fetch_object` method, the GET setup, error mapping, and
sensitivity-metadata parse are factored into one private helper shared by both
reads:

- `_open_get(key, etag) -> (resp, sensitivity, retention_class)` — builds the GET
  kwargs (adds `IfMatch` iff `etag is not None`, ADR-0054), calls
  `self._client.get_object`, maps a 404/412 `ClientError` to `STALE_HANDLE` and any
  other `ClientError`/`BotoCoreError` to `INFRASTRUCTURE_FAILURE`
  (`_infrastructure_error`), and parses `sensitivity`/`retention-class` from
  `resp["Metadata"]` (absent/invalid → `INFRASTRUCTURE_FAILURE`). This is exactly
  the pre-body logic `get_artifact` runs today; `get_artifact` is refactored to
  call it and then read the body.
- `get_artifact_stream(key, etag)` — a `@contextmanager` that calls `_open_get`,
  then yields a `StreamedArtifact(reader, sensitivity, retention_class)` whose
  `reader` wraps `resp["Body"]`, and closes the body on exit (which aborts the
  underlying HTTP download when the caller stops early). Async callers offload the
  whole `with` block via `asyncio.to_thread`, as with `get_artifact`.

`StreamedArtifact` is a new value type in `kdive.artifacts.storage`
(`reader: IO[bytes]`, `sensitivity: Sensitivity`, `retention_class: str`),
paralleling `FetchedArtifact` but with a reader instead of `bytes`.

**The reader maps mid-stream transport faults.** `get_artifact` catches
`(BotoCoreError, ClientError)` around `resp["Body"].read()` because a mid-stream
timeout or dropped connection raises after the headers. On the streaming path the
body is read *inside* `tarfile`, far from the store, so a boto error would
otherwise surface untyped out of the extractor. The reader is a thin
`io.RawIOBase` subclass whose `readinto` catches `(BotoCoreError, ClientError)`
from the wrapped body and re-raises `_infrastructure_error("get_object", key,
err)` — the same category and detail shape `get_artifact` produces. The extractor
never sees or couples to boto types; a `CategorizedError` propagates through
`tarfile` and out unwrapped.

"Bounded reader" means exactly this: the reader never buffers the whole object
(it is a forward-only fixed-window read), and the *memory* bound on the
decompressed content is the tar scan bounds below, now enforced on a forward-only
stream. No artificial byte cap is added — S3's `ContentLength` already bounds the
compressed stream and the scan bounds bound the decompressed content; a second cap
would be redundant (see rejected alternative in the ADR).

### Part 2 — `extract_kernel_bundle` consumes a reader in stream mode

`extract_kernel_bundle(combined_tar: Path, ...)` becomes
`extract_kernel_bundle(source: IO[bytes], ...)` and opens
`tarfile.open(fileobj=source, mode="r|gz")` (stream mode) instead of
`tarfile.open(combined_tar, "r:gz")` (seekable). Only the install path calls this
function, so the Path form is replaced, not kept alongside (replace, don't
deprecate).

The single forward `capped_tar_members` walk is unchanged in structure, and every
bound is preserved because each operates on `TarInfo`/read-order, not on random
access:

- **member-count bomb** — `capped_tar_members` lazy enumeration → `CONFIGURATION_ERROR`;
- **oversize boot member** — `reject_oversize_member(member.size)` before the read;
- **cumulative module tree** — `reject_oversize_member(total)` in the repack;
- **`..`-path skip** — the `".." in normalized.split("/")` `continue`.

The extract-in-loop discipline (`extractfile(member)` fully consumed before the
iterator advances) is already what the code does; it is the exact pattern stream
mode requires (no backward seek). The boot-only early `break` still stops the walk
at `boot/vmlinuz` — and now, because the source is the live S3 body, closing the
reader after the break **aborts the rest of the download** rather than reading it
to discard. For a plain-boot install of a DWARF-bloated tar this is the
time-to-first-byte and bandwidth win, on top of never buffering the object.

The missing-boot / unreadable-tar error mapping (`(OSError, tarfile.TarError)` →
`INFRASTRUCTURE_FAILURE`) and the `.part` modules cleanup are unchanged. The
missing-boot detail no longer carries a scratch tar path (there is no on-disk
combined tar to name); it names the absent member.

### Part 3 — install path streams instead of fetch-then-extract

`_stage_install_artifacts` replaces the fetch-to-disk + open-from-disk pair

```
self._fetch_kernel(request.kernel_ref, combined_tar)
extract_kernel_bundle(combined_tar, kernel_path, modules_tar if needs_modules else None)
```

with a single streaming extraction:

```
with self._stream_kernel(request.kernel_ref) as streamed:
    modules_found = extract_kernel_bundle(
        streamed.reader, kernel_path, modules_tar if needs_modules else None
    )
```

Consequences for the install seam:

- The kernel fetch seam changes from `fetch_kernel: Fetch =
  Callable[[str, Path], None]` to `stream_kernel: StreamFetch = Callable[[str],
  AbstractContextManager[StreamedArtifact]]`. The initrd and debuginfo-`vmlinux`
  fetches keep the `Fetch` seam (`fetch_initrd`, `fetch_modules`); `fetch_modules`
  now defaults to `fetch_initrd` rather than the removed `fetch_kernel`.
- `_stream_object(store, ref)` is the testable core paralleling `_stage_object`: it
  calls `store.get_artifact_stream(ref, None)` — **unconditional**, `etag=None`
  (ADR-0054, a system-produced key with no client handle). A host-free unit test
  asserts the `None` at the call site, guarding the same empty-etag regression
  ADR-0054 pinned. `_real_stream` is the `# pragma: no cover - live_vm` wrapper
  supplying `object_store_from_env()`.
- The combined tar **never lands on disk**: `combined_tar = scratch_dir /
  "kernel.tar.gz"` is removed, and `_delete_install_intermediates` drops its
  `combined_tar` parameter (it still reclaims the repacked `modules.tar.gz` and the
  debuginfo `vmlinux`). The scratch dir, its separate-mount handling, and its reap
  are unchanged; on a plain-boot streaming install nothing lands in scratch (an
  empty per-Run scratch dir is created and reaped, as today — harmless).

### Memory / failure story

- **Memory:** the combined tar is streamed, so its bytes are never resident as a
  whole `bytes` and never written to disk. The tmpfs-scratch RAM tradeoff ADR-0399
  documented for the combined tar disappears; only the repacked modules tar (and a
  debuginfo `vmlinux`) still stage to scratch, bounded by `reject_oversize_member`.
- **Failure:** a mid-stream S3 fault surfaces as `INFRASTRUCTURE_FAILURE` (via the
  reader), a vanished key as `STALE_HANDLE` (via `_open_get`'s 404 mapping), a
  member-count/oversize bomb as `CONFIGURATION_ERROR` (unchanged bounds), a missing
  boot member or corrupt gzip as `INFRASTRUCTURE_FAILURE`. Every category matches
  the buffered path; recovery is a new Run in all cases (ADR-0030 §2).
- **Two accepted residuals of forward-only streaming** (ADR-0400 Consequences): the
  S3 GET connection is held open across the whole extract+repack, so a scratch-write
  stall can trip botocore's per-read timeout into a mid-stream
  `INFRASTRUCTURE_FAILURE`; and streaming interleaves download with extraction, so a
  later mid-stream fault can leave a durable staging `kernel` (always written from a
  fully-read boot member) behind an install that then failed. Both are contained by
  the run-step ledger + dead-letter + full re-fetch on retry — boot is a separate
  step that never runs on a failed install. Whole-object checksum verification is
  intentionally absent on both reads, unchanged here.

## AI-surface note

This change is object-store I/O plumbing and tar extraction. It adds no LLM call,
prompt, system message, retrieval path, classifier, agent loop, or tool-use chain,
and changes no agent-facing tool schema. No model-eval plan applies.

## Acceptance criteria

1. `ObjectStore.get_artifact_stream(key, etag)` yields a `StreamedArtifact` whose
   `reader` streams the object body and whose `sensitivity`/`retention_class` match
   the object metadata (MinIO round-trip: streamed bytes are byte-identical to
   `get_artifact(...).data`, same sensitivity).
2. `get_artifact_stream` error mapping matches `get_artifact`: a missing key (404)
   and an etag mismatch (412) raise `STALE_HANDLE`; a non-stale `ClientError` and a
   `BotoCoreError` raise `INFRASTRUCTURE_FAILURE`; absent/invalid sensitivity
   metadata raises `INFRASTRUCTURE_FAILURE`; a `BotoCoreError`/`ClientError` raised
   **while the reader is read** raises `INFRASTRUCTURE_FAILURE` with the
   `{"key", "s3_error_code"}` detail shape. `IfMatch` is sent iff `etag is not
   None`.
3. `get_artifact` is unchanged for existing callers: its signature, return type,
   and error mapping are identical (verified by the existing `get_artifact` tests
   still passing against the refactor onto the shared `_open_get`).
4. `extract_kernel_bundle` extracts `boot/vmlinuz` byte-identically (x86_64 and
   ppc64le members, including a `./`-prefixed member) from a reader in `r|gz`
   stream mode, and given a `modules_dest` repacks the `lib/modules/<ver>/` subtree;
   given `modules_dest=None` it extracts only the boot member and stops early.
5. Every bound holds on the streaming path: member-count bomb →
   `CONFIGURATION_ERROR`; oversize boot member → `CONFIGURATION_ERROR`; oversize
   cumulative module tree → `CONFIGURATION_ERROR` with the `.part` temp cleaned;
   missing boot member → `INFRASTRUCTURE_FAILURE`; corrupt/undecompressable stream →
   `INFRASTRUCTURE_FAILURE`; a `..`-path module member is skipped.
6. The install path streams: no `kernel.tar.gz` is ever written under staging or
   scratch (asserted via a streaming fake store), `_stream_object` reads with
   `etag=None`, and a mid-stream store fault propagates its category out of
   `install`. The `kernel` staging output, initrd staging, modules inject, and
   scratch reap are unchanged.
7. `just ci` green (lint, `ty` whole-tree, tests, doc guards).
