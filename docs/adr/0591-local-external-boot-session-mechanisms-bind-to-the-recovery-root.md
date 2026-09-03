# 0591 — Local external-boot session mechanisms bind to the recovery root

## Status

Accepted (2026-09-03)

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
`str(ownership.system_id)`, then `ownership.binding.run_id`, each with
`O_DIRECTORY|O_NOFOLLOW` and each validated by `_require_private_owned_directory` — a real
directory, mode exactly 0700, owned by the running euid. Both child names are `CanonicalUuid`
values, so neither can carry a separator or a traversal component. This matches the System/Run
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
the virtio channel does not exist and `qemuAgentCommand` fails deterministically against a local
domain. Rendering that channel changes the domain XML of every local System, not only
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

`finalize_tombstone` becomes reachable for an activation that captured a module archive, which it
would not be under a descriptor-scoped cleanup. That reachability is proven by a test that fails
with the real "unexpected payload" error when cleanup is bound descriptor-scoped and passes when
it is bound as decided here.

Payload cleanup now deletes outside the descriptor it is handed. That is the part of this
decision that can go wrong badly, so the deletion is by exact name at an exact
binding-derived path and the directory is re-validated on open; a foreign or symlinked recovery
directory is refused rather than swept.

One configured setting covers both trees. An operator provisions `KDIVE_LIBVIRT_RECOVERY_ROOT`
per worker slot and gets the artifact subtree with it; there is no second setting to keep in step,
and no second failure mode where one root is provisioned and the other is not.

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
  directory onto a non-empty one (POSIX `rename(2)`; Linux 6.17, external_boot.py at
  54f346f55). No ordering of materialize and prepare avoids both.
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
- **Have `cleanup_payloads` sweep the recovery directory of everything but `intent.json`.**
  judgment: a deletion outside the handed descriptor whose extent is defined by exclusion is
  unbounded by construction; an exact name is checkable and a sweep is not.
- **Bind `observe_running` on the qemu-guest-agent, as remote does.** verified:
  `grep -rn "org.qemu.guest_agent" src/ deploy/ tests/` (exit 0) hits `remote_libvirt/lifecycle/
  xml.py:32` and four remote tests, and nothing under `local_libvirt/`; local's `render_domain_xml`
  renders no `<channel>`, so `qemuAgentCommand` returns `VIR_ERR_ARGUMENT_UNSUPPORTED` ("QEMU guest
  agent is not configured") against every local domain. The images are not the gap —
  `guest_base_image/tasks/build_scratch.yml:72-80` enables the service — the host-side channel is.
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
