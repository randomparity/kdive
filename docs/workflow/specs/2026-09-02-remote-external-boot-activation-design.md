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
                    observe_guest_identity(agent_exec, domain, definition)
```

The pure layer takes strings and returns strings. The definition layer takes a source XML string
plus the shared ADR-0583 values and returns one frozen value carrying everything a later activation
needs. The operation layer takes injected libvirt and guest-agent seams, exactly as every other
remote lifecycle module does, so unit tests drive every path with no libvirt host.

Signatures in this section are the contract; the implementation plan repeats them verbatim.

### Data flow

1. #2118's job reads the System's inactive domain XML over the existing mutual-TLS connection.
2. `prepare_target_definition` admits that XML as an owned disk/GRUB baseline, renders the target
   projection, and computes both identities on both sides plus the expected running-kernel
   observation and expected command line.
3. #2120 stores the resulting `RemoteExternalBootDefinition` in its recovery point; #2140 wraps the
   next step in the authority fence.
4. `activate_definition` re-observes the domain inactive with the exact recorded source XML, defines
   the target XML, and starts the domain.
5. `observe_guest_identity` makes one bounded attempt to read the running-kernel identity and
   `/proc/cmdline`, and compares both exactly. It does not wait: an agent that is not yet answering
   is a retryable `TRANSPORT_FAILURE`, and #2118's job owns the readiness deadline and the retry
   that constitutes the wait, exactly as it owns every other deadline on this lane.

ADR-0583 assigns the `/proc/cmdline` comparison to core. It is performed inside the provider here
and its bytes are deliberately not returned, because the command line a hostile guest reports is
untrusted content that must not reach a shared value, a response envelope, or a transcript. What
crosses the seam is the `RunningKernelObservation` the shared contract already defines, plus a
failure naming which field differed.

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

### `require_disk_grub_source(domain_xml, *, system_id, pool) -> None`

ADR-0583's remote source admission, checked in this order and raising
`CategorizedError(ErrorCategory.CONFLICT)` on the first failure:

1. The boot projection has no kernel, initrd, or cmdline.
2. `metadata/{kdive}system` equals this System id.
3. There is exactly one `<disk device="disk">`, its `<source>` names the expected pool and the
   System's deterministic overlay volume — derived here as `overlay_volume_name(system_id)` rather
   than supplied, so a caller cannot admit a source against the wrong volume — its
   `<driver type="qcow2">`, and its `<target dev="vda" bus="virtio">`.
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

Both XML fields are bounded at 65536 bytes — the same cap the shared ports module applies to a
canonical value, and the same cap the guest reads use. This value reaches durable storage in #2120's
recovery point, and an unbounded field there would let one malfunctioning or hostile remote host
write an arbitrarily large row.

The recompute validators call the parsing layer, which raises `CategorizedError`. Pydantic v2
converts only `ValueError` and `AssertionError` into a `ValidationError`, so the validators catch
`CategorizedError` and re-raise it as `ValueError`. Every construction failure therefore surfaces as
one exception class, including rehydration of a corrupted stored record through
`model_validate_json`.

It deliberately does not carry `ProviderStateIdentity`. That value pairs the definition digest with
a module-tree component state, and the module tree is #2129's. #2120 composes the two.

### `prepare_target_definition(source_xml, *, plan, materialization, binding, pool, kernel_path, initrd_path) -> RemoteExternalBootDefinition`

Pure. Validates that `binding` matches `materialization.ownership` and `plan.ownership`, that
`materialization.plan_identity == plan.identity`, and that the materialization carries a kernel
reference (and an initrd reference exactly when the plan carries an initrd). Admits the source, then
renders the target from `kernel_path`, `initrd_path`, and `plan.cmdline`.

The kernel and initrd paths written into the XML are resolved by the caller from the opaque
references #2109 minted, because ADR-0583 forbids a provider path crossing the shared seam and this
module never learns the host's pool directory. They are host filesystem paths libvirt hands QEMU, so
this function checks each is nonempty, NFC, absolute, at most 1024 bytes, free of NUL and of a `..`
segment, and rejects otherwise with `CONFLICT` and `rule="artifact-path"`. That is a shape check on
a trusted caller's value, not a substitute for the caller's own resolution.

Nothing is re-derived: `cmdline` is `plan.cmdline` verbatim, passed to libvirt as one string with no
tokenizing, quoting, normalization, or shell.

### `observe_guest_identity(agent_exec, domain, definition) -> RunningKernelObservation`

Reads three facts from the running guest through `GuestAgentExec`, the seam the rest of the remote
provider already drives the guest with, constructed here with the two-program allowlist
`{"/usr/bin/uname", "/usr/bin/cat"}`:

- `/usr/bin/uname -r -m` returns the kernel release and the machine architecture on two lines.
- `/usr/bin/cat /proc/cmdline` returns the saved command line. Exactly one trailing newline is
  removed; the remaining bytes must equal `definition.expected_cmdline` encoded UTF-8, byte for
  byte.
- `/usr/bin/cat /sys/kernel/notes` returns the running kernel's ELF notes, parsed by the existing
  `kdive.build_artifacts.validation.parse_gnu_build_id`.

A non-zero exit from any of the three is `READINESS_FAILURE`: the guest is running, and a kernel
that cannot produce its own release or notes has failed identity proof rather than suffered a
transport fault. Each captured stream is capped at 65536 bytes.

`READINESS_FAILURE` is retryable by category, so every identity failure is raised with
`terminal=True`. Without it a guest that booted the wrong kernel would be re-dispatched to observe
the same wrong guest until the deadline expired, which is the one condition this proof exists to
stop. A transport failure or an agent that is not answering raises `TRANSPORT_FAILURE` with no
terminal flag, which stays retryable so the caller's readiness deadline is the wait.

Chosen over two alternatives, both of which are the same shape of cost — a provisioning change
this issue would then own. The `guest-file-open` / `guest-file-read` / `guest-file-close` RPCs read
the two files without exec, but the guest images do not permit them: verified at
`deploy/ansible/roles/guest_base_image/tasks/build_one.yml:142-173`, which adds exactly
`guest-exec,guest-exec-status` to the RHEL-family `--allow-rpcs` allowlist and nothing else, so the
first call would return "Command guest-file-open has been disabled" as a retryable transport
failure on every provisioned System. Adding an `observe` subcommand to the in-guest
`kdive-install-kernel` helper would be one round trip, but the helper ships in the base image
(`deploy/remote-libvirt-guest-helpers/kdive-install-kernel`), so it reaches only re-imaged guests
and an existing System would fail identity proof for a deployment reason. The chosen route needs no
provisioning change at all: `guest-exec` is already allowlisted, and `uname` and `cat` are in every
supported base image. Its residual is that the worker-side program allowlist for this one operation
names two general-purpose binaries rather than one purpose-built helper — the worker composes the
whole argv and the guest supplies none of it, so the widening is in what may be run, not in who
chooses it.

ADR-0584 exempts read-only observation from mutation authority, so no fence is required for this
path.

### `activate_definition(conn, definition) -> None`

The compare-and-set write. It looks the domain up by `domain_name_for(UUID(binding.system_id))`,
requires `isActive()` false, requires the observed inactive definition to match
`definition.source_definition` and `definition.source_boot`, defines `definition.target_xml`,
re-reads the inactive definition and requires it to match `definition.target_definition` and
`definition.target_boot`, then starts the domain.

The comparison is ADR-0583's two-part digest comparison, not a byte comparison. libvirt does not
store and return the bytes it was handed: `defineXML` parses into its own model and `XMLDesc`
regenerates, with its own indentation, attribute quoting, `<os>` child order, and content it adds on
define. `preserved_definition_identity` and `boot_projection_identity` exist because the ADR requires
identity to survive exactly that, and local-libvirt compares the same pair. `source_xml` and
`target_xml` remain the bytes to define and the bytes to hand #2120 — never comparands.

Every other combination of observed definition and power performs no write and raises `CONFLICT`
with the observed digests and power in `details`, which is ADR-0583's `recovery_conflict` signal for
its caller.

A define that succeeds and a start that fails is its own state, not an infrastructure fault: the
persistent definition now names the external kernel while the guest is not running it, so it raises
`CONFLICT` with `terminal=True` and the observed digests retained, so the caller enters recovery
rather than retrying a half-applied write. The interleaving that produces it — another actor
starting the domain between the power check and `create()` — is the stale-actor case #2140's fence
owns; this function's job is to fail closed when it happens rather than to prevent it.

A domain lookup failing with `VIR_ERR_NO_DOMAIN` raises `NOT_FOUND`, matching
`lifecycle/provisioning.py:544-548`; any other libvirt error is `INFRASTRUCTURE_FAILURE`.

Idempotence: an already-target, already-running domain is the achieved post-state and returns
without a write, so a retried attempt after a lost response converges instead of redefining. An
already-target but inactive domain starts it. This mirrors how `RemoteLibvirtControl.power` treats
ON/OFF as idempotent on the achieved post-state.

ADR-0583's pre-write gate also requires proof of current exclusive mutation authority immediately
before this write. That proof is #2140's; this function takes the connection its caller already
fenced and performs the state half of the gate.

## Failure handling

`Effective` below is what the caller actually gets: `RETRYABLE_BY_CATEGORY` for the category, unless
the error sets `terminal=True`. `CONFLICT` and `NOT_FOUND` are already non-retryable
(`src/kdive/domain/errors.py:126`, `:123`) and need no flag. `READINESS_FAILURE` is **retryable** by
category (`:109`), so every identity failure sets the flag explicitly — relying on the taxonomy here
would re-dispatch a job to re-observe the same wrong guest until its deadline expired.

| Condition | Category | `terminal` | Effective |
| --- | --- | --- | --- |
| Malformed, non-NFC, or non-`domain` XML | `INFRASTRUCTURE_FAILURE` | no | retry |
| Artifact path is empty, non-NFC, relative, oversized, or carries `..` | `CONFLICT` | no | stop |
| Source is not this System's owned disk/GRUB baseline | `CONFLICT` | no | stop |
| Observed definition or power is not an admitted combination | `CONFLICT` | no | stop |
| Target defined, start failed | `CONFLICT` | no | stop |
| Running kernel or command line differs from the plan | `READINESS_FAILURE` | **yes** | stop |
| A guest read exits non-zero or returns unparseable output | `READINESS_FAILURE` | **yes** | stop |
| Domain does not exist | `NOT_FOUND` | no | stop |
| Guest agent unreachable or not answering | `TRANSPORT_FAILURE` | no | retry |
| Malformed agent reply | `INFRASTRUCTURE_FAILURE` | no | retry |

No path logs guest bytes verbatim. `details` carries digests, the domain name, the System id, and
bounded observed values that are already provider-internal identifiers.

## Threat model

**Boundaries this change adds.** Three.

1. Guest-supplied bytes entering the worker through the QEMU guest agent — `uname` output and the
   contents of `/proc/cmdline` and `/sys/kernel/notes`.
2. A whole inactive domain definition from the remote libvirt host, used in a new way. Existing
   remote code parses domain XML only to extract scalars (`lifecycle/xml.py:154-215`). This change
   makes the whole definition the anchor of a compare-and-set and persists it: #2120 stores
   `RemoteExternalBootDefinition` in a recovery point.
3. Caller-resolved artifact paths written verbatim into `<os><kernel>` and `<os><initrd>`, which
   libvirt hands QEMU as host filesystem paths.

**Boundaries this change widens.** One, and only in what may be run: the worker-side `GuestAgentExec`
program allowlist for the observation operation names `/usr/bin/uname` and `/usr/bin/cat` rather than
a single purpose-built helper. The `guest-exec` RPC itself was already permitted for the install
lane, the worker composes the whole argv, and the guest supplies none of it.

**Actors.** The guest is untrusted: a Run boots a caller-supplied kernel, so guest user space and the
running kernel are under the tenant's control and may lie. The remote libvirt host, its mutual-TLS
credentials, and its agent transport are trusted to the same degree the existing remote provider
already trusts them — a compromised host already owns the System. The caller that resolves artifact
paths is a worker, trusted to the same degree as the worker running this module. The
operator-configured pool and overlay names are trusted configuration.

**Control per boundary.**

Boundary 1. Every guest-supplied value is treated as an assertion to be checked, never as a fact to
be recorded. The architecture must be one of the shared contract's two literals; the release must
match the shared `KernelRelease` pattern; the build ID must parse out of well-formed ELF notes and
match the shared hex pattern; the command line must equal the plan's bytes exactly. Each captured
stream is bounded at 65536 bytes, so a guest cannot exhaust worker memory by presenting an enormous
`/proc/cmdline`. A guest that lies fails identity proof, which is the intended outcome: the
observation exists to detect exactly that. Failure details carry digests and identifiers, not guest
bytes, so a hostile command line cannot be reflected into a transcript. No guest value is
interpolated into a command, path, URL, or template — the three argv vectors are literals.

Boundary 2. The XML is parsed with `defusedxml`, so DTDs and entities are refused. Both XML fields
are bounded at 65536 bytes in `RemoteExternalBootDefinition`, matching the shared ports module's
cap for a canonical value, so a malfunctioning host cannot drive an unbounded durable row. The
definition is never executed, never interpolated, and never used to derive a filesystem path; only
its digests decide anything.

Boundary 3. Each path is checked nonempty, NFC, absolute, at most 1024 bytes, free of NUL and of a
`..` segment before it reaches the XML. That is a shape check on a trusted caller's value — it
catches a resolution defect, not an attack — and is stated so that a later caller change does not
silently make the boundary load-bearing without anyone noticing.

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
materialization whose initrd presence disagrees with the plan's; each rejected artifact-path shape;
canonical-JSON round trip; a digest that does not recompute from its recorded XML; an XML field over
the byte cap; and `source_xml` that does not parse, asserting `ValidationError` rather than a bare
`CategorizedError`.

**Activation matrix.** Every combination of observed definition in `{source, target, other}` and
power in `{inactive, active}`: the two admitted cells act, the rest raise `CONFLICT` with no
`defineXML` and no `create` recorded on the double. Plus a `create()` that raises after a successful
`defineXML`, asserting `CONFLICT` with `terminal is True`; a lookup raising `VIR_ERR_NO_DOMAIN`
asserting `NOT_FOUND`; and a lookup raising another libvirt code asserting
`INFRASTRUCTURE_FAILURE`.

**Observation.** A happy path; a wrong release; a wrong build ID; a wrong architecture; an
architecture the shared contract does not name; a command line differing by one byte; a command line
with no trailing newline and one with two; a `uname` reply with too few lines; a non-zero exit from
each of the three reads; an oversized capture; empty and malformed ELF notes; a malformed agent
reply; an unreachable agent. Each asserts the category above, each identity case asserts
`terminal is True`, and each failing identity case asserts that no guest bytes appear in `details`.

Doubles model libvirt and the agent as they behave, not as permissive echoes. The domain double
**reparses and reserializes** the XML it was defined with before returning it from `XMLDesc`, with
different indentation and attribute quoting, so the digest comparison is what the matrix proves — a
double that echoed its input verbatim would pass a byte comparison that a real libvirt fails.
`create()` flips `isActive`. The agent double answers a fixed argv with a canned
`AgentExecResult`, and an argv it was not given raises rather than returning success.

## Alternatives considered

**Extract the projection and identity functions into `providers/shared/`.** local-libvirt already
has private equivalents at `local_libvirt/lifecycle/boot/session.py:1260-1291` and
`local_libvirt/lifecycle/boot/external_boot.py:2163`, so this is a second in-tree copy of an
algorithm ADR-0583 calls normative. Rejected for now: ADR-0076 makes the two providers deliberately
independent and remote already carries its own `xml.py`, `readiness.py`, and `control.py` for the
same reason, and extracting into `providers/shared/` would edit local-libvirt in a change whose
charter surface does not include it.

The residual is real and is stated rather than argued away. After this change the ADR's three
published golden digests are asserted by the remote suite only — `rg '3e3cde0b|c48b5e5a|06bf5b2a'`
over the tree today matches nothing outside `docs/adr/` — so the local copy remains ungated and the
two can drift without a test noticing. A follow-up carries both halves: adding the three vectors to
the local-libvirt suite, and converging the copies into one module once remote's adapter is complete
and there is something to converge rather than a shared module with one and a half users.

**Implement `ExternalBootPorts` now with `recover` and `cleanup` raising.** Rejected: a class that
advertises a protocol it cannot honor is worse than one that does not claim it, and #2120 would have
to unpick the claim.

**Wire `ProviderRuntime.external_boot`.** Rejected: ADR-0587 assigns advertisement to #2140, and
advertising a capability whose recovery path does not exist would let a Run reach an activation it
cannot get out of.
