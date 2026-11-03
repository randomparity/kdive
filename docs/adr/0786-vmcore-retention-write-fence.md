# 0786 — Fence vmcore writes before optional retention

## Status

Accepted (2026-10-17)

The operator approved the schema, public contract and finite legacy transition for #3238.
Maintenance execution and retention activation require separate operator authorization.

## Context

Run-owned vmcores have no reclaim policy. Worker lease expiry and successful publication do not
close older writes: a canceled thread can finish a PUT, and a remote guest can retain a presigned
PUT capability. Deleting deterministic keys after their job expires can therefore recreate evidence
behind a completed cleanup. ADR-0559 already supplies conditional-create arbitration for traffic
capture; the previous vmcore streaming and delegated guest writes used unconditional PUT.

## Decision

Use optional positive `KDIVE_VMCORE_RETENTION_DAYS`, unset disabled with no numeric default.
Only terminal Runs in terminal Investigations become eligible. Use PostgreSQL time and the later
raw/redacted capture timestamp for the pair deadline. Eligibility is earliest reclamation, not a
completion SLA. This adds vmcore policy to ADR-0234 without changing build or host-volume cleanup.

Record each capture invocation's unique raw/redacted destinations before provider I/O in a narrow
vmcore journal. Preserve actual Run ownership. Both local streams and remote signed PUTs require
atomic `If-None-Match: *`; guest command headers carry the signed condition. Admission, replay and
publication validate the journal and monotonic Run retirement under existing Run locks.

Retirement commits its database fence first. Outside database locks, arbitrate each destination
against delayed conditional PUTs: remove verified exact data versions, then conditionally create
and retain a verified zero-byte execution tombstone. A competing data PUT postpones completion.
After the tombstone wins, drain historical data/delete-marker versions in bounded batches while
retaining its exact version. Keep journal state until completion and retain its fence identity
thereafter. Both upload cleanup passes must exclude journal-owned keys in every journal state.
Retired means no crash evidence remains; two zero-byte fence objects and identity rows remain.

Existing Run locks protect synchronous acquisition and analysis. Session loss aborts with
`infrastructure_failure` and discards the result; uninterrupted analysis after lock loss is not
promised. Persist each issued GET URL's actual signed expiration before returning it. Protect
validity through expiration, not external transfer completion afterward. Surface absolute deadlines
and `server_time` on affected reads, and authorized retirement as `not_found`; no new MCP tool.

Legacy unconditional write capability cannot be retrofitted. Use an offline, forward-only
transition for attested stock single-process bundled SeaweedFS: inventory signing identities and
all producers, stop producers and the complete store process, permanently revoke old S3 credentials,
restart preserved storage with new credentials, prove old/anonymous writes denied, then import
catalog-owned legacy pairs and immutable versions without changing capture age. Unknown identities,
extra gateways or unproved request termination block activation. External legacy deployments require
an equivalent backend-owner procedure before rollout. No reset or live maintenance is authorized.

## Consequences

- No new dependency, proxy, authority API or general ownership framework is required.
- Old guest URLs and in-flight conditional requests cannot overwrite a retained tombstone on a
  conforming backend. The pinned bundled implementation checks at serialized publication, and the
  slow signed-body/tombstone proof passes on the pinned image. An isolated build of the pinned
  source with only that publication-time check disabled lets the slow PUT return 200 and fails
  the proof, so the test depends on the backend guard rather than client headers.
- Partial failures retain rows and retry; availability failures have no cleanup completion SLA.
- Migration and deployment require a maintenance window and permanent S3 credential change for
  legacy data. Rollback cannot restore old producers or old credentials.
- Final review includes the schema, read failure boundary, signed URL contract,
  permanent tombstone cost, finite legacy procedure and complete implementation plan.

## Considered & rejected

- **Keep all vmcores.** judgment: does not meet the approved optional reclaim outcome.

- **Trust job expiry or one successful PUT.** verified: at source `0b177a335435`,
  `jobs/handlers/artifacts/vmcore.py` calls capture in `asyncio.to_thread`; remote
  `retrieve/kdump_capture.py` delegates presigned PUT through the guest artifact channel. Neither
  job expiry nor one response revokes that write capability or drains an accepted request.
- **Wait indefinitely for historical writer proof.** judgment: the operator selected destination
  fencing with finite historical transition, not indefinite protection as the retention outcome.
- **Add a proxy or distributed writer authority service.** judgment: unnecessary additional
  ownership surface when existing conditional creation can serialize the destination.
- **Reset every installation.** judgment: ADR-0559's earlier fresh-install choice does not authorize
  discarding this issue's historical evidence; preserve known legacy pairs through explicit offline
  quiescence and permanent capability revocation.
