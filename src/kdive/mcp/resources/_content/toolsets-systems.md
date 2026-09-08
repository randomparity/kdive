# systems toolset

A System is the target guest onto which a Run installs and boots a kernel. Allocate capacity
first, then provision the System. Read each tool's schema for exact parameters and provider gates.

## Provision and inspect

`systems.profile_examples` returns starting profiles for configured providers. Select the image
with resource://kdive/docs/guide/toolsets/images.md, then call `systems.provision` with a granted
Allocation and a matching provider/architecture profile. Provisioning requires contributor;
it creates the System and returns a job. Poll `jobs.wait`, then check `systems.get` for READY.
There is one System per Allocation; repeating provision is not a way to create a second one.

Choose provision-time debug flags before creating the guest. Enable `debug.gdbstub` for GDB;
follow resource://kdive/docs/guide/toolsets/introspect.md for live-introspection prerequisites.
Local-libvirt manages its own bootstrap key; do not invent a profile credential to enable it.

`systems.get` reports state, connection/capability details, accelerator, and recorded CPU data.
`systems.list` provides filters and cursor pagination. Missing capability/CPU data is unknown,
not proof of support. Check `data.supports_snapshots` before using checkpoints.

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
for direct mirror access. Remote networking is also operator configured. Use a prepared image
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
