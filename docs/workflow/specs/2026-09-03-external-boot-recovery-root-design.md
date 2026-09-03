# Local external-boot recovery root — setting and provisioning

Issue: [#2210](https://github.com/randomparity/kdive/issues/2210). Parent: #2208.
Governing decision: [ADR-0586](../../adr/0586-local-external-boot-recovery-uses-an-owned-host-directory.md).
Registry regime: [ADR-0087](../../adr/0087-config-registry.md).

## Why this exists

`RealLocalExternalBootIO.__init__`
(`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py:741`) takes a
`recovery_root: Path` that nothing supplies. ADR-0586 already decided what that root is —
"Local-libvirt stores each recovery point beneath its configured provider-owned recovery
root" — and its Consequences already state the obligation this change discharges: "The
configured recovery root becomes durable provider state and must share the worker's
lifecycle, permissions, backup expectations, and provisioning parity on x86_64 and ppc64le
hosts."

So this change writes no new decision. It supplies the missing configuration and the
missing provisioning for a decision that is accepted, and stops there. #2211 owns the
session-factory mechanisms; #2212 owns constructing `RealLocalExternalBootIO` and consuming
the root.

## The contract the root has to satisfy

Two stores open the root itself, not merely its children:

- `RecoveryMetadataStore.__init__` (`external_boot.py:1552-1558`)
- `TargetProjectionStore.__init__` (`external_boot.py:238-244`)

Both do `os.open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)` and then
`_require_private_owned_directory` (`:1916-1922`), which raises unless the opened
descriptor is a directory whose `S_IMODE` is exactly `0o700` and whose `st_uid` equals
`os.geteuid()`.

Those are the conditions the setting validates and the conditions provisioning must
produce. This change alters none of the four named guards; #2210 places them out of scope
and they are complete.

## Requirement 1 — the setting

`KDIVE_LIBVIRT_RECOVERY_ROOT`, declared in
`src/kdive/providers/local_libvirt/settings.py` beside the module's five existing settings
and appended to its `SETTINGS` list, `group="local-libvirt"`,
`processes=frozenset({"worker"})`.

**Worker only, deliberately not the module's `_RT`.** The five siblings are host-uniform
values — a libvirt URI, an allocation cap, two second-counts, a multiplier — so the same
string is correct in every process that reads them. This one is uid-bound by construction:
`parse` requires `st_uid == geteuid()`, and the roots are `0700` owned by `kdive-worker-N`.
The reconciler runs as `User=kdive`
(`deploy/systemd/system/kdive-reconciler.service:9`), and `src/kdive/__main__.py:442` calls
`config.validate(args.command)` for every runnable, so declaring `reconciler` would make the
reconciler fail to start with a `CONFIGURATION_ERROR` on exactly the hosts that configure
this setting. It would also publish `reconciler` in the generated reference's `Processes`
column, inviting an operator to put the value in the shared `/etc/kdive/kdive.env` that both
services read. Copying the siblings' process set is the one thing that must not be done
here.

**It carries no default, and `required_when` stays at the registry default
`never_required`.** Both halves are load-bearing:

- No default is what makes absence rejectable. `Registry.get` (`registry.py:120`) returns
  `None` only when the variable is absent *and* the setting has no default; `require`
  (`:143`) then raises `CONFIGURATION_ERROR` naming the variable and carrying `suggest`. A
  default would resolve absence to a path instead of rejecting it, and issue #2210 requires
  absence to be rejected.
- Never-required is what keeps the mechanism opt-in. `Registry.validate` (`:167`) fails a
  process when `required_when` holds and no value is present, so `required_when=_always`
  would make every worker and reconciler host fail startup until it had provisioned a
  recovery root — turning a dormant path on ahead of #2212. Never-required leaves an
  unconfigured host working exactly as it does today.

The combination gives the behaviour #2210 asks for without a third mechanism: an
unconfigured host is unaffected, a configured host is validated at startup, and a consumer
that needs the root gets a named error if it is missing.

Two mechanics constrain how those two halves are written, and both are silent when got
wrong:

- **`required_when` must be omitted from the constructor call, not set to a local
  always-false predicate.** `scripts/generate/gen_config_reference.py:32` tests
  `setting.required_when is never_required` — an identity comparison against the sentinel it
  imports at `:15`. A locally-defined equivalent fails that `is`, falls through to `:34`,
  evaluates `required_when({})` to `False`, and renders `Required: conditional` for a
  setting that is never required. `just config-docs` would regenerate that wrong value
  consistently, so `config-docs-check` stays green and the published reference ships the
  error.
- **A default would also escape the startup preflight, not merely swallow the absent
  rejection.** `Registry.validate` calls `get(s)` only for settings where `s.name in env`
  (`registry.py:170-171`), so a malformed *default* is never parsed by it. Carrying no
  default is what keeps the validation requirement above reachable at all.

### Validation runs at configuration resolution

`Registry.validate(process)` (`:170`) calls `get(s)` for every declared setting whose name
is present in the environment snapshot, and `get` turns any `ValueError` from `parse` into
`CONFIGURATION_ERROR`. So putting the directory checks in `parse` places them at process
startup — the requirement that a misconfigured root fails "before any provider work
begins" — with no new validation pathway.

`parse` rejects, each with its own message: a relative path, a path that does not exist, a
symlink, a non-directory, a mode other than `0o700`, and an owner other than the running
euid. It uses `os.lstat`, so a symlink is judged as itself rather than as its target.
`Registry.get` prefixes every one with the setting name, so the "message naming the
setting" requirement holds structurally rather than per-message.

### Why the guard is restated rather than imported

`settings.py` is deliberately dependency-light — its docstring says so — "so aggregating it
through the manifest never pulls the `libvirt` C-extension into a process that does not use
the provider". `_require_private_owned_directory` lives in `external_boot.py`, which reaches
libvirt through its session import, and #2210 excludes changing that module. Extracting the
guard to a shared module would therefore be an edit to an excluded file.

The restatement is held honest by test rather than by inspection: one test creates a
directory of exactly the shape provisioning produces and opens it through the **real**
`_open_private_directory` and `RecoveryMetadataStore`, unchanged. If the two ever disagree,
that test fails rather than the divergence reaching a live recovery.

## Requirement 2 — provisioning

The consuming process is a fixed worker slot running `User=kdive-worker-%i`
(`deploy/systemd/system/kdive-live-worker@.service:8`), one of the eight accounts in
`live_vm_host_worker_accounts`. Because the guard compares `st_uid` to the running euid, a
single shared 0700 root cannot satisfy more than one slot. The root is therefore per
account, and that follows from the guard plus ADR-0586's "share the worker's lifecycle,
permissions" consequence — it is not a new decision.

Layout, following the idiom the role already uses for `/etc/kdive/credentials`
(`tasks/main.yml:383`, "Traverse-only lets each service reach its own private child without
listing sibling names") and for the authority paths (`:433-517`):

- `live_vm_host_worker_recovery_root`, defaulting to
  `/var/lib/kdive/live-workers/external-boot-recovery`, created `root:root` mode `0711`.
  Traverse-only: a worker slot reaches its own child without being able to enumerate its
  siblings.
- one child per account, `{{ root }}/{{ account }}`, owned
  `kdive-worker-N:kdive-worker-N`, mode `0700` — exactly what the guard accepts.

**The child is named for its owning account, not for a stripped slot number**, so this tree
does not mirror the existing `{{ state_root }}/slots/N` layout. That divergence is
deliberate. Deriving `N` would mean a `regex_replace('^kdive-worker-', '')` applied
identically in the create task and the verify task, and a transform duplicated across two
tasks is a transform that can drift — the gate would then check a path provisioning never
created. The recovery tree is new and owes the `slots/N` layout nothing, so the simplest
correct name is the account itself. This is a maintainability choice with no security
content: the slot names were already derivable from the role either way, and the confinement
argument below rests on the parent's mode alone.

Both the parent **and** every per-slot child are preceded by a `follow: false` stat and an
assert that any existing entry is a real directory and not a symlink, matching `:445-455`
and `:490-500`. The children's guard is not decoration: `ansible.builtin.file` with
`state: directory` treats a symlink pointing at a directory as already satisfied, so without
it a substituted slot root is reported converged, the health gate stays red, and the
`fail_msg`'s advice to rerun provisioning can never clear it. `ansible.builtin.file` is
idempotent by construction; the harness proves it by running the role twice and requiring
the second run to report no change.

The root is deliberately **not** placed under `{{ state_root }}/slots/N`. Those directories
are runtime-managed: `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` opens,
permissions-checks and prunes them, and provisioning-created state inside a directory a
state machine validates would couple two owners for no gain.

### Health gate

`tasks/verify.yml` gains stat-and-assert tasks over the parent and every child, following
`verify.yml:327-365`. It asserts `stat.exists` first — so an absent root reports as absent
rather than as an undefined-attribute error — then `isdir`, `not islnk`, `pw_name`,
`gr_name`, and `mode == '0700'` for each child, and `0711` for the parent.

**The parent's owner arm compares against the literal `root`, never against a variable.**
This is the reason there is no `..._recovery_root_owner` setting: an assertion that reads
back the same value the create task consumed agrees by construction and can never fail, so
it constrains nothing. The arm exists because a parent owned by a worker account could
unlink and recreate any other slot's recovery root, which would defeat the confinement the
`0700` children provide — so it must be able to reject a deployment, not merely describe
one. The clean-host harness exercises it **negatively**, with a parent it owns itself,
proving the arm rejects rather than merely that it passes.

## Requirement 3 — the gate stays closed

`ProviderRuntime.external_boot` must still be `None`. #2212 is the single point at which it
becomes non-`None`, and asserting that here makes a reordering of the chain fail a test
rather than quietly violate a dependency edge.

The existing `assert runtime.external_boot is None`
(`tests/providers/local_libvirt/test_composition.py:60`) does not discharge this: it passes
identically whether or not this change exists. The new test binds the assertion to this
change's own mechanism — it configures a valid recovery root, the one input that could
plausibly open the gate, and requires composition to remain unadvertised anyway. It checks
the attribute is present before checking its value, so it cannot pass by the attribute
having been renamed away.

This assertion is the part of the change most likely to rot, so both of its failure modes
are proven rather than assumed. The test is verified to bite against a stub binding
(composition sets `external_boot` to a non-`None` value — the #2212 reordering the criterion
exists to catch) **and** against vacuity (the attribute is removed, confirming the presence
check is what stops the value assertion passing on nothing). A gate test that passes by
absence is worse than no gate test, because it reads as proof the gate is still closed.

Nothing in this change delivers the variable into a worker environment. That would mean
editing `/var/lib/kdive/live-workers/slots/%i/worker.env`, which is written at runtime by
`systemd_worker_state.py` rather than by Ansible — Python, and consuming-change work owned
by #2212. The directory therefore ships provisioned and unreferenced, in the same dormant
posture as the authority paths under `live_vm_host_authority_enabled: false`.

## Threat model

This change widens no entry point and adds no parser, but it sets file modes and creates
directories a privileged play owns, so the boundaries are worth stating.

**Boundaries added.** One: the recovery root directory, between a fixed worker slot
(untrusted-ish — it runs provider code against guest-influenced input) and the host
filesystem. **Boundaries widened.** None; no existing path changes mode or owner.

**Actors.** The worker slot account `kdive-worker-N`, which must reach its own root and must
not reach another slot's; a local operator running the play as root; the guest, which never
sees these paths.

**Controls.** Cross-slot read and write is stopped by each child being `0700` and owned by
its account, enforced twice: by the mode, and by the application's own
`_require_private_owned_directory` at open time, which refuses a root it does not own even
if provisioning were wrong. That is the whole of the confinement.

The `0711` parent is **not** part of it, and the spec should not be read as claiming
otherwise. Traverse-only prevents *enumeration*, not *guessing*: the slot names are
`kdive-worker-1..8`, published in this role's own `defaults/main.yml` and in the systemd
unit's `%i`, so an actor who can traverse the parent can name every child without listing
it. What a non-listable parent buys is that a process which does not already know the naming
scheme cannot discover it by reading the directory — a hardening detail, not a boundary. Any
control that matters here is the child's mode and owner.

Symlink substitution before creation is refused by the pre-create `follow: false` stat and
its assert, on the parent **and** on every per-slot child — the children's guard is the one
that matters operationally, since `ansible.builtin.file` would otherwise treat a symlinked
slot root as already converged. Symlink substitution afterwards is refused by the stores'
`O_NOFOLLOW` open. The setting's `parse` adds a third, earlier refusal at configuration
resolution.

**Two separate mechanisms, and the spec should not conflate them.** The pre-create `stat` and
`assert` are a **diagnostic**: they detect a wrong state that already exists — a prior
layout, an operator's hand-edit, a partially-run play — and produce a message naming the
path. They are not atomic, because `stat` and `file` are separate operations.

The **atomic** refusal is `follow: false` on the `file` tasks themselves. This matters more
than it looks: `ansible.builtin.file`'s `follow` defaults to **true**, and a directory create
through a symlink with the default does not fail — it succeeds and applies the owner and
mode to the link's *target*. Verified on this branch: creating `state: directory` over a
symlink pointing at a mode-`0755` directory left that target at `0700`, reporting success.
With `follow: false` the module fails with "already exists as a link" and writes nothing.

Reaching that window requires write access to the root-owned parent, so no modelled actor
can — this is defence in depth, not a closed exploit. It is set because it costs nothing and
removes the window rather than arguing the window is unreachable.

Likewise, validation at configuration resolution does not survive to use: the root could be
changed between process start and the store's open. That is why the stores re-check on every
open and why this change does not weaken them. The setting's `parse` is an early, loud
failure for the common misconfiguration, not a substitute for the guard at the point of use.

**Out of scope.** A root-equivalent local actor can defeat any of this and is not modelled.
Backup and retention of recovery payloads is ADR-0586's accounting, not this change's.
Whether the eight slots should share one account is settled by the fixed-worker contract
(ADR-0574, whose Decision runs one `kdive-live-worker@N.service` per slot, "each instance
runs as its own no-login account") and not reopened here.

## Testing

- Setting field pins and the `SETTINGS` ordering, extending
  `tests/providers/local_libvirt/test_settings.py`, which already pins the other five.
- One rejection test per condition in requirement 1, asserting the message names the
  condition, plus a `Registry`-level test proving `validate("worker")` raises
  `CONFIGURATION_ERROR` for a bad value and that absence raises through `require`.
- The real-guard equivalence test described above.
- The closed-gate test described above.
- `deploy/ansible/tests/run-external-boot-recovery-root.sh`, a clean-host harness in the
  style of the five already there: it drives the **real** tagged tasks against localhost,
  asserts the created modes and owners, re-runs to prove idempotence, and asserts the verify
  gate fails on a chmodded root. Its line is added to the `test-ansible` recipe, which
  enumerates its harnesses explicitly — a harness not listed there never runs in CI.
- `tests/deploy/test_live_worker_provisioning.py` extended with the role's parsed
  expectations, rather than a parallel copy of them.

## Out of scope

Constructing `RealLocalExternalBootIO` (#2212); the session-factory mechanisms (#2211); any
change to `RecoveryMetadataStore`, `_require_private_owned_directory`,
`_open_private_directory`, or `recovery_directory_name`; delivering the variable into a
worker process environment (#2212).
