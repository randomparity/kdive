# systems toolset

A System is the target guest onto which a Run installs and boots a kernel. Allocate capacity
first, then provision the System. Read each tool's schema for exact parameters and provider gates.

## Provision and inspect

`systems.profile_examples` returns starting profiles for configured providers. Select the image
with resource://kdive/docs/guide/toolsets/images.md, then call `systems.provision` with a granted
Allocation and a matching provider/architecture profile. Provisioning requires contributor;
it creates the System and returns a job. Poll `jobs.wait`, then check `systems.get` for READY.
On local-libvirt that job lasts through the guest's first boot — minutes on KVM, longer on an
emulated arch.
There is one System per Allocation; repeating provision is not a way to create a second one.

Choose provision-time debug flags before creating the guest. Enable `debug.gdbstub` for GDB;
follow resource://kdive/docs/guide/toolsets/introspect.md for live-introspection prerequisites.
Local-libvirt manages its own bootstrap key; do not invent a profile credential to enable it.

`systems.get` reports state, connection/capability details, accelerator, and recorded CPU data.
It also names the owning `investigation_id` and a bounded, newest-first `run_investigation_ids`
list of the Investigations with Runs on the System (`run_investigation_ids_truncated` marks a
longer history). Read the System's full run history with `runs.list(system_id=…)`.
`systems.list` provides filters and cursor pagination. Missing capability/CPU data is unknown,
not proof of support. Check `data.supports_snapshots` before using checkpoints, `data.hotplug_devices` (sorted
device kinds, empty when none) before hot-plugging a device, and `data.forced_unplug_devices`
(the subset a forced un-plug can remove, empty when none) before `systems.force_unplug_device`.

## Checkpoint and restore

- `systems.snapshot` captures a READY System's disk and, by default, RAM/CPU. A live Run can
  remain attached during capture; memory capture pauses the guest while RAM is written.
  Poll the job before using the checkpoint.
- `systems.list_snapshots` lists checkpoints newest first; only `available` ones can restore.
- `systems.restore` requires a READY System, no Run holding it, no attached debug session,
  and no conflicting snapshot operation. It is not a recovery route for a CRASHED System.
- `systems.delete_snapshot` enqueues deletion and reclaims the checkpoint's storage.
  A creating checkpoint or an in-progress restore can prevent deletion.

Memory checkpoints resume saved CPU/RAM state; disk-only restores reboot the guest. A memory
restore with `start_paused=true` leaves the System PAUSED. If the Run's other debug prerequisites
hold, attach GDB and set breakpoints before `control.power(action="resume")` returns it to READY.
SSH and drgn-live need the guest executing. All snapshot mutations require contributor; listing
requires viewer. These operations return jobs; poll them rather than assuming immediate completion.

## Hot-plug a NIC or scratch disk

- `systems.plug_device` adds a `nic` or a new empty scratch `disk` (`size_gb` 1 to 32) to a READY
  System. It returns `status: running` with a `job_id` and a `device_id`; poll `jobs.wait`, then
  read the result with `systems.list_devices`. A plug has a 60 s observation window per attempt,
  measured on the worker's monotonic clock from provider-step start; queue and System-lock wait
  are excluded. A started mutation keeps its System lock until completion, so settlement can
  be later than the observation deadline.
- `systems.list_devices` reads the System's device rows (`plugging`, `attached`, `unplugging`,
  `failed`, `lost`) from the database. Compare `settle_deadline` with the response's
  `server_time`, not your own clock. The boot disk and SSH NIC are never listed.
- `systems.unplug_device` gracefully removes an `attached`, `failed` or `lost` device with a 30 s
  observation window under the same clock and scope. Started mutations, including disk absence
  cleanup, finish before settlement. A known completed outcome is recorded; a pending outcome
  at expiry marks the row `failed` and the device may still be in the guest. A guest or HMC
  refusal returns the row to its prior state. Read `systems.list_devices`, then repeat unplug
  to clear a `failed` or `lost` row.
- `systems.force_unplug_device` is the admin-only, destructive variant: it removes an `attached`
  or `failed` device without waiting for the guest to release it, and can wedge or crash the
  guest. It needs the project `admin` role and a profile that lists `force_unplug_device` in its
  provider block's `destructive_ops`; a denial names every missing factor. Only the fault-inject
  provider forces an un-plug today: local and remote libvirt advertise no forced kind and refuse
  with `capability_unsupported` even when the profile opts in. Check `data.forced_unplug_devices`
  on `systems.get`. A `lost` row is refused (`device_not_forceable`); use
  `systems.unplug_device` to clear it.

At most 8 device rows per System. Only one plug or un-plug (graceful or forced) runs at a time,
and while one is queued or running `control.power`, `systems.snapshot`, `systems.restore` and `systems.reprovision` are
refused `conflict` (`reason: device_op_in_flight`); `systems.teardown` is not. Devices are
live-only: a power OFF or cycle and a snapshot restore turn `attached` rows into `lost`. A
provider that cannot hot-plug the kind is refused `capability_unsupported`. Plug and un-plug
require contributor, a forced un-plug requires admin; listing requires viewer.

## Reach and customize the guest

1. `systems.ssh_info` returns coordinates for a READY System with a provider SSH forward.
   Respect `host_scope`: worker-loopback coordinates require access to that host, and are not
   your remote client's loopback address. Remote-libvirt SSH needs operator-configured parity.
2. `systems.check_ssh_reachable` runs a fresh worker probe. Poll its job and read `refs.result`.
   A successful measurement can report `reachable=false`; `true` confirms an SSH banner, not
   authorization to log in.
3. `systems.authorize_ssh_key` installs your public key in the guest root account. It requires
   contributor; poll the job to success before connecting. KDIVE does not need your private key.

After login, use the guest package manager as root to install tools within guest disk and
network limits. Local-libvirt has
no outbound egress by default: the operator must enable `guest_egress = true` on the resource
for direct mirror access. Enabling it also lets the guest reach services that listen only on the
host's loopback interface, which the network-zone firewall does not cover; the image lifecycle
runbook has the detail. Remote networking is also operator configured. Use a prepared image
when package mirrors are unavailable.

## Rebuild and finish

`systems.reprovision` rebuilds a READY System with a replacement profile and requires contributor.
Preserve evidence first; this resets the guest and is not admitted on a CRASHED or FAILED System.

`systems.teardown` requires the project's admin role. Poll its job to success before releasing
the Allocation with `allocations.release`: teardown does not itself release the Allocation.
Do not treat allocation release as proof that provider resources or checkpoints were destroyed.

For an external boot in `recovery_conflict`, `systems.resolve_external_boot_conflict` requires
admin and the exact observed identity. Its worker rechecks that identity before acting; poll the
job and preserve failure evidence if it refuses. Read the owning Run with `runs.get` for the
current recovery guidance. See resource://kdive/docs/guide/toolsets/runs.md for ordinary release
of an active external boot.
