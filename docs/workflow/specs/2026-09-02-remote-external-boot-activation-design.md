# Remote-libvirt external Run-boot activation

Issue: [#2110](https://github.com/randomparity/kdive/issues/2110) — part of epic
[#2105](https://github.com/randomparity/kdive/issues/2105), decomposition entry 13.

Governing decisions: [ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md)
(the provider-neutral external Run-boot contract, normative and incorporated without variation),
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md) (mutation
fencing), [ADR-0585](../../adr/0585-remote-offline-module-restoration-appliance.md) (offline module
restoration), [ADR-0080](../../adr/0080-remote-provisioning-disk-image-profile.md) (remote domain shape),
[ADR-0076](../../adr/0076-remote-libvirt-provider-package.md) (remote-libvirt is independent of
local-libvirt).

## Goal

Give remote-libvirt the pieces that boot a System from the exact finalized kernel, optional initrd,
and command line through direct-kernel domain XML instead of the in-guest GRUB helper, and prove
after boot that the running kernel and effective command line are exactly the ones the plan named.

## Scope

Owned here:

1. The remote direct-kernel XML projection and the two ADR-0583 definition identities.
2. Remote source admission: proving an inactive definition is this System's owned disk/GRUB baseline
   before it is captured as a source.
3. The activation operation: exact-state preconditions, define, start.
4. Guest identity observation: architecture, kernel release, GNU build ID, and `/proc/cmdline`, with
   exact comparison and fail-closed semantics.
5. Golden tests proving the projection changes only the three boot fields and preserves the disk
   overlay, networking, guest-agent channel, console, gdbstub, and capture devices.

Not owned here, with owners:

| Out of scope | Owner |
| --- | --- |
| Recovery to the disk/GRUB baseline | #2120 |
| Offline module capture, replacement, restoration | #2129 |
| Provider-host authority integration and capability advertisement | #2140 |
| Kernel/initrd artifact volumes (consumed, not modified) | #2109 |
| Boot-artifact capacity, reaping, provisioning | #2119 |
| Job execution and reconciliation | #2118 |
| Native live-tier proof | #2121 |

x86_64 work and verification complete first; the native ppc64le proof is deferred to a separate
later run on native POWER hardware.

Because recovery, module publication, and authority integration are separately owned, this change
does not implement `ExternalBootPorts`. It lands the activation primitives those children assemble,
and it does not set `ProviderRuntime.external_boot` — local-libvirt is at the same point for the
same reason (ADR-0587 "#2140 remains responsible for authority integration and advertisement").

## Architecture

One new module, `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`, in three layers
that can each be tested alone:

```
pure XML layer      render_target_xml / preserved_definition_identity / boot_projection_identity
                    require_disk_grub_source
                            |
definition layer    prepare_target_definition(source_xml, plan, materialization, binding, ...)
                        -> RemoteExternalBootDefinition   (closed, canonical-JSON value)
                            |
operation layer     activate_definition(conn, definition)
                    observe_guest_identity(agent_command, domain, definition)
```

The pure layer takes strings and returns strings. The definition layer takes a source XML string
plus the shared ADR-0583 values and returns one frozen value carrying everything a later activation
needs. The operation layer takes injected libvirt and guest-agent seams, exactly as every other
remote lifecycle module does, so unit tests drive every path with no libvirt host.

### Data flow

1. #2118's job reads the System's inactive domain XML over the existing mutual-TLS connection.
2. `prepare_target_definition` admits that XML as an owned disk/GRUB baseline, renders the target
   projection, and computes both identities on both sides plus the expected running-kernel
   observation and expected command line.
3. #2120 stores the resulting `RemoteExternalBootDefinition` in its recovery point; #2140 wraps the
   next step in the authority fence.
4. `activate_definition` re-observes the domain inactive with the exact recorded source XML, defines
   the target XML, and starts the domain.
5. `observe_guest_identity` waits for the guest agent, reads the running-kernel identity and
   `/proc/cmdline`, and compares both exactly.

## Components

### `render_target_xml(source, *, kernel, initrd, cmdline) -> str`

Reproduces ADR-0583's projection exactly: reject non-NFC source, defused-parse, require a `domain`
root, find or create `<os>`, remove any existing `<kernel>`, `<initrd>`, and `<cmdline>` children,
then append `<kernel>`, `<initrd>` when an initrd is present, and `<cmdline>`. Nothing else is
touched — not `<boot dev="hd">`, not `<type arch=…>`, not any device, not `<qemu:commandline>`. The
kdive and qemu namespace prefixes are registered before serialization so they survive.

`<boot dev="hd">` deliberately stays. ADR-0583 excludes only the three boot fields from the
preserved digest, and libvirt ignores `<os><boot>` once `<kernel>` is present, so removing it would
change the preserved digest for no behavioral gain.

### `preserved_definition_identity(domain_xml) -> str` and `boot_projection_identity(domain_xml) -> str`

The ADR-0583 two-part comparison. The preserved digest clones the parsed tree, removes only the
three boot fields, drops whitespace-only `.text` on elements with children and every whitespace-only
`.tail`, canonicalizes with `with_comments=False`, `strip_text=False`, `rewrite_prefixes=True`,
encodes UTF-8 with no trailing newline, and hashes `kdive-libvirt-preserved-v1` NUL those bytes. The
boot projection is compact sorted-key JSON
`{"cmdline":…,"initrd":…,"kernel":…,"schema":"libvirt-boot-projection-v1"}` with `null` for an
absent field, hashed under `kdive-libvirt-boot-projection-v1` NUL. Both are proved against
ADR-0583's three published golden digests.

### `require_disk_grub_source(domain_xml, *, system_id, pool, volume) -> None`

ADR-0583's remote source admission, checked in this order and raising
`CategorizedError(ErrorCategory.CONFLICT)` on the first failure:

1. The boot projection has no kernel, initrd, or cmdline.
2. `metadata/{kdive}system` equals this System id.
3. There is exactly one `<disk device="disk">`, its `<source>` names the expected pool and the
   System's deterministic overlay volume, its `<driver type="qcow2">`, and its
   `<target dev="vda" bus="virtio">`.
4. `<os>` carries exactly one `<boot>`, with `dev="hd"`.
5. `<os>` carries no `firmware` attribute and no `<loader>` or `<nvram>` child, matching what remote
   provisioning renders.

A source carrying external-boot fields fails check 1 rather than being captured. ADR-0583 admits
such a source only while a matching durable activation row owns it, and that row is #2116/#2120
state this module cannot read, so the conflict is raised for its caller to resolve.

### `RemoteExternalBootDefinition`

A closed frozen pydantic value (`extra="forbid"`, canonical JSON) carrying: `binding`,
`plan_identity`, `materialization_identity`, `source_xml`, `source_definition`, `source_boot`,
`target_xml`, `target_definition`, `target_boot`, `expected_running`, `expected_cmdline`. Validators
require both XMLs NFC and both digest pairs to recompute from their recorded XML, so a tampered
record cannot present digests that do not describe its own bytes.

It deliberately does not carry `ProviderStateIdentity`. That value pairs the definition digest with
a module-tree component state, and the module tree is #2129's. #2120 composes the two.

### `prepare_target_definition(source_xml, *, plan, materialization, binding, pool, volume) -> RemoteExternalBootDefinition`

Pure. Validates that `binding` matches `materialization.ownership` and `plan.ownership`, that
`materialization.plan_identity == plan.identity`, and that the materialization carries a kernel
reference (and an initrd reference exactly when the plan carries an initrd). Admits the source, then
renders the target from `materialization.artifacts` and `plan.cmdline`.

The kernel and initrd paths written into the XML are resolved by the caller from the opaque
references #2109 minted, because ADR-0583 forbids a provider path crossing the shared seam and this
module never learns the host's pool directory. The caller passes `kernel_path` and `initrd_path`.

Nothing is re-derived: `cmdline` is `plan.cmdline` verbatim, passed to libvirt as one string with no
tokenizing, quoting, normalization, or shell.

### `observe_guest_identity(agent_command, domain, *, expected_cmdline, ...) -> RunningKernelObservation`

Reads three facts from the running guest through the injected `AgentCommand` seam already used by
the rest of the remote provider, with no `guest-exec` and therefore no program allowlist:

- `guest-get-osinfo` returns `machine` (the architecture) and `kernel-release`.
- A bounded `guest-file-open` / `guest-file-read` / `guest-file-close` sequence on `/proc/cmdline`
  returns the saved command line. Exactly one trailing newline is removed; the remaining bytes must
  equal `expected_cmdline` encoded UTF-8, byte for byte.
- The same sequence on `/sys/kernel/notes` returns the running kernel's ELF notes, parsed by the
  existing `kdive.build_artifacts.validation.parse_gnu_build_id`.

Reads are capped at 65536 bytes and the file handle is closed on every path. An architecture the
shared contract does not name, a release that fails the shared pattern, missing notes, or a command
line that differs by any byte raises `READINESS_FAILURE`, which the taxonomy already marks
non-retryable — ADR-0583's "terminal on this attempt". A transport failure or an agent that is not
answering raises `TRANSPORT_FAILURE`, which stays retryable to the caller's deadline.

Chosen over two alternatives. Adding an `observe` subcommand to the in-guest
`kdive-install-kernel` helper would be one round trip, but it only reaches guests built from a
re-imaged base, so an existing System would fail identity proof for a deployment reason. Allowlisting
`/usr/bin/uname` and `/usr/bin/cat` through `GuestAgentExec` widens the exec allowlist from one
purpose-built helper to two general-purpose binaries for a read this provider can do without exec at
all. ADR-0584 exempts read-only observation from mutation authority, so no fence is required for
this path.

### `activate_definition(conn, definition, *, domain_name) -> None`

The compare-and-set write. It looks the domain up, requires `isActive()` false, requires
`XMLDesc(INACTIVE)` to equal `definition.source_xml` byte for byte, defines `definition.target_xml`,
re-reads the inactive XML and requires it to equal the target, then starts the domain. Every other
combination of observed XML and power performs no write and raises `CONFLICT` with the observed
state in `details`, which is ADR-0583's `recovery_conflict` signal for its caller.

Idempotence: an already-target, already-running domain is the achieved post-state and returns
without a write, so a retried attempt after a lost response converges instead of redefining. An
already-target but inactive domain starts it. This mirrors how `RemoteLibvirtControl.power` treats
ON/OFF as idempotent on the achieved post-state.

ADR-0583's pre-write gate also requires proof of current exclusive mutation authority immediately
before this write. That proof is #2140's; this function takes the connection its caller already
fenced and documents the obligation rather than inventing a second fence.

## Failure handling

| Condition | Category | Retryable |
| --- | --- | --- |
| Malformed, non-NFC, or non-`domain` XML | `INFRASTRUCTURE_FAILURE` | yes |
| Source is not this System's owned disk/GRUB baseline | `CONFLICT` | no |
| Observed XML or power is not an admitted combination | `CONFLICT` | no |
| Running kernel or command line differs from the plan | `READINESS_FAILURE` | no |
| Guest agent unreachable or not answering | `TRANSPORT_FAILURE` | yes |
| Malformed agent reply | `INFRASTRUCTURE_FAILURE` | yes |

No path logs guest bytes verbatim. `details` carries digests, the domain name, the System id, and
bounded observed values that are already provider-internal identifiers.

## Threat model

**Boundaries this change adds.** One: guest-supplied bytes entering the worker through the QEMU
guest agent — `guest-get-osinfo` fields and the contents of `/proc/cmdline` and `/sys/kernel/notes`.
Nothing else crosses a trust level; the libvirt connection, its mutual-TLS credentials, and the
domain lookup are the existing remote boundary this change reuses unchanged.

**Boundaries this change widens.** None. The projection layer consumes domain XML the worker already
reads, and the activation layer writes a definition the worker already has authority to define.

**Actors.** The guest is untrusted: a Run boots a caller-supplied kernel, so guest user space and the
running kernel are under the tenant's control and may lie. The remote libvirt host and its agent
transport are trusted to the same degree the existing remote provider already trusts them. The
operator-configured pool and overlay names are trusted configuration.

**Control per boundary.** Every guest-supplied value is treated as an assertion to be checked, never
as a fact to be recorded. The architecture must be one of the shared contract's two literals; the
release must match the shared `KernelRelease` pattern; the build ID must parse out of well-formed
ELF notes and match the shared hex pattern; the command line must equal the plan's bytes exactly.
Each read is bounded at 65536 bytes with the handle closed on every path, so a guest cannot exhaust
worker memory by presenting an enormous `/proc/cmdline`. A guest that lies fails identity proof,
which is the intended outcome: the observation exists to detect exactly that. Failure details carry
digests and identifiers, not guest bytes, so a hostile command line cannot be reflected into a
transcript. No guest value is interpolated into a command, path, URL, or template — the two paths
read are ASCII literals in this module.

**Explicitly out of scope.** Mutation fencing against a stale worker (#2140, ADR-0584) — this module
performs its compare-and-set against observed state and documents that its caller owns the fence.
Module-tree trust (#2129, ADR-0585). Artifact content trust: the kernel and initrd bytes were
verified at finalization (#2107) and materialization (#2109), and this module consumes their
identities rather than re-verifying them. Denial of service by a guest that never boots is covered
by the caller's existing readiness deadline.

## Testing

New tests in `tests/providers/remote_libvirt/lifecycle/test_external_boot.py`.

**Golden device preservation.** Render a full remote domain with
`render_domain_xml(..., ssh_addr=..., ssh_port=...)` so it carries the qcow2 overlay disk, the
virtio NIC, the serial console pair, the guest-agent channel, the kdive metadata, the gdbstub
`<qemu:commandline>` args, the `vmcoreinfo` capture feature, and the SSH hostfwd NIC. Project it,
then assert every one of those survives verbatim and that the preserved digest is unchanged across
the projection. Assert the boot projection changed and names exactly the supplied values.

**Golden vectors.** The three ADR-0583 digests, asserted literally.

**Projection edges.** No `<os>` element; a source already carrying the three fields; a non-NFC
source; malformed XML; a DTD/entity payload; a non-`domain` root; `initrd=None` omitting the
element.

**Source admission.** One passing case and one failing case per numbered rule, each asserting
`CONFLICT` and that no XML was produced.

**Definition value.** Ownership mismatch between binding, plan, and materialization; a
materialization whose initrd presence disagrees with the plan's; canonical-JSON round trip; a
digest that does not recompute from its recorded XML.

**Activation matrix.** Every combination of observed XML in `{source, target, other}` and power in
`{inactive, active}`: the two admitted cells act, the rest raise `CONFLICT` with no `defineXML` and
no `create` recorded on the double.

**Observation.** A happy path; a wrong release; a wrong build ID; a wrong architecture; a command
line differing by one byte; a command line with no trailing newline and one with two; an oversized
read; missing ELF notes; a malformed agent reply; an unreachable agent. Each asserts the category
above and, for the failing identity cases, that no guest bytes appear in `details`.

Doubles model libvirt as it behaves rather than as a permissive echo: the domain double stores the
XML it was defined with and returns it from `XMLDesc`, and `create()` flips `isActive` — it does not
accept and silently drop anything.

## Alternatives considered

**Extract the projection and identity functions into `providers/shared/`.** local-libvirt already
has private equivalents, so this would be the second occurrence of the same algorithm. Rejected for
now: ADR-0076 makes the two providers deliberately independent, remote already carries its own
`xml.py`, `readiness.py`, and `control.py` for the same reason, and the anti-drift control that
actually binds both is ADR-0583's published golden digests, which both suites assert. A follow-up
tracks converging them once remote's adapter is complete, when there is something to converge rather
than a shared module with one and a half users.

**Implement `ExternalBootPorts` now with `recover` and `cleanup` raising.** Rejected: a class that
advertises a protocol it cannot honor is worse than one that does not claim it, and #2120 would have
to unpick the claim.

**Wire `ProviderRuntime.external_boot`.** Rejected: ADR-0587 assigns advertisement to #2140, and
advertising a capability whose recovery path does not exist would let a Run reach an activation it
cannot get out of.
