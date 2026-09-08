# artifacts toolset

Use these tools to read evidence or upload externally built files. Exact argument shapes,
limits, and returned fields belong to each tool's schema.

## Reading evidence

- `artifacts.list` takes a **System ID** and lists its redacted artifacts, newest first,
  across Runs and debug sessions. Follow `data.next_cursor` while `data.truncated` is true.
  For a non-failed Run's console, prefer `runs.get`: `refs.latest_console` selects its newest
  evidence; `include_console_artifacts=true` requests the bounded Run-scoped manifest. Failed
  Runs omit these surfaces: list the bound System's artifacts and inspect available job refs.
- `artifacts.get` takes an **artifact ID** inside `request` and returns redacted text in
  `data.content`. Follow `data.next_offset` when `data.content_truncated` is true. Use `find`
  for literal search; inspect `data.match_found` instead of treating an empty window as a hit.
  An artifact over the fetch ceiling cannot be searched and returns `artifact_too_large`.
  A plain read may offer `refs.download_uri`; `data.content_unavailable` can mean neither
  inline content nor a download is available. These two tools require viewer access.
- `artifacts.fetch_raw` takes a **Run ID** and `asset` (`vmcore`, `vmlinux`, or `pcap`). It
  requires contributor access and returns a sensitive download URL in `refs.download_uri`,
  with no inline bytes. For multiple pcaps, select `artifact_id` from the capture job's
  `refs.result`, or omit it for the newest. See resource://kdive/docs/guide/toolsets/postmortem.md
  for the difference between redacted crash evidence and a raw core.

Only `artifacts.get` nests its read arguments under `request`; the other two reads use
flat parameters. Uploaded build files are not part of the System's redacted listing.

## Uploading files

`artifacts.create_run_upload` creates an external-build Run's upload manifest. Declare the
complete artifact set on each call: a later call replaces the manifest. Upload through the
returned signed URLs and headers, then finalize with `runs.complete_build`. Read the byte
contract at resource://kdive/contracts/external-build and follow the detailed procedure at
resource://kdive/docs/operating/external-build-upload.md, which owns packaging and build upload.

`artifacts.create_investigation_upload` creates signed upload instructions for one
Investigation-owned rootfs. PUT its bytes using the returned URL and required headers, then
finalize with `investigations.complete_rootfs_upload`. Use the returned `checksum_sha256` in a
System's upload-rootfs profile bound to that Investigation. Rootfs uses a single PUT; optional
gzip transport encoding and its size constraints are described in the tool schema. Build
uploads do not accept that transport encoding.

For both upload tools, follow the returned `expires_at`, `manifest_deadline`, `server_time`,
and `on_expiry` contract. URL expiry and the deadline to finalize the whole manifest are
different. Do not replace a manifest while its finalization is running.
