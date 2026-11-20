# Forced device un-plug behind the destructive-operation gate (#3721)

Decision: [ADR-0828](../../adr/0828-forced-device-unplug-behind-the-destructive-operation-gate.md).
Builds on [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md) and its
[spec](2026-11-18-provider-device-hotplug-design.md). Hot-plug epic: #1380. PowerVM: #3688.

## Problem

`systems.unplug_device` sends a graceful request and stops at the settle window. An agent cannot
make a device vanish under a guest that holds it, and an un-plug the guest never answers ends as a
`failed` row that only a retry or teardown clears. The issue asks for an authorized forced removal
behind the destructive-operation gate. This spec fixes the contract the implementation issues build
against.

## Verified facts about the code on main

Checked at `76834c6b0`. Each design statement below that depends on one is marked [F#].

| # | Fact | Where |
|---|---|---|
| F1 | The gate is two checks, role (default `admin`) and profile opt-in; `DestructiveOp.kind` must be in `OPT_IN_DESTRUCTIVE_JOB_KINDS` or construction raises `ValueError`. | `security/authz/gate.py:34-86` |
| F2 | `OPT_IN_DESTRUCTIVE_JOB_KINDS` is `{FORCE_CRASH}`; the profile write boundary derives its valid `destructive_ops` tokens from it. | `domain/operations/jobs.py:120`, `services/systems/validation.py:29-50` |
| F3 | `force_crash_system` checks project membership, then `_authorize_destructive`, then state, then `keyed_mutation`. A denial is audited `force_crash:denied` with `missing`, and returns `authorization_denied`. The opt-in comes from `runtime.profile_policy.destructive_opt_in(profile, kind)`. | `mcp/tools/lifecycle/control/registrar.py:197-239, 261-330` |
| F4 | `control.force_crash` does not use the role wrapper of `with_runtime_for_system`; the gate owns the admin check. `systems.unplug_device` uses it with `Role.CONTRIBUTOR`. | `control/registrar.py:730-775`, `systems/registrar.py:902-955`, `support/_runtime_resolution.py:249` |
| F5 | The role-filtered tool listing keys on tool name: `control.force_crash: _ADMIN`, `systems.unplug_device: _CONTRIBUTOR`. The destructive hint is a reviewed name set. | `mcp/exposure.py:145, 266-267`, `mcp/tools/_docmeta.py:37-60` |
| F6 | `unplug_device` admits `attached`, `failed`, `lost`; refuses when a device job is active or any row is transient; sets the row `unplugging` and enqueues `UNPLUG_DEVICE` (`max_attempts=1`, dedup `unplug:<device_id>:<attempt>`) in one transaction under the System advisory lock and `keyed_mutation`. | `mcp/tools/lifecycle/systems/devices.py:58, 334-405` |
| F7 | The un-plug handler re-checks `READY`, calls `hotplugger.unplug` inside a 30 s window, polls `is_attached`, deletes the row; a refusal (`unplug_refused`, `handle_unknown`, `system_not_ready`) returns the row to `from_state`; anything else makes it `failed`. | `jobs/handlers/devices.py:49-52, 249-325` |
| F8 | `DEVICE_JOB_KINDS` is the pair of plug and un-plug kinds; power, snapshot, restore and reprovision refuse while one is active; teardown is exempt. | `mcp/tools/lifecycle/support/_device_ops.py:20-33` |
| F9 | The reconciler pairs `unplugging` with `UNPLUG_DEVICE` and fails a row with no active job of that kind. | `reconciler/repairs/systems.py:739-775` |
| F10 | `STATE_FENCED_JOB_KINDS` lists both device kinds; payloads map kind to class. | `domain/operations/jobs.py:75-85`, `jobs/payloads.py:216-224, 732-733` |
| F11 | `DeviceHotplugger.unplug` has no force parameter, and its docstring says it never forces. The libvirt adapter calls `detachDeviceFlags(xml, VIR_DOMAIN_AFFECT_LIVE)` and treats the already-pending error as accepted. | `providers/ports/lifecycle.py:408-446`, `providers/shared/libvirt_hotplug.py:45-47, 311-332` |
| F12 | `ProviderRuntime.__post_init__` enforces `bool(support.hotplug_devices) == (device_hotplug is not None)`. | `providers/core/runtime.py:215-238` |
| F13 | fault-inject `unplug`: `busy` refuses `unplug_refused`; no behaviour removes at once; `slow` and `hang` accept unsettled. | `providers/fault_inject/lifecycle/hotplug.py:144-168` |
| F14 | libvirt v12.0.0 QEMU driver: `qemuDomainDetachDeviceLiveAndConfig` calls `virCheckFlags(VIR_DOMAIN_AFFECT_LIVE | VIR_DOMAIN_AFFECT_CONFIG, -1)`, so `MODIFY_FORCE` is invalid for detach. `VIR_DOMAIN_DEVICE_MODIFY_FORCE` is read only in `qemuDomainUpdateDeviceFlags` (header: "force eject a cdrom"). Source read from the `v12.0.0` tag; the project pins `libvirt-python==12.9.0`, so the spike re-checks the daemon version in use. | libvirt `src/qemu/qemu_driver.c`, `include/libvirt/libvirt-domain.h` |
| F15 | The rbac tool matrix and toolset pages are generated from the registrar; `ProvisioningProfile.destructive_ops` docs name only `force_crash`. | `mcp/resources/_content/safety-and-rbac.md`, `profiles/provisioning.py:184` |

## Design

### Tool

`systems.force_unplug_device(system_id: str, device_id: str, idempotency_key: str | None = None)`.
Registered beside `systems.unplug_device`; `annotations=_docmeta.destructive()`; added to
`DESTRUCTIVE_TOOLS` and to `exposure` as `_ADMIN` [F5]. Resolved with
`with_runtime_for_system(..., required_role=Role.VIEWER)` [F4]: membership only, the gate judges
`admin`. The wrapper docstring states the role, the profile opt-in, that it can wedge or crash the
guest, the 30 s window, and carries the shared hot-plug limits text.

Admission, in this order. A step's refusal ends the call.

1. `system_id` or `device_id` not a UUID: `configuration_error` (as F6).
2. Provider has no forced kind (`forced_unplug_devices` empty):
   `capability_unsupported(capability="forced_unplug", supported=[])`. No audit.
3. System missing or not in `ctx.projects`: `configuration_error` (the in-project check precedes
   the gate so the denial audit's project is always readable, ADR-0028).
4. Gate [F1, F3]: `DestructiveOp(kind=JobKind.FORCE_UNPLUG_DEVICE, profile_opt_in=<resolved>)`
   through `assert_destructive_allowed(ctx, allocation, op)` with the default `admin` role. On
   `DestructiveOpDenied`, audit `tool="systems.force_unplug_device"`,
   `transition="force_unplug_device:denied"`, `args={system_id, device_id, missing}` and return
   `authorization_denied` listing `missing`. The authorize helper (`_authorize_destructive`, now
   private to `control/registrar.py`) moves to `lifecycle/support/` and both tools call it; it
   is not copied.
5. Under the System advisory lock and `keyed_mutation` (key kind scoped to the device, as F6):
   - state not `READY`: `conflict`, `system_not_ready`;
   - external-boot admission with `ExternalBootOperation.SYSTEM_DEVICE_HOTPLUG` (no new member);
   - row absent or of another System: `not_found` with the fixed detail (ADR-0123);
   - any device job active on the System, or the row `plugging`/`unplugging`, or any row transient:
     `conflict`, `device_op_in_flight`;
   - row `lost`: `conflict`, `device_not_forceable`, `suggested_next_actions=["systems.unplug_device"]`;
   - row kind not in `forced_unplug_devices`: `capability_unsupported(capability="forced_unplug:<kind>",
     supported=<sorted forced kinds>)`.
6. Set the row `unplugging`; audit `transition="force_unplug_device"`, `args={system_id,
   device_id, kind, from_state}`; enqueue `FORCE_UNPLUG_DEVICE` with `UnplugDevicePayload`
   (unchanged class), dedup key `force_unplug:<device_id>:<attempt>` (`attempt` as F6), `max_attempts=1`,
   authorizing principal as F6. Response: `status: running`, `data={job_id, device_id, kind,
   state: "unplugging", settle_timeout_s: 30, forced: true}`, `suggested_next_actions=["jobs.wait",
   "systems.list_devices"]`.

The gate precedes the replay lookup: a repeat of a keyed call by a caller who has since lost
the role or whose System's profile does not opt in is denied, not replayed. A principal that is
not a member of the project gets the membership refusal at step 3, so the gate reveals nothing
about other projects.

### Gate token and profile

`JobKind.FORCE_UNPLUG_DEVICE = "force_unplug_device"`; added to `OPT_IN_DESTRUCTIVE_JOB_KINDS`
[F2]. The profile write boundary then accepts `destructive_ops: ["force_unplug_device"]` with no
other change, and still rejects every other unknown token. The `destructive_opt_in` implementations
of local-libvirt, remote-libvirt and fault-inject test `op.value in destructive_ops` and need no
change. The `ProvisioningProfile.destructive_ops` docstring, the profile examples, the
safety-and-rbac guide and the `OPT_IN_DESTRUCTIVE_JOB_KINDS` docstring name both tokens [F15]. The
BYO-host profile policy, which has no destructive ops, stays as is.

### Port and capability

```python
class DeviceHotplugger(Protocol):
    def force_unplug(
        self, domain_name: str, device_id: str, provider_handle: str | None
    ) -> bool: ...
```

Semantics are `unplug`'s [F11] with these differences: it does not wait for the guest to release
the device; `True` means gone, `False` means accepted and the caller polls `is_attached`; the
postcondition is that `is_attached` is `False` when the call or the poll succeeds. Idempotent: an
absent device returns `True` after freeing any backing volume. A device whose kind the provider
does not force: `CONFIGURATION_ERROR`, `data.reason: forced_unplug_unsupported`. A provider
refusal: `CONTROL_FAILURE`, `data.reason: unplug_refused` (existing reason, so the handler's
refusal set [F7] is unchanged), with `data.forced: true`. A fault, an absent domain, a stopped
domain that still holds the device: as `unplug`.

`ProviderSupport.forced_unplug_devices: frozenset[HotplugDeviceKind] = frozenset()`.
`ProviderRuntime.__post_init__` raises `ValueError` unless it is a subset of `hotplug_devices`
[F12]. `systems.get` adds `data.forced_unplug_devices` (sorted list) beside `hotplug_devices`. No
other port field.

### Job and handler

`FORCE_UNPLUG_DEVICE` joins `ACTIVE_JOB_KINDS` (automatic), `STATE_FENCED_JOB_KINDS`,
`DEVICE_JOB_KINDS`, the payload map (`UnplugDevicePayload`) [F8, F10], and the reconciler pairing,
which becomes "`unplugging` rows are owned by either un-plug kind" and queries `kind = ANY(...)`
[F9]. It is not in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`. One migration widens `jobs_kind_check` the
way `0200_system_devices.sql` did. The registry binds the existing handler twice; the handler picks
`hotplugger.force_unplug` when `job.kind is JobKind.FORCE_UNPLUG_DEVICE` and `hotplugger.unplug`
otherwise, and everything else in [F7] applies unchanged: same 30 s window, same `from_state`
return on a refusal, same `failed` on timeout or fault, same row deletion on success.

The worker does not re-run the gate. Like `force_crash`, the enqueue is the authorization point:
the job carries the authorizing principal, and the profile is immutable for the System's life.

### Ledger and reclaim

No new column, state or edge. The edges are those of ADR-0823's table: `unplugging -> row deleted`,
`unplugging -> from_state` (refusal, `system_not_ready`), `unplugging -> failed` (`settle_timeout`,
fault, reconciler). `forced` is carried by the audit row and the job envelope only.

| Event | Result |
|---|---|
| forced removal settles | row deleted; a disk's backing volume freed once the device is absent (the existing rule) |
| provider refuses to force | row back to `from_state`; job `control_failure`, `unplug_refused`; the device stays |
| provider does not settle in 30 s | row `failed`, `settle_timeout`; the device may still be in the guest; `systems.force_unplug_device` or `systems.unplug_device` retry is safe on a `failed` row |
| worker dies mid-call | reconciler fails the row (`repair_stalled_device_ops`); the agent retries |
| guest later shows the device again (snapshot restore) | no row exists; `systems.list_devices` does not list it; teardown's `release_all` frees its volume (ADR-0823 accepted case) |
| teardown, reprovision, system reclaim | unchanged: `release_all` best effort, rows deleted; teardown is exempt from the device-job exclusion so a stuck forced job cannot block reclaim |
| power OFF/CYCLE or restore after a forced removal | the row is already gone; nothing to move to `lost` |

### Providers

- **fault-inject** advertises `forced_unplug_devices = {nic, disk}`. `force_unplug` removes the
  device from the in-memory table immediately and returns `True`, ignoring the `busy`, `slow` and
  `hang` behaviours that stall the graceful path; that is the point of the op. The per-plane
  `fault_rate` still injects `INFRASTRUCTURE_FAILURE`. One new `hotplug_behaviour` value,
  `no_force`, makes `force_unplug` raise `unplug_refused`, so the row-returns-to-`from_state` path
  is testable. A device that is not present returns `True`, as `unplug` does.
- **libvirt (local and remote)** advertises nothing at first [F14]. The libvirt implementation issue
  starts with a spike on a live domain per kind that answers one question: is there a call that
  removes the device from the live domain when the guest has not released it, such that
  `is_attached` becomes `False`? Candidates to evaluate are a repeated `device_del` through the
  QEMU monitor, `virDomainDetachDeviceAlias`, and backend-side removal; each is judged against the
  postcondition, not against whether the call returns. A call that leaves the device in the domain
  XML (a backend removed under a still-present device, a link set down) is not a forced un-plug and
  is not advertised under this tool. The outcome of the spike is recorded either as an advertised
  set (with the live proof) or as an amendment to ADR-0828 saying that libvirt cannot force the
  kind. The shared adapter serves both libvirt providers, so remote-libvirt advertises only the
  kinds the local proof covers and its own live proof shows.
- **PowerVM** is #3688's: nothing here, except that its forced HMC remove advertises through the
  same set.

## Failure model

1. **Actors and deployments.** An MCP client holding project `admin` on an allocation, on a `READY`
   System whose profile lists `force_unplug_device`; fault-inject, local-libvirt and remote-libvirt
   runtimes; one worker per job; x86_64 and ppc64le guests.
2. **Invariants.** The op runs only when both gate factors hold at enqueue, and the denial names
   every missing factor and is audited. A forced job never overlaps another device job, power,
   snapshot, restore or reprovision. kdive removes only devices in its ledger: the boot disk and
   SSH NIC are never listed. A provider claims a forced kind only when `is_attached` is `False`
   after the call. A row is deleted only after the provider confirms absence.
3. **Accepted.** Forcing can wedge or crash the guest, and the host's domain can be lost; the System
   then follows the existing failure paths. kdive does not detect that a removed device is still
   used by guest software, and a restore can bring the device back with no row. A worker that
   dies after the provider call leaves a `failed` row, as for the graceful path. A profile
   provisioned before the token existed denies the op until reprovisioned. A forced and a graceful
   request cannot overlap, so forcing waits out a pending graceful job's window; the settle window
   is the same 30 s.
4. **Covered elsewhere.** PowerVM forced mode: #3688. Forced removal of devices kdive did not plug,
   pre-existing volumes, PCIe and SR-IOV: unowned deferrals of ADR-0823, unchanged. Disk quota for
   scratch disks: unowned (ADR-0823). A per-call confirmation argument: not planned.

## Security considerations

- **Entry point.** One new admin tool. The gate precedes the replay lookup and every state or
  device check; the in-project check precedes the gate. A cross-project caller never reaches the
  gate; a same-project non-admin learns only the missing factors (which `force_crash` also
  reveals).
- **Argument handling.** `device_id` is parsed as a UUID; the provider re-parses it
  (`parse_device_id`) and derives the alias from it, never from caller text. No caller string reaches
  a provider command.
- **Authority at enqueue only.** The job carries the authorizing principal; the profile is fixed
  for the System's life, so the opt-in cannot change between enqueue and run. A role revoked in that
  window does not cancel a queued job, as for `force_crash`.
- **Audit.** Success and denial are both audited with the device id and prior state; `detail` text
  from the provider still passes the redactor before reaching `list_devices`.
- **Default.** `forced_unplug_devices` is empty and `destructive_ops` is empty: the op is denied
  twice over until a provider proves a kind and a profile opts in.
- **No new secret, dependency, network exposure or permission grant.**

## Non-goals

| Non-goal | Owner |
|---|---|
| PowerVM forced remove | #3688 |
| libvirt mechanism before the spike proves one | the libvirt implementation issue |
| Forcing devices kdive did not plug; pre-existing volumes; PCIe, SR-IOV, vFC | unowned (ADR-0823) |
| A per-call `confirm` argument; a graceful-first precondition | not planned |
| Detecting guest-side use of a removed device | not planned (no guest channel) |
| Filing the issues below | the orchestrator |

## Proposed implementation split

Do not file from this spec; the orchestrator files after operator review. Order is the dependency
order.

| # | Title | Scope | Effort | Live proof |
|---|---|---|---|---|
| 1 | feat(hotplug): force-unplug port, job kind and fault-inject provider | `JobKind.FORCE_UNPLUG_DEVICE` in `OPT_IN_DESTRUCTIVE_JOB_KINDS`, `STATE_FENCED_JOB_KINDS`, `DEVICE_JOB_KINDS`, payload map; migration widening `jobs_kind_check` and the four window tests; `ProviderSupport.forced_unplug_devices` and the subset check; `DeviceHotplugger.force_unplug` on the protocol, the libvirt adapter (raises `forced_unplug_unsupported`) and fault-inject (with `no_force`); handler and reconciler generalised; `ProvisioningProfile`, profile docs and `destructive_ops` token docs. No tool. | M | none: unit and DB integration tests, including a controlled fault on the gate-token validation |
| 2 | feat(mcp): `systems.force_unplug_device` | Tool, admission order above, shared authorize helper moved to `support/`, audit, exposure `_ADMIN`, `DESTRUCTIVE_TOOLS`, `systems.get` `data.forced_unplug_devices`, generated RBAC and toolset docs, CLI verb, the hot-plug limits text. Depends on 1. | M | fault-inject live-stack cell: denial without opt-in, denial for a contributor, forced removal of a `hang` device, `no_force` refusal, audit rows |
| 3 | spike and feat(libvirt): forced un-plug for NIC and disk, or a recorded negative | The spike of "Providers"; if a mechanism passes the postcondition, `force_unplug` and `forced_unplug_devices` in the shared adapter, else an ADR-0828 amendment. Depends on 1; the live proof of the tool path needs 2. | L | local libvirt on x86_64 (a guest that refuses release, per kind); ppc64le only if the mechanism differs |
| 4 | test(remote-libvirt): live proof of forced un-plug | Advertise on remote-libvirt the kinds issue 3 proved, with a recorded run. Only if 3 finds a mechanism. Depends on 2 and 3. | S | remote libvirt |
