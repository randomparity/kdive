# External-boot command-line readiness design

## Scope and authority

Issue #2175 closes the implementation gap in accepted ADR-0583. The running-kernel observation
must carry the bytes read from `/proc/cmdline` after removing exactly one trailing newline. Core
must compare those bytes with the UTF-8 encoding of the persisted plan command line, alongside the
existing architecture, release, and GNU build-ID proof. This applies to local-libvirt and
remote-libvirt. Real-hardware proof remains with #2174, and live ppc64le testing is excluded from
this campaign.

The existing ADR settles the contract, so this change makes no new architecture decision and adds
no ADR. The current three-field value is renamed `KernelIdentity` and remains the type of
`ExternalBootMaterialization.kernel_observation`; its field name and canonical JSON are unchanged.
A transient `RunningKernelObservation` carries `identity: KernelIdentity`, `cmdline: bytes`, and
`expected_cmdline: bytes`. It is returned by `ExternalBootPorts.observe` but is never embedded in
an identity-bearing or persisted value. Existing `external-boot-materialization-v1` bytes,
identities, and database rows therefore remain valid without a schema change or migration.

## Data flow

Each provider reads the live guest and returns one observation. Provider reads require a final
newline and remove that one byte only; an absent newline is a truncated read and any second newline
remains data. Content is bounded at 2,048 bytes, one byte above the maximum valid plan, so an
appended byte is observable while an oversized capture fails as malformed evidence. The remote
provider keeps using its bounded qemu-guest-agent reads.

The existing remote `GuestAgentExec` moves behavior-preservingly to a shared libvirt module, with its
allowlist, two-phase polling, per-call and whole-command timeouts, base64 decoding, and libvirt
error classification unchanged. Both providers consume that seam. Output bounding remains owned by
each observation reader, as it is today. The local
`RunningObserver` receives its session's already opened domain and executes only fixed absolute
`uname` and `cat` arguments. The local domain renderer adds the standard virtio guest-agent channel
for newly provisioned Systems, and the observer fails with an actionable readiness error when an
older System lacks it; reprovisioning that System is the recovery action. #2212 still owns
assembling and advertising the completed local external-boot port, but consumes this concrete
observer rather than inventing one.

The provider's durable recovery definition already holds the plan-derived target XML or explicit
expected command line, tied to the recovery point's plan identity. Each provider returns those
expected bytes beside the live bytes. After `activate`, core compares the live identity with
`materialization.kernel_observation` and compares `cmdline` with `expected_cmdline`. Recovery,
conflict resolution, and release retain ADR-0595's source-receipt criteria and do not require a
running target. Providers
validate that their expected bytes come from the exact recovery definition before returning them;
the port is the trusted boundary through which core already receives live kernel identity.

## Failure contract

A command-line mismatch is a terminal-on-this-attempt `READINESS_FAILURE`, so the existing worker
failure mapper records failure and follows the recovery path. Core puts the two bounded raw byte
values and zero-based first differing byte into categorized error details under a closed mismatch
discriminator. The exception is never logged before the authority runner suppresses it. The runner
recognizes only that exact detail shape, surrogate-decodes and redacts both values with its injected
process `SecretRegistry`, renders them safely, and places an
`external-boot-cmdline-mismatch-v1` diagnostic inside `failure_context`. The existing
outer `external-boot-authority-result-v1` remains unchanged.

The diagnostic has exactly `schema`, `expected_cmdline`, `observed_cmdline`, and
`first_differing_byte`. Bytes first decode as UTF-8 with `surrogateescape`, preserving invalid bytes
as distinct low surrogates while leaving printable secret text matchable. After redaction, a fixed
renderer preserves printable Unicode, doubles a literal backslash, renders ASCII controls and
surrogate bytes as `\xNN`, and renders other non-printable scalars as `\uNNNN` or `\UNNNNNNNN`.
Thus NUL and every C0 byte become PostgreSQL-safe ASCII, while an invalid byte and literal
backslash escape remain distinguishable. Each rendered string is capped at 8,192 UTF-8 bytes after
rendering, and the offset is an integer from 0 through 2,048. The offset is the shorter byte length
when one sequence is a strict prefix. These bounds retain every admitted 2,048-byte observation
even when each byte expands to a four-character escape.

Migration `0130` changes only `commit_external_boot_authority_result`'s validation of a `fail`
result. It admits the optional versioned diagnostic beside `phase`, checks exact keys, types,
schemas, byte bounds, and offset range, then persists it in the existing `jobs.failure_context`
JSONB column. Old phase-only failures and readers remain valid. The runner receives the same
process-owned `SecretRegistry` already present in `WorkerHandlerAssembly`; it never persists raw
values and never relies on response-time redaction.

Unavailable provider evidence retains the provider seam's existing retry behavior until the
activation readiness deadline. Malformed, oversized, unterminated, or mismatched live evidence is
terminal. Remote provider-local comparison is removed once core owns the check, avoiding two
different diagnostics for one contract.

## Shared input validation

Platform arguments are embedded in XML by both providers. The shared validator rejects every
character XML 1.0 cannot represent, rather than normalizing it. Because platform arguments are
ASCII and already reject whitespace and NUL, the added rule rejects C0 controls other than tab,
line feed, and carriage return; those three are already rejected as whitespace. Provider renderers
may retain defensive parsing checks, but the shared port is the trust-boundary control.

## Threat model

The design adds no anonymous or tenant-facing entry point. It widens three existing boundaries:

- A running guest controls qemu-agent replies. The shared executor admits only fixed absolute
  programs, bounds time, validates the reply protocol, and classifies transport failures. Each
  provider observation reader bounds decoded output before validating identity fields and command
  lines.
- Plan-derived and guest-derived command lines cross into failure persistence. Core renders bytes
  deterministically, the authority runner redacts with the process registry before constructing the
  closed diagnostic, Pydantic applies byte/range bounds, and migration `0130` independently repeats
  exact-key and bound validation before storing JSONB.
- Newly provisioned local guests receive a standard virtio channel. The channel reaches only the
  guest agent already installed by the image role; the executor's program allowlist remains the
  operation boundary, and no user-supplied program or shell text reaches it.

Trusted actors are the worker process, provider implementations, authority commit function, and
operator-managed image/provisioning configuration. An authenticated tenant can influence
`debug_cmdline` but cannot select an executable or bypass redaction. Out of scope are compromise of
the worker, libvirt daemon, or guest-agent package and retrofitting existing Systems in place;
reprovisioning is the explicit recovery for a System without the channel.

## Testing

Port tests prove the transient byte fields, byte-identical legacy materialization serialization,
and XML-illegal C0 rejection. Core lifecycle tests prove
exact acceptance plus truncated, reordered, and appended command-line failures, including offset
and diagnostic values and terminal readiness classification. Remote tests prove exactly-one-newline
handling and that the provider returns bytes without performing the core comparison. Local tests
prove the production guest-agent observer, missing-channel recovery diagnostic, channel rendering,
and exact byte preservation. Authority model, runner, and database tests prove raw registered
secrets never reach the versioned diagnostic, NUL/C0/invalid-byte values persist only in the safe
escaped form, malformed or oversized diagnostics are refused, and old phase-only failures still
commit. A native x86_64 `live_vm` case provisions a compatible local
System and proves the real channel plus exact `/proc/cmdline` observation. Provider contract bindings
gain transient expected and observed bytes.
