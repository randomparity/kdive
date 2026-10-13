# Resource-boundary proofs on x86_64

## Authority and scope

Issue #3118 and epic #2803 require six real resource cells: minimum sizing, insufficient
storage and slow boot, each on local-libvirt and remote-libvirt. Frozen charter6055580616,
claim q3118-5bd082b7. Human2026-10-07 approved these campaign scopes and exclusions.
#2816 owns remote fixture/remapping, #3117 orchestration faults, #2818 native POWER.
Operator decision R4 (2026-10-13) folds one product fix into this issue: a retried remote
runs.boot waits for a still-booting guest before its guest-agent boot-id baseline read (see
"Remote boot retry ordering"). No other product behavior, new command API or
installed-authority prerequisite is introduced. #2816 and #3093 are closed. Local cells run
on the assigned x86_64 Rocky 10 lab guest; remote cells need a separate x86_64 provider host,
requested from the root and not yet assigned.

## Source findings and dependencies

#2816's provider-qualified remap has landed: scripts/coverage_campaign/contract.py emits
failure-resource/<provider>/x86_64/<failure> owned by 3118. Do not extend its fixed NBD
listener into a general command service. The new carrier maps only 3118's six x86 cells.

Remote config._build_config reads storage_pool from operational environment, not from
Resource inventory. Fixed WorkerSettings, SlotStore._environment and the pre-import gate
all omit it. An actual gate-function probe drops an exported task pool. Patching a running
worker, changing a retained default pool, or bypassing lifecycle registration is forbidden.
The existing Compose deployment is a supported remote-provider path: its lifecycle wrapper
inherits COMPOSE_FILE and managed-worker overrides can pass the pool to all three roles.
Compose config with the managed-worker profile verified this propagation, not runtime
TLS/identity or live parity. Local proofs retain the managed host stack, never containerize
local-libvirt. Remote proofs use a fresh task-scoped Compose project and in-network client.

Remote overlay creation is sparse and takes virtual capacity from the staged base. Merely
making a pool smaller than virtual capacity does not force allocation failure. Local
uploaded-rootfs staging has a real free-space/reservation guard with infrastructure_failure;
remote overlay creation maps real createXML failure to provisioning_failure. No generic
10-GB virtual-size check is presumed. The canonical remote virt-builder lane creates a
10G disk; inspect actual fixture dimensions rather than trusting its catalog name.

Local runs.boot uses the configured poll-count readiness window (default900s), remote
runs.boot waits for a fresh boot ID (default300s). The remote provisioning guest-agent
wait is a different operation/category. Boot failures are retryable, default three
attempts; observe the real terminal job rather than canceling a running operation and
calling it failed. Retry ownership must remain one System/domain/overlay.

## Test architecture

Add a live carrier and domain support alongside existing live_stack lifecycle support;
use CellRun/run_cell, verified kernel fixtures, public MCP allocation/provision/install/
boot/release and existing provider observers/cleanup. Read database state for evidence
only; no synthetic jobs, Systems or allocations. Existing seed_metering provisions the
owned test project's funding. Claims and lifecycle mutations remain through public APIs.

Use one selected Resource by ID with no concurrent allocations and one owned project per
subcase. Preserve normal server/worker/reconciler identity, KVM guest/provider identity,
image digest and required kernel source/config/build/compiler identities. Bind only
observed candidate-matched artifacts. A staged historical image may receive an inspected
RootSpec sidecar on its task-owned copy using inspect_root_boot/write_sidecar; this says
nothing about unknown builder provenance. Never invent that history.

Use test-owned SSH fixed commands for host fixture creation/observation, already-trusted
host keys, validated task identifiers and owned paths. No arbitrary remote product API.
All host commands require future exclusive fixture assignment, manifest ownership checks,
and bounded output/time. Never act on a pool or mount whose ownership cannot be proven.

## Minimum CPU/RAM

Request the existing small shape:1 vCPU,1024MiB,10GB disk, not the existing frame's hardcoded
2CPU/2GB. Use an image whose inspected virtual disk is at most10GB; the resulting local
overlay must be10GB and the remote staged base exactly10GB because remote creation follows
base capacity. If current fixture is larger, build a new owned canonical10G representative
or block; never shrink a retained image or label a larger disk as the minimum.

Provision through MCP, verify effective domain XML vCPU/RAM/disk and persisted concrete
sizing, reach real guest SSH or guest-agent and boot identity. Upload/install/boot the
verified longterm fixture with capture disabled; minimum capacity is not a kdump proof.
Record uname/build identity and actual bound, then release and prove complete cleanup.

## Insufficient disk

