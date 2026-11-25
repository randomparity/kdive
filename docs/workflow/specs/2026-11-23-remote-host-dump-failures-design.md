# Remote host-dump failure boundaries

## Problem

Required stale-volume deletion currently suppresses failure. Fresh-volume cleanup starts
only after capacity checking and spool creation, while unlink failure skips later cleanup
and replaces an active persistence error. Refresh and local size lookup can escape raw.

## Scope and architecture

Keep orchestration with `host_dump_capture.py`; no owner/caller migration is needed.
`RemoteLibvirtRetrieve.capture` and `HostDumpCapturer.capture` signatures stay unchanged.
Apply [ADR0850](../../adr/0850-remote-host-dump-failure-boundaries.md) in three local source
commits: required stale delete; independent cleanup; refresh/spool/stat categorization.
Existing expected digest, stream and artifact behavior remains. No database change is needed.

## Success

1. Native stale-delete failure chains an infrastructure error naming operation and volume;
   that invocation makes no dump, fresh download or object-store write.
2. Once a fresh handle is resolved, capacity checks, spool creation, download and persistence
   are inside its cleanup boundary. Native unlink/rmdir/volume-delete failures each warn
   independently and preserve the active exception or successful capture result.
3. Refresh libvirtError and mkdtemp/stat OSError chain infrastructure errors with operation,
   System ID and volume or path. The existing digest error shape stays unchanged.
4. The native expected cleanup failures in criterion2 emit no raw exception text/traceback;
   private spool modes, stream ceilings/abort and artifact redaction/provenance stay intact.

## Failure model

- Actors/deployments: operator-configured remote libvirt over the existing TLS connection;
  synchronous worker capture, local private filesystem and configured object store.
- Assets/invariants: sensitive core bytes; correct primary error/cause; required pre-delete;
  bounded private spool; distinct fresh cleanup evidence; existing lease/reaper ownership.
- Accepted classes: native cleanup failure can leave an owned spool/volume, with warning;
  this unit does not add retries or claim deletion succeeded. SIGKILL/OOM and unexpected
  programming/process-control failures are not broadly suppressed. Before a fresh handle
  is available there is no handle to delete safely; existing next-capture/reaper owns residue.
- Covered elsewhere: connection/TLS lifetime in retrieve.common; stream abort/limits in
  ADR0848; capture serialization and orphan sweep in ADR0562/ADR0094; final campaign CI.

## Threat model

- Boundaries: unchanged configured host XML/volume download, local sensitive spool and store.
  No new/widened boundary or privilege is introduced.
- Actors: authenticated tenant requesting capture; trusted operator configures libvirt/store.
  Existing higher-level authorization and capture lease admit calls before provider execution.
- Controls: keep defused pool XML, private0600 file/0700 directory, bounded stream, redacted
  dmesg and execution-scoped conditional writes. Warning context is generated System/volume
  and owned path; provider diagnostic text/traceback is omitted from new cleanup warnings.
- Outside scope: hostile local privileged operator, transport/store redesign and host policy
  remain with existing owners. Tests inject native failures; no remote hardware proof is claimed.

## Validation

Three focused-test entries are detailed in the private implementation plan. Drive the public
capture boundary, verify no unintended store writes, exact cause/identity, independent cleanup,
real private-file absence and warning redaction. Correct ceiling/info fakes to distinguish
stale and fresh handles. Run retrieval/remote-module/capture regressions, lint, whole types,
hooks and actual wheel bytes/198SQL. Controlled faults and source restoration prove tests bite.
