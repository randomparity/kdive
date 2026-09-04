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

The local provider adds the same bounded qemu-guest-agent commands to its production
`RunningObserver`. The local domain renderer adds the standard virtio guest-agent channel for newly
provisioned Systems, and the observer fails with an actionable readiness error when an older System
lacks it; reprovisioning that System is the recovery action. This is a server-preparation/provider
port prerequisite authorized for #2175. #2212 still owns assembling and advertising the completed
local external-boot port, but it consumes this concrete observer rather than inventing one.

The provider's durable recovery definition already holds the plan-derived target XML or explicit
expected command line, tied to the recovery point's plan identity. Each provider returns those
expected bytes beside the live bytes. After `activate` or `recover`, core compares the live identity
with `materialization.kernel_observation` and compares `cmdline` with `expected_cmdline`. Providers
validate that their expected bytes come from the exact recovery definition before returning them;
the port is the trusted boundary through which core already receives live kernel identity.

## Failure contract

A command-line mismatch is a terminal-on-this-attempt `READINESS_FAILURE`, so the existing worker
failure mapper records failure and follows the recovery path. Its details include
`expected_cmdline`, `observed_cmdline`, and the zero-based `first_differing_byte`. Bytes decode as
UTF-8 with `backslashreplace`; JSON encoding then escapes control characters. This keeps invalid
UTF-8 and byte offsets distinguishable without terminal-control injection. Values are bounded by
the provider's 2,048-byte content limit. The offset is the shorter length when one byte sequence is
a strict prefix. These fields deliberately report the two strings required by #2175; callers must
continue applying the repository redaction registry before persistence or response output.

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

## Testing

Port tests prove the transient byte fields, byte-identical legacy materialization serialization,
and XML-illegal C0 rejection. Core lifecycle tests prove
exact acceptance plus truncated, reordered, and appended command-line failures, including offset
and diagnostic values and terminal readiness classification. Remote tests prove exactly-one-newline
handling and that the provider returns bytes without performing the core comparison. Local tests
prove the production guest-agent observer, missing-channel recovery diagnostic, channel rendering,
and exact byte preservation. Provider contract bindings gain transient expected and observed bytes.
