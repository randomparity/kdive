# Reproducible lab lanes and image fixtures

Scope: [#2805](https://github.com/randomparity/kdive/issues/2805), charter
[q2805-193ac5fe](https://github.com/randomparity/kdive/issues/2805#issuecomment-5851844632).
Status: operator approved the design and budgets in the interactive quest checkpoint.

## Problem

The initial inventory found KVM on the supplied x86 hosts but no installed libvirt/system QEMU stack.
Capacity alone does not establish a usable native or emulated lane. The existing warm store
checks three artifact digests but reuses a set by kernel NVR alone, allowing a different
catalog selection or build input to reuse the same set. The build already verifies cloud
source checksums and writes a provenance sidecar; staging must retain and bind that evidence.

## Scope and ownership

C1–C2: Keep addresses, SSH identities, credentials and inventory outside Git in private files.
Publish host aliases, OS/architecture, measured capacity, assigned budgets and actual readiness.
The operator approved isolated task-owned VM/storage capacity on the four supplied hosts,
one fixture build per host, and cleanup limited to this task's resources. OS reprovisioning
is excluded. Use existing Ansible roles to prepare the selected lanes; do not introduce a
scheduler or an automatic host-reset mechanism.

C3: The rootfs catalog and build pipeline remain the source-selection, checksum-verification
and package-provenance owners. Extend the existing staging owner to bind a selected fixture
to those inputs and to its output digests. Warm-store callers keep their existing wiring
variables and NVR pin. A set lacking the new identity evidence must rebuild before reuse.

C4–C5: Extend preflight's declared-family mechanism to distinguish native x86/KVM,
native POWER/KVM-HV and foreign POWER/TCG preparation. Keep the existing test-environment
families. A passed host preflight establishes prerequisites, not a passed VM scenario.
Prepare x86 first; record POWER input selection and a later reservation request without
holding POWER hardware. Native POWER execution remains #2818's responsibility.

Excluded work and owners are the charter's approved set: full scenarios #2807–#2817;
source-built kernel baselines #2806; native POWER execution #2818; accounting/release
policy #2804/#2819; existing product defects their linked issues; hardware purchase and
shared-host reprovisioning a separate operator decision.

## Design

Extend `scripts/live-vm/preflight-env.sh`, `warm-store.sh`, and their shared staging helpers.
Use the existing catalog resolver and provenance sidecar reader when validating fixture
inputs. Native preflight must verify the host architecture, configured libvirt connection,
usable KVM and the requested guest/accelerator capability; TCG preflight must request the
foreign emulator explicitly and cannot satisfy a native lane. Capacity checks use the
selected storage filesystem and available memory, with explicit CPU/RAM/disk budgets.

The initial plan uses Ubuntu for fixture building and Fedora as the separate-provider
candidate. Rocky and SUSE retain measured inventory for their later installation scenarios.
Each build is bounded to one active build per host, at most 8 vCPU, 16 GiB guest RAM and
64 GiB scratch; preflight requires available headroom before starting. These are preparation
budgets, not new product capacity promises or qualification of minimum supported resources.

Fixture identity includes catalog selection/source identity, builder revision, customized
rootfs, extracted kernel, matching debuginfo and retained provenance digests. The staging
manifest remains private working evidence; publish selected safe identities and outcomes.
A changed output or mutable package input creates a different fixture identity and requires
requalification. No bit-for-bit cold rebuild claim is made without pinned package snapshots.
A fresh staging workspace and the existing force-refresh path exercise source acquisition
and customization again. Warm reuse must verify the recorded evidence before emitting wiring.

Failures retain an honest blocked/failed result and diagnostic references. They do not
become unsupported cells. Cleanup may remove only resources created under this task's
recorded ownership; a pre-existing store or domain is never an implicit cleanup target.

### Fixture record

Replace the internal key/value `MANIFEST` with one versioned JSON manifest. No other production
reader uses the old fields. The manifest records selected catalog row, builder commit, kernel NVR
and build ID, and SHA-256 values for the rootfs, kernel, debuginfo, provenance sidecar and captured
kernel config. Its fixture ID hashes the canonical record excluding that ID itself. A warm read
recomputes current inputs, checks the manifest shape and ID, and rehashes the fixed artifact set.
Legacy or incomplete records are stale and rebuild; a failed rebuild preserves the current set.
The TCG staging path writes the same record using its actual extracted kernel release as NVR.

Use the existing catalog resolver and sidecar reader; require a checksum-pinned cloud source,
nonempty recorded package versions, matching source provenance and the config sibling. An
unpinned virt-builder template can still use `build-fs`, but cannot enter this reproducible store.
The builder must be a clean Git checkout with a full commit ID. No source archive without a
verifiable revision or dirty tree can become qualified fixture input. The selected Python must
import KDIVE from that same checkout; reject a stale editable install before building.
Staging consumers still receive the same three `KDIVE_LIVE_VM_*` wiring lines. Tests inject only Git/tool boundaries.

### Lane readiness and ownership

Add declared preflight families `native-x86`, `native-power`, `tcg-host`, and `capacity`.
A small Python helper reuses the existing safe libvirt capability parser. Native checks require
matching local host architecture, guest KVM capability and a working KVM API; POWER additionally
opens an empty KVM VM with the explicit PPC-HV type and immediately closes it (no vCPUs,
guest memory or libvirt domain); use the architecture-specific Linux ioctl encoding.
The foreign lane requires an x86 host and ppc64le TCG capability plus an executable foreign emulator. The configured URI must be local; remote
provider candidates run the check on that host through the private inventory.

Capacity checks accept an existing task workspace and positive CPU, available-RAM and free-disk
thresholds, defaulting to 8 CPUs, 16384 MiB and 48 GiB free. The free-space admission floor
is separate from the approved 64 GiB scratch ceiling; it allows several copies of the selected
6 GiB image plus source/kernel/debuginfo work. Measure actual peak use during the cold build.
These are point-in-time checks, not reservations. The operator's exclusive task assignment and sequential build execution
own concurrency. The actual customization guest is already fixed at 2 vCPU/2048 MiB; libguestfs
gets an explicit 2048 MiB appliance bound. Use a task-owned 64 GiB filesystem for scratch so
exhaustion fails within that reservation; do not present free-space observation as a hard cap.
Place TMPDIR and libguestfs temporary/cache directories there, and remove extracted-kernel
temporaries on both success and failure. Retaining a current set can reduce free space below
the admission floor; stop then rather than exceeding the reservation.
No runtime scheduler, resource-admission API or automated host locking service is added.

Record the actual builder checkout revision and no deployed runtime roles when staging has no
server/worker/reconciler. Later scenario owners must independently verify their deployed roles.
Run one cold native x86 stage followed by warm reuse; verify artifact identity and no new
customization domains after completion. Record native POWER hardware unavailable/pending;
foreign readiness cannot substitute for its native proof.

## Alternatives

- Extend the existing entry points (preferred): reuse source verification, customization,
  locking, atomic store publication and preflight dispatch already exercised by tests.
- Documentation only: cannot prevent stale fixture reuse or false native-lane selection.
- A new lab orchestration tool: duplicates staging/provisioning ownership and adds a scheduler
  outside the approved preparation scope.

## Failure model

- Actors and deployments: trusted operators and trusted CI on explicitly assigned Linux
  x86_64/ppc64le lab capacity; private inventory is operator-controlled.
- Invariants and assets: accurate accelerator/fixture identity, bounded resource use,
  preservation of unrelated host state, and no private target data in public evidence.
- Accepted failure classes: unavailable repositories, missing tools/capacity and unavailable
  POWER hardware stop preparation with a recorded blocked outcome; they are not success.
  Mutable repositories can change rebuilt bytes; the new identity needs requalification.
- Covered elsewhere: runtime provider defects remain with their existing issues; full VM
  lifecycle and installation qualification remain with the charter's downstream owners.

## Threat model

- Boundaries: operator parameters enter preflight/staging; catalog and provenance files enter
  identity validation; public evidence is derived from private lab observations.
- Actors: the operator and reviewed repository are trusted; fetched image/package bytes and
  malformed local evidence cannot be assumed valid. Multi-tenant lab scheduling is excluded.
- Controls: reuse catalog checksum verification, explicit architecture/accelerator checks,
  positive bounded budgets, digest verification and fail-closed provenance checks. Keep command
  arguments separated and shell values quoted. Review public artifacts for target identifiers.
- Out of scope: compromised root on a selected host and distro signing infrastructure;
  existing host policy and source-verification owners retain those responsibilities.

## Success and validation

C1–C2: A sanitized measured inventory assigns or blocks each preparation lane, names budget
units and ownership, and distinguishes source-host readiness from deployed runtime proof.
C3: Focused tests reject wrong image/input identity, missing or corrupted provenance, and
changed artifact bytes; an unchanged complete set reuses without rebuilding. Existing source
checksum tests remain the acquisition proof. Capture actual fixture digests after staging.
C4: Focused preflight tests reject wrong host architecture, wrong accelerator, unavailable KVM,
missing foreign emulator, and insufficient capacity. Exercise a real x86 cold build and warm
reuse, retain POWER input selection, and explicitly mark unavailable native POWER pending.
C5: Report preparation commands, candidate identities, durations, actual results, owned
resources and cleanup. Run relevant lint/type/script/image tests, independent design and branch
review, the installed pre-push gate, and CI. A blocked required preparation is reported as such.
