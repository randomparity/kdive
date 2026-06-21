# Spec — Stream large vmcores in the local kdump harvest (#657)

- **Status:** Draft
- **Date:** 2026-06-21
- **ADR:** [0203](../adr/0203-local-libvirt-kdump-overlay-harvest.md) (amended — streaming follow-up)
- **Issue:** #657 (companion to #654)

## Problem

The local-libvirt KDUMP harvest (ADR-0203) reads the whole guest-written
`/var/crash/<ts>/vmcore` into worker RAM via libguestfs `read_file`, then carries those
bytes through `LocalLibvirtRetrieve.capture` to the object store as a single
`put_artifact(data=bytes)`. For a core up to the 5 GiB `MAX_CORE_BYTES` ceiling that is a
multi-GB transient allocation on the worker — the exact case ADR-0203 pre-acknowledged
deferring ("if a future change needs to lift the ceiling, switch the reader to
`g.download(path, tmpfile)` and stream — out of scope here").

The #115 live verification harvested an ~805 MB core successfully via `read_file`,
confirming the mechanism works but is memory-heavy.

## Goal

Harvest a staged core, extract its build-id + redacted dmesg, and persist the raw core to
the object store **without ever holding the whole core in a single in-memory buffer**. Keep
the pure selection / size-cap / readiness logic unit-tested with a fake reader; keep the
real libguestfs `download` call `live_vm`-gated.

## Non-goals

- Lifting the 5 GiB `MAX_CORE_BYTES` ceiling. The cap stays, checked from `statns` size
  **before** download.
- Touching the MCP surface, admission, the boot/install plane, the image catalog, the
  schema, or any other provider. The remote host_dump streaming path (ADR-0094) is the
  precedent this mirrors and is left unchanged.
- Changing the local HOST_DUMP branch, which is a `MISSING_DEPENDENCY` stub on local.

## Design

`GuestCoreReader` (the unit-tested seam) changes its read method from returning bytes to
**downloading the chosen core to a caller-owned temp file and returning its `Path`**:

```python
class GuestCoreReader(Protocol):
    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]: ...
    def download_vmcore(self, overlay: str, path: str, dest: Path) -> None: ...
```

`harvest_vmcore(reader, overlay, *, dest, max_bytes)` takes the destination path from its
caller and returns `Path | None` (the populated `dest`, or `None` when no core exists):

1. list cores, `select_newest` (unchanged, pure);
2. `None` when none present (→ `READINESS_FAILURE` upstream, unchanged);
3. `chosen.size_bytes > max_bytes` → `CONFIGURATION_ERROR` **before** any download
   (unchanged ordering — the size is read from `statns`, no bytes touched);
4. `reader.download_vmcore(overlay, chosen.path, dest)`; return `dest`.

`harvest_vmcore` does **not** own the temp file: it neither creates nor unlinks it — it
only writes into the `dest` it is handed (see temp-file ownership below).

The real `_LibguestfsCoreReader.download_vmcore` calls `self._guest.download(path, dest)`
(constant-memory streaming through the appliance) instead of `read_file`. It stays
`# pragma: no cover - live_vm`.

### Seam-type change and the two build-id callers

Today a **single** injected seam `read_vmcore_build_id: Callable[[bytes], str]`
(`_ReadBuildId`) is used by **both** `capture` (line 139) and `run_crash_postmortem`
(passed as `read_build_id=`, line 198). `run_crash_postmortem` reads build-id from bytes it
already `fetch_object`'d out of the object store, so it must keep a **bytes** seam. We
therefore do **not** re-type the shared seam. Instead:

- The KDUMP capture path computes the core's build-id directly from the spooled `Path` via
  the existing Path-based helper `read_core_build_id_from_file` (the same function
  `_real_read_build_id` already wraps), threaded into `LocalLibvirtRetrieve` as a new,
  distinct `read_vmcore_build_id_from_file: Callable[[Path], str]` seam selected by
  `from_env`.
- `run_crash_postmortem` keeps the existing bytes-based `read_vmcore_build_id` seam
  unchanged.
- The redacted-dmesg seam (`extract_redacted: Callable[[bytes], bytes]`) is used **only**
  by `capture`, so it is safe to re-type to `Callable[[Path], bytes]`.

### The KDUMP vs HOST_DUMP branch split

`capture` dispatches both `KDUMP` and `HOST_DUMP` through one body today
(`_host_dump_capture(system_id) -> bytes | None` vs `_wait_for_vmcore(system_id) -> bytes`).
The local `HOST_DUMP` seam is a `MISSING_DEPENDENCY` stub, but its contract and its test
(`test_capture_host_dump_uses_dump_seam`) must keep passing. The two methods therefore
**diverge** after this change:

- `KDUMP`: `_wait_for_vmcore(system_id) -> Path | None`; on a `Path`, compute build-id +
  redacted dmesg from the `Path`, `put_stream` the raw core, `head`-verify, persist the
  redacted derivative, and unlink the spool in a `finally` that wraps everything from the
  seam call onward.
- `HOST_DUMP`: unchanged bytes path — `_host_dump_capture(system_id) -> bytes | None`,
  `read_vmcore_build_id(bytes)`, `_put(raw bytes)`, `_put(redacted bytes)`. (On local this
  raises before returning bytes; the branch and its test stay intact.)

`capture` selects the branch on `method` up front and shares only the `None →
READINESS_FAILURE` guard and the `CaptureOutput` shape.

### Temp-file ownership

Ownership is split so there is no leak window, because the seam must **return** the path for
`capture` to consume it (the seam cannot unlink a path it is about to hand back):

- **On the seam's own failure**, `_real_wait_for_vmcore` cleans up: it creates a private
  temp directory, passes `dest` into `harvest_vmcore`, and if it returns `None` or raises
  before handing the path back, removes the file and directory in its own `try/except`.
  Because the file is on the host filesystem (not inside the libguestfs appliance), it
  outlives `reader.close()`.
- **On a successfully returned path**, `capture` owns cleanup: its KDUMP branch wraps the
  `_wait_for_vmcore` call and everything downstream (build-id, dmesg, `put_stream`,
  `head`-verify) in a single `try/finally` that unlinks the spool file and its directory.
  There is no window between the seam returning and `capture`'s `finally` arming, because the
  seam call is the first statement inside the `try`.

A unit test drives `capture` with a fake `_wait_for_vmcore` that returns a real temp `Path`
and asserts the path is gone after both a successful capture and a store-failure capture.

## Acceptance criteria

- A large staged core is harvested and stored without a whole-core in-memory buffer: the
  harvest returns a `Path`, and the raw store write is `put_stream` from that path (verified
  by a unit test asserting the fake store received a `path`, not `data` bytes).
- Unit tests still cover: newest-core selection, the size cap rejecting **before** download,
  and absent core → `READINESS_FAILURE`.
- The size cap is checked from `statns` size before the download call (a fake whose
  `download_vmcore` fails the test if called on an oversize entry).
- The temp file is removed after capture (success and store-failure paths).
- `just ci` green.

## Risks

- **Temp-file lifecycle.** A leaked multi-GB temp file is as bad as the RAM buffer it
  replaces. The temp file is created by `harvest_vmcore`'s caller boundary and unlinked in a
  `finally` in `capture`; a unit test asserts deletion on both the success and store-failure
  paths.
- **Seam signature churn.** The build-id/extract-redacted seams move from `bytes` to `Path`.
  The bytes-based `run_crash_postmortem` build-id seam is deliberately distinct and
  untouched; the test suite (`just type` whole-tree + the retrieve tests) catches any
  mismatched caller.