Create a task-owned preallocated loop image, ext4 filesystem and mount; physical bound
and free bytes are observed with statvfs. Format with mkfs.ext4 -m 0 so no root-only
reserve lets libvirtd or qemu-img write past the calibrated bound, then remove lost+found so
the mount is empty; record both f_bfree and f_bavail. Upper initial budget8GiB, task host free space
at least image bound plus8GiB safety; larger fixture need is a blocker/design input, not
permission to consume shared storage. Nodev/nosuid mount, required libvirt access follows
existing host provisioning; no blanket chmod or policy weakening. Never fill shared fs.

Local: production ROOTFS_DIR reads KDIVE_LIBVIRT_ROOTFS_ROOT, while the fixed worker
serializes only KDIVE_ROOTFS_DIR; those are distinct and no supported redirect is assumed.
Uploaded staging instead uses fixed UPLOADS_DIR=/var/lib/kdive/rootfs-uploads and exact
<UPLOADS_DIR>/<investigation UUID>/<digest>. Create one owned investigation through MCP,
verify its UUID subdirectory is new/empty and no operation/dependent System exists, then
mount the owned loop filesystem at that exact owned subdirectory, never its shared parent.
Check mount identity against actual staged_rootfs_path before provision. Use a real valid
rootfs uploaded through artifact APIs and complete_rootfs_upload. Bound free bytes below
its observed staging reservation. Assert infrastructure_failure,
capacity-shortfall context, no partial staged object/domain/overlay, and retained allocation
until explicit cleanup. Do not substitute a malformed qcow2 or fail on permissions.

Remote: create a uniquely named libvirt directory pool over the owned mount through trusted
operator SSH, stage only the task-copy valid10G base, and select that exact pool in all
Compose roles. Verify runtime selection, not just the rendered Compose model. A positive
control verifies this same base and pool work before constraining space. Reserve the
remaining space with one owned bounded ballast file inside this filesystem, leaving less
than the measured overlay metadata requirement. Record statvfs and calibration; if sparse
creation still succeeds, retain failure of the experiment and recalibrate only from observed
allocation, never report a storage refusal that did not happen. Domain-start permission,
TLS or missing-base failures do not satisfy this cell. Required result is actual storage
create failure mapped provisioning_failure with no partial volume/domain after retries.
No default pool mutation, quota on unrelated files or host-wide low-space condition.

After failed provision reaches its actual terminal state, remove only owned ballast,
release through public lifecycle and verify accounting and provider absence before pool
undefine, unmount and loop detach. The local investigation mount additionally requires
worker quiescence and no fetch lease/job/System dependent on its staged image; then unmount
and detach BEFORE closing the investigation, because the close-driven rootfs reclaim removes
the UUID directory and cannot remove a live mountpoint (it would clear its drain marker and
leave an orphan in the shared uploads root). After unmount, check the underlying directory
is empty and remove the directory the test created, then close the investigation. The
close-driven reclaim only runs after its grace period (a day by default), so the carrier does
not wait on it. A host whose preparation never created the uploads root is a blocked cell, not
a fixture the test may create.
Uncertainty leaves the fixture mounted and retained. Existing image/catalog/object ownership cleanup remains
explicit; failed cleanup never counts as the cleanup assertion.

## Slow boot: inside and beyond the existing window

Make a task-owned image copy with a systemd delay unit, initially unarmed, ordered before
kdive-ready and guest-agent: Type=oneshot, TimeoutStartSec=infinity, and
ConditionKernelVersion= the installed test-kernel release. Because qemu-guest-agent can be
started by udev, the proof is the same-boot journal: the delay unit's start and finish
timestamps precede the guest agent's and kdive-ready's start, and the finish precedes job
success (inside-window) or the attempt's boot_timeout (beyond-window). Do not delay shutdown or old-boot responses. Provision baseline,
verify healthy readiness, then arm the unit via authorized guest access for the exact
installed test-kernel release. An observed boot-ID/journal marker proves the delay ran on
the new boot; simply withholding client polling is not a slow guest.

Inside-window subcase: measure baseline, require substantial margin, choose a fixed delay
at least60s beyond baseline but below half the actual boot window. If those bounds cannot
both hold, block this fixture instead of inflating product deadlines. Assert pending job,
retained allocation/capacity while delayed, then successful fresh-kernel readiness.
Beyond-window subcase: the same owned System and Run, re-armed, have a persistent per-boot
delay (forced runs.boot) longer than
the actual configured window plus60s. Record each actual attempt phase/category and terminal
job outcome. Remote boot reads the old boot ID via QGA before rebooting; with the R4 fix a
retry that starts while the delayed boot is still in progress waits for the agent inside the
boot window (see below), so every attempt ends in boot_timeout and the terminal job outcome is
boot_timeout. The carrier still records each attempt's actual category rather than presuming
it, so a regression of the fix reads as a failed cell, not a pass.

