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

`harvest_vmcore` becomes `Path | None`:

1. list cores, `select_newest` (unchanged, pure);
2. `None` when none present (→ `READINESS_FAILURE` upstream, unchanged);
3. `chosen.size_bytes > max_bytes` → `CONFIGURATION_ERROR` **before** any download
   (unchanged ordering — the size is read from `statns`, no bytes touched);
4. allocate a caller-owned temp file and `reader.download_vmcore(overlay, chosen.path, dest)`;
   return `dest`.

The real `_LibguestfsCoreReader.download_vmcore` calls `self._guest.download(path, dest)`
(constant-memory streaming through the appliance) instead of `read_file`. It stays
`# pragma: no cover - live_vm`.

`_real_wait_for_vmcore` returns the spooled `Path` (and no longer closes the reader before
the caller has the file — the file is on the host, independent of the appliance handle).

`LocalLibvirtRetrieve.capture` KDUMP branch operates on the `Path`:

- build-id via `read_core_build_id_from_file(path)` (already Path-based);
- redacted dmesg via the Path-based extractor (already Path-based);
- raw core persisted via `store.put_stream(ArtifactStreamRequest(path=..., sha256_b64=...))`
  — streamed from disk, mirroring `host_dump_capture._store_core` (ADR-0094), with the same
  post-put `head` checksum verification;
- the spooled temp file is deleted in a `finally`, whether or not the put succeeds.

The injected build-id / extract-redacted seams that today take `bytes` become `Path`-typed
for the capture path. `run_crash_postmortem`'s build-id check operates on bytes fetched from
the object store and is a **separate** seam — it is not changed.

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
