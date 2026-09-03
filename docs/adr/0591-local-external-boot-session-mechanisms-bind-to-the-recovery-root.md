# 0591 — Local external-boot session mechanisms bind to the recovery root

## Status

Proposed (2026-09-03)

## Context

ADR-0587 defined `LocalExternalBootSessionFactory` and six injected host mechanisms —
`pin_lease`, `open_artifact_root`, `open_guest`, `readiness`, `observe_running`,
`cleanup_payloads` — and deferred binding them. They have stayed unbound since: every one is a
bare `Callable` alias in `lifecycle/boot/session.py`, and `build_external_boot_session_factory`
has no caller in `src/`. ADR-0586 settled the durable recovery artifacts and ADR-0590 settled the
in-guest programs the identity proof spawns, but neither says where the local mechanisms get
their configuration or what their failure contracts are. #2210 added
`KDIVE_LIBVIRT_RECOVERY_ROOT`, which is the first configured host location this provider owns.

Binding them raises three questions that merged code does not answer, each with viable
alternatives.

**Where the artifact root is.** `open_artifact_root` receives only `OperationOwnership` — a
System id and an activation binding — and returns a directory descriptor the session then uses
for single-segment `open_artifact` names. The per-activation recovery directory is the obvious
candidate and is wrong: `_publish_initial_intent` refuses a partial directory holding anything
but `intent.json`, and `complete_preparation` renames that partial directory onto the final name,
which fails once payloads exist under it.

**What payload cleanup owns.** `RecoveryArchiveSink.publish` writes `modules.tar` into the
per-activation recovery directory during prepare. `publish_tombstone` unlinks only `intent.json`.
An enumeration of every deletion in `lifecycle/boot/` — `os.unlink`, `os.rmdir`, `os.remove`,
`rm_rf`, `shutil.rmtree`, `Path.unlink` — finds nothing that removes a published `modules.tar`:
`_unlink_if_same` removes only `.modules.tar.partial`, and only on `publish`'s own failure path
when the file is still the one it created. So `finalize_tombstone`, which requires the directory
to hold exactly `tombstone.json` and otherwise raises "cleanup tombstone directory contains
unexpected payload", cannot succeed once an archive exists.

Nothing is broken for a user today, because `cleanup_payloads` has no production binding at all —
the factory falls back to `_unconfigured_cleanup`, which raises. This decision is therefore taken
at the moment of wiring, and it decides whether the defect ships rather than repairing one that
shipped. The `binding` parameter the session already passes alongside the descriptor is dead
weight under any reading that keeps cleanup inside the descriptor.

**How the running kernel is observed.** `observe_running` must return an architecture, a `uname`
release, and a GNU build id read from the live domain, keyed only on a System id. Local has four
ways to read guest state and none of them serves: SSH exec needs a per-System bootstrap key that
the MCP tool boundary materializes per call, which a callable built once at the composition seam
cannot obtain; libguestfs requires the domain *inactive*, and this observation runs only while the
target is running; the serial console carries a readiness marker and crash signatures, not a build
id. The fourth — the qemu-guest-agent — is installed and enabled in every catalog image
(`guest_base_image` enables `qemu-guest-agent.service`), but
`grep -rn "org.qemu.guest_agent" src/` finds it in `remote_libvirt/lifecycle/xml.py` only. Local's
domain renderer emits `<disk>`, `<serial>`, `<console>` and `<emulator>` and no `<channel>`, so
the agent is running in the guest and unreachable from the host.

Deriving the observation from the materialization or from the running domain's own
`<os><kernel>` would compare the expected value against the artifact that produced it. That
defeats the identity interlock this observation exists to enforce, so it is not an option however
easily it would pass a test.

## Decision

