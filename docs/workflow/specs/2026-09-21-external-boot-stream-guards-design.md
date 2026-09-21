# External-boot stream guards (#2571)

## Scope and success

Authority is issue #2571, frozen scope token q2571-6b3d38e8, and the operator-approved
campaign dispatch. Replace the separate decompressing preflight with a bounded raw tar
reader interposed between one `gzip.GzipFile` and `tarfile.open(mode="r|")`.
[ADR-0671](../../adr/0671-external-boot-stream-guards.md) records the alternatives.
The validator keeps ownership; its callers and evidence schema do not change.

The same object must produce field-for-field identical external-boot-evidence-v1.
Raw headers (including GNU/PAX extensions) retain their own count, separately from
logical members. Header bytes plus rounded-up payload bytes retain their raw budget.
Extension limits are checked before releasing that header or payload to tarfile.
The existing logical-member, regular-file-byte, and boot-member checks remain.

## Global Constraints

Python 3.14; stdlib only. Preserve the values and accounting of
`_EXTERNAL_BOOT_EXTENSION_MAX_BYTES`, `_EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS`,
`_EXTERNAL_BOOT_ARCHIVE_MAX_BYTES`, and `_EXTERNAL_BOOT_MEMBER_MAX_BYTES`.
No evidence schema/value changes, Semaphore(1) changes, other validation optimization,
chunk tuning, or digest-pass removal. Mandatory POWER remeasurement is excluded.

## Design

`_GuardedTarReader(source: _BinaryReader)` owns the former preflight's raw state:
header count, padded byte count, pending 512-byte header, remaining payload, and EOF.
`read(size: int = -1) -> bytes` returns at most the requested positive size, bounded
by the existing decode chunk for payload reads; a negative size selects that quantum.
It serves one validated header or payload fragment at a time. Zero reads do no work.
`TarInfo.frombuf` remains the parser for checksum, numeric fields, and type; there is
no new tar grammar and no private CPython interposition.

A nonempty short 512-byte header is rejected. Payload EOF before the padded size is
rejected. The first zero header or header-boundary EOF ends raw accounting, as today.
Keep the same four extension types and the same order of member, extension, then
raw-byte guards. Do not reinterpret PAX size overrides or GNU sparse records in this
reader: the existing raw preflight does not do so either. Tarfile remains responsible
for logical interpretation and the existing validator for member semantics.

After successful tar iteration, drain the guarded reader to its own end. This is
necessary because tarfile can stop before the independent raw walker; no accepted
archive may skip raw accounting that the old preflight performed. A semantic failure
may stop immediately because the archive is already rejected and tarfile cannot
consume any later unchecked extension. The source is consumed once, without seeking.
Stop at the same first raw zero header; compressed trailing bytes remain owned by the
existing exact-once bundle digest's drain.

## Failure model

- Actors and deployments: authenticated tenants uploading gzip tar bundles to ordinary
  server finalization on x86_64 or ppc64le; trusted store implementation may fail reads.
- Invariants and assets: bounded extension allocation, raw header/byte accounting,
  unchanged evidence, one archive decompression pass, categorized errors.
- Accepted failure classes: error precedence among multiple invalid members may move
  with interleaving; no success may bypass a pre-existing raw bound. Existing limits
  bound work rather than total process RSS. First-zero termination remains unchanged.
- Covered elsewhere: transport/authz and upload immutability remain their existing
  owners; cancellation/admission and POWER timing belong to ADR-0656 and excluded work.

## Threat model

- Boundary inventory: tenant-controlled compressed bytes cross into gzip, then raw tar
  framing, then tarfile extension allocation. No new external boundary is introduced.
- Actor model: an authenticated tenant can control lengths, types, payloads, checksum,
  extensions, padding, and compression ratio; stdlib and store implementation are trusted.
- Controls: existing compressed-object cap and ranged-response bound; bounded gzip reads;
  validated raw headers and unchanged count/extension/padded-byte limits before tarfile
  receives them; existing decoded-kernel, logical-member and file limits after parsing.
  Failures expose existing categorized messages and limits, not uploaded payloads.
- Out of scope: retuning limits, aggregate concurrency, malicious store implementations,
  new archive formats, or changing the accepted artifact/evidence contracts.

## Validation

Before moving guards, add raw member and padded-byte regressions, run green, disable
each corresponding condition in a controlled local fault, observe red, and restore.
Keep the existing PAX-ordering and gzip-bomb tests byte-identical. Test all four guarded
extension types with a spy proving the payload never reaches tarfile, exact boundaries,
padding, malformed/truncated headers and payloads, EOF, source errors and varied read
sizes. Existing ranged-reader tests cover short, oversized, empty and failed store reads.
Compare complete evidence against a pre-change snapshot for x86_64 codecs and ppc64le.
Record one-pass read/decompression evidence and update the historical measurement record
without extrapolating new timings. Run focused tests, lint/type/docs/records guards,
iterating adversarial review, security review, final CI and the mandatory pre-push gate.
