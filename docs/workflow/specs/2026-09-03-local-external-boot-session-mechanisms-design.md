# Local external-boot session mechanisms design

## Scope

Issue #2211, the second child of #2208's three-way split, supplies concrete host mechanisms for
`LocalExternalBootSessionFactory` (`lifecycle/boot/session.py`) and gives
`build_external_boot_session_factory` (`composition.py`) its first production caller. It implements
[ADR-0591](../../adr/0591-local-external-boot-session-mechanisms-bind-to-the-recovery-root.md)
under [ADR-0587](../../adr/0587-local-external-boot-uses-an-operation-session.md) and
[ADR-0586](../../adr/0586-local-external-boot-recovery-uses-an-owned-host-directory.md), on Python
3.14 for x86_64 and ppc64le. No dependency, no schema change, no migration.

Out of scope, with owners: the recovery-root setting and its provisioning (#2210, merged);
`RealLocalExternalBootIO`, the materializer, `resolve_operation_lease` and the composition binding
(#2212); the shared authority chain (ADR-0584 / #2140); and any change to
`LibguestfsAuthenticatedGuestTree`, `_GuardedGuest`, `_GuestContext`, `RecoveryMetadataStore` or
`TargetProjectionStore`.

`ProviderRuntime.external_boot` stays `None`. Binding these mechanisms supplies one half of
ADR-0584's advertisement precondition; the authenticated authority boundary is the other half.

## What is built

Five of the six mechanism aliases gain implementations. The sixth does not, deliberately.

| alias | implementation | source of configuration |
| --- | --- | --- |
| `PinOperationLease` | new `LocalOperationLane.pin` | none — the lane's own invariants |
| `OpenArtifactRoot` | new `LocalArtifactRoot.open` | `KDIVE_LIBVIRT_RECOVERY_ROOT` at construction |
| `OpenGuest` | new `_open_libguestfs_guest` | none — constructs a libguestfs handle |
| `ReadinessProbe` | existing `_real_readiness`, reused unchanged | `KDIVE_LIBVIRT_URI` inside the probe |
| `CleanupPayloads` | new `LocalPayloadCleanup.cleanup` | `KDIVE_LIBVIRT_RECOVERY_ROOT` at construction |
| `RunningObserver` | **not bound** — keeps `_unconfigured_observation` | n/a; owner #2212 |

No mechanism takes a libvirt URI, filesystem path, command, XML document or credential from
protocol input. Every configured value is resolved from a `kdive.config` setting at construction
time, inside the composition seam, never from a call argument.

### Why `observe_running` is not bound

`RunningObserver` must read an architecture, `uname` release and GNU build id from a **running**
guest, keyed only on a System id. Local has four ways to read guest state and none serves. SSH exec
needs the per-System bootstrap key the MCP tool boundary materializes per call; a callable built
once at the composition seam cannot obtain it. libguestfs requires the domain inactive, and this
observation runs only when the domain is running. The serial console carries the `kdive-ready`
marker and crash signatures, not a build id. The qemu-guest-agent is installed and enabled in every
catalog image, but local's `render_domain_xml` renders no `<channel>` element, so the host half of
the virtio channel does not exist and `qemuAgentCommand` fails deterministically.

Deriving the value from the materialization, or reading `<os><kernel>` back from the running
domain, is refused: both compare the expected value against the artifact that produced it, so the
identity comparison passes for every input and observes nothing.

`_unconfigured_observation` therefore stays in place and raises at first call. This is the issue's
own stated requirement — a partially wired factory fails loudly rather than degrading silently —
and it is a stronger demonstration of that requirement than a bound mechanism would be. Deferral
owner: **#2212**. The guest half of that work is already done (`guest_base_image` enables
`qemu-guest-agent.service` on both build paths); only the host-side channel is missing, and adding
it does not retrofit already-provisioned domains.

## Architecture

Two new small classes and three module-level functions, all in
`local_libvirt/lifecycle/boot/session_mechanisms.py`, plus one new builder in `composition.py`.

The mechanisms live in a **new module**, not in `session.py`. `session.py` is already 1301 lines and
owns the session's own machinery; the mechanisms are its injected collaborators, resolve
configuration it deliberately does not resolve, and are the half #2212 rewires. Keeping them apart
is the boundary ADR-0587 drew when it made them injected callables. `session.py` gains nothing.

```
composition.build_external_boot_session_mechanisms()   # new production caller
  ├── LocalArtifactRoot(recovery_root)      -> .open    : OpenArtifactRoot
  ├── LocalPayloadCleanup(recovery_root)    -> .cleanup : CleanupPayloads
  ├── LocalOperationLane()                  -> .pin     : PinOperationLease
  ├── _open_libguestfs_guest                             : OpenGuest
  └── _real_readiness                        (reused)    : ReadinessProbe
        │
        └── build_external_boot_session_factory(...)  # existing, unchanged signature
```

`build_external_boot_session_factory` keeps its current keyword-only signature and its laziness:
the existing test asserting it opens nothing at build time
(`test_external_boot_session_factory_builder_is_lazy_and_unadvertised`) must keep passing, so the
new builder resolves settings but opens no descriptor, no connection and no guest.

`observe_running` is simply not passed, so the factory's `or _unconfigured_observation` fallback
selects the default.

## Component contracts

### `LocalArtifactRoot.open(ownership) -> int`

Opens `<recovery_root>/<ownership.system_id>/<ownership.binding.run_id>` and returns the
descriptor. Creates the two child directories with mode 0700 when absent.

Confinement is a descriptor-relative walk, one component at a time:

1. `os.open(recovery_root, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)`, then
   `_require_private_owned_directory(fd, "artifact root")`.
2. `_open_or_create_private_child(root_fd, str(ownership.system_id))`.
3. `_open_or_create_private_child(system_fd, ownership.binding.run_id)`.

Every component is re-validated as a real directory, mode exactly 0700, owned by the running euid.
`O_NOFOLLOW` on each open means a symlinked component fails with `ELOOP` rather than being
followed. Both child names are `CanonicalUuid` values, so neither can contain `/` or `..`; the
mechanism additionally rejects a name that is not a canonical UUID before using it, so a future
loosening of the binding type cannot silently become a traversal.

`_require_private_owned_directory` and `_open_or_create_private_child` are the existing helpers in
`lifecycle/boot/external_boot.py`, reused rather than restated. The reuse is deliberate: ADR-0586
made those the definition of an acceptable directory, and a second definition would drift.

**Errors.** A missing, non-directory, wrong-mode, wrong-owner or symlinked component raises
`ValueError` with a message naming the failing component class and no host path.

**Ownership of the descriptor.** The caller (`LocalExternalBootSessionFactory.open`) takes
ownership and closes it via `close_descriptor`. Intermediate descriptors are closed by this
mechanism on both the success and failure paths.

### `LocalPayloadCleanup.cleanup(root_fd, binding) -> None`

Two bounded removals, in order:

1. Under `root_fd`, unlink each name in `_PAYLOAD_NAMES = ("kernel", "initrd", "modules")`, by
   exact name, `dir_fd=root_fd`. `FileNotFoundError` is success.
2. Open `<recovery_root>/<binding.system_id>.<binding.activation_id>` through
   `_open_private_directory`, then unlink `modules.tar` by exact name. `FileNotFoundError` on the
   unlink is success. `FileNotFoundError` on the *directory* is also success: an activation whose
   recovery directory never existed has no archive.

No recursion, no glob, no `listdir`-driven removal, no name the mechanism did not write literally.
The recovery-directory name is built from the binding's own `system_id` and `activation_id`, both
`CanonicalUuid`; the mechanism rejects a non-canonical value before composing the name.

Idempotence is a property of both halves: every removal treats absence as success, so a second
call after a crash converges rather than failing.

**Why it reaches outside `root_fd`.** `RecoveryArchiveSink.publish` writes `modules.tar` into the
per-activation recovery directory during prepare. `publish_tombstone` unlinks only `intent.json`.
An enumeration of every deletion in `lifecycle/boot/` finds nothing else that removes a published
`modules.tar`. `finalize_tombstone` requires that directory to hold exactly `tombstone.json`. So a
cleanup confined to `root_fd` leaves the archive behind and makes finalization fail permanently for
every activation that captured one. See ADR-0591; the `binding` parameter the session already
passes is what makes the second removal addressable.

### `LocalOperationLane.pin(lease) -> PinnedOperationOwnership`

A new concrete class implementing ADR-0587's provider-local nominal capability. `pin` is the
`PinOperationLease` the composition builder passes. It enforces exactly three rules:

- a lease that is not this module's concrete `LocalOperationLease` type is refused with `TypeError`
  — nominal, not structural, so an arbitrary object carrying the right attribute names is not a
  lease;
- a released lease is refused with `RuntimeError`;
- `release()` raises while any pin is outstanding, and a pin's `close()` is what releases it.

It returns `PinnedOperationOwnership(OperationOwnership(lease.system_id, lease.binding), pin)`. It
never re-reads the caller lease afterwards, which is the property ADR-0587 names.

**What this change does *not* bind, and why that is correct.** ADR-0587 says the lease is "issued
only by the System ownership and serialization-lane context", and that #2144 — now #2212 — "will
adapt its already-held database lane into this provider-local capability". Acquiring the per-System
Postgres advisory lock is therefore #2212's, exactly as `resolve_operation_lease` is. This change
supplies the lane object and its invariants; #2212 supplies the lock the lane sits behind and is
the only thing that issues a lease. The class deliberately exposes no `issue()`: a mechanism that
could mint its own lease would be the synthetic identity the rejected #2126 attempt reached for.

That split is a third thing #2212 inherits, alongside the `observe_running` channel work and
#2210's `_WORKER_ENV_NAMES` gap.

The mechanism itself performs no expected-ownership comparison, because it is not given one:
`PinOperationLease` is `Callable[[LocalExternalBootOperationLease], PinnedOperationOwnership]`.
The comparison against `ExpectedOperationOwnership` already exists in
`LocalExternalBootSessionFactory.open`, which rejects a mismatch and closes the pin before opening
any libvirt or filesystem resource. The mechanism's job is narrower and is the half the factory
cannot do: prove the lease is live and lane-owned, and produce the pin.

A lease the lane does not recognise, or one already released, propagates the lane's own refusal.
Nothing is pinned in either case.

### `_open_libguestfs_guest() -> _Guest`

Constructs and returns a `guestfs.GuestFS` handle. It attaches no drive, launches nothing and
mounts nothing: `_ConcreteSession._open_guest_context` does all of that, and does it only after
`require_inactive()` and an overlay-identity recheck. The mechanism must not duplicate those
checks — the session's `require_inactive` path is the single fence, and a second check in the
opener would be a second place to get it wrong.

### `_real_readiness`, reused

Already `Callable[[UUID], ReadinessResult]`, already the production probe wired at
`LocalLibvirtInstall.from_env`. It resolves `KDIVE_LIBVIRT_URI` itself, tails the truncated console
log, and returns bounded `probe_error` text capped at 200 characters by `_bounded_probe_error`. It
is imported and passed, not reimplemented and not wrapped.

## Threat model

This change is security-relevant: it opens directories under a privileged worker-owned root from
identifiers that arrive inside an operation binding, and it deletes files.

**Boundary inventory.** One boundary is added and one is widened.

- *Added:* `LocalArtifactRoot.open` — the configured recovery root is walked using two names taken
  from `OperationOwnership`.
- *Widened:* `LocalPayloadCleanup.cleanup` — deletion, previously confined to a descriptor the
  caller supplied, now also names a directory derived from the binding.

**Actor model.** The mechanisms run in the worker process, which is trusted. Neither mechanism is
reachable from an MCP caller in this change, because `ProviderRuntime.external_boot` is `None` and
nothing constructs the factory outside tests. The realistic adversaries are therefore (a) a local
user who can create paths under or beside the recovery root between the setting's startup
validation and an operation's open, and (b) a future caller — #2212 — that supplies a binding from
a less trusted source than today's. The design places its trust in the `CanonicalUuid` type on the
binding and in the per-component filesystem checks, and in nothing else; in particular it does not
trust that the recovery root is still what startup validated.

**Control per boundary.**

| boundary | validation | authorization | bound | leaks on failure |
| --- | --- | --- | --- | --- |
| artifact-root walk | `CanonicalUuid` on both names, re-asserted; `O_NOFOLLOW` per component | `_require_private_owned_directory` per component: directory, mode 0700, euid owner | exactly two components, both from the ownership | `ValueError`, no host path, no `strerror` |
| recovery-dir open | `CanonicalUuid` on both name halves, re-asserted; `O_NOFOLLOW` | `_open_private_directory`, same three checks | one component | as above |
| payload deletion | fixed `_PAYLOAD_NAMES` tuple | descriptor-relative, `dir_fd=root_fd` | three literal names | absence is success |
| archive deletion | one literal name, `modules.tar` | descriptor-relative under the validated recovery dir | one literal name | absence is success |

TOCTOU between the setting's startup validation and an operation's open is handled by
re-validating on every open rather than caching a verdict — which is what
`_require_private_owned_directory` already exists to do, and why it is reused instead of replaced.

**Explicitly out of scope.** A worker account compromised at the OS level: every check here is
`euid`-relative, so an attacker who *is* that account defeats them by definition, and the control
for that is deployment isolation. Concurrent operations racing on the same activation's recovery
directory: serialization is the caller's per-System advisory lane (ADR-0587), not this mechanism's.
Authenticating the *authority* that produced the binding: ADR-0584 and #2140.

## Testing

Every mechanism gets its own test. The suite reuses the doubles already in
`tests/providers/local_libvirt/lifecycle/boot/test_session.py` — `FakeLease`, `FakePin`, `FakeLane`,
`Guest`, `Domain`, `Conn`, and the `_factory` builder — rather than defining parallel ones.

Directory fixtures create mode-0700 trees with `mkdir()` then `chmod()`, never `mkdir(mode=...)`,
because the mode argument is masked by the umask and a test that relies on it passes for the wrong
reason. `tests/providers/local_libvirt/test_composition.py` already establishes that idiom and
states why.

The load-bearing tests:

- **Fail-closed defaults survive.** Three independent tests, one each for `readiness`,
  `observe_running` and `cleanup_payloads`, asserting a factory built without that mechanism raises
  the specific `RuntimeError` message from `_unconfigured_readiness` /
  `_unconfigured_observation` / `_unconfigured_cleanup` at first call. Plus one test asserting the
  three defaults are the functions the factory actually falls back to, by identity, so a rename or
  a permissive replacement fails.
- **`observe_running` stays unbound.** A test asserts the production builder does not pass an
  observer and that calling `session.observe_running()` raises
  `"local external-boot running observation is not configured"`. This is the deferral's guard: it
  fails the moment someone binds it without doing the domain-XML work.
- **Artifact-root confinement.** Separate tests for a foreign root outside the configured one, a
  symlinked `system_id` component, a symlinked `run_id` component, a mode-0755 component, and a
  component owned by another uid. Each asserts refusal, and asserts no descriptor is leaked.
- **Cleanup removes the archive, and this is proven to bite.** One test drives a real
  `RecoveryMetadataStore` through `publish_pre_stop` → `recovery_archive_sink` → `sink.publish`
  (which really writes `modules.tar`) → `complete_preparation` → `record_phase("recovered")` →
  cleanup → `publish_tombstone` → `finalize_tombstone`, and asserts finalization succeeds. The same
  test, run against a descriptor-scoped cleanup, must fail with the real
  `"cleanup tombstone directory contains unexpected payload"` — that pairing is the bite proof, and
  it is what distinguishes this from a test that passes because nothing happens.
- **Cleanup idempotence.** Run twice; the second run succeeds and removes nothing further,
  asserted by comparing the directory listing before and after the second run.
- **Cleanup is bounded.** A foreign file placed beside the payloads under `root_fd`, and a foreign
  file beside `modules.tar` in the recovery directory, both survive cleanup. A symlinked recovery
  directory is refused.
- **No protocol input.** One test per mechanism constructor asserting it takes its configuration
  from the composition seam only — no parameter accepts a URI, path, command, XML or credential.
- **`ProviderRuntime.external_boot` is still `None`** after this change, guarding #2199's gate.

Every new test is proven to bite: commit the implementation first, inject a controlled fault,
observe a clean assertion failure rather than a collection or connection error, revert, and verify
the file is byte-identical by `sha256sum`. A test that passes because the thing it exercises is a
no-op is the defect class this repository has shipped three times in this campaign.

## Failure behavior

No mechanism raises a message carrying a host path, a libvirt or libguestfs string, a guest byte,
or a secret. Directory failures raise `ValueError` naming the component class
("artifact root", "recovery directory"). `_real_readiness` already bounds its own probe text.
Descriptor cleanup runs on every failure path; a mechanism that fails partway closes what it
opened before propagating.
