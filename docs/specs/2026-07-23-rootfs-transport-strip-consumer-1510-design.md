# Design — Rootfs consumer: transport-strip, streaming fetch & qcow2 format check (#1510)

- **Issue:** #1510 (epic #1508 Sub 2). **ADR:** [ADR-0438](../adr/0438-rootfs-transport-strip-streaming-fetch.md).
- **Migration:** none.
- **Scope:** the local-libvirt uploaded-rootfs fetch — resolve the declared transport encoding,
  streaming-strip a gzip to the staged qcow2 base, and validate qcow2 magic on the canonical base.
  **Out of scope:** remote-libvirt parity + agent-surface docs (Sub 3, #1511), catalog-rootfs magic
  check (pre-vetted), the declaration side (Sub 1 / on main).

## Problem

Sub 1 landed the declaration model and the shared `strip_gzip_to_writer` utility but wired no
consumer. The rootfs install path (ADR-0434) buffers the whole uploaded object into RAM and stages
it verbatim, and validates only the checksum. So a `gzip(qcow2)` upload is staged as an invalid
qcow2, and a wrong-format upload fails late at `qemu-img`. See ADR-0438 for the full decision.

## Requirements → design

### 1. Encoding source: the DB manifest, read in the fetch's `from_env` wrapper

The presign stamps no `content-encoding` (only sensitivity/retention), so the manifest is the sole
source. Mirroring `rootfs_catalog_fetch_from_env` (ADR-0228), `rootfs_upload_fetch_from_env` opens a
short-lived sync `psycopg` connection per call, reads the systems `rootfs` manifest entry, and
passes `(encoding, uncompressed_size)` to the pure fetch:

```python
def rootfs_upload_fetch_from_env() -> UploadFetch:
    def _fetch(upload: RootfsUploadContext) -> Path:
        with psycopg.connect(config.require(DATABASE_URL)) as conn:
            encoding, uncompressed_size = read_rootfs_upload_encoding(conn, upload.system_id)
        return fetch_uploaded_rootfs(
            object_store_from_env(), upload,
            encoding=encoding, uncompressed_size=uncompressed_size,
        )
    return _fetch
```

`read_rootfs_upload_encoding(conn, system_id) -> tuple[str | None, int | None]` reads the manifest
via a new **sync** `upload_manifest.get_manifest_sync` (a sync twin of `get_manifest`, reusing
`_entry_from_payload`), finds the `"rootfs"` entry, and returns
`(normalize_encoding(entry.encoding), entry.uncompressed_size)`. A **missing manifest / missing
entry ⇒ `(None, None)`** (identity fallback — today's behavior; a stray gzip then fails at the magic
check). `normalize_encoding` collapses absent/`"identity"` to `None`.

`RootfsUploadContext` is **unchanged** — encoding is a fetch argument, not a context field (see
ADR-0438 "Considered & rejected").

### 2. Streaming strip on the gzip path — `rootfs_upload_fetch.py`

`UploadObjectStore` Protocol gains `get_range(key, *, start, length) -> bytes` so it satisfies
`transport_encoding.RangedReadStore` (`ObjectStore` already implements it).

`fetch_uploaded_rootfs(store, upload, *, encoding, uncompressed_size)`:

- Reuse a present staged `dest` (already verified + magic-checked when staged).
- HEAD the key: absent ⇒ `CONFIGURATION_ERROR` ("never uploaded"); no `checksum_sha256` ⇒
  `INFRASTRUCTURE_FAILURE` (both unchanged).
- **gzip branch** (`encoding == GZIP_ENCODING`): require `uncompressed_size` (a gzip encoding
  without it ⇒ `CONFIGURATION_ERROR`, defensive — the declaration validator already enforces it).
  Open the `.partial` temp for writing and call
  `strip_gzip_to_writer(store, StripDecodeRequest(key, compressed_size=head.size_bytes,
  expected_sha256=head.checksum_sha256, uncompressed_size=uncompressed_size), writer)`. The utility
  streams ranged reads, bounds output (bomb guard), and verifies the compressed hash. On success,
  magic-check the temp, then `os.replace` into `dest`. Any raise unlinks the temp.
- **identity branch** (`encoding is None`): unchanged behavior — `get_artifact(key, None).data`,
  verify `sha256(data) == head.checksum_sha256`, magic-check `data[:4]`, then `_atomic_write`.

The gzip decode wraps the same atomic-staging discipline: write to `.partial`, validate, `os.replace`.

### 3. qcow2 magic check (both paths), scoped to upload

`_QCOW2_MAGIC = b"QFI\xfb"`. A helper `_require_qcow2_magic(first4: bytes, *, system_id)` raises a
`CONFIGURATION_ERROR` `CategorizedError` ("staged rootfs is not a qcow2 image: it does not start with
the qcow2 magic; the uploaded object (after any transport decode) must be a qcow2") when
`first4 != _QCOW2_MAGIC`. Called on `data[:4]` (identity) and on the first 4 bytes read back from the
`.partial` temp (gzip) **before** `os.replace`, so a bad base never becomes `dest` and never reaches
`qemu-img`. Living in the fetch scopes the check to the upload path only; catalog bases (resolved via
`catalog_fetch`) are not touched.

### 4. Declaration cap

No change — Sub 1's `_SYSTEM_UNCOMPRESSED_CAP` (50 GiB) already gates at declaration.

## Testing (behavior)

Unit (`tests/providers/local_libvirt/test_rootfs_upload_fetch.py`), with an in-memory fake store
implementing `head`/`get_artifact`/`get_range`:

- **gzip happy path:** a `gzip(qcow2)` object staged → the `.partial` is decoded, magic-valid, and
  `dest` holds the qcow2 canonical bytes; `get_artifact` is **not** called (streamed, not buffered).
- **gzip bomb:** decoded output exceeds `uncompressed_size` ⇒ `CategorizedError` (bomb message from
  the utility); `dest` absent.
- **non-qcow2 canonical (gzip and identity):** decoded/staged bytes don't start with `QFI\xfb` ⇒
  `CONFIGURATION_ERROR` naming the format; `dest` absent.
- **identity happy path + magic:** an unencoded qcow2 stages unchanged and passes the magic check.
- **identity checksum mismatch:** unchanged `INFRASTRUCTURE_FAILURE`.
- **manifest encoding threading:** `read_rootfs_upload_encoding` returns the entry's
  `(encoding, uncompressed_size)`; absent manifest/entry ⇒ `(None, None)`.
- **transport checksum mismatch (gzip):** stored bytes' sha256 ≠ signed ⇒ `CONFIGURATION_ERROR`.
- **sync manifest read** (`tests/.../test_upload_manifest*` or a focused test): `get_manifest_sync`
  round-trips an entry written by `replace_manifest`, including a non-identity encoding.

Integration/live (best-effort, `require_live_vm_provisioned` if the harness allows): a `gzip(qcow2)`
with canonical >5 GiB / compressed ≤5 GiB provisions end-to-end. The unit fetch+magic proof is the
minimum bar.
