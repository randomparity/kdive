# Remote module attempt-volume preparation

Issue: #2170

Governing decisions: [ADR-0588](../../adr/0588-remote-module-volume-ownership-lives-in-the-volume-name.md)
and [ADR-0603](../../adr/0603-remote-device-identity-port.md).

## Outcome

Land the remote-libvirt source/scratch preparation slice without ever introducing libvirt's
discarded storage-volume metadata channel. Persist attempt intent before either volume is created,
emit the appliance operation in its framed wire form, and supply the bounded remote-host identity
adapter that server preparation and the module-volume reaper can share.

## Architecture

`remote_module_volumes.py` remains a synchronous provider-private storage primitive. It receives no
repository callback or reusable authorization value. The async server/worker preparation seam
opens ADR-0605's versioned obligation receipt, then passes the exact request and expected attempt
to `run_verified_module_attempt_preparation`. That service verifies the committed open row while
holding the transaction-scoped System advisory lock and invokes one inline consumer for both
volumes. A row without a volume is an expected retry state, so a verified replay continues through
the existing deterministic lookup/create behavior.

The inline consumer submits the whole synchronous two-volume operation once to a bounded,
completion-owned offload. Cancellation shields the underlying future and drains it to actual
completion before re-raising cancellation, so neither the verification transaction nor its System
lock can unwind while libvirt still mutates storage. Repeated cancellation cannot detach the
future or shorten that lifetime. The synchronous primitive checks the one captured provider
deadline before every `createXML`, upload/download stream stage, repair, and cleanup delete. An
expired deadline starts no later stage. A kernel-blocked libvirt call can therefore retain the lock
until it returns; the design does not claim to terminate such a call.

Volume names come only from `render_module_volume_name`. Reopen and deletion compare the name
returned by libvirt with the expected `ModuleVolumeOwner`, then check only the persisted raw format
and `info()[1]` capacity. `PreparedVolume.identity`, `_METADATA_NS`, `_identity`, and `_metadata` do
not land. Source content remains bound by the existing full-image readback and manifest comparison.
Deletion continues to require `AttachmentInspection.proves_detached` before any lookup or delete.

The operation bytes supplied to the image writer are `RemoteModuleOperationV1.to_wire_bytes()`.
The producer-facing constructor accepts the typed operation rather than arbitrary pre-encoded
bytes, so callers cannot accidentally select the unframed canonical representation. The image
writer still stores bytes and its readback test drives the appliance's real framing reader.

## Remote-host identity adapter

ADR-0604 already supplies the bounded `resolve-device-identity` operation beneath ADR-0606's typed,
Resource-bound `AuthorityRequestSender`, plus `RemoteAuthorityDeviceIdentity`. Server preparation
calls `build_remote_device_identity(runtime.authority, preparation_deadline)` after Resource
rebinding and fails closed when no authority sender is present. #2170 adds no callable transport,
host selector, credential, listener, serializer, or duplicate response parser.

The landed adapter recomputes the remainder from the one captured preparation deadline, translates
it to the private event loop's clock, and sends only the normalized absolute path through the fixed
mutual-TLS route. The provider host follows aliases with `stat(2)` and returns the strict versioned
absent/inode/block result. Malformed success is a redacted `CONFLICT`; timeout, authentication,
transport, lookup, and service-capacity failures are redacted `INFRASTRUCTURE_FAILURE`. Its
existing symlink, hard-link, bind-alias, duplicate block-node, and distinct-device regressions are
the contract #2170 composes rather than reimplements.

## Data flow and failures

1. Server preparation obtains ADR-0605's committed obligation request and binds ADR-0604's identity
   adapter from the already Resource-bound authority sender and absolute deadline.
2. The worker verifier checks the receipt and open row while acquiring the System advisory lock.
3. Its inline consumer submits one completion-owned two-volume operation and retains the lock until
   that underlying operation finishes, including after caller cancellation.
4. Attachment inspection uses the ADR-0604 identity port to compare protected and observed remote
   identities before storage mutation.
5. The synchronous operation checks its captured deadline before each mutation/stream stage, builds
   and verifies the framed source image, creates missing deterministic volumes, uploads source
   bytes, reopens both, and validates them.

Identity absence and malformed values are conflicts. Timeout and lookup/transport failure are
infrastructure failures. Volume ownership, raw-format, capacity, and content mismatches remain
conflicts; libvirt operations retain their existing infrastructure-failure mappings. Rollback
deletes only volumes created by the current call and only after positive detachment evidence.

## Threat model

The provider-host identity boundary and its malformed-response controls are inherited unchanged
from ADR-0604. This change adds the server-written receipt crossing to the worker verifier and the
async-to-synchronous libvirt offload boundary. The receipt is evidence to look up, not a bearer
capability; exact tuple/read-only-row verification and the System lock authorize one inline
consumer. Completion ownership prevents cancellation from letting that consumer outlive the lock.

The design widens no agent, generic provider, credential, or command surface. The server-side
call accepts only one validated path and performs only following `stat`; it cannot select a host,
program, argv, environment, or credential. Privileged host interference after inspection remains
outside the trust boundary under ADR-0585's serialized mutation authority. Compromise of the
authenticated provider host is out of scope: that actor already controls libvirt storage and can
forge or destroy the objects being inspected.

## Verification

- Shared fake-storage tests prove submitted metadata is discarded while preparation succeeds.
- Name/readback tests cover foreign names, wrong kind, wrong raw format, capacity mismatch, and
  preservation of `proves_detached`.
- A shared call log proves one obligation open precedes both `createXML` calls; row-only retry
  creates the normal missing volumes without an orphan error.
- The source-image test calls the appliance's real operation reader and proves wire framing.
- Composition tests prove server preparation uses only the Resource-bound ADR-0604 sender and its
  captured deadline, and fails closed when the route is absent.
- Blocked-call and repeated-cancellation tests prove the verifier transaction/System lock remains
  held until underlying completion; completion then re-raises cancellation and a verified retry
  can acquire the lock and reopen the deterministic volumes.
- Deadline tests expire each mutation/stream boundary independently and prove no later stage starts.
- Focused tests, lint, type checking, the ordinary suite, and `just ci` run before publication.

## Exclusions

The reconciler sweep (#2168), appliance (#2169), operation runtime and reap journal (#2171),
obligation discharge lifecycle (#2172), phase orchestration (#2173), and recorded source capacity
(#2154) remain separately owned. This change does not add SSH, a generic remote command API, a new
network listener, credentials, persistence, migration, or an agent-facing contract.
