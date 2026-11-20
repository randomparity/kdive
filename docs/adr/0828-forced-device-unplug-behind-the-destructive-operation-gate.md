# 0828 — A forced device un-plug is a separate admin tool behind the destructive-operation gate

## Status

Accepted (2026-11-19)

Implementation is tracked by the issues the spec proposes (it files none). Nothing in this record
is implemented yet.

## Context

[ADR-0823](0823-provider-device-hotplug-nic-and-scratch-disk.md) ships `systems.unplug_device`,
a graceful request that never forces the guest. It deferred a forced un-plug as "a new decision
that would need the gate", with no owner (#3721). Two things are missing today. An agent cannot test
how a driver behaves when a device disappears while the guest still holds it. An un-plug the guest
never answers ends as `failed` after the 30 s settle window and can only be retried or abandoned
until teardown.

The destructive-operation gate is the existing mechanism for an op that can damage the guest. It
is two independent checks: the caller holds the required role on the allocation's project, and the
System's provisioning profile listed the op in `provider.<kind>.destructive_ops`. Its only user is
`control.force_crash` (admin). The gate takes a `JobKind` that is a member of
`OPT_IN_DESTRUCTIVE_JOB_KINDS`, and the profile write boundary rejects any `destructive_ops`
token that is not such a kind's value
(`security/authz/gate.py:34-46`, `domain/operations/jobs.py:120`,
`services/systems/validation.py:29`).

The issue asks for forced removal "through the libvirt detach force flag". That flag does not
exist for this call. `virDomainDetachDeviceFlags` accepts only `AFFECT_LIVE` and `AFFECT_CONFIG`
(libvirt v12.0.0, `qemuDomainDetachDeviceLiveAndConfig`: `virCheckFlags(VIR_DOMAIN_AFFECT_LIVE |
VIR_DOMAIN_AFFECT_CONFIG, -1)`), so passing `VIR_DOMAIN_DEVICE_MODIFY_FORCE` is rejected.
That flag appears in the QEMU driver only in `qemuDomainUpdateDeviceFlags`, and its header comment
says "force eject a cdrom". QEMU's own `device_del` is also a request the guest must honour. Whether
any libvirt call removes a NIC or a disk from a guest that refuses it is unproven, so this record
fixes the contract and the provider honesty rule, not a libvirt mechanism.

## Decision

**A separate tool, not a flag.** `systems.force_unplug_device(system_id, device_id,
idempotency_key?)`, a worker job. `systems.unplug_device` is unchanged and stays contributor and
ungated. A separate tool is the only shape that lets the role-filtered tool listing
(`mcp/exposure.py`), the RBAC table, the destructive tool hint (`mcp/tools/_docmeta.py`) and the
denial audit name the operation, as they do for `control.force_crash`. A flag would hide an admin
operation behind a contributor tool and put a role check inside a contributor handler.

**Gate and role.** The tool uses the existing gate with the default `admin` role factor and the
profile opt-in factor. The opt-in token is `force_unplug_device`: a new `JobKind` member
`FORCE_UNPLUG_DEVICE`, added to `OPT_IN_DESTRUCTIVE_JOB_KINDS`, so the token is valid at the
profile write boundary with no change to the gate. The graceful `unplug_device` kind is not reused
as the token because `destructive_ops: [unplug_device]` would read as gating the graceful call, and
the denial audit transition `unplug_device:denied` would collide with the success transition
`unplug_device`. The "confirmation" the issue names is the gate itself, as for `force_crash`: the
profile opt-in is the caller's pre-declared consent and the role is the authority. There is no
per-call `confirm` argument; `force_crash` has none.

The tool resolves the wrapper with `required_role=Role.VIEWER` (project membership only) and lets
the gate judge `admin`, so a denial lists every missing factor and is audited as
`force_unplug_device:denied`, as `force_crash_system` does. The gate runs before the replay lookup
and before any device or state check, so a replay never outlives the caller's authority and a
non-admin cannot probe device state through this tool.

**The port expresses force as a separate method.** `DeviceHotplugger.force_unplug(domain_name,
device_id, provider_handle) -> bool`, with `unplug`'s return meaning (`True`: gone; `False`:
accepted, poll `is_attached`). A `force: bool = False` parameter on `unplug` is rejected: a default
that silently selects the graceful path is one omission from the wrong behaviour in either
direction, and a separate method keeps the destructive call greppable. The method does not wait for
the guest to release the device. The postcondition is the same one the graceful path has: the call
returns `True`, or `is_attached` becomes `False` inside the settle window, or the provider raises.
A provider must not report success for a call that only detached a backend and left the device in
the live domain.