**The recovery root is the one configured host location, and the artifact root is a System/Run
subtree beneath it.** `open_artifact_root(ownership)` opens `KDIVE_LIBVIRT_RECOVERY_ROOT`, then
resolves `str(ownership.system_id)` and `ownership.binding.run_id` as children through
`_open_or_create_private_child`, **creating either when absent** with mode 0700. Every open is
`O_DIRECTORY|O_NOFOLLOW` and validated by `_require_private_owned_directory` — a real directory,
mode exactly 0700, owned by the running euid — because `_open_or_create_private_child` delegates
to `_open_private_directory` after its `mkdir`, so creation carries the identical guard as
opening. Both child names are `CanonicalUuid` values, so neither can carry a separator or a
traversal component.

Creation is necessary, not incidental: #2210 provisions the per-slot recovery root and nothing
below it, so an open-only resolution could never succeed for a System that has not run before. It
is also not novel — `TargetProjectionStore.publish` already creates the same `<system>/<run>` pair
with the same helper under an artifact root. This mechanism **writes** to the privileged recovery
root, and every description of it says so. This matches the System/Run
artifact directory ADR-0587 and the #2144 design already assume, and it keeps the per-activation
recovery directories — named `<system_id>.<activation_id>`, with a separator no UUID contains —
disjoint from it under the same root.

The mechanism performs no reference-versus-binding comparison. `recovery_directory_name` refuses
a reference whose embedded owners disagree with a binding, but this mechanism receives no
reference: it would have to construct one from the binding and then check it against that same
binding, which cannot fail. Confinement is the descriptor-relative `O_NOFOLLOW` walk and the
per-component ownership check, which a symlinked component, a foreign root, and a
non-owner-only directory each fail.

**Payload cleanup owns the activation's payloads under the descriptor and its recovery archive
under the recovery root.** `cleanup_payloads(root_fd, binding)` removes the payload names under
`root_fd`, then removes `modules.tar` from
`<recovery_root>/<binding.system_id>.<binding.activation_id>`. The second removal is bounded to
that one name at that one binding-derived path: no recursion, no glob, no unlink of an entry the
mechanism did not name. The recovery directory is opened under the same `O_NOFOLLOW` and
ownership discipline as the artifact root. A missing entry is success, in both places, so a
repeated cleanup after a crash converges instead of failing.

**Running observation stays unbound, on its fail-closed default.** Local has no host-reachable
channel into a *running* guest. The qemu-guest-agent is installed and enabled in every catalog
image, but local's `render_domain_xml` emits no `<channel>` element at all, so the host half of
the virtio channel does not exist and there is nothing for `qemuAgentCommand` to reach. Rendering
that channel changes the domain XML of every local System, not only
external-boot ones, and is therefore its own change with its own tests and its own redefinition
consequence for already-provisioned Systems. `observe_running` keeps
`_unconfigured_observation`, which raises at first call — the loud partial-wiring failure this
factory was built to produce — and the channel work is tracked separately as the prerequisite it
is. Nothing regresses: `ProviderRuntime.external_boot` is `None`, so no production caller reaches
this mechanism either way.

The three mechanisms that already have a home keep it. `readiness` is the existing
`_real_readiness`, whose signature is already `ReadinessProbe` and which is already the
production probe wired at `LocalLibvirtInstall.from_env`. `pin_lease` and `open_guest` are
constructed from the lane capability and the libguestfs handle the session already defines.

The `_unconfigured_readiness` / `_unconfigured_observation` / `_unconfigured_cleanup` defaults are
untouched and stay the fallback for an unsupplied mechanism.

## Consequences

**`finalize_tombstone` becomes reachable for an activation whose `prior_power` is `inactive`, and
only for that case.** The archive removal decided here is *necessary* for reachability — without
it the recovery directory keeps `modules.tar` and finalization fails for every archived
activation — but it is not *sufficient*, and the earlier unconditional wording overstated it.