### Remote boot retry ordering (R4 product fix)

RemoteLibvirtInstall.boot read the baseline boot ID with a single guest-agent round trip. A
retry dispatched after boot_timeout found the agent still down from the earlier reboot and
failed with transport_failure before reaching its own boot wait, so the natural slow guest
could exhaust its attempts without a terminal boot_timeout. The baseline read now polls the
agent-down categories (transport_failure, infrastructure_failure from the agent seam) inside
the same boot window as the fresh-boot wait, then proceeds as before; an agent that never
answers in that window is boot_timeout and no reboot is issued. A non-zero helper exit still
maps through ADR-0489 at once and any other fault still propagates. Both categories are
retryable, so queue policy is unchanged. ADR-0082 records this as a dated amendment. A unit
test drives a retry against a still-booting guest and fails without the fix; a second pins
the silent-window boot_timeout. For a persistent delay D > W + 60 s, attempt 1 times out at
W; the retry's baseline either catches the delayed boot and reboots into another delay (fresh
wait times out) or waits out its own window (baseline times out), so each attempt reports
boot_timeout whichever happens.

No queue pause, fake agent response, retry/timeout policy mutation or cancellation may
force a desired category. Preserve first-attempt timeout evidence separately from final
job outcome. A terminal transport_failure does not satisfy the required boot_timeout;
record the actual failure as a failed cell, never as coverage.
Local retries can power-cycle and repeat their readiness wait, but actual terminal results
still govern. Carrier bounds derive from observed attempt/window/probe limits, retaining
local poll-count semantics rather than mislabeling the default900s as a hard wall. One
remote attempt is bounded by baseline wait + reboot + fresh-boot wait + kdump-arm wait, each
up to its configured window plus one agent exec (whose own exit wait is the boot window), so
the typical case is about twice the boot window plus the arm window and the worst case adds
one boot window per phase. The carrier deadline uses the worst case times attempts plus
cleanup; a carrier deadline expiry records an incomplete cell, never a category.

Per-attempt evidence: a retry resets the jobs row's error_category and failure_context and
restarts at once, so database polling sees only the terminal attempt. The per-attempt
category source is the worker journal (systemd slot units locally, compose logs remotely):
the only failure line, "job <id> failed: <category>", carries no attempt number, so attempt N
is the Nth such line for that job. The line count must equal jobs.attempt and the last line
must equal the terminal category; on any mismatch (for example an attempt lost to a lease
lapse logs nothing) every per-attempt category for that job is recorded unknown.

Keep the delayed guest and capacity owned until the job is terminal and teardown/release
is safe. Verify no duplicate System/domain/volume across retries. Explicit release proves
provider absence and capacity restoration; a boot timeout itself does not promise instant
allocation release. Guest delay files disappear with owned images/disks during cleanup.

## Protected state and cleanup

During each in-flight interval poll actual job state, Allocation state and Resource in_use
with consistent read-only snapshots and observe owned domain/volume names. Retain timestamps
and attempt IDs. Assert allocation remains granted, capacity charged, and at most one owned
System/domain/overlay; do not claim instantaneous observation between samples. Final ledger/
state checks complement samples. Scope to exact owned IDs rather than fleet totals alone.

Cleanup ordering: terminal worker operation; public teardown/release; verify domain/volumes
and allocation/capacity; remove owned base/ballast/pool; unmount/detach (local: before the
investigation close, see Insufficient disk); close owned investigation and its artifact
lifecycle; stop task Compose through canonical lifecycle; remove owned image after artifact
evidence retained. On uncertainty keep bounded fixtures and exact
IDs, report cleanup failed with operator recovery; never force delete a potentially live
backing file or infer cancellation means provider I/O stopped.

## Failure model and validation

Missing host assignment, inaccessible runtime pool, unreachable object
store/guest return path, absent matching kernel or insufficient safe workspace blocks cells.
#2816 and #3093 are closed; the remote slow-boot retry ordering is fixed in this PR
(R4). A missing remote provider host keeps the three remote cells unproven: they stay
required and are reported awaiting a host, never counted as passed. Neither managed Compose rendering nor ordinary tests proves reachability.
Real product faults are recorded under existing owners/root, not worked around in the carrier.

Focused tests cover owned-path/pool guards, quota measurements, state/attempt predicates,
retry timing bounds and no false success on wrong category, missing delay marker, wrong
shape, duplicate resource, premature release or failed cleanup. Use independent realistic
records/XML and boundary fakes only. Controlled faults must turn assertions red. No collected
test imports; reusable helpers live in domain support. An actual-shaped build_record/qualify
regression covers all six cell IDs/required assertions and identity. Each new gate's first
real result remains required. Final full suite belongs to installed pre-push hook; native
POWER excluded here, never deleted from qualification.