**Capability, per provider and per kind.** `ProviderSupport.forced_unplug_devices:
frozenset[HotplugDeviceKind]`, empty by default (fail closed), required to be a subset of
`hotplug_devices` (checked in `ProviderRuntime.__post_init__` beside the existing parity check).
There is no second port field: the capability rides the existing `device_hotplug` port, whose
`force_unplug` raises `CONFIGURATION_ERROR` (`data.reason: forced_unplug_unsupported`) for a device
whose kind the provider does not force. `systems.get` adds `data.forced_unplug_devices`, a sorted
list. fault-inject advertises both kinds. local-libvirt and remote-libvirt advertise nothing until
a mechanism is proven live for a kind (spec, Providers); a provider that cannot force a kind never
claims it. PowerVM is out (#3688); the contract only has to leave room: its HMC remove with a forced
mode would advertise through the same set.

**Ledger outcome.** A forced removal uses the existing `unplugging` state and the existing edges:
`unplugging -> row deleted` when absence is confirmed, `unplugging -> from_state` on a provider
refusal, `unplugging -> failed` on `settle_timeout` or a fault. It adds no state and no column. The
row is deleted when the provider confirms the device is absent, whatever the guest later does. kdive
has no channel into the guest, so it cannot observe a driver still bound to a removed device;
that is the agent's observation, like every other guest-side fact. A removal the guest later
contradicts (a restored snapshot brings the device back) is the case ADR-0823 already accepts: the
row is gone, the device has no row, and teardown's `release_all` frees what remains. What a forced
removal adds is one fact the ledger does not hold: it was forced. That fact is in the audit row
(`transition` `force_unplug_device`) and in the job envelope (`data.forced: true`).

**Admission.** In order: ids; provider has any forced kind (else `capability_unsupported`,
`capability: forced_unplug`); project membership; the gate; then under the System advisory lock and
the replay key, `READY`, external-boot admission, the row (`not_found`), the row's state, the
row's kind against the provider's forced set (`capability_unsupported`, `capability:
forced_unplug:<kind>`). Admitted row states are `attached` and `failed`. A `lost` row has no device
for the provider to force, and its graceful path already frees the volume, so it is refused
`conflict`, `data.reason: device_not_forceable`, pointing at `systems.unplug_device`. A `plugging` or
`unplugging` row, or any active plug or un-plug job on the System, is `device_op_in_flight`: the
agent waits out the 30 s window of a graceful request, or `failed` appears, then forces. No
graceful attempt is required first.

**Reuse, not a new limit contract.** The forced job joins the device job kinds: it is state-fenced,
`max_attempts=1`, not contributor-cancelable, excluded with power, snapshot, restore and
reprovision, exempt for teardown, and repaired by `repair_stalled_device_ops`. It runs the existing
un-plug handler with `force_unplug` in place of `unplug`, in the existing 30 s window.

## Consequences

- The sole caller of the gate gains a second op, and `destructive_ops` gains a second valid token.
  A System provisioned before the token existed has the op denied until it is reprovisioned with
  the token in its profile; the intended path is preventive (ADR-0130).
- One migration widens `jobs_kind_check` with `force_unplug_device`. Per the migration landmine,
  the four `migrations[-K:]` window tests need the new row.
- A forced un-plug can wedge or crash the guest, or lose the domain. That is the purpose of the
  op and the reason it is admin-only; recovery uses the existing paths (`control.power`, teardown,
  reconciler repairs for a lost domain).
- libvirt may never advertise the capability. The first provider to prove it is fault-inject,
  which exercises the tool, gate, ledger and audit end to end; a libvirt spike either finds a
  mechanism or records that none exists.
- A graceful un-plug that the guest ignores no longer ends in abandonment: `failed` row, then
  `systems.force_unplug_device` for an admin on an opted-in System.

## Considered & rejected

- **A `force` flag on `systems.unplug_device`.** judgment: it makes an admin-gated operation a
  contributor tool; exposure, the RBAC matrix, the destructive hint and denial audit all key on the
  tool name, and the contributor wrapper's role check would have to be bypassed for half its inputs.
- **Contributor role plus the profile opt-in.** judgment: the gate supports a per-op role, so it is
  possible, but removing a device under a guest can crash it, which is why `force_crash` is admin;
  leaving the role at admin keeps one rule for the gate. It is the operator's call (PR body).
- **Reusing `JobKind.UNPLUG_DEVICE` as the opt-in token with a `force` payload field.** judgment:
  the token would gate only half the kind, the denial transition would collide with the graceful
  one, and it saves a migration only by blurring the profile vocabulary.
- **A `force: bool` parameter on `DeviceHotplugger.unplug`.** judgment: see Decision.
- **A second port field for forced removal.** judgment: it adds a runtime field and a parity check
  for a capability that is a per-kind subset of an existing port.
- **A new device state such as `removed_forced` that stays until the guest confirms.** judgment: the
  guest cannot confirm to kdive, so the row would never leave the state; the audit row records that
  the removal was forced.
- **Force only after a graceful attempt failed.** judgment: it adds a state-machine precondition
  and a 30 s wait for the case the agent wants to test directly (surprise removal of a healthy
  device); the `failed` row already shows when a graceful attempt gave up.
- **Pass `VIR_DOMAIN_DEVICE_MODIFY_FORCE` to `detachDeviceFlags`.** verified: rejected by the QEMU
  driver's flag check (Context).
- **A per-call `confirm` argument.** judgment: `force_crash` has none; the profile opt-in is the
  durable consent, and the tool name and destructive hint tell the client to prompt.