For `prior_power == "running"` a second, independent gate blocks the same path one layer above
this decision. `restore_power` records phase `recovered` with the domain **still active**: it
calls `start()`, requires readiness, and records. `cleanup` then opens a fresh session and calls
`_ConcreteSession.cleanup_payloads`, whose first statement is `require_inactive()` — which raises
`"domain must be inactive before overlay mutation"` while the domain is up. So cleanup raises
before any deletion, `publish_tombstone` never runs, and no tombstone exists to finalize. No
binding of `CleanupPayloads` can change that, because the gate sits above the callable.

The gate also looks wider than its own justification: `cleanup_payloads` removes host-side payload
files and the host-side recovery archive, and touches no guest overlay, yet it is fenced by a check
whose message is about overlay mutation. Deciding whether that fence should narrow means editing
`_ConcreteSession`, which this change declares unmodified, so it is reported for routing rather
than taken here.

The scope of this record's reachability claim is therefore: **necessary for all activations,
sufficient for `prior_power == "inactive"`, and blocked above this seam for
`prior_power == "running"` pending that separate decision.**

**Cleanup and the recovery store must resolve one root, and the builder is what makes that so.**
Cleanup opens `<recovery_root>/<system_id>.<activation_id>` using the root it was constructed
with, while the directory actually holding `modules.tar` is the one
`RecoveryMetadataStore(self._recovery_root)` uses — and that `_recovery_root` is a constructor
argument of `RealLocalExternalBootIO`, which #2212 owns. If the two ever diverged, cleanup would
open a path that does not exist, the idempotence rule would report success, and
`finalize_tombstone` would raise for every archived activation: the exact state this record
refuses to ship, arriving silently. A documented invariant is not a control, because it depends on
a future change honouring it. So the composition builder returns the resolved root alongside the
factory, as one value, and that is the value #2212 passes the store. A mismatch then requires
deliberately discarding what the builder handed you rather than merely forgetting an invariant.

That is as far as this seam reaches, and the limit is stated rather than papered over: #2212 could
still call `config.require` itself and ignore the returned root. This change cannot prevent that;
it can only make the correct wiring the obvious one and leave a single resolution point to point
at in review.

**Two states this decision does not reach, recorded so its enumeration is not read as complete.**
First, `RecoveryArchiveSink.publish` writes `modules.tar` into the `.{system}.{activation}.partial`
directory, and the archive reaches the final directory only when `complete_preparation` renames it.
A worker that dies between a successful publish and that rename leaves the partial holding
`{intent.json, modules.tar}`, which `_publish_initial_intent` then refuses on every retry with the
same activation — permanently, until an operator intervenes — while a retry under a fresh
activation orphans it on the per-slot root with no reaper. `cleanup_payloads` cannot reach either
state, because cleanup runs only after `record_phase(..., "recovered")` on a completed directory.
This is a gap in the recovery model rather than in this mechanism, and it is not fixed here.
Second, nothing removes the `<system_id>` and `<run_id>` directories this mechanism creates:
`finalize_tombstone` rmdirs only `<system>.<activation>`. Both are reported for routing rather than
absorbed.

**The artifact root is per-Run while cleanup and recovery are per-activation.** Two activations
within one Run share one payload directory and the same three names, so the second
materialization overwrites the first's payloads and either activation's cleanup removes the
other's. The likely surface is a hard mid-recovery failure rather than silent corruption, because
`_kernel_bundle_source` checks the payload's digest and byte count against the recovery metadata
before use. The layout follows the System/Run artifact directory the accepted design already
assumes, so this record keeps it and states the consequence instead of inventing a per-activation
layout no other component expects. Relatedly, `_projection_ref` mints
`local-artifact-v1/<system>/<run>/<digest>/<filename>` while payloads sit one level above that
`<digest>` directory; merged code never notices because `_kernel_bundle_source` discards the first
components and opens `parts[4]` relative to the session descriptor, but a future reader resolving
a reference as a path would look in the wrong place.

Payload cleanup now deletes outside the descriptor it is handed. That is the part of this
decision that can go wrong badly, so the deletion is by exact name at an exact
binding-derived path and the directory is re-validated on open; a foreign or symlinked recovery
directory is refused rather than swept.

