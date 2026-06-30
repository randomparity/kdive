# artifacts toolset

Artifacts are the files a run or system produces or consumes: console logs, kernel images,
build outputs. Reach for these to read evidence after a boot or crash, and to upload a
prebuilt kernel on the external build lane. For exact parameters, types, and return schema,
read each tool's own description.

## Reading evidence

- `artifacts.list` — list the artifacts available for a run or system, by name.
- `artifacts.get` — fetch an artifact's bytes in a token-safe window. Use its optional
  `find` / `direction` jump-cursor to locate a crash signature in a large console log
  without pulling the whole file.
- `artifacts.fetch_raw` — get a download URL for a large or binary artifact (such as a
  vmcore or vmlinux) instead of inlining its bytes.

## Uploading a build

- `artifacts.expected_uploads` — learn the exact artifacts and byte layout a run expects
  before you upload.
- `artifacts.create_run_upload` — mint presigned PUTs for a run's build artifacts.
- `artifacts.create_system_upload` — mint presigned PUTs for a system's artifacts.
