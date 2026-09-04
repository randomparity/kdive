# External-boot command-line readiness design

## Scope and authority

Issue #2175 closes the implementation gap in accepted ADR-0583. The running-kernel observation
must carry the bytes read from `/proc/cmdline` after removing exactly one trailing newline. Core
must compare those bytes with the UTF-8 encoding of the persisted plan command line, alongside the
existing architecture, release, and GNU build-ID proof. This applies to local-libvirt and
remote-libvirt. Real-hardware proof remains with #2174, and live ppc64le testing is excluded from
this campaign.

The existing ADR settles the contract, so this change makes no new architecture decision and adds
no ADR. `RunningKernelObservation` remains the provider-neutral value. Its new `cmdline: bytes`
field is required and participates in canonical serialization and equality.

## Data flow

Each provider reads the live guest and returns one observation. Provider reads require a final
newline and remove that one byte only; an absent newline is a truncated read and any second newline
remains data. The remote provider keeps using its bounded qemu-guest-agent reads. The local
provider supplies the same observation through its injected `RunningObserver`; production binding
and guest-agent channel reachability remain owned by #2212, which is the issue that makes the local
port live.

The core lifecycle receives the observation after `activate` or `recover`. It first requires a
materialization and observation, then compares kernel identity fields with the persisted expected
observation and command-line bytes with `activation.plan.cmdline.encode("utf-8")`. The plan is the
authority for the command line: materialization captures the build-time kernel identity, not a
second copy of the requested boot arguments.

## Failure contract

A command-line mismatch is a terminal-on-this-attempt `READINESS_FAILURE`, so the existing worker
failure mapper records failure and follows the recovery path. Its diagnostic includes escaped
expected and observed strings and the zero-based first differing byte offset. The offset is the
shorter length when one byte sequence is a strict prefix of the other. Escaping preserves evidence
without allowing embedded control characters to alter logs.

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

Port tests prove the required byte field and XML-illegal C0 rejection. Core lifecycle tests prove
exact acceptance plus truncated, reordered, and appended command-line failures, including offset
and diagnostic values and terminal readiness classification. Remote tests prove exactly-one-newline
handling and that the provider returns bytes without performing the core comparison. Local tests
prove its observation seam preserves the exact bytes and rejects mismatches only in core. Existing
provider contract bindings gain canonical command-line bytes.