One configured setting covers both trees. An operator provisions `KDIVE_LIBVIRT_RECOVERY_ROOT`
per worker slot and gets the artifact subtree with it; there is no second setting to keep in step,
and no second failure mode where one root is provisioned and the other is not.

**The local external-boot path stays unexercised end to end.**
`build_external_boot_session_mechanisms` becomes the factory's only caller in `src/`, and is itself
called by nothing until #2212 wires it. So "the factory gains a production caller" is true and also
terminates one level up in a function no production code invokes. That is the same dormancy this
record relies on when it argues the null option is unacceptable, and when it routes the residuals
to #2212 on the ground that neither can manifest while `ProviderRuntime.external_boot` is `None`.
Stating it here keeps the record from claiming, by omission, more liveness than it delivers.

Five of the six mechanisms gain production implementations and the sixth does not, so a factory
built by this change still raises from `_unconfigured_observation` the first time anything asks
it to observe a running kernel. That is the designed behaviour of a partially wired factory, and
it is visible rather than silent. It also means the local external-boot activation path cannot
complete until the domain-XML channel work lands, which is a prerequisite worth being blocked by
explicitly rather than discovering at first live run.

That work — render the qemu-guest-agent channel into `local_libvirt/lifecycle/xml.py` and bind
`observe_running` on it — is owned by #2212. Two facts belong with it. The guest half is already
done: `guest_base_image` installs `qemu-guest-agent` and enables the service on both build paths,
so only the host-side channel is missing. And adding the channel to the renderer does not retrofit
already-provisioned domains, so the observation still fails on Systems that exist today until they
are redefined; whoever takes it chooses redefine-on-next-boot or an explicit migration.

`ProviderRuntime.external_boot` stays `None`. Binding the mechanisms supplies one half of
ADR-0584's precondition; the authenticated authority boundary is the other half, and
advertisement still requires both.

## Considered & rejected

- **Make the artifact root the per-activation recovery directory.** verified: with payloads under
  it, `_publish_initial_intent` raises "recovery partial is not the exact pre-stop intent" because
  `set(os.listdir(directory_fd)) - {"intent.json", ".intent.initial"}` is non-empty, and
  `complete_preparation`'s `os.rename(partial_name, final_name, ...)` fails `ENOTEMPTY` renaming a
  directory onto a non-empty one. Measured: `os.rename` of a directory onto a non-empty directory
  raises `OSError` `errno` 39 (`ENOTEMPTY`), while the same rename onto an *empty* directory
  succeeds — so the distinction the argument rests on is real (Python 3.14.7, Linux
  7.1.12-200.fc44, x86_64; `external_boot.py` at 54f346f55). No ordering of materialize and
  prepare avoids both.
- **Gate `open_artifact_root` through `recovery_directory_name`, as #2211 originally asked.**
  verified: the mechanism's only input is `OperationOwnership`, so the reference argument can come
  only from `_recovery_ref(binding)`, and `recovery_directory_name` compares that reference's
  embedded owners against the same `binding` — the two operands have one source, so no input
  makes it raise. A traversal test against it passes without exercising anything.
- **Add a second setting for the artifact root.** judgment: a second per-slot path to provision,
  validate and keep consistent with the first, for a subtree that is already derivable from it.
- **Keep `cleanup_payloads` inside the handed descriptor and file the archive leak separately.**
  verified: leaves `os.listdir` on the recovery directory returning `["modules.tar",
  "tombstone.json"]` after cleanup, so `finalize_tombstone` raises "cleanup tombstone directory
  contains unexpected payload" for every activation with a module archive. Since this change is
  what first binds the mechanism, shipping that is creating the defect, not inheriting it.
