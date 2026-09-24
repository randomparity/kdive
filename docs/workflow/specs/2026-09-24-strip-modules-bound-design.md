# Strip staged modules and reject oversized bundles early

Status: implementation design for #2733.

## Scope and contract

The live-stack spine builder installs modules with `INSTALL_MOD_STRIP=1` while leaving the
build-tree `vmlinux` untouched for debugging. The external-build upload recipe and its served
MCP copy tell builders to use the same flag and state that the installed module tree has a
2 GiB cumulative uncompressed regular-file limit for local-libvirt legacy installs that need
modules. A module tree above that limit is rejected at job planning before the provider extracts
it. The provider's install-time bound remains a defense against old or malformed build evidence.

The upload validator already scans the exact object version and records
`module_uncompressed_bytes` in the immutable build. The legacy install job reads that evidence
for a local-libvirt install needing modules, using the same bound as its provider extractor.
Authority-marked staging-only jobs bypass this check because they do not run that extractor.
The check does not trust compressed size or caller-supplied metadata. External boot retains its
separate 8 GiB module limit; a global finalization check would reject bundles it supports.

No upload metadata, object layout, or provider-plan field changes. Raising the 2 GiB limit and
compression tuning belong to separate work.

## Acceptance and verification

- A focused test observes `INSTALL_MOD_STRIP=1` in the spine's `modules_install` argv.
- Legacy install planning accepts a measured module tree exactly at the configured limit and rejects
  one byte above it with a configuration error naming the 2 GiB limit. The rejection occurs
  before provider extraction. External boot, authority-marked staging-only jobs, and boot-only
  legacy installs retain their own behavior. A missing referenced build record fails closed.
- Both recipe copies show the flag and state the limit and its recovery: strip or reduce the
  module tree, rebuild the archive, and upload again.
- Existing kernel-bundle tests continue to prove the worker-side cap and partial-file cleanup.
- Native ppc64le live install proof is required before merge; without a POWER host the PR stays
  draft. Run the affected live-stack spine path against a matching deployed checkout.

## Failure model

An untrusted tar member can lie about its size or contain a compressed bomb. The existing
bounded finalization scan and complete-object digest verify the readable bytes before storing
the measured total. The provider still enforces the limit while extracting. An old build without
measurement falls through to that guard. A missing referenced build record must fail closed,
not silently skip the preflight. A module tree at the exact limit remains valid. A boot-only run
does not repack modules and need not pass this path-specific gate.

## Threat model

The uploader controls gzip and tar bytes, including member names and declared sizes. The
server-side finalization scanner and install worker are trust boundaries. The new comparison
uses the server-measured cumulative regular-file size and the shared install constant; it adds
one indexed build-record read, no parser, and no decompression buffer. Existing member-count,
raw-byte, path, and archive-version guards remain in force.
