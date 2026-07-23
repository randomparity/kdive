# artifacts toolset

Artifacts are the files a run or system produces or consumes: console logs, kernel images,
build outputs. Reach for these to read evidence after a boot or crash, and to upload a
prebuilt kernel on the external build lane. For exact parameters, types, and return schema,
read each tool's own description.

## Reading evidence

- `artifacts.list` — list the artifacts available for a run or system, by name.
- `artifacts.get` — fetch an artifact's bytes in a token-safe window.
- `artifacts.find` — locate a crash signature in a large console log without pulling the
  whole file.
- `artifacts.fetch_raw` — get a download URL for a large or binary artifact (such as a
  vmcore or vmlinux) instead of inlining its bytes.

## Uploading a build

- `artifacts.expected_uploads` — learn the exact artifacts and byte layout a run expects
  before you upload.
- `artifacts.feature_config_requirements` — advisory map of each debug feature to the
  kernel `CONFIG_*` it needs, so you can build them in before uploading.
- `artifacts.create_run_upload` — mint presigned PUTs for a run's build artifacts.
- `artifacts.create_system_upload` — mint presigned PUTs for a system's artifacts.

## Uploading a large rootfs (gzip transport encoding)

A system's rootfs is uploaded as a single PUT (chunked upload is rejected), so a canonical qcow2
larger than the 5 GiB single-PUT cap cannot be uploaded directly. Instead, gzip the qcow2 and
declare a transport encoding on `artifacts.create_system_upload`: set `encoding: "gzip"` and
`uncompressed_size` to the canonical (decompressed) qcow2 size in bytes. kdive strips the gzip on
download — streaming and bomb-bounded against `uncompressed_size` — then verifies the qcow2 magic
before the image backs the guest, so a wrong-format or truncated upload is rejected early with a
clear message rather than failing late at boot.

Constraints: gzip is the only encoding; `uncompressed_size` is required with it and is capped at
50 GiB; `encoding` cannot be combined with `chunks`; and `sha256`/`size_bytes` describe the
uploaded (compressed) bytes. Transport encoding is a rootfs-only surface —
`artifacts.create_run_upload` rejects it, since build artifacts are validated and uploaded as-is.