- **Remove `modules.tar` inside `RecoveryMetadataStore.finalize_tombstone` instead.** verified:
  that method already holds a validated descriptor on the exact recovery directory and already
  unlinks `tombstone.json` and `rmdir`s the directory there, so the removal would happen inside the
  component that owns the directory, under the guard it already applies — with no deletion outside a
  handed-in descriptor, no second root to keep in step, and no `recovery_root` plumbing obligation
  for #2212. It is arguably the natural home, and it is unavailable here for a scope reason rather
  than a technical one: this change's frozen scope declares `RecoveryMetadataStore` complete and out
  of scope. Recorded so this list is not read as an exhaustive account of where the removal could
  live; it is an account of where it could live **within this change's surface**.
- **Have `cleanup_payloads` sweep the recovery directory of everything but `intent.json`.**
  judgment: a deletion outside the handed descriptor whose extent is defined by exclusion is
  unbounded by construction; an exact name is checkable and a sweep is not.
- **Bind `observe_running` on the qemu-guest-agent, as remote does.** verified:
  `grep -rn "org.qemu.guest_agent" src/ deploy/ tests/` (exit 0) hits `remote_libvirt/lifecycle/
  xml.py:32` and four remote tests, and nothing under `local_libvirt/`;
  `rg -n "channel" src/kdive/providers/local_libvirt/lifecycle/xml.py` returns nothing, so local
  domains carry no host-side virtio channel. That grep is the whole of the reproduced ground and is
  sufficient on its own. The further claim that `qemuAgentCommand` answers
  `VIR_ERR_ARGUMENT_UNSUPPORTED` ("QEMU guest agent is not configured") is **inferred** — from
  libvirt's behaviour for a domain with no agent channel, and from remote's own
  `_DETERMINISTIC_CONFIG_CODES` comment in `remote_libvirt/guest/agent.py` — and was **not**
  reproduced: no libvirt daemon or defined domain is reachable from this worktree, and defining one
  would write outside a read-only review. The rejection does not rest on it. The images are not the
  gap — `guest_base_image/tasks/build_scratch.yml:72-80` enables the service — the host half is.
- **Render the guest-agent channel into local domain XML here, then bind the observer.**
  judgment: it changes the domain definition of every local System rather than only external-boot
  ones, carries its own provisioning and redefinition consequence for already-provisioned Systems,
  and belongs in a change whose tests are about domain XML.
- **Derive the observation from the materialization, or read `<os><kernel>` back from the running
  domain XML.** verified: both compare the expected value against the artifact that produced it.
  `_RealLocalExternalBootOperation.observe_running` raises unless
  `observed == metadata.expected_running`, and `metadata.expected_running` is copied from
  `materialization.kernel_observation` by `_validate_preparation_owner`; the domain's `<os><kernel>`
  is the host path of the kernel this activation installed. Either source makes that comparison
  compare a value with itself, so the check passes for every input and observes nothing. It would
  produce a green test on a security interlock while proving the interlock absent, which is the
  failure mode this repository has shipped before, so it is refused rather than costed.
- **Observe the running kernel over SSH, reusing `debug/live_introspect.py`'s transport.**
  verified: that path takes `key_path` as a call argument — the per-System bootstrap private key
  the MCP tool boundary materializes (ADR-0289) — and `RunningObserver` is
  `Callable[[UUID], RunningKernelObservation]` constructed once at the composition seam, which has
  no System-scoped key material and no protocol input to receive it from.
- **Read the running kernel from the serial console, reusing the readiness log path.** verified:
  `classify_console` matches only the `kdive-ready` marker and crash signatures; the console
  carries no GNU build id, and `RunningKernelObservation` requires one to a 4–64 byte hex pattern.
- **Relocate the guest-agent seam to `providers/shared/` in this change.** judgment: a correct
  move, but it edits remote's import surface, which this change's frozen scope excludes; deferred
  with an owner instead of taken silently.
- **Do nothing — leave the six mechanisms unbound.** verified: `build_external_boot_session_factory`
  has no caller in `src/` (`grep -rn build_external_boot_session_factory src/`, exit 0, returns the
  definition and one docstring mention), so the local external-boot path cannot be exercised at
  all, and #2212 is blocked on exactly these six callables.
