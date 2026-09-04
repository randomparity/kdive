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
(#2212); the shared authority chain (ADR-0584 / #2140); any change to
`LibguestfsAuthenticatedGuestTree`, `_GuardedGuest`, `_GuestContext`, `RecoveryMetadataStore` or
`TargetProjectionStore`; **redaction of the readiness probe's `probe_error`, which still carries
raw libvirt text and host paths (#2220 — `readiness.py` and `install.py` are not modified here, and
criterion 7's redaction half is not discharged by this change)**; and narrowing
`_ConcreteSession.cleanup_payloads`'s `require_inactive` gate, which blocks cleanup for a System
restored to running (`session.py` is not modified here; routing pending).

`ProviderRuntime.external_boot` stays `None`. Binding these mechanisms supplies one half of
ADR-0584's advertisement precondition; the authenticated authority boundary is the other half.

## What is built

Five of the six mechanism aliases gain implementations. The sixth does not, deliberately.

| alias | implementation | source of configuration |
| --- | --- | --- |
| `PinOperationLease` | new `LocalOperationLane.pin` | none — the lane's own invariants |
| `OpenArtifactRoot` | new `LocalArtifactRoot.open` | `KDIVE_LIBVIRT_RECOVERY_ROOT` at construction |
| `OpenGuest` | new `open_libguestfs_guest` | none — constructs a libguestfs handle |
| `ReadinessProbe` | **not bound** — keeps `_unconfigured_readiness` (amendment 7); owner #2212 | — |
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

Four public classes — `LocalOperationLease`, `LocalOperationLane`, `LocalArtifactRoot`,
`LocalPayloadCleanup` — plus one private pin and one module-level function
(`open_libguestfs_guest`), all in `local_libvirt/lifecycle/boot/session_mechanisms.py`, and one new
builder in `composition.py` returning a `LocalExternalBootMechanisms`. `_real_readiness` is
imported and passed through, not redefined here.

The mechanisms live in a **new module**, not in `session.py`. `session.py` is already 1301 lines and
owns the session's own machinery; the mechanisms are its injected collaborators, resolve
configuration it deliberately does not resolve, and are the half #2212 rewires. Keeping them apart
is the boundary ADR-0587 drew when it made them injected callables. `session.py` gains nothing.

```
composition.build_external_boot_session_mechanisms()   # new production caller
  │   resolves KDIVE_LIBVIRT_RECOVERY_ROOT exactly once, and returns it beside the
  │   factory as LocalExternalBootMechanisms(factory=..., recovery_root=...) so #2212
  │   passes RecoveryMetadataStore the same value cleanup uses (see "One root" below)
  ├── LocalArtifactRoot(recovery_root)      -> .open    : OpenArtifactRoot
  ├── LocalPayloadCleanup(recovery_root)    -> .cleanup : CleanupPayloads
  ├── LocalOperationLane()                  -> .pin     : PinOperationLease
  ├── open_libguestfs_guest                              : OpenGuest
  └── _real_readiness                        (reused)    : ReadinessProbe
        │
        └── build_external_boot_session_factory(...)  # existing, unchanged signature
```

`build_external_boot_session_factory` today declares `observe_running: RunningObserver` as a
**required** keyword with no default, so it cannot simply be omitted — omitting it is a `TypeError`
at build. Its type widens to `RunningObserver | None`, and it stays **required**: the new builder
passes `observe_running=None` explicitly, and the factory's own `or _unconfigured_observation`
fallback selects the default.

Keeping the parameter required is the point. Giving it a default would make all six mechanisms
omittable by any caller, so a caller that forgot `readiness` or `cleanup_payloads` would get a
factory that looks built and raises only when an operation reaches that mechanism — possibly
mid-activation with the domain already stopped. Today the builder fails at composition instead.
Widening the type without adding a default keeps that property for every caller and makes the one
deliberate omission explicit at the call site. Passing `_unconfigured_observation` directly would
work too; `None` is preferred because it routes through the factory's own `or` fallback, keeping
**one** definition of what "unconfigured" means rather than a second reference to it at the call
site. The earlier justification — that it avoids importing a private name across modules — was
wrong and is withdrawn: this design already imports the private `_Guest` protocol from `session.py`,
and the fail-closed tests import all three `_unconfigured_*` functions to assert identity. Private
names do cross these module boundaries; that is not the reason.

That is the only edit to existing code outside the new module. The builder's laziness is unchanged
and `test_external_boot_session_factory_builder_is_lazy_and_unadvertised` must keep passing, so the
new builder resolves settings but opens no descriptor, no connection and no guest.

## Component contracts

### `LocalArtifactRoot.open(ownership) -> int`

Opens `<recovery_root>/<ownership.system_id>/<ownership.binding.run_id>` and returns the
descriptor. **This mechanism writes:** it creates the two child directories, mode 0700, when they
are absent. #2210 provisions the per-slot recovery root and nothing below it, so an open-only
resolution could never succeed for a System that has not run before. Creation is not novel either —
`TargetProjectionStore.publish` already creates the same `<system>/<run>` pair with the same helper.

Confinement is a descriptor-relative walk, one component at a time:

1. `os.open(recovery_root, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)`, then
   `_require_private_owned_directory(fd, "artifact root")`.
2. `_open_or_create_private_child(root_fd, str(ownership.system_id))`.
3. `_open_or_create_private_child(system_fd, ownership.binding.run_id)`.

`_open_or_create_private_child` does `os.mkdir(name, 0o700, dir_fd=parent_fd)`, fsyncs the parent,
swallows `FileExistsError`, and then delegates to `_open_private_directory` — so the `O_NOFOLLOW`
open, the mode check and the owner check all still apply to the descriptor it returns. Creation
carries the same guard as opening.

Every component is re-validated as a real directory, mode exactly 0700, owned by the running euid.
`O_NOFOLLOW` on each open means a symlinked component is refused rather than followed. Measured on
this host (Python 3.14.7, Linux 7.1.12-200.fc44, x86_64), that refusal surfaces as
`NotADirectoryError` / `ENOTDIR`, not `ELOOP`: with `O_DIRECTORY` also set, Linux reports the
symlink as not-a-directory before reaching the `O_NOFOLLOW` `ELOOP` path. The flag is nonetheless
what does the work — the identical open *without* `O_NOFOLLOW` succeeds and follows the link.
Both child names are `CanonicalUuid` values, so neither can contain `/` or `..`; the
mechanism additionally rejects a name that is not a canonical UUID before using it, so a future
loosening of the binding type cannot silently become a traversal.

`_require_private_owned_directory` and `_open_or_create_private_child` are the existing helpers in
`lifecycle/boot/external_boot.py`, reused rather than restated. The reuse is deliberate: ADR-0586
made those the definition of an acceptable directory, and a second definition would drift.

**Errors.** Every failure — missing, non-directory, wrong-mode, wrong-owner or symlinked, at the
root or at either child — is re-raised as `ValueError` with a fixed message and **no** host path.

This is not automatic and the first revision of this design got it wrong. `os.open` on the root
raises `OSError` subclasses that carry `.strerror`, and — because the root is opened by path rather
than descriptor-relative — `.filename` holding the recovery root itself. `_open_private_directory`
raises the same for a child. A test asserting `NotADirectoryError` therefore *enforces* the leak
the threat model forbids. So the mechanism catches `OSError` around the whole walk and re-raises
`ValueError("artifact root is not an owner-only service-owned directory") from None`.

**The `from None` is load-bearing.** `raise ... from exc` re-attaches the original exception, and
its `filename`, through the chained traceback — which reaches a log or a CI transcript looking
exactly like the fixed version. Suppressing the context is what actually removes the path. The
tests assert the suppression itself — `__cause__ is None`, `__suppress_context__ is True`, and the
root's path string absent from `str(exc)` — because a wrapper written with `from exc` produces an
identical message and would satisfy a message-only assertion.

**Only `OSError` is wrapped.** `_require_private_owned_directory` raises `ValueError` carrying its
own label and no path, so it is already within contract; re-wrapping it would trade a message that
names the failing check for one that does not.

**One honest limitation.** Reusing `_open_or_create_private_child` means a *child* failure carries
that helper's fixed `"recovery directory"` label, not a per-component one; only the root's own open
is labelled `"artifact root"` here. Adding a label parameter would mean modifying
`external_boot.py`, which this change declares unmodified, and the reuse is worth more than the
label. So the spec promises a fixed, path-free message — not a message naming the exact failing
component.

**Ownership of the descriptor.** The caller (`LocalExternalBootSessionFactory.open`) takes
ownership and closes it via `close_descriptor`. Intermediate descriptors are closed by this
mechanism on both the success and failure paths.

### `LocalPayloadCleanup.cleanup(root_fd, binding) -> None`

Two bounded removals, in order:

1. Under `root_fd`, unlink each name in `PAYLOAD_NAMES = ("kernel", "initrd", "modules")`, by
   exact name, `dir_fd=root_fd`. `FileNotFoundError` is success.

   Those names are not free choices. `TargetProjectionV1` fixes them as
   `kernel_filename: Literal["kernel"]`, `modules_filename: Literal["modules"]` and
   `initrd_filename: Literal["initrd"] | None`, and `_artifact_ref_parts` admits exactly
   `{"kernel", "modules", "initrd"}` as a reference's fifth component — which
   `_kernel_bundle_source` then opens by bare name against this same descriptor. Because a missing
   name is treated as success, a future projection that renames or adds an artifact would make
   cleanup silently remove nothing for it. A test therefore couples the tuple to that admitted set
   rather than restating it, so drift goes red instead of going quiet.
2. Open the **recovery root itself** — `os.open(self._root, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)`
   followed by `_require_private_owned_directory(root_fd, "recovery root")` — then resolve
   `<system_id>.<activation_id>` relative to it with `_open_private_directory(root_fd, name)`, then
   unlink `modules.tar` by exact name. Both descriptors are closed in `finally`.
   `FileNotFoundError` on the unlink is success. `FileNotFoundError` on the *directory* is also
   success: an activation whose recovery directory never existed has no archive.

   The root's own open is stated here because it is on the **deleting** path and an earlier
   revision left it unspecified, mentioning only the child open. Without `O_NOFOLLOW` an `os.open`
   of the root path follows a symlink, so an attacker-substituted root would be opened and then
   deleted from; without the re-validation the mechanism would trust that the root is still what
   startup checked, which the read path explicitly refuses to do. Both controls apply identically
   here, and the same `ValueError ... from None` wrapping keeps the path out of the message — fixed
   as `"recovery directory is not an owner-only service-owned directory"`, parallel to the artifact
   root's. Only `OSError` is wrapped: `_require_private_owned_directory` already raises a
   `ValueError` carrying its label and no path, so re-wrapping it would replace a more precise
   path-free message with a less precise one. Both forms match the same
   `"owner-only service-owned directory"` substring, so a test need not know which fired.

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

**A third state exists that this mechanism cannot reach, and it is not this change's.** The archive
lands in `.{system}.{activation}.partial` first and only reaches the final directory when
`complete_preparation` renames it. A worker that dies between a successful `sink.publish` and that
rename leaves the partial holding `{intent.json, modules.tar}`; `_publish_initial_intent` then
refuses every retry under the same activation, and a retry under a fresh activation orphans the
partial on the per-slot root with no reaper. `cleanup_payloads` runs only after
`record_phase(..., "recovered")` on a *completed* directory, so it can reach neither outcome.
Widening it to sweep partials is explicitly not done here: that would be a deletion of unknown
extent under the privileged root, aimed at a gap in the recovery model rather than in this
mechanism. The state is recorded so this design's enumeration is not mistaken for a complete
account of archive reachability. **Owner: #2212.** It cannot manifest before that issue merges —
no production `.partial` is ever written while `ProviderRuntime.external_boot` is `None` — so the
gap is fixed inside this queue rather than deferred out of it. The same state was found
independently by the review on #2207, which meets it without creating it.

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

### `open_libguestfs_guest() -> _Guest`

Constructs and returns a `guestfs.GuestFS` handle. It attaches no drive, launches nothing and
mounts nothing: `_ConcreteSession._open_guest_context` does all of that, and does it only after
`require_inactive()` and an overlay-identity recheck. The mechanism must not duplicate those
checks — the session's `require_inactive` path is the single fence, and a second check in the
opener would be a second place to get it wrong.

### `readiness` is not bound here

An earlier revision of this design bound `readiness` to the existing `_real_readiness`, "reused
unchanged", on the ground that it already has the `ReadinessProbe` signature, is already the
production probe wired at `LocalLibvirtInstall.from_env`, and "tails the truncated console log".
**The last clause is false on this path, and it was the load-bearing one.** Charter amendment 7
removes the binding; criterion 1 goes from five bound mechanisms to four.

The console log is truncated only by `LocalLibvirtInstall`'s identity-checked prepare —
`read_console_log`'s own docstring names that as its precondition, and the serial log is rendered
`append="on"`. `_ConcreteSession.start()` is `self._require_open_domain().create()` and truncates
nothing. Both `_require_readiness` call sites sit on the `xml == "target" and active and
prior_power == "running"` arm, reached immediately after `start()`; `prior_power == "running"`
means `LocalLibvirtInstall.boot()` already ran, truncated, and waited for `kdive-ready`. So the
marker is in the file before the target kernel starts, `classify_console` scans only
`text[: marker_match.start()]`, and a target-boot panic is invisible by construction. The gate
returns `ReadinessResult(True, True, None)` and `_require_readiness` passes.

On a markerless log the same probe sleeps one poll interval and returns `answered=False`, which
this single unlooped consumer treats as failure — `_real_readiness` is built for
`install._await_ready`'s polling loop, and nothing here loops. So the reuse is a no-op on the
reachable branch and a false failure on the other.

A correct probe must anchor its window at `start()`, by truncating there or recording the log
offset there. `start()` is in `session.py`, which this change leaves unmodified, so the binding is
not made here. The factory keeps `_unconfigured_readiness`, which raises at first call — so #2212,
the only row that makes `ProviderRuntime.external_boot` non-`None`, cannot ship a live port
without supplying a real probe. This is a blocking obligation on that row, not a dormant residual.

**Criterion 7's redaction half is not discharged here either, and this design does not claim it
is.**
`_bounded_probe_error` bounds *length* and nothing else — it is `message[:200]`.
`_domain_exit_probe` forwards `proc.stderr` verbatim into `probe_error`, so an unreachable
hypervisor yields raw libvirt text and a host filesystem path, well inside 200 characters. That is
a real exposure and it is **owned by #2220**, which holds the whole call path in view and has an
explicit brief to weigh redaction against operator diagnosability.

An earlier revision of this design added a redacting wrapper for the external-boot path only. It is
withdrawn. It was a second, narrower answer to a question #2220 owns, it could not derive its
classification from what `_real_readiness` actually returns, and the wrapper itself raised a host
path — producing the defect it existed to remove. Removing it deletes that class of problem at the
root instead of fixing it repeatedly.

Nothing on this path can leak in production before #2220 lands: the session factory has no `src/`
caller until #2212 wires it, so no readiness probe on the external-boot path runs at all.
`readiness.py` and `install.py` are unmodified either way, so existing boot diagnostics are
untouched.

## Threat model

This change is security-relevant: it opens directories under a privileged worker-owned root from
identifiers that arrive inside an operation binding, and it deletes files.

**Boundary inventory.** Three boundaries are added and one is widened.

- *Added (read):* `LocalArtifactRoot.open` — the configured recovery root is walked using two names
  taken from `OperationOwnership`.
- *Added (write):* `LocalArtifactRoot.open` again — the same walk **creates** the `<system_id>` and
  `<run_id>` directories, mode 0700, when absent. This mechanism writes to the privileged
  per-worker-slot recovery root, and the controls below cover creation as well as opening.
- *Added (read):* `LocalPayloadCleanup.cleanup`'s **own open of the recovery root** — the mechanism
  holds a `Path`, not a descriptor, so it must open the root itself before resolving anything
  relative to it. This is a distinct boundary from `LocalArtifactRoot`'s walk, on the deleting path,
  and it was absent from an earlier revision of this inventory.
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
| artifact-root walk (read) | `CanonicalUuid` on both names, re-asserted; `O_NOFOLLOW` per component | `_require_private_owned_directory` per component: directory, mode 0700, euid owner | exactly two components, both from the ownership | `ValueError`, no host path, no `strerror` |
| artifact-root walk (create) | same `CanonicalUuid` re-assertion before any `mkdir` | `mkdir(0o700, dir_fd=)` then the same per-component check via `_open_private_directory` | at most two directories per distinct `(system_id, run_id)`; **count is not otherwise bounded** — see below | as above |
| cleanup's recovery-**root** open | none needed — the path is the constructed value, never a call argument | `O_DIRECTORY\|O_NOFOLLOW` then `_require_private_owned_directory(fd, "recovery root")` | one open, closed in `finally` | `ValueError` re-raised `from None` |
| recovery-**dir** open (child) | `CanonicalUuid` on both name halves, re-asserted; `O_NOFOLLOW` | `_open_private_directory`, same three checks | one component | as above |
| payload deletion | fixed `PAYLOAD_NAMES` tuple, coupled by test to `TargetProjectionV1`'s `*_filename` fields | descriptor-relative, `dir_fd=root_fd` | three literal names | absence is success |
| archive deletion | one literal name, `modules.tar` | descriptor-relative under the validated recovery dir | one literal name | absence is success |

TOCTOU between the setting's startup validation and an operation's open is handled by
re-validating on every open rather than caching a verdict — which is what
`_require_private_owned_directory` already exists to do, and why it is reused instead of replaced.

**Directory growth is unbounded, and nothing reclaims it.** Under adversary (b) — a future caller
supplying bindings from a less trusted source — each distinct `(system_id, run_id)` pair mints a
fresh directory pair under the 0700 per-slot root. Every control above refuses a *bad* directory;
none bounds how many *good* ones may be created. Nor does anything remove them:
`finalize_tombstone` rmdirs only `<system>.<activation>`, and no code in `lifecycle/boot/` removes
an artifact `<system_id>` or `<run_id>` directory. Bounding creation would mean rate-limiting or
authenticating the binding, which is ADR-0584's authority chain and #2140's work, not this
mechanism's; reclaiming the directories is **owned by #2212**. Neither is reachable in this change
because `ProviderRuntime.external_boot` is `None`: no production cleanup runs and no production
directory is created until #2212 flips that, and #2212 is the only entry that can.

**One root, resolved once.** Cleanup's second removal targets
`<recovery_root>/<system_id>.<activation_id>`, while the directory that actually holds
`modules.tar` is the one `RecoveryMetadataStore` is constructed with — and that constructor
argument belongs to #2212. A divergence would make cleanup open a non-existent path, report success
under the idempotence rule, and leave `finalize_tombstone` failing for every archived activation:
a silent failure producing exactly the state this design refuses to ship. An invariant in prose
would not control that, because it depends on a future change honouring it. So the builder resolves
the setting **once** and returns the value beside the factory as one
`LocalExternalBootMechanisms(factory, recovery_root)`, and that is the value #2212 hands the store.
The residual is stated plainly rather than claimed away: #2212 could still call `config.require`
itself and discard what it was given. This seam cannot prevent that — it can only make the correct
wiring the obvious one and leave a single resolution point for a reviewer to check. **The
enforcement point is #2212's own contract, not a hope**: consuming
`LocalExternalBootMechanisms.recovery_root` and never re-resolving the setting is a binding
obligation recorded against that issue, so the remaining hole sits in a row whose dispatch is
controlled rather than in prose nobody owns.

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
- **Artifact-root confinement.** Separate tests for a symlinked `system_id` component, a symlinked
  `run_id` component, a mode-0755 component, a non-directory root, a missing root, and a
  non-canonical component name. Each asserts refusal, asserts the fixed path-free message, and
  asserts no descriptor is leaked. What these discharge is that the root is **re-validated on every
  open** — not that it cannot be pointed elsewhere.

  **There is deliberately no foreign-root test** (charter amendment 3). An earlier revision of this
  spec called one "the test that demonstrates the mechanism cannot be pointed elsewhere by its
  caller"; it is not, and it cannot be. `.open`'s only argument is an `OperationOwnership` carrying
  two canonical UUIDs, so no caller-supplied input can name a second root — which means no input
  distinguishes a correct implementation from an incorrect one, and a test built on one would pass
  against both. The caller-level claim follows from the type signatures instead. Writing a third
  such test would repeat, one level up, the tautological-gate error ADR-0591 rejects
  `recovery_directory_name` for.
- **The euid check is not claimed as covered.** `_require_private_owned_directory` also rejects a
  directory owned by another uid, and an ordinary unprivileged test process cannot create one. That
  case is exercised only by a fixture that skips unless a non-owner directory is genuinely
  available, so it does not run in ordinary CI. Faking `os.fstat` to simulate it would assert
  against the mock rather than the guard, so it is not done. This spec therefore does **not** list
  the owner case among the refusals it proves — the gap is stated rather than papered over.
- **The `open_guest` binding is asserted, not assumed**, by identity on the built factory.
  Without it, a future edit that drops the argument leaves the class quietly falling back to a
  fail-closed default, and nothing goes red.
- **Both unbound probes are asserted by identity *and* invoked.** A test through the production
  builder asserts `_readiness is _unconfigured_readiness` and `_observe_running is
  _unconfigured_observation`, then calls each and requires its specific `RuntimeError`. Identity
  alone would pass against a binding that returns something harmless; an amendment that removes a
  binding must leave behind a test that fails if the binding silently returns.
- **No mechanism takes configuration from protocol input.** This is charter criterion 8, and it is
  tested by **provenance, not by signature shape**. `LocalArtifactRoot` and `LocalPayloadCleanup`
  each take a `Path`, so "no parameter accepts a path" would have to fail on the very constructors
  it inspects; and `inspect.signature` reports arity, never where an argument came from. The tests
  instead assert that `build_external_boot_session_mechanisms` is the only construction site of
  those two classes in `src/`, that the value it constructs them with is the one
  `config.require(LIBVIRT_RECOVERY_ROOT)` returned (via a sentinel), and that the builder itself
  takes no parameters, so no caller can inject configuration into it.
- **The payload names are coupled to their source.** A test asserts `set(PAYLOAD_NAMES)` equals the
  set discovered by iterating `TargetProjectionV1`'s `*_filename` fields and unwrapping each
  `Literal`, so a projection that renames or adds an artifact fails here instead of making cleanup
  a silent no-op.

  **Not `_artifact_ref_parts`**, which an earlier revision named. It holds `{"kernel", "modules",
  "initrd"}` as an inline literal inside a boolean expression, with no module-level constant and no
  accessor, so a test could only restate the set it exists to check or probe it with candidates it
  had already restated. `model_fields` is genuinely introspectable, and field *discovery* — rather
  than a hard-coded list of the three known names — is what catches an **added** fourth artifact: a
  new `dtb_filename: Literal["dtb"]` would otherwise never enter the expected set, the equality
  would still hold, and cleanup would silently stop removing it. The test asserts the discovered set
  is non-empty first, because an unwrapping that silently yields nothing would make the equality
  vacuous.
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

**The no-leak guarantee is scoped to the two directory mechanisms**, `LocalArtifactRoot` and
`LocalPayloadCleanup`. Neither raises a message carrying a host path, a libvirt or libguestfs
string, a guest byte, or a secret: every directory failure becomes a `ValueError` with a fixed
message, re-raised `from None` so no chained `OSError` re-attaches `.filename` or `.strerror`.
Descriptor cleanup runs on every failure path; a mechanism that fails partway closes what it opened
before propagating.

It is explicitly **not** a claim about readiness. `_real_readiness` is passed through unchanged and
still returns raw libvirt stderr, including host paths, in `probe_error` — for both its callers.
That exposure is #2220's, and this design neither fixes it nor pretends to.
