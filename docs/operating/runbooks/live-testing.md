# Runbook: running the live test tiers

The canonical answer to "how do I run each live test tier, and what does it
need?" KDIVE has three live-test tiers — one per `just` recipe — at different
maturity and hardware levels, and the `live_vm` tier further spans four
families (below). Their environment quirks — session vs system libvirt, a short
session-mode socket path (`XDG_CONFIG_HOME`), modular daemons, per-mode guest
confinement — used to live only in one test file and in maintainer memory, so
every new live test re-derived them. This page is the single place that records
them.

**Design of record:** the spec
[`2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
and ADR-0386 (runner topology), ADR-0353 (the `live_vm_tcg` tier), and ADR-0387
(self-hosted host codification). This runbook summarizes and points into that
design; it does not restate it.

## The three tiers at a glance

The pytest markers are declared in `pyproject.toml`
(`[tool.pytest.ini_options].markers`) and gate two distinct test *vehicles* — a
tier is not a single harness.

| Marker | `just` recipe | Vehicle | What it drives | Accel | Where it runs |
| --- | --- | --- | --- | --- | --- |
| `live_stack` | `just test-live-stack` | live-stack spine over MCP/HTTP | the running stack, end to end | n/a | any host with the stack + compose backends |
| `live_vm` (native) | `just test-live` | direct provider ops / a provisioned System | boot a real kernel, crash it, introspect | KVM | self-hosted, arch-labeled KVM host |
| `live_vm_tcg` | `just test-live-tcg` | live-stack spine (ADR-0353) | the ppc64le provision→boot→crash→retrieve proofs, emulated | TCG | hosted `ubuntu-latest` (compose + S3, no `/dev/kvm`) |

`just test` (the default PR suite) selects `-m "not live_vm and not
live_stack"`, so none of the tiers below run in the ordinary gate. Each tier
*gate* **skips cleanly** when its environment is absent — but a tier whose env is
set *wrong* fails loud rather than skipping, and in the native `live_vm` tier
`KDIVE_GUEST_IMAGE` fails loud even when unset (see
[Skip vs. fail](#skip-vs-fail-a-skip-must-not-look-like-a-pass)).
Above the gates, `just test-live-tcg` makes the recipe itself red when no proof
passed, so an all-skip tcg run cannot read as green (#2517).

The `live_vm` tier spans four families (below); `just test-live` runs all of them
(each gated), and one — the remote-libvirt family, which drives a remote
`qemu+tls://` host rather than local silicon — additionally has a focused
`just test-live-remote` recipe (`-m live_vm_remote`) for driving it on its own —
see the `live_vm_remote` section below.

`live_vm_tcg` is deliberately **not** a throwaway-domain test: by ADR-0353 every
`live_vm_tcg` proof also carries the `live_stack` marker and runs the spine over
the stack. It needs no `/dev/kvm`, but it does need a full stack bring-up.

## The `live_vm` marker spans four families

`pytest -m live_vm` selects every family below. A run that exports only one
family's env would silently skip the others and still report green — the "green
run that is no coverage" failure this framework exists to kill. So each family
has its own `require_live_vm_*` gate that fails loud on a mis-set env. Three
additive sub-markers exist — `live_vm_throwaway`, `live_vm_provisioned`, and
`live_vm_remote` — and every test keeps the bare `live_vm` marker alongside its
sub-marker. The gdbstub debug tests are **not** a fourth sub-marker: both the
preserve-crash and stepping proofs reuse `live_vm_throwaway`. Their
`KDIVE_LIVE_VM_BZIMAGE` / `KDIVE_LIVE_VM_VMLINUX` inputs and corresponding
gates distinguish them from ordinary throwaway tests; stepping also consumes
`KDIVE_LIVE_VM_ROOTFS`.

| Family (sub-marker) | Required env | Default libvirt mode | Served by |
| --- | --- | --- | --- |
| Throwaway (`live_vm_throwaway`) | `KDIVE_LIVE_VM_ROOTFS` (a bootable rootfs qcow2) | `qemu:///system` (per-test; some tests force `qemu:///session`) | `boot_throwaway_domain` (`kdive.testing.live_vm`) |
| gdbstub debug (`live_vm_throwaway`, shared) | `KDIVE_LIVE_VM_BZIMAGE` + matching `KDIVE_LIVE_VM_VMLINUX`; the stepping proof also needs `KDIVE_LIVE_VM_ROOTFS` | `qemu:///session` | `boot_gdbstub_domain` (`kdive.testing.live_vm`); the caller renders the domain XML (ADR-0392) |
| Provisioned (`live_vm_provisioned`) | `KDIVE_LIVE_VM_SYSTEM_ID` + `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` + `KDIVE_GUEST_IMAGE` (the rootfs `mint-system.sh` stages, #2518) | `qemu:///system` | an externally provisioned System through the live stack |
| Remote (`live_vm_remote`) | `KDIVE_LIVE_VM_REMOTE_URI`, `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE`, `KDIVE_LIVE_VM_REMOTE_SSH`, `KDIVE_LIVE_VM_REMOTE_KERNEL`, `KDIVE_LIVE_VM_REMOTE_INITRD`, `KDIVE_LIVE_VM_REMOTE_ROOT_DEVICE`, `KDIVE_LIVE_VM_REMOTE_GDB_ADDR`, `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET`, and `KDIVE_LIVE_VM_REMOTE_RECONCILER` | `qemu+tls://` (operator-named; no default host) | direct provider ops against a genuinely remote libvirt host (ADR-0425) |

The env reads live in `tests/live_vm/__init__.py` (kept out of `src/` so the
ADR-0087 config-env guard is not tripped by test-only vars). That module also
exposes the `require_live_vm_throwaway` / `require_live_vm_bzimage` /
`require_live_vm_vmlinux` / `require_live_vm_provisioned` /
`require_live_vm_remote` gates — the `live_vm`
analogue of the `require_issuer` / `require_stack` / `require_guest_arch` gates
the stack tiers use.

`KDIVE_GUEST_IMAGE` is the exception in that table: it is read by the proof
itself and asserted by `preflight-env.sh`'s `provisioned` family, not by
`require_live_vm_provisioned`. Its consumer carries the bare `live_vm` marker,
so `pytest -m live_vm_provisioned` deselects it — run the whole tier, the way
`just test-live` and the native CI job do, or that proof silently drops out of
the selection before its gate can fire (#2518).

The **remote** family is the fourth (#1424, epic #1423): the only `live_vm`
family that drives a genuinely remote `qemu+tls://` host the worker shares no
filesystem with, so remote-provider capabilities get a direct provider-op proof
instead of being asserted only through the operator-run `live_stack` spine
(`test_remote_live_stack.py`). Its contract is wider than a URI because two
dependents are otherwise unprovable: the two-phase vmcore retrieve flows through
a **guest-routable** object store (`KDIVE_S3_*`, ADR-0084/ADR-0110), and remote's
console collector is **reconciler-resident** (ADR-0095/ADR-0235), so a
console-dependent proof needs one alive — hence the `KDIVE_LIVE_VM_REMOTE_RECONCILER`
presence marker. The trigger is the `qemu+tls://` URI itself (there is no default
remote host, so `KDIVE_LIBVIRT_URI` is not the lever here); a non-TLS URI or one
carrying `no_verify` **fails loud**, because remote mandates verified mutual TLS
(ADR-0076; the [remote live-stack runbook](remote-live-stack.md) forbids `no_verify`).

## The environment contract

The contract is the seam between "the runner host" and "the tests" — the thing
whose absence forces relearning. `KDIVE_LIBVIRT_URI` is the operator escape
hatch across every family; the resolved URI is the single value a test threads
into `boot_throwaway_domain(mode=…)`.

- **libvirt URI / mode is a per-test contract variable, not one global pin.**
  The throwaway family itself splits: a capture-traffic test needs
  `qemu:///session` (unprivileged, dodges the `qemu:///system` root-readback
  wall) while a snapshot test uses `qemu:///system` (the product default). The
  harness carries each test's mode rather than forcing one.
- **Environment variables** are read in one place per family (the table above).
  The provisioned-family fixture gate checks endpoint and bucket presence, not authentication.
  The object-store client uses the standard boto3 credential chain, including `AWS_*` variables;
  `KDIVE_SECRETS_ROOT` belongs to a separate secret backend. Use the [live-stack environment](live-stack.md).
- **The session-mode QMP socket path is length-limited (108 bytes), and the
  lever is `XDG_CONFIG_HOME`.** Session-mode libvirt derives each domain's QMP
  monitor socket under `$XDG_CONFIG_HOME`, so a deep pytest tmp path overflows
  it. The harness redirects `XDG_CONFIG_HOME` to a short `/tmp/kdive-cl-<hex>`
  path for the duration of a session-mode boot and restores it in teardown
  (`prepare_session_runtime`) — a test that boots through the harness need not
  manage it. (Separately, the self-hosted runner keeps `XDG_RUNTIME_DIR` short
  for the session libvirt daemon's own socket; see its runbook.)
- **Daemon layout follows the host distro.** The supported Debian/Ubuntu lifecycle host
  uses monolithic `libvirtd`; Red Hat-family hosts use modular `virtqemud`. Use the
  endpoint published by host provisioning rather than assuming either layout.
- **Guest confinement is named per environment.** On SELinux hosts, staged images
  use `virt_image_t` in system mode and `svirt_image_t` in session mode. SELinux
  session guests run as `svirt_t` with per-domain MCS categories, but the
  unprivileged daemon performs no image relabel
  ([ADR-0640](../../adr/0640-static-svirt-image-label-for-session-mode-domains.md)).
  On the supported Ubuntu AppArmor session deployment, the daemon's enforcing
  profile does **not** confine its QEMU guests: capabilities report `none` and
  guests run `unconfined`. The system endpoint advertises AppArmor capability;
  that observation is not a system-mode guest proof or permission to switch
  endpoints. [ADR-0741](../../adr/0741-apparmor-session-confinement-posture.md)
  accepts this documented session limitation without changing daemon ownership.
  The required host-install confinement assertion still fails for unconfined
  guests; this posture does not qualify the Debian cell or release 0.5.0.
- **Guest image and matching debuginfo** are staged at a known location and kept
  warm between runs on the self-hosted host.

### Skip vs. fail: a skip must not look like a pass

The `require_live_vm_*` gates distinguish three states, so a mis-provisioned
runner cannot masquerade as "no environment":

- **required env unset** → the gate **skips** (this host simply isn't set up for
  the tier);
- **env set but wrong** (rootfs file missing, staging dir not writable, partial
  `KDIVE_S3_*`) → the gate **fails loud**;
- **env present and valid** → the gate returns the resolved contract.

One prerequisite sits outside that tri-state, **in the native `live_vm` tier
only**: there `KDIVE_GUEST_IMAGE` fails loud when unset, not just when wrong
(#2518). `scripts/live-stack/env.sh` exports no default for it, so "unset" is the
*normal* state on a correctly provisioned host, and that tier has no `<N> passed`
summary gate of the kind the hosted tcg spine carries
(`.github/workflows/live.yml`). A skip there was therefore indistinguishable from
a proof that ran — the silent green #2497 exists to close. The gate is
`_preflight` in `tests/integration/test_console_parts_live.py`, and it reverses
ADR-0035 §4's "a missing fixture is a clear, actionable skip" for this one
prerequisite. Set it from `python -m kdive build-fs`'s printed `export` line, or
source `examples/local-libvirt/env.sh`.

The `live_stack` tier still **skips** on the same variable
(`tests/integration/test_live_stack.py`); #2497 tracks the tier-by-tier state.

## Running each tier

### Installed local authority carrier

Issue #2151's local x86_64 carrier is selected by the `live_vm` marker but remains dormant unless
`KDIVE_LIVE_VM_LOCAL_AUTHORITY_CONFIG` names an owner-only mode-`0400` or mode-`0600` JSON file.
The closed file contains `installed_revision` (the exact 40-character SHA), `system_id` (a
pre-provisioned disposable System UUID), its `project`, `ownership_prefix`
(`kdive-2151-<sha12>-<nonce8>`), and the literal
`kdive-external-boot-authority.service` service name. `barrier_socket` is optional and belongs only
to the separate deterministic fault arms. `fixture_mode` is optional: `create` (the default) or
`verify-existing`, as [fixture mode and System reuse](#fixture-mode-and-system-reuse) describes.
It contains no credentials; the active fixed worker owns authentication.

Prepare `system_id` as a disposable local-libvirt System before running the carrier:

`scripts/live-vm/mint-system.sh` computes the SHA-256 of the staged rootfs and includes the pin
in its provisioning profile. Set `KDIVE_LIVE_VM_ROOTFS` to the matching registered image's bytes
and `KDIVE_PROJECT` to the System's project. Admission binds provenance only from a registered,
verified image visible to that project; a caller's access to other projects does not qualify it.
An unmatched image remains usable for ordinary provisioned fixtures, but does not qualify for
this authority carrier. Keep the persisted-row check below when using the mint script.

The script stages that image at the fixed path
`${KDIVE_ROOTFS_DIR:-/var/lib/kdive/rootfs}/live-vm-provisioned-rootfs.qcow2`, replacing whatever
is there. A run with an unmatched image therefore overwrites the fixture bytes the carrier
requires, and the carrier fails admission until you restage the matching registered image's bytes
at that path and re-verify the SHA-256.

1. Select a registered `local-libvirt` catalog image visible to the System's project, with
   `arch=x86_64`, a digest, and an inspected `provenance.root_spec` (see
   [image staging](../../../examples/local-libvirt/README.md#optional-inventory-systemstoml)).
   Copy that image's qcow2 bytes to the fixed worker input
   `/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2`. Use a standalone qcow2 of at most
   16 GiB, with no backing file or external data file; the fixture rejects other paths and formats.
2. Verify the copy's SHA-256 equals the catalog digest. Provision the System with
   `provider.local-libvirt.rootfs` set to
   `{kind: "local", path: "/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2",
   sha256: "sha256:<catalog digest hex>"}`. The pin must describe those bytes and match that
   visible catalog row; an unpinned local rootfs does not bind root provenance.
3. Confirm `system_root_provenance` has a row for the new System before using its UUID in the
   carrier config. A missing row makes the authority-routed install fail with
   `root_provenance_missing`; stage the verified image and provision a new disposable System.

**Carrier DSN.** The carrier reads authority-private tables for its evidence, for example
`external_boot_release_cleanup_receipts`. The migrations revoke these tables from `PUBLIC` and
give no read grant to `kdive_server`. Thus the server DSN that the other live tiers use cannot
read them. Set `KDIVE_DATABASE_URL` to the least-privilege harness login, which is
`KDIVE_HARNESS_DATABASE_URL` in the
[live-stack DSN table](live-stack.md#2-review-the-host-process-env). The role bootstrap creates
that login and grants it `SELECT` on every table (ADR-0769). Before it provisions the
fixture, the carrier checks that its DSN can `SELECT` each table that it reads. If the DSN
cannot, the carrier fails before any mutation. The error names each table and the required DSN.

Run only the focused carrier after provisioning and backend bring-up:

```sh
KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" \
KDIVE_LIVE_VM_LOCAL_AUTHORITY_CONFIG=/protected/local-authority-carrier.json \
  uv run python -m pytest tests/live_vm/test_installed_local_authority.py -q
```

An unset trigger skips. Any configured mismatch fails before mutation: the carrier requires the
authority disk revision and the `/readyz` build revision from the server and every active fixed
worker slot to resolve as commits exactly equal to `installed_revision` (abbreviated reports are
resolved against the checkout; unknown, ambiguous, and ref-name reports fail). After its disposable
fixture is re-provisioned, the carrier runs non-writing `O_RDWR` open attempts as every active fixed
worker and the invoking control identity against that fixture's exact private overlay and console.
It also requires both identities to fail unlink and replacement attempts against invocation-owned
sentinels in those same private parents; only the authority identity creates and removes those exact
sentinels. The normal carrier then opens a uniquely titled
Investigation, creates a labeled Run on the operator-provided disposable System, uploads the
kernel through the public artifact contract, and drains the real install, activate, and root
release jobs with `jobs.wait`. The root release owns its derived recover and cleanup phases; the
carrier requires their terminal journal/receipt evidence, receipt consumption, and one reservation
credit before it closes that exact Investigation. The provider and database retain their ordinary
audit/history records; do not run a prefix-wide or host-wide reaper.

Set `live_vm_host_authority_fault_proof_enabled: true` only for a disposable native proof host to
install the disabled-by-default authority-owned socket at
`/run/kdive/provider-authority/proof-control/control.sock`. Put that exact absolute path in the
carrier's optional `barrier_socket`. Root may then send only bounded `arm`, `status`, `release`
and `abort` requests. An arm binds the configured System and exact Run, one existing operation, and
the fixed `before-provider` or `after-provider` checkpoint, or `before-finalize` for the `cleanup`
operation only ([ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md)). `abort` is
accepted only while that `before-finalize` arm is reached: it fails that one cleanup finalization,
which the authority then publishes as a real recovery quarantine. It is root-only by
`SO_PEERCRED`, permits one arm, and never selects or executes a provider action. Releasing an arm
that was never reached clears it. Shutdown aborts a wait rather than releasing it, and a shutdown at
`before-finalize` publishes nothing. The fault carrier uses `after-provider` to restart the authority before it can record
`provider-returned`, then requires the real job's ordinary recovery path to finish.

The barrier does not support journal deletion or synthetic stale database writes. Those #2151
arms remain unavailable and fail loud until a separately authorized, deterministic mechanism can
exercise their existing fences; the carrier must not represent them as acceptance from timing luck
or fake ports. Helper tests under `test_installed_local_authority_support.py` prove only input and
cleanup orchestration; they are not native acceptance. Recover, resolve-conflict, and teardown
remain separate state/fault arms.

Before opening the carrier Run, the native test re-provisions only the configured disposable
System on the fixed private authority daemon. The setup reads that exact System's durable
provisioning profile, removes only its UUID-derived legacy domain/overlay/baseline/console fixture,
drops to the authority uid, and recreates the same domain and artifacts. It then verifies the
overlay and console owner and the live private-daemon domain before any public install or boot
request. The configured System must be disposable; this setup is not a migration mechanism for
ordinary worker-owned Systems.

**Fixture cleanup.** In `create` mode the fixture script reports the six exact names it made for
the configured System, and the carrier records each one in its resource ledger:

- `/var/lib/kdive/provider-authority/rootfs/<uuid>-fixture-base.qcow2`
- `/var/lib/kdive/provider-authority/rootfs/<uuid>-baseline`
- `/var/lib/kdive/provider-authority/rootfs/<uuid>-overlay.qcow2`
- `/var/lib/kdive/provider-authority/console/<uuid>.log`
- `/var/lib/kdive/provider-authority/console/<uuid>.boot` (the per-start boot marker, #3254)
- the domain `kdive-<uuid>` on the private authority daemon

When the rest of the run ends, whether it passed or failed and after the Investigation close
where the carrier makes one, the carrier removes those names newest first with `provision-authority-fixture.py --remove <uuid> <name>`. The
domain goes first. The script accepts only a name from that set, and it refuses a symlinked
directory, a hard-linked file, or a parent that is not the authority-owned mode-`0700`
directory. It stops at the first failed name, so a domain that did not go away keeps its files.
`verify-existing` records and removes nothing. The authority journal lane
`/var/lib/kdive/provider-authority/journal/<uuid>.jsonl` stays: the authority starts only when
every lane matches its database head, so the lane is an audit record, not a fixture artifact.

Removal after a failed or interrupted run does not wait for the System's in-flight jobs, so
list the authority daemon's domains and the two authority directories afterwards. Remove the
fixture by hand only after a failed removal, a `create` that failed part way, or a name that
came back. Run the same exact-name mode, domain first:

```sh
uuid=<system_id>
for name in "kdive-$uuid" \
  "/var/lib/kdive/provider-authority/console/$uuid.boot" \
  "/var/lib/kdive/provider-authority/console/$uuid.log" \
  "/var/lib/kdive/provider-authority/rootfs/$uuid-overlay.qcow2" \
  "/var/lib/kdive/provider-authority/rootfs/$uuid-baseline" \
  "/var/lib/kdive/provider-authority/rootfs/$uuid-fixture-base.qcow2"; do
  sudo -n /opt/kdive-provider-authority/.venv/bin/python \
    scripts/live-vm/provision-authority-fixture.py --remove "$uuid" "$name"
done
```

An absent name is success, so a repeated run is safe.

The carrier's order is: release the external-boot Run, close the Investigation, then remove the
fixture. The System row stays `ready` after that. Then end the disposable System in this order:

1. `systems.teardown` for the System with an `admin` token for its project, and wait for its
   job. The fixture is already gone, so the teardown finds no domain.
2. `allocations.release` for its allocation.

This order is valid whether or not `allocations.release` refuses while a non-terminal System on
the allocation has external-boot history (#2966): the teardown always runs first.

#### Fixture mode and System reuse

`fixture_mode` selects how the carrier gets the authority fixture of the configured System:

- `create` (the default): the fixture script refuses when the System's domain still exists on
  the private authority daemon. Otherwise it removes the System's worker domain and old fixture
  files, provisions a new fixture on the private authority daemon, and reports its six names.
  The carrier records them and removes them when the run ends
  ([fixture cleanup](#installed-local-authority-carrier)).
- `verify-existing`: the carrier only verifies, read-only, a fixture that is already present, and
  it records and removes nothing. A `create` run removes its fixture at the end, so use this mode
  only after you provision the fixture by hand with
  `sudo -n /opt/kdive-provider-authority/.venv/bin/python scripts/live-vm/provision-authority-fixture.py <uuid>`
  (the System's provisioning profile JSON on stdin). Remove that fixture by hand after the run
  with the exact-name loop above.

A System can carry more than one carrier run (ADR-0713). After a run whose root release finished
(the activation is `recovered` with `cleanup_complete`), the next run on the same System, in
either mode, opens a new activation on the same authority journal lane. A `create` run after a
`verify-existing` run needs the hand-provisioned fixture removed first. Do not tear down the
System between those runs; end it only after the last run. A System whose activation did not
finish its release stays restricted, and `runs.boot` refuses a new activation on it
(`external_boot_restricted`).


### Installed local authority carrier — ppc64le (#2152)

Issue #2152's native ppc64le carrier is selected by the `live_vm` marker but remains dormant
unless `KDIVE_LIVE_VM_POWER_AUTHORITY_CONFIG` names an owner-only mode-`0400` or mode-`0600`
JSON file. The config file has the same shape as the x86_64 carrier's `KDIVE_LIVE_VM_LOCAL_AUTHORITY_CONFIG`
file: `installed_revision` (the exact 40-character SHA), `system_id` (a pre-provisioned
disposable System UUID on the POWER host), `project`, `ownership_prefix`
(`kdive-2151-<sha12>-<nonce8>`), and the literal `kdive-external-boot-authority.service`
service name. `barrier_socket` and `fixture_mode` are optional. The
[fixture mode and System reuse](#fixture-mode-and-system-reuse) rules apply to this carrier too.

Prepare the POWER host's disposable System from a registered, project-visible `local-libvirt`
catalog image with `arch=ppc64le`, a digest, and an inspected `provenance.root_spec`. Copy its
qcow2 bytes to `/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2` and verify that file's
SHA-256 matches the catalog digest. Use a standalone qcow2 of at most 16 GiB, with no backing
file or external data file. Provision with `provider.local-libvirt.rootfs` set to
`{kind: "local", path: "/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2",
sha256: "sha256:<catalog digest hex>"}`; the fixture rejects another path, and an unpinned rootfs
does not bind root provenance. Confirm `system_root_provenance` has a row for the new System
before putting its UUID in the carrier config. If the row is absent, stage the verified image
and provision a new disposable System; otherwise authority-routed install fails with
`root_provenance_missing`.

**Machine-checkable carrier gate.** Before reading the config, the test asserts:

1. `platform.machine() == "ppc64le"` — the test skips on any other host (it is not an
   error to run the suite on an x86_64 host where this carrier is unconfigured).
2. `/dev/kvm` is present — the test **fails loud** if the host is ppc64le but `/dev/kvm`
   is absent. A ppc64le host without KVM-HV is a mis-provisioned runner; TCG is not
   an accepted substitute for native ppc64le authority proof (#2152).

Run only the focused carrier after provisioning and backend bring-up on the POWER host:

```sh
KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" \
KDIVE_LIVE_VM_POWER_AUTHORITY_CONFIG=/protected/power-authority-carrier.json \
  uv run python -m pytest tests/live_vm/test_installed_local_authority_ppc64le.py -q
```

This carrier also needs the harness DSN in `KDIVE_DATABASE_URL`, and it does the same
DSN check before any mutation. See the **Carrier DSN** paragraph in
[Installed local authority carrier](#installed-local-authority-carrier).

The proof is structurally identical to the x86_64 carrier's normal-operations arm: it opens
an Investigation, creates a labeled Run on the disposable System with `arch=ppc64le` in the
build profile, uploads the kernel through the public artifact contract, and drains the real
install, activate, and root release jobs. Confinement, revision-coherence, and artifact-ownership
checks all apply identically to the x86_64 carrier, and so does the
[fixture cleanup](#installed-local-authority-carrier) contract. The fault arms (`barrier_socket`,
restart-recovery, takeover, journal-loss, stale-write) are not yet implemented for the ppc64le
carrier; they remain separate scope.

Bring up the stack on the POWER host following the
[POWER host integration guide](../../development/cross-platform.md#native-power-host-integration)
and the [live-stack runbook](live-stack.md) before running this carrier.
The mock-OIDC image selection note in the cross-platform guide covers the POWER OIDC image
requirement; `qemu:///system` and root-process constraints also apply.


### Separate installed authority lanes

For #3183/#3152, assign an exclusive Ubuntu 26.04 x86_64 local-authority host and a
separate Fedora 44 x86_64 remote-authority target. Keep the ordinary remote-provider lane on
its existing assignment: adding an authority tuple changes install/boot routing. These lanes
establish deployment prerequisites for #3184–#3186; readiness does not qualify their HTTP cells.
Native POWER qualification remains pending under #2818, and #2803 coverage is unchanged.

Before either host changes, retain a private observation of current ownership, snapshot level,
running consumers and jobs, nested KVM usability, confinement mode, available CPU/RAM/disk and
exclusive availability. A snapshot called `kernel-src` proves neither an installed authority nor
a candidate revision. Budget one live scenario at a time from observed headroom; do not overlap
kernel builds. Record the separate ordinary-provider assignment as well.

Review the exact restore and installation plans before apply. Installation approval is separate
from permission to restore a snapshot. The installation plan names:

- the clean candidate's full commit SHA, target assignment and exact provisioning invocation;
- protected input **references**, their owners/modes and certificate lifetime, authority modes
  and routes, capacity bytes and concurrency, plus manifest/base SHA-256 digests and provenance;
- backend/schema prerequisites, the database LOGIN's existing role membership, S3 access scope,
  and the readiness, denial, deployed-identity and cleanup observations to run;
- retained installation state versus proof-owned transient state, failure handling and the
  exact-owned retirement or separately approved snapshot restore procedure.

Keep addresses, accounts, inventory paths, credential locations and raw evidence private. Public
results use anonymous lane labels and the candidate SHA. Do not publish credential values or
put them in command-line extra vars. `--check` is not an approval bypass: some existing preflight
commands deliberately run in check mode.

#### Local composition without runner registration

Use [installed-local-authority.yml](../../../deploy/ansible/playbooks/installed-local-authority.yml).
It targets the existing `live_vm_runners` inventory group, reuses `libvirt_stack`,
`libvirt_pool_net` and `live_vm_host`, and registers no GitHub runner. Set
`installed_authority_operator_user` explicitly; it maps to the host role's operator account.
Set `live_vm_repo_version` to the reviewed full 40-character SHA, not a branch or tag.

The protected vars must enable `live_vm_host_authority_enabled`,
`live_vm_host_worker_authority_enabled` and `live_vm_host_authority_local_mutation_enabled`,
and supply `live_vm_host_authority_system_manifest_source` with
`live_vm_host_authority_system_local_bases`. Existing preflight validates the canonical manifest
and the immutable base digests before host roles run. The authority DSN/server key/server
certificate/server CA/worker-client CA/health-client certificate/health-client key use the seven
existing `live_vm_host_authority_*_source` inputs. Add the worker-client certificate/key sources
and protected S3 shared-credentials source described in the
[authority input contract](self-hosted-kvm-runner.md#external-boot-authority-diagnosis).
Supply the lifecycle witness member DSN through the protected vars, too.

A custom inventory outside this directory must carry the existing
`inventory/group_vars/all.yml` and `inventory/group_vars/live_vm_runners.yml` settings it uses;
Ansible does not load another inventory's adjacent group vars. Override their example network
values, checkout/venv location and reservation concurrency with reviewed lane values. The
runner default reserves six recovery slots; select capacity from current disk headroom, not
that example. Account for the host PostgreSQL package installed by the authority role; the
[stack port overrides](live-stack.md) allow a separate backend port when needed.

From the clean candidate checkout, with shell variables naming the approved private inventory,
vars file and single inventory alias:

```bash
cd deploy/ansible
uv run --with 'ansible-core==2.21.1' ansible-playbook \
  playbooks/installed-local-authority.yml -i "$AUTHORITY_INVENTORY" \
  --limit "$LOCAL_AUTHORITY_LANE" --extra-vars "@$LOCAL_AUTHORITY_VARS"
```

The role installs the candidate, authority, fixed worker lifecycle and client route. Its existing
one-shot readiness probe, installed revision check and worker/reconciler path/socket denial
checks must pass. They prove authority artifact isolation. Ubuntu session guests lack
per-domain AppArmor confinement (ADR-0741); an enforcing daemon profile does not prove guest
sandboxing. Retain this limitation in the lane report.

#### Dedicated remote composition

Stage a clean checkout at the same candidate through the reviewed private provisioning workflow.
Provision Python 3.14 and uv on the target first through the existing host prerequisite workflow;
record their resolved paths in `provider_authority_host_python` and
`provider_authority_host_uv_bin` in the approved plan. The authority role validates these tools
but does not install them.
Use [site.yml](../../../deploy/ansible/site.yml) with the dedicated target in
`remote_libvirt_hosts`; never include the ordinary remote-provider assignment in this limit.
Set `provider_authority_host_source_root` to that checkout, enable the authority and its mutation
mode, and provide its existing seven protected credential-source inputs, S3 credential source,
canonical `provider_authority_host_system_manifest_source` and
`provider_authority_host_system_remote_bases`. The manifest binds the selected resource,
instance, immutable base, machine and fixed port ranges; validate it with the existing
`validate_authority_system_provisioning.py` through role preflight, not an ad hoc format.
An entry has no `network` field: an authority System's only NIC is its user-mode SSH forward
([ADR-0804](../../adr/0804-remote-authority-systems-have-only-the-forward-nic.md)), and the
preflight rejects a manifest that still names one. The entry's optional `guest_egress` (default
`false`) opts the guest into outbound NAT through that NIC. Before deploying this manifest shape
over an earlier one, confirm that `system-provisioning/remote/` under the authority's state
directory holds no `*.provision.json` intent; the authority refuses an intent of the old shape.

On a host where SELinux is enabled, the role labels each staged authority base `virt_content_t`
through an exact-path fcontext rule and `restorecon -F`. It restarts the authority when that
changes a label. The authority domain tells libvirt not to relabel the base, so the label must
stay in place: on an enforcing host a base relabelled out of band fails its next domain start
with a qemu permission error. Any relabel also changes the base's ctime, so the authority exits
and must be restarted ([ADR-0623 amendment](../../adr/0623-authority-owned-system-provisioning.md)).
Deploying this contract over an authority that already holds Systems strands them: a local
System's recorded domain identity no longer matches, and a stored remote provision intent no
longer loads. Deploy it only to an authority host with no live authority Systems and no stored
provision intents.

Remote System bases also require `provider_authority_host_remote_module_enabled: true` and
`provider_authority_host_remote_module_architectures` containing the selected supported
architectures. Enable `remote_libvirt_module_appliance_enabled` and supply the corresponding
pinned URL/SHA256 entries in `remote_libvirt_module_appliance_images` through the existing
[module-appliance build and installation contract](../../../deploy/remote_module_appliance/README.md).
The authority readiness check requires those installed assets and its private storage pool;
installation readiness does not qualify a module operation or System construction.

The production authority and installer use `/var/lib/kdive/remote-module-appliance/v1`,
outside the ordinary boot pool and the mutable authority home. Authority-enabled remote-module
deployments reject a different `remote_libvirt_module_appliance_install_dir` before host mutation.
Keep these assets root-owned and read-only; do not grant the authority ordinary pool group access
or change ordinary pool permissions to make them readable. The role runs the installed loader as
the authority before service start, then retains its post-start readiness probe. Root-only digest
verification cannot prove the authority can traverse the installation path.

For an upgrade from the former appliance location, review installation at the new location before
starting the new candidate; retain the old tree until separately approved retirement. There is no
fallback to the old path. Loader success does not establish QEMU access or guest confinement at
the new location: observe those through the separate System qualification, including SELinux on
the selected host. Do not relax host policy to substitute for that proof.

Review the authority network address/port and worker TLS route separately from ordinary libvirt
TLS. Use the existing [remote setup contract](remote-libvirt-host-setup.md) and
[remote provider configuration](../providers/remote-libvirt.md) for client credentials and the
resource's authority tuple. Keep TLS verification and SSH host-key checking enabled.

```bash
cd deploy/ansible
uv run --with 'ansible-core==2.21.1' ansible-playbook site.yml \
  -i "$AUTHORITY_INVENTORY" --limit "$REMOTE_AUTHORITY_LANE" \
  --extra-vars "@$REMOTE_AUTHORITY_VARS"
```

The remote role preflights before host mutation and runs its configured installed readiness
probe after firewall and service convergence. Its failure path withdraws failed readiness;
a failed play is not usable qualification evidence. Fixed fault controls remain opt-in only
on disposable lanes, as described below; they are not needed for routine readiness.

#### Candidate evidence and cleanup

After approved provisioning, use `scripts/live-stack/stack-services.sh` and `just onboard` on
the local host, with the reviewed backend and provider configuration. Use the host stack described
in the [live-stack runbook](live-stack.md), including its lifecycle witness member setup.

The role publishes the fixed-worker authority route in `/etc/kdive/live-worker-authority.env`, and
every worker start, including each `stack-services.sh` restart, forwards it. Do not export the
`KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_*` values: an export that contradicts the file is refused
(ADR-0770). A backend port variable may hold `ADDR:PORT`, for example a Postgres bound to the
lane address; `scripts/live-stack/env.sh` then builds the default DSNs, including
`KDIVE_HARNESS_DATABASE_URL`, from that address, so no hand-written DSN is needed. A loopback or
wildcard address keeps `localhost`. An unparseable value stops every script that sources
`env.sh`, `stack-down.sh` and `stack-status.sh` included, until it is corrected.
Record the candidate full SHA and clean tree, manifest and actual immutable-base byte digests,
and actual server, **each running worker**, reconciler and selected authority revisions.
Resolve process build identifiers to full SHAs as ADR-0715 requires; read the installed authority
revision at `/opt/kdive-provider-authority/revision` on the selected authority host. A missing,
unknown, mismatched or skipped identity observation fails required evidence. Repeat identity
observations when running a later carrier; the snapshot or this installation report is not a
substitute. Use strict stack skew policy and retain the visible revision header for HTTP proofs.

Keep a private per-lane result containing the exact command, exit code, readiness/denial
observations, terminal effects and cleanup readback. Routine readiness creates no disposable
System; the installation remains for downstream children. If the existing transient local
proof is explicitly enabled, verify its incarnation is terminated and its temporary account,
group and proof material are removed; a failed proof must retain a named cleanup obligation.
Do not run the whole `prove-external-boot-authority-host.sh` carrier over a retained lane: it
installs a predecessor and tears down its authority. Review any reuse of its disposable DSN
preparation separately, including database grants. Issue and renew the lane's TLS credentials as
described in [Renewing retained-lane TLS credentials](#renewing-retained-lane-tls-credentials).

Provisioning preflight (on the controller sources) and verification (on the installed copies)
refuse any authority or fixed-worker TLS certificate that is expired or expires within
`live_vm_host_authority_tls_expiry_margin_seconds` (default 43200, twelve hours), naming the
credential and its `notAfter`; the controller needs `openssl` for that check. Renew before
provisioning. The authority unit allows five starts of any kind (automatic, manual or
provisioning) within ten minutes; a sixth is refused and leaves the unit `failed`, stopping it if
it was running. A provisioning run spends one or two of those starts and one pass of the
installed-authority live proofs about three, so do not re-provision or rerun those proofs
repeatedly within that window without first running the reset below; after replacing credentials
on a failed unit run
`sudo systemctl reset-failed kdive-external-boot-authority.service` before restarting it.

Retire only explicitly owned proof objects after confirming terminal provider effects. Do not
remove shared backends, bases or journals to make a failure disappear. The existing local
`authority_host_teardown.yml` is an authority retirement operation requiring its own reviewed
inputs, not routine per-test cleanup; remote retirement follows its separately reviewed plan.
Preserve narrower blockers under their existing owners: #3138, #3144/#3148,
#3130/#3131/#3135. Exhausted-job recovery (#3143) is merged; report a recurrence as a new
issue. System framing, fault construction and HTTP qualification belong to
#3184–#3186. Do not publish the release from this procedure.

#### Renewing retained-lane TLS credentials

A retained lane's authority server, health client and worker client certificates come from
`scripts/operations/authority-pki.sh` and are installed by
`deploy/ansible/playbooks/renew-local-authority-tls.yml` (ADR-0803). Keep the lane's CA key and
leaf keys in one private directory on the control host that runs Ansible, for example
`~/.local/share/kdive/authority-pki/<lane>/`. The script refuses a directory that is not owned by
you with mode `0700`, is a symlink, or lies inside any Git work tree or `.git` directory, and it
never copies the CA key to a lane host. Point the lane's protected vars at that directory:
`live_vm_host_authority_server_key_source` and `..._server_certificate_source` at `server.key`
and `server.pem`; `..._server_ca_source`, `..._worker_client_ca_source` and
`live_vm_host_worker_authority_server_ca_source` (which defaults to the authority's server CA) at
`ca.pem`; the health client sources at `health-client.{pem,key}`; and
`live_vm_host_worker_authority_client_{certificate,key}_source` at `worker-client.{pem,key}`.
Renewal rewrites those files in place, so the vars file does not change. Back the directory up
privately: losing it forces a CA rotation.

Every lifetime is your choice, in whole days, at each issuance; the script has no default. It
refuses a leaf that would outlive the CA certificate, and a CA re-issue that existing leaves would
outlive. The recommended lifetimes are 30-day leaves under a 365-day CA. For a new lane, with
`D` the lane directory and `I` the lane's `live_vm_host_authority_instance`:

```bash
P=scripts/operations/authority-pki.sh
install -d -m 0700 "$D"
$P ca --dir "$D" --subject <lane>-authority-ca --days 365 --key new
$P leaf --dir "$D" --name server --usage server --days 30 --key new --authority-instance "$I"
$P leaf --dir "$D" --name health-client --usage client --days 30 --key new
$P leaf --dir "$D" --name worker-client --usage client --days 30 --key new
```

To renew, run the same `leaf` commands with `--key existing`, which keeps every key and issues new
certificates. When the CA certificate no longer covers a new 30-day leaf, first re-issue it from
the same key with `ca --key existing` and the same `--subject`. Keep the printed lines (file name,
`notAfter`, public-key hash) in the private lane result; they contain no secret.

A lane installed from a revision that predates this playbook must first be reprovisioned once
with `installed-local-authority.yml` at a revision that contains it. Then, from a clean checkout
at exactly the installed authority revision (the value of
`/opt/kdive-provider-authority/revision`, which must equal `live_vm_repo_version` in the vars):

```bash
cd deploy/ansible
uv run --with 'ansible-core==2.21.1' ansible-playbook \
  playbooks/renew-local-authority-tls.yml -i "$AUTHORITY_INVENTORY" \
  --limit "$LOCAL_AUTHORITY_LANE" --extra-vars "@$LOCAL_AUTHORITY_VARS"
```

Before changing the host it runs the provisioning preflight (so the lane's System manifest and
bases must still be present on the controller, and their digests are checked again), verifies the
server certificate against both server CAs with the authority's server name and both client
certificates against the worker-client CA, checks that each certificate matches its key, and
refuses a dirty or different checkout. It then installs the authority credentials through the
same `live_vm_host` task provisioning uses, runs `systemctl reset-failed` on
`kdive-external-boot-authority.service` (clearing a failed state and the start limit), and
restarts the authority exactly once, even when no file changed, so a rerun repairs an interrupted
run. The restart returns only after the authority's TLS self-check passes. Only then does it
install the fixed worker's three files, through the same tasks as provisioning. It finishes by
checking expiry of the six installed certificates, that the authority is active, the installed
chains, and a TLS handshake as each fixed-worker account that must verify the authority and see
it serving the installed server certificate.

The fixed workers are not restarted: a worker reads its authority certificate, key and CA on every
request, so it uses the renewed files from its next request. Never restart a fixed worker with
`systemctl` for a renewal; that can wedge its slot. The playbook does not perform a real
worker-to-authority request; the first authority-backed operation afterwards is that proof.

If a precondition fails, nothing on the host changed. If the authority fails to start, the
worker files are untouched and the authority is not ready; read
`journalctl -u kdive-external-boot-authority.service`, which names an expired or invalid
certificate, correct the source files, and rerun the playbook. Do not rerun without a
correction: each failed start counts against the unit's start limit.

A CA rotation replaces the CA key and every leaf key. Use it when the lane directory is lost or a
key may be exposed; there is no revocation. Move the old directory aside
(`mv "$D" "$D.retired-$(date +%F)"`), recreate `$D` with mode `0700`, issue everything with
`--key new` as for a new lane, and run the playbook. Between the authority restart and the worker
file installation, worker requests fail closed, so rotate only while no job is using the authority;
the playbook does not check this. Keep the retired directory until the rotated lane verifies, then
destroy it if a key was exposed, or archive it like any backup.

### Installed remote authority proof prerequisite

The production `provider_authority_host` role keeps the ADR-0622 proof socket absent by default.
On a disposable remote-authority proof host, add this explicit role input before running
`deploy/ansible/site.yml`:

```yaml
provider_authority_host_fault_proof_enabled: true
```

The role then projects only the fixed
`KDIVE_EXTERNAL_BOOT_AUTHORITY_PROOF_SOCKET=/run/kdive/provider-authority/proof-control/control.sock`
setting, installs that owner-only runtime directory through tmpfiles, and adds the directory to the
authority unit's writable paths. It does not grant a worker or operator identity access to the
private provider daemon or artifacts. The general provider-authority, private session-libvirt,
fixed-pool, selected-architecture appliance, and credential inputs remain required independently;
this option does not stage a base image or provision a System.

Test control configuration uses `KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG` to name an owner-owned
mode-`0400` or mode-`0600` JSON file. Its closed fields are `installed_revision`, `project`, the
single `resource_name`, matching `authority_instance`, `ownership_prefix`, one atomic `ssh_target`,
the literal `kdive-external-boot-authority.service`, and optional fixed `barrier_socket` above. It
contains no URI, pool, filesystem path, credential, base-image, or manifest selector. SSH relies on
the operator's preconfigured authentication and permits only batch-mode, fixed remote controls.

This installs and configures the proof prerequisite only. A collected remote six-operation carrier
is not implemented by this phase; do not report these helper-level checks as remote acceptance.

### `live_stack` — drive the running stack over HTTP

```
just stack-backends          # backends healthy, bucket verified, schema migrated
just test-live-stack   # runs -m live_stack; skips cleanly if the stack is absent
```

`just stack-backends` does not run the role bootstrap, so the runtime members and the harness login
(`KDIVE_HARNESS_DATABASE_URL`) do not exist yet after it. The full `scripts/live-stack/stack-services.sh`
bring-up runs the bootstrap. After a backends-only bring-up, run it yourself:
`env -u KDIVE_MIGRATION_DATABASE_URL docker compose run --rm --no-deps role-bootstrap`.

For a proof tied to a particular deployed revision, run the selected proof with
strict skew and a visible pytest header:

```bash
KDIVE_STACK_SKEW_POLICY=strict uv run python -m pytest -v -m live_stack \
    tests/integration/test_live_stack.py
```

The header lists the revision probed for each app process, or `unknown` when
none was reported. Keep `KDIVE_STACK_SKEW_POLICY=strict` set for the entire
revision-bound run. Strict mode skips any proof with an unknown or non-fresh
exercised process; a skipped proof is not a passing revision proof.
The `just test-live-stack` quiet recipe prints the same initial revision line.
The strict path checks both the header and test-admission probes. If a stack
becomes fresh only after pytest prints its header, restart the proof run so
the recorded revisions describe the run that passes.
Strict admission probes each test gate, including when the worker PID set is
unchanged, so a server or reconciler restart cannot reuse an earlier verdict.
Run revision-bound proofs serially, as in the commands above; the initial
report and admission snapshot are process-local under pytest workers.
An absent `lifecycle-witness` is shown as `not deployed` in the portable
three-role stack and does not block it. Check the header and passed/skipped
counts before recording proof against a PR head.

`just stack-backends` reuses the compose backends (Postgres + SeaweedFS + mock-OIDC) and
keeps the host `server`/`worker`/`reconciler` outside compose. Full bring-up,
including the host-process env block, is in the
[live-stack runbook](live-stack.md). To drive a genuinely remote
`qemu+tls://` libvirt host instead, use the
[remote live-stack runbook](remote-live-stack.md).

#### Finalization measurement (#2318)

One `live_stack` driver measures `runs.complete_build` rather than exercising a boot:
`tests/integration/test_finalization_measurement.py`. It records the phase attribution — queue
wait, archive scan, publication, object-store request count and bytes — that ADR-0656's
completion-contract decision rests on.

```
scripts/live-stack/stack-services.sh --skip-libvirt   # the Run is unbound: no System, no VM
KDIVE_KERNEL_SRC=/path/to/built/tree \
KDIVE_MEASUREMENT_STAGE_DIR=/path/on/real/disk \
KDIVE_MEASUREMENT_OUT=/path/to/rows.jsonl \
uv run python -m pytest tests/integration/test_finalization_measurement.py \
  -m live_stack -k x86_64 -q
```

Four things about it differ from the boot proofs:

- **It needs no libvirt and no System.** The Run is created unbound, so `--skip-libvirt`
  bring-up is enough and nothing is provisioned.
- **The staging directory matters.** `combined_kernel_tar` runs `make modules_install` beside
  the tar it cuts, and a distro-config tree with debug info stages ~7.6 GB to produce a ~2.5 GB
  bundle. `tmp_path` is rooted at the system temp directory, commonly tmpfs, and pytest keeps
  the last three runs — so set `KDIVE_MEASUREMENT_STAGE_DIR` to real disk for a large bundle.
- **A distro-config debug tree will not finalize as built.** That ~2.5 GB bundle is above the
  2 GiB accepted ceiling (`_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES`) and is rejected, and the
  distro default `CONFIG_KERNEL_ZSTD=y` is rejected separately by the defect filed as #2476. The
  tree has to be pruned and relinked with `CONFIG_KERNEL_GZIP=y` first; the exact steps are in
  [the proof record](../../design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md),
  under "## Reproducing".
- **Two timeouts, deliberately separate.** `KDIVE_MEASUREMENT_TIMEOUT_S` (default 1800) is what
  the driver runs under, so a finalization that would breach the budget is still recorded rather
  than truncated at it. The supported client budget it is measured *against* is 300 s — the
  read timeout the MCP SDK applies when `LiveStackClient.over_http` passes none — and that is a
  module constant, not a knob. The 30 s figure is the *connect* default and is not the request
  bound; `test_supported_budget_matches_the_shipped_client` reads the real one off a client.

The `ppc64le` arm runs the same code under `KDIVE_PPC64LE_BUNDLE`. Unset, it skips; set but
lacking `kernel.tar.gz`, it raises rather than skipping, because a measurement that silently
produces no row is indistinguishable from one nobody started. That arm is unrun and owned by
[debt record 0015](../../debt/0015-ppc64le-finalization-measurement-unrun.md).

#### Multi-client stress (#2769)

`scripts/live-stack/stress-allocations.py` drives a running stack from many concurrent MCP
clients at once: allocation churn, deliberate races, invalid arguments, grants the clients walk
away from, and optionally System provisioning. It checks invariants while the load runs and
after it, prints a report, and exits non-zero on a violation. It is an operator tool, not a
pytest tier. Design: [the spec](../../workflow/specs/2026-09-24-stress-allocations-design.md).

Bring the stack up with `stack-services.sh` (with or without `--skip-libvirt`), run
`just onboard`, and make the server URL and token available the way `kdivectl` reads them.
Then, from the repository root:

```bash
uv run python scripts/live-stack/stress-allocations.py --clients 8 --duration 120 --seed 7
```

| Flag | Default | Meaning |
|---|---|---|
| `--project` | `demo` | Project the clients allocate in |
| `--clients` | 8 | Concurrent simulated clients, one MCP session each |
| `--duration` | 60 | Seconds of load before the drain |
| `--seed` | random | Replays each client's action sequence; always printed |
| `--invalid-ratio` | 0.2 | Share of actions that send a malformed call |
| `--race-ratio` | 0.2 | Share of actions that run a race (double release, renew vs release, shared key) |
| `--abandon-ratio` | 0.1 | Share of actions that abandon a grant or cancel a request in flight |
| `--lease` | 0.02 | Lease window in hours on every request (72 s) |
| `--call-timeout` | 60 | Seconds before one call counts as a timeout |
| `--drain-timeout` | 600 | Seconds the drain may take; at least the lease plus 60 s, plus 240 s with a profile |
| `--provision-profile` | off | JSON provisioning profile; enables `systems.provision` |

The three ratios must sum to at most 1; the rest of the actions are churn cycles. A violation is
recorded when:

1. a schedulable host reports `in_use` above `cap`;
2. an invalid call is accepted, or ends in a transport failure or timeout;
3. a double release where both calls replied does not return `ok` both times;
4. two `ok` replies for one idempotency key carry different ids;
5. anything the run created is still unsettled at the end: an allocation not `released`,
   `expired` or `failed`, a System not `torn_down` or `failed`, or a call whose reply never
   arrived and could not be replayed.

Transport failures, timeouts and tool-errors on valid calls are counted in the report, not
treated as violations. Exit status: 0 no violation, 1 at least one violation, 2 a usage or
preflight failure (no shape, no schedulable host, no token, invalid profile), 130 interrupted.

Before you run it:

- **Abandoned grants are reclaimed by lease expiry.** The reconciler sweeps every 30 s, and that
  interval is fixed, so a grant nobody releases goes to `expired` within the lease plus 30 s. The
  drain waits for that, which is why `--drain-timeout` has a floor.
- **Ctrl-C once** ends the load and starts the drain. A Ctrl-C during the drain stops it; the
  report still lists what was left, and short-lease grants still expire. A `kill -9` skips the
  drain entirely.
- **`--provision-profile` needs the stack with libvirt** and a provisionable profile. Start from
  `systems.profile_examples` and replace its placeholder image. The script drops the profile's
  `vcpu`, `memory_mb` and `disk_gb`, which the server fills from each allocation, and the report
  warns if no provision succeeded. Each client keeps at most one System in flight.
- **The server must stay up for the run.** If it crashes, each client stops after five
  transport failures in a row with a note that the server may be down; a crash under load is
  itself a finding worth reporting.
- **Do not change host caps mid-run.** The monitor compares `in_use` with the cap it reads at
  that moment, so a cap lowered under load reports a false violation.

#### Catalog image smoke and coverage evidence (#2808)

`tests/integration/test_image_smoke_live.py::test_image_smoke` has one parameter for each
`image-smoke` coverage cell whose guest architecture is the host's. Each parameter writes one
version-1 evidence record for `python -m scripts.coverage_campaign qualify`
([coverage qualification](../../development/coverage-qualification.md),
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md)).
The stack, from `examples/local-libvirt/demo-up.sh`, must run the test checkout's exact clean
`HEAD`. Do not restart it during the run.

```bash
sha=$(git rev-parse HEAD)
examples/local-libvirt/build-image.sh <every catalog image for this architecture>
uv run python -m tests.integration.live_stack.image_smoke bindings --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)   # an empty evidence root for this run
uv run python -m pytest -m live_stack tests/integration/test_image_smoke_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

Write the bindings after staging and before the smoke. Each binding's `image_sha256` is the
digest of the staged bytes. A rebuilt image needs new bindings.

A cell's record says what happened:

- An image that is not registered, a missing issuer or a missing `KDIVE_DATABASE_URL` is
  `blocked` (`missing-prerequisite`).
- A deployed revision other than `HEAD`, or a dirty checkout, is `failure` before anything is
  provisioned.
- An assertion that does not hold is `failure`, carrying the assertions proven so far.

The pytest parameter fails in each of those cases. It also fails when a role is missing.
Image-smoke cells do not require the provider authority. When one is installed, its revision
must be the candidate, and the evidence seam must be able to read
`/opt/kdive-provider-authority/revision` through `sudo -n`. Otherwise the run stops
(ADR-0715). `qualify` exits 1 whenever any required cell lacks a result, which on a
single-cell-type run is always.

The artifacts under `coverage-evidence/artifacts/` hold assertion observations: digests, guest
`os-release` fields, whether the boot ID changed, and cleanup counts. They hold no host names or
keys. Check them before sharing.

#### Deep lifecycle across representative guests (#2809)

`tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle` has one parameter for each
local-libvirt `deep-lifecycle` cell whose guest architecture is the host's: four families times
the two pinned baselines in `fixtures/kernel/baselines.toml`. Each family runs on one
representative catalog image (`REPRESENTATIVES` in `tests/integration/live_stack/deep_lifecycle.py`):
`debian-kdive-ready-13`, `fedora-kdive-ready-44`, `rocky-kdive-ready-10` and
`opensuse-leap-kdive-ready-15.6` on x86_64. A parameter uploads its baseline's fixture kernel,
completes the build, installs and boots it, reconnects over SSH with the same key, checks the
running release and GNU build ID against the fixture, loads the `loop` module and compares its
bytes with the uploaded copy, then releases and proves owned cleanup, including the installed
kernel file. The stack requirements are those of the image smoke above.

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix            # one fresh directory per baseline below it
for b in longterm stable; do
  python3 scripts/kernel_fixtures.py build --baseline "$b" --arch "$(uname -m)" \
    --source "$KDIVE_FIXTURE_ROOT/src-$b" --output "$KDIVE_FIXTURE_ROOT/$b" --jobs "$(nproc)"
done
examples/local-libvirt/build-image.sh <each representative image>
uv run python -m tests.integration.live_stack.deep_lifecycle bindings --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)
uv run python -m pytest -m live_stack tests/integration/test_deep_lifecycle_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

Build the fixtures on the native architecture. A missing `KDIVE_FIXTURE_ROOT`, a fixture that
`verify` rejects, or an unregistered representative is `blocked` (`missing-prerequisite`); the other
outcomes are those of the image smoke. A binding's kernel fields are null when its fixture is absent,
which `qualify` reports as a missing required input.

The default fragment requests `CONFIG_KEXEC_FILE=y` for RHEL-family kdump loading, alongside
`CONFIG_KEXEC=y` and `CONFIG_CRASH_DUMP=y`. For the default x86_64 configuration, the builder
also appends `CONFIG_FW_CFG_SYSFS=y`, required by the existing crash-capture contract. The
assembled fragment is retained as `input.config` and hashed in the manifest. Native ppc64le
and explicit custom config files retain their selected input unchanged. After changing these
inputs, rebuild both baselines
into fresh output directories and regenerate their evidence bindings. An older manifest can
still pass integrity verification; that does not establish these options or current capture
evidence. Check the rebuilt effective `.config` and rerun the affected live cells.

After an interrupted run, release the leftover allocation with `allocations.release` (or let the
lease expire) and check `KDIVE_INSTALL_STAGING` (`/var/lib/kdive/install` on the demo lane) for
the run's installed kernel.

The four ppc64le local cells share the node and report `missing-result` (owner #2818) until a
native POWER host runs it. The remote deep cells are the remote runbook's
([remote deep lifecycle](remote-live-stack.md#7-remote-deep-lifecycle-2810)).

Last run: candidate `09761ef44` (server, worker and reconciler at that SHA), a Fedora 44 x86_64
KVM lab host, fixtures `v6.18.54` (longterm) and `v7.2.8` (stable) built on that host. All eight
cells proved `upload`, `install`, `reconnect`, `boot-identity`, `modules` and `cleanup`, and
`qualify` reported each one qualified. `KDIVE_INSTALL_STAGING` held no System directory after the
run. The earlier run at `749bd29d6` failed `cleanup` on every cell because teardown left the
installed kernel behind (#3078).

| Cell (`deep-lifecycle/local-libvirt/x86_64/…`) | Guest | Outcome |
|---|---|---|
| `debian/longterm`, `debian/stable` | `debian:13` | success |
| `fedora/longterm`, `fedora/stable` | `fedora:44` | success |
| `enterprise/longterm`, `enterprise/stable` | `rocky:10` | success |
| `suse/longterm`, `suse/stable` | `opensuse-leap:15.6` | success |

#### Broken/fixed kernel corpus (#2817)

`tests/integration/test_kernel_corpus_live.py::test_kernel_corpus` has one parameter for each
`kernel-corpus` cell whose guest architecture is the host's: two cases times the `broken` and
`fixed` revision. `fixtures/kernel/corpus.toml` pins the revisions; each `broken` commit is the
first parent of its `fixed` commit, so both build from the same configuration fragment. The case
notes in `docs/development/kernel-test-cases/` chose the cases and stay human-only.

| Case | Note | Reproducer (same on both revisions) | `broken` | `fixed` |
|---|---|---|---|---|
| `case-1` | 05, dcache | install with `dhash_entries=1`; the Run declares the console crash `RIP: 0010:__d_lookup` | boot ends `expected_crash_observed`; console shows one hash bucket and the page-fault oops | ready boot, parameter in `/proc/cmdline`, two buckets, no crash signature |
| `case-2` | 16, procfs | read the link, subdirectory and file counts of every directory under `/proc/bus/pci` | each link count is at least `2 + subdirectories + files` | each link count is `2 + subdirectories` |

Every parameter verifies both fixtures of its case and requires the same `input.config` and
`.config` digests (`same-reproducer-config`). It provisions `fedora-kdive-ready-44`, uploads its
revision with `vmlinux`, installs and boots it, and checks that the domain boots the uploaded
`bzImage`. `debug-or-crash-evidence` is the identity-bound artifact of the outcome:
for `case-1/broken`, every reliable call-trace frame `symbol+0xOFF/0xSIZE` of the oops resolves
in the fixture's `System.map` with that size (which rejects another kernel version, not the case's
sibling revision) and the console banner names the fixture release, whose `-g<commit>` suffix
identifies the revision; for the
other cells, the running release and GNU build ID with the case's observation. Any other
observation, including a timeout or a crash on a `fixed` revision, fails the cell. The frame then
releases and proves cleanup, which also covers the halted guest of `case-1/broken`.

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix            # one fresh directory per revision below it
for c in case-1-broken case-1-fixed case-2-broken case-2-fixed; do
  python3 scripts/kernel_fixtures.py build --baseline "$c" --arch "$(uname -m)" \
    --source "$KDIVE_FIXTURE_ROOT/src-$c" --output "$KDIVE_FIXTURE_ROOT/$c" --jobs "$(nproc)"
done
examples/local-libvirt/build-image.sh fedora-kdive-ready-44
uv run python -m tests.integration.live_stack.kernel_corpus bindings --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)
uv run python -m pytest -m live_stack tests/integration/test_kernel_corpus_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

A missing `KDIVE_FIXTURE_ROOT`, or either revision of the case missing or rejected by `verify`, is
`blocked` (`missing-prerequisite`). The four ppc64le cells share the node. Without a ppc64le run
they report `missing-result`; a run on a ppc64le host today records them `blocked`, because the
case table and representative image are x86_64 only until #2818 supplies POWER fixtures and
cases.

Last run (2026-10-15): candidate `3b35b4c82` (server, worker and reconciler at that SHA), a
Fedora 44 x86_64 KVM lab host, the four revisions built on that host. All four x86_64 cells
proved their outcome, `same-reproducer-config`, `debug-or-crash-evidence` and `cleanup`, and
`qualify` reported each one qualified; no `kdive-*` domain remained afterwards.

| Cell (`kernel-corpus/local-libvirt/x86_64/…`) | Observation | Outcome |
|---|---|---|
| `case-1/broken` | `expected_crash_observed`; 14 reliable oops frames resolved in `System.map`, first `__d_lookup+0x3c/0xf0`; banner `6.19.0-g970296997869` | success |
| `case-1/fixed` | ready boot with `dhash_entries=1`, two hash buckets, no crash signature, build ID matched | success |
| `case-2/broken` | `/proc/bus/pci/00` link count 12 with 10 files and no subdirectories | success |
| `case-2/fixed` | every `/proc/bus/pci` link count equal to 2 plus its subdirectories | success |

#### Local resource-boundary cells (#3118)

`tests/integration/test_resource_limits_live.py::test_local_resource_boundary` has one parameter
for each `failure-resource/local-libvirt/x86_64/{minimum-cpu-ram,insufficient-disk,slow-boot}`
cell. Every parameter uses the `rocky-kdive-ready-10` image and the longterm fixture kernel, and
proves its resource outcome plus `no-duplicates`, `protected-state` and `cleanup`:

- `minimum-cpu-ram` requests the seeded `small` shape (1 vCPU, 1024 MiB, 10 GB). It checks the
  domain XML and the overlay's virtual size, then uploads, installs and boots the fixture kernel
  and reconnects over SSH.
- `insufficient-disk` opens an investigation and mounts a 64 MiB test-owned loop filesystem
  (`mkfs.ext4 -m 0`) at that investigation's new directory under `/var/lib/kdive/rootfs-uploads`.
  It uploads the staged image as the investigation's rootfs and provisions it. Every attempt
  must fail the staging precheck with `infrastructure_failure`, and nothing may be left staged or
  defined. After release it unmounts, detaches and removes the directory, then closes the
  investigation.
- `slow-boot` installs the fixture kernel and times a plain `runs.boot`. It then writes a
  `oneshot` delay unit, active only on the fixture kernel's release and ordered before
  `kdive-ready` and the guest agent. A forced boot with a delay inside half the readiness window
  must succeed. A delay longer than the window plus 60 s must fail every attempt with
  `boot_timeout`. The guest's own journal must show each delay ran in full before
  `kdive-ready` started.

The test reads the fixed workers' journal (`journalctl -u 'kdive-live-worker@*'`) to label each
attempt, so the operator needs journal access (the `systemd-journal` group). It also needs
`sudo -n` for the loop filesystem and for reading worker-owned disks. The window is
`KDIVE_LIBVIRT_BOOT_WINDOW_S` (default 900 s), so `slow-boot` runs for about an hour at the
default. The test reads the variable from its own environment, so it must match the workers'
setting.

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix
python3 scripts/kernel_fixtures.py build --baseline longterm --arch "$(uname -m)" \
  --source "$KDIVE_FIXTURE_ROOT/src-longterm" --output "$KDIVE_FIXTURE_ROOT/longterm" --jobs "$(nproc)"
examples/local-libvirt/build-image.sh rocky-kdive-ready-10
uv run python -m tests.integration.live_stack.resource_limits bindings --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)
# insufficient-disk uploads the staged image file; pytest sandboxes the XDG default inventory.
export KDIVE_SYSTEMS_TOML=$HOME/.config/kdive/systems.toml
uv run python -m pytest -m live_stack tests/integration/test_resource_limits_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

A cell whose loop filesystem cannot be proven unchanged keeps it mounted and records
`bounded_mount: retained` with its target, loop device and backing image; unmount and detach
it by hand after checking that no job is running. The three `remote-libvirt` cells are in the
[remote runbook](remote-live-stack.md#10-remote-resource-boundary-cells-3118).

Last run: candidate `3a0081d02`, a Rocky 10.2 x86_64 KVM host (SELinux enforcing) with the
longterm fixture. All three
cells qualified. `slow-boot` measured a 6 s baseline boot; a 66 s delay reached ready, and a
1020 s delay against the 900 s window ended all three attempts `boot_timeout`.
`insufficient-disk` refused the 1.2 GB upload on its single attempt with about 53 MiB
available, leaving nothing staged. Before the uploads root joined host preparation and the
`svirt_image_t` labels, an uploaded rootfs could not be staged at all, and qemu was denied
reading one in place.

#### Local orchestration-fault cells (#3117)

`tests/integration/test_orchestration_faults_live.py::test_local_orchestration_fault` has one
parameter for each `failure-resource/local-libvirt/x86_64/{worker-interruption,lost-response,backing-service}`
cell. Each cell provisions the `rocky-kdive-ready-10` image at the default shape on the direct
provision path (a stack with no installed authority) and binds the longterm fixture kernel's
inputs without booting it. The worker pause and the Postgres outage land in the provision's
first boot, while the domain is running and attempt 1 still holds the job; the lost-response
replay runs while the provision job is running. Each cell proves its recovery
outcome plus `no-duplicates`, `protected-state` and `cleanup`.

- `worker-interruption` pauses the worker holding the job with `SIGSTOP`. It never restarts
  the unit, because an out-of-band restart wedges the slot. After the 5-minute lease lapses,
  another worker must take the job at a higher attempt and finish it. The paused worker is then
  resumed, and its late result must leave the job row, the `ready` System, the domain and the
  overlay unchanged.
- `lost-response` provisions with a fresh idempotency key. It reads the committed
  `idempotency_keys` row and drops the reply. It replays the key from a new session twice, once
  while the job runs and once after it finishes. Both replays must return the stored job and
  System, with exactly one provision job and one System on the Allocation.
- `backing-service` runs `docker compose stop postgres` mid-provision. After 75 s it runs
  `docker compose start postgres`, which must bring back the same container. Every role's
  `/readyz` must return to ready. The job must then succeed at an attempt above 1, and the
  worker journal labels whether it was requeued or reclaimed after its lease lapsed. No worker
  unit's `InvocationID` or `MainPID` may change.

Preconditions, checked before anything is allocated (each one missing blocks the cell):

- `KDIVE_WORKER_COUNT=2` or more running fixed worker slots;
- passwordless `sudo -n /usr/bin/python3` for the worker hold;
- journal access (the `systemd-journal` group);
- for `backing-service`, the docker group and the compose `postgres` service of this checkout's
  stack.

Run it against an exclusively assigned host: `backing-service` stops the stack's shared
database.

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix
uv run python -m tests.integration.live_stack.resource_limits bindings --owner 3117 \
  --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)
uv run python -m pytest -m live_stack tests/integration/test_orchestration_faults_live.py \
  -k test_local_orchestration_fault
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

After the run, check all of the following:

- `scripts/live-stack/worker-lifecycle.sh status` shows every slot running;
- every `/readyz` answers 200;
- no `kdive-` domain the run added is still defined.

A cell that stops mid-fault resumes the paused worker and restarts Postgres in its own
`finally`. If the run itself is killed, send `SIGCONT` to the paused unit's main PID and run
`docker compose start postgres` by hand before anything else. A wedged slot is recovered with
`worker-lifecycle.sh recover` (see the live-stack runbook). The three `remote-libvirt` cells are
in the [remote runbook](remote-live-stack.md#11-remote-orchestration-fault-cells-3117).

#### Core tool cells and server configurations (#2811)

`tests/integration/test_core_tool_cells_live.py::test_core_tool_cell` has one parameter for each
contract cell of `session.whoami`, `projects.list`, `tools.search`, `tools.invoke`,
`fixtures.validate` and `systems.profile_examples`: 84 cells, each a configuration (`default` or
`recovery`), an exposure (`direct` or `gateway`, or `cli` for the core service tools, which drives
the tool's generated `kdivectl` verb per
[ADR-0795](../../adr/0795-cli-translation-exposure.md)) and a kind (`functional`, `authentication` or
`validation`). [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
defines what each of those means. The harness in `tests/integration/live_stack/tool_cells.py` is
shared by the later tool-cell carriers.

A run proves its configuration before recording anything. It reads the operator catalog of a
`kdivectl` token holding `platform_operator`: both build-use recovery tools listed means
`recovery`, neither means `default`, and anything else fails the run. A catalog clipped to the
gateway's core tools also fails it, which happens when the server's `KDIVE_CLI_CLIENT_ID` differs
from the test process's, and so does a missing mock-OIDC issuer. Cells of the other configuration
skip and write no record. The server runs the `recovery` configuration only when it starts with a
durable worker-death verifier, `docker` or `kubernetes`; `local` leaves the recovery tools out.
The verifier only gates registration. These cells never call a recovery tool; the
[build-use recovery cells](#build-use-recovery-tool-cells-3111) do. The two lanes are two bring-ups that share one evidence
root:

```bash
sha=$(git rev-parse HEAD)
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)        # one evidence root for both lanes
examples/local-libvirt/demo-up.sh             # default configuration
uv run python -m pytest -m live_stack tests/integration/test_core_tool_cells_live.py
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery configuration
uv run python -m pytest -m live_stack tests/integration/test_core_tool_cells_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

Each pytest run reports 42 cells skipped for the other configuration: the second run must skip the
`…/default/default/…` cells, not the `…/default/recovery/…` ones. Run the pytest commands with
`KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`, the least-privilege harness login: the
protected-state snapshot reads every table with a `project` column, some of which the server DSN
cannot read. The snapshot checks its access first. It fails before any read, naming each table it
cannot `SELECT`, rather than skipping one. A table that a newer migration added is granted on the
next role-bootstrap run.

A functional cell compares the tool's answer with the token's own claims, the fixture catalog and `systems.toml`
read by the test process, or, for `tools.search`, the operator-direct catalog. A rejection cell
proves its boundary (HTTP 401 for a token signed by a foreign key, where the issued token is not
refused; a schema-validation failure for invalid arguments; on `cli`, the readings of
[ADR-0795](../../adr/0795-cli-translation-exposure.md) decision 3) and that the cell's project rows did not change. The tools create nothing, so
there is nothing to tear down beyond the stack itself. `qualify` exits 1 because other owners'
cells have no result; read the 84 `tool/…` rows of these six tools.

Last run: candidate `e3354a190` (server, worker and reconciler at that SHA in both lanes), a
disposable Fedora 44 x86_64 lab guest with SELinux enforcing; the same result held at `09a80767c`
before the merge with `main`. The `default` lane recorded the 28
`…/default/default/…` cells and skipped the rest; the `KDIVE_WORKER_DEATH_VERIFIER=docker` lane
listed both recovery tools and recorded the 28 `…/default/recovery/…` cells. `qualify` reported
all 56 qualified: 24 `success` (functional) and 32 `rejection` (authentication and validation),
with the issued token's control request not refused in every authentication cell. An earlier
recovery lane started with `local` proved `default` again, which is why the lane uses `docker`.

#### Catalog and configuration tool cells (#3095)

`tests/integration/test_catalog_tool_cells_live.py::test_catalog_tool_cell` carries the 228 cells
of `images.{delete,describe,kernel_config,list,upload}`, `shapes.{delete,list,set}` and
`resources.{availability,describe,list}` on the same harness, lanes, bindings and assembly as the
core cells above; run both files in each lane's pytest command. The
[design](../../workflow/specs/2026-10-02-catalog-tool-cells-design.md) lists what each functional
cell compares and with which independent source.

Before the first lane, stage the public image the image cells read, once per host:
`examples/local-libvirt/build-image.sh fedora-kdive-ready-44`. It writes the qcow2 with its
`.provenance.json` and `.config` siblings and declares it in `systems.toml`; a wiped stack
re-registers it on the next bring-up's reconcile. Without it the image cells record `blocked`.
Run pytest in a shell that sourced `examples/local-libvirt/env.sh`: the cells read the object
store (`KDIVE_S3_*`, `AWS_*`), `KDIVE_LIBVIRT_URI` and the funded `KDIVE_PROJECT`, beside
`KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`. Also export
`KDIVE_SYSTEMS_TOML=~/.config/kdive/systems.toml`: the test session sandboxes the XDG default and
keeps only an exported path for a live tier (`tests/conftest.py`). The core
`systems.profile_examples` cell reads the same file, so once an image is declared both carriers
need it.

Unlike the core cells, these write and remove state. Each cell works in a fresh `cov-<hex>`
project and proves its cleanup with a snapshot that adds that project's private images and the
whole shapes catalog to the per-project one:

- `images.upload`, `images.delete` and `images.list` upload the staged qcow2 to
  `uploads/q/<project>/` with the metadata the upload reassembly writes, register it, delete the
  image and purge every version of the quarantine key and the published object;
- `shapes.set` and `shapes.delete` create and remove a `cov-<hex>` shape;
- `shapes.set` takes one allocation in `KDIVE_PROJECT` and releases it.
- `resources.availability` requires an idle fleet with one available local host of capacity
  one and room for 1 vCPU/1 GiB RAM/1 GB disk. It temporarily raises the funded project's
  pending cap, preserving both concurrency caps, holds one grant and queues one by-kind and
  one by-ID request. It compares the full queue split with the database, withdraws both queued
  rows before releasing the grant, then verifies zero queue depth and restored quota caps.
  Unsuitable topology is `blocked`. Failed or indeterminate withdrawal retains known blockers;
  reconcile the reported subject and IDs, withdraw queued rows before releasing blockers,
  and restore the recorded caps. A restored pending cap alone does not clear residual rows.
  Released allocations and ledger entries remain as history.

A cell killed mid-run can leave any of these behind; `demo-down.sh --wipe --yes` clears them.

#### Investigation and artifact tool cells (#3096)

`tests/integration/test_investigation_tool_cells_live.py::test_investigation_tool_cell` carries the
390 cells of `investigations.{open,get,list,set,link,unlink,close,complete_rootfs_upload}` and
`artifacts.{create_investigation_upload,create_run_upload,fetch_raw,get,list}` on the same harness,
lanes, bindings and assembly; run all three carriers in each lane's pytest command. The
[design](../../workflow/specs/2026-10-02-investigation-tool-cells-design.md) lists what each
functional cell compares and with which independent source. Its environment is the catalog
carrier's (the sourced `env.sh`, `KDIVE_DATABASE_URL`, `KDIVE_SYSTEMS_TOML`), and it needs the
same staged `fedora-kdive-ready-44` image plus KVM on the host.

Investigations cannot be deleted, so each cell works in a fresh `cov-<hex>` project and proves its
cleanup by closing what it opened: its investigations end `closed` and its Runs `canceled` (or
`succeeded`, for the `artifacts.fetch_raw` build). The snapshot keeps every live investigation,
live Run and the upload manifests and artifact rows they own; closed and ended ones are history.

- The upload cells PUT 4 KiB of random bytes through the presigned URL. Bytes nothing adopted are
  purged; a finalized rootfs and the `fetch_raw` build (a synthetic kernel bundle and vmlinux
  accepted by `runs.complete_build`) are left to the reclaim the close schedules, which runs after
  the reconciler's one-day grace.
- `artifacts.list` and `artifacts.get` read the console parts of one System per lane, provisioned
  in `KDIVE_PROJECT` on first use (about a minute to `ready` plus a minute for its console part to
  settle) and released at the end of the module, with its domain and disks proven gone.
- `investigations.list` authorization and project-isolation and `artifacts.list`
  project-isolation answer a denied caller with an empty page instead of a failure
  ([ADR-0756](../../adr/0756-filtered-list-boundary-evidence.md)). Each cell first lists a known
  protected item with a viewer's token and the same arguments: the investigation its setup opened,
  or the session System's newest console part. Then the denied call must return exactly the
  tool's empty page, with no item, cursor or extra field. The `artifacts.list` cell protects that
  part and the System's project instead of the fresh project. If console rotation pushes the
  part off the viewer's first page, the cell fails rather than accepting another part.
- The three upload tools' project-isolation cells accept `configuration_error` only when it is
  indistinguishable from the answer for a nonexistent owner, called in the same cell.

A lane expects no failing cell. The System is released in a
module finalizer outside every cell, so a failed reclaim shows only as an `ERROR at teardown` of
`test_investigation_tool_cell`; record that line, if present, with the run's result.

A cell killed mid-run can leave an open investigation, a created Run or the System;
`demo-down.sh --wipe --yes` clears them, as it clears the history above.

#### Read-only operator tool cells (#2812)

`tests/integration/test_operator_tool_cells_live.py::test_operator_tool_cell` carries the 124
cells of `audit.query`, `inventory.list`, `ops.diagnostics`, `ops.export_cost_classes`,
`ops.export_systems_toml`, `ops.jobs_list`, `ops.tool_trail` and `secrets.list` on the same
harness, lanes, bindings and assembly; add it to each lane's pytest command. The
[design](../../workflow/specs/2026-10-06-operator-tool-cells-design.md) lists what each
functional cell compares and with which independent source. Its environment is the catalog
carrier's: the sourced `env.sh`, `KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"` and an
exported `KDIVE_SYSTEMS_TOML`. The lanes are local-libvirt only; an `ops.diagnostics` cell that
sees a remote-libvirt check stops `blocked` rather than probe a remote seam.

The tools only read. Except for the secret-presence fixture below, each cell works in a
fresh `cov-<hex>` project whose snapshot must not change. What the cells leave behind is history the snapshot excludes:

- `audit_log`, `platform_audit_log` and `tool_invocation` rows from the cells' own calls, which
  the `audit.query` and `ops.tool_trail` cells read back;
- one allocation per `inventory.list` cell in `KDIVE_PROJECT`, requested and released;
- the `diagnostics_worker_check` jobs `ops.diagnostics` enqueues.

The four `secrets.list` functional cells use an owned Fedora 44 local System, the
verified longterm fixture under `KDIVE_FIXTURE_ROOT/longterm`, and a successful
`debug.start_session` with `drgn-live`. The existing server path registers the
System's bootstrap key. They require an empty server listing before setup and
attach, then exactly `<process-global>` through the selected direct/gateway
exposure, with no configured or owned-key value in the answer. A server already
holding a secret blocks before creating the fixture: restart it first.

Run these four functional cells separately from other live cells, on an idle
operator-owned stack. For each configuration, use the canonical stack bring-up
with `KDIVE_WORKER_DEATH_VERIFIER=disabled` (default) or `docker` (recovery), verify
all three role SHAs, and select one exposure's functional cell:

```sh
uv run python -m pytest tests/integration/test_operator_tool_cells_live.py \
  -k 'secrets.list and functional and direct' -vv --tb=short
```

Repeat with `gateway`; the opposite configuration's parameter is skipped, not
counted as proof. Each successful cell records source presence and actual owned
session, System, disk, allocation-capacity and bootstrap-key cleanup. Terminal
history remains. The server's global registration deliberately remains after
resource cleanup: it lasts until that process exits. Between cells and after the
last cell, stop the owned server through the canonical stack lifecycle, verify
its old PID exited, start the same candidate again, and retain an authenticated
empty `secrets.list` answer alongside the cell records. Record the old/new PID,
configuration and candidate identity without tokens or key material. Do not use
registry clearing, DB deletion or a synthetic response as cleanup evidence.

An unavailable image/kernel or failing attach prerequisite cannot qualify the
cell. Preserve that failure and its owned cleanup evidence. These cells prove
secret presence, not image-builder provenance, introspection or native POWER.
`ops.export_systems_toml` is never called with `persist`. A killed run can leave
owned resources; use their recorded IDs for supported cleanup, preserving shared
images and prior histories.

Historical pre-positive-control run: candidate `86ed1b021` (server, worker and reconciler at that SHA in both lanes), a
disposable Fedora 44 x86_64 lab guest; earlier runs at `44503b3bf` and `f1150fc13` gave the same
result. Each lane recorded its 62 cells and skipped the other
lane's 62; the `KDIVE_WORKER_DEATH_VERIFIER=docker` lane proved `recovery`. `qualify` reported
120 of the 124 qualified, 28 `success` and 92 `rejection`, and the four `secrets.list`
functional cells `blocked` with an empty listing and no secret served.

#### Build-use recovery tool cells (#3111)

The same carrier also holds the 16 cells of `ops.build_uses_list` and `ops.recover_build_use`.
They exist only in the `recovery` configuration, so start that lane with
`KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh`. The verifier only decides
whether the server registers the two tools. A recovery succeeds on the durable `terminated`
incarnation row that the systemd lifecycle witness publishes, not on any Docker endpoint
(ADR-0722, 2026-10-14 amendment). The
[design](../../workflow/specs/2026-10-14-build-use-recovery-cells-design.md) gives the details.

The 12 rejection cells (`authentication`, `authorization`, `validation`) need nothing beyond the
operator cells' environment. Each of the four functional cells strands a real pin:

1. It provisions the lane image in a fresh project and uploads the verified `longterm` fixture
   (`KDIVE_FIXTURE_ROOT/longterm`) with its `vmlinux`.
2. It calls `runs.install`.
3. It freezes the holder's `kdive-live-worker@N` unit with `sudo -n systemctl freeze` once the pin
   row appears.
4. Later it kills that unit with `sudo -n systemctl kill --signal=SIGKILL`, and has
   `scripts/live-stack/worker-lifecycle.sh status` publish the termination.

So the operator needs non-interactive `sudo -n` for `systemctl` (`freeze`, `thaw`, `kill`); the
cell checks it first, and a refused `sudo` stops the cell `blocked`. What each cell proves:

- The `ops.build_uses_list` cell compares the listing with the project's pin rows twice, once
  while the holder is alive and once after it is killed. It then recovers the orphan as cleanup.
- The `ops.recover_build_use` cell is refused while the holder is alive and the pin is retained.
  After the kill it recovers exactly that pin, with one matching recovery row, and every other
  pin is unchanged.

A pin released before the freeze, or a holder frozen inside its release transaction, thaws the
holder and stops the cell `blocked`; rerun it.

Every cell restores the lane before its System teardown:

- A live holder is thawed and finishes its install.
- For a killed holder, any pin the cell left is recovered through the tool. The install job is
  then canceled, and the fleet is replaced with `worker-lifecycle.sh stop`, `recover` and
  `start <the started slot count>`.
- The cell then requires that no pin is left.

Run these cells serially on an otherwise idle stack: the restart replaces every worker slot.

```sh
uv run python -m pytest -m live_stack tests/integration/test_operator_tool_cells_live.py \
  -k 'build_use' -vv --tb=short
```

If the witness never published the killed holder's termination, the restore cannot recover its
pin. The cell fails naming the use id and holder: run `worker-lifecycle.sh status`, then
`ops.recover_build_use` with that holder. Recovery rows, audit rows and the terminated
incarnation stay as history.

Last run: candidate `21250f083` (server, worker and reconciler at that SHA) on a disposable
Fedora 44 x86_64 lab guest with one worker slot. The server's `TMPDIR` pointed at an on-disk
directory, because Fedora's `/tmp` is a small tmpfs. `qualify` reported all 16 cells qualified:
4 `success` and 12 `rejection`.

- Both recover cells were refused while the holder was alive. After the kill, each recovered
  exactly its pin, with recovery rows recording `authority_kind = 'local'` and outcome `killed`.
- After the run no pin was left, no unit was frozen, the slot was started again, and every
  System was torn down.
- An earlier run at `c7302a912` qualified 14. Its recover cells read the expected alive refusal
  through a helper that rejects error envelopes; that is fixed, and the restore still left the
  lane clean on that path.

#### Mutating operator tool cells (#3110)

`tests/integration/test_operator_mutation_tool_cells_live.py::test_operator_mutation_tool_cell`
carries the 216 cells of the fourteen mutating operator tools (`images.extend`,
`images.prune_expired`, `inventory.clear_override`, `ops.reconcile_now`,
`ops.reconcile_systems`, `ops.set_cost_class_coeff`, `ops.set_host_capacity`,
`ops.set_queue_paused` and the six `resources.*` mutation tools) on the same harness, bindings and
assembly as the cells above. The
[design](../../workflow/specs/2026-10-14-operator-mutation-tool-cells-design.md) lists what each
functional cell seeds, observes and restores.

Run it on the installed local-authority lane: the `ops.reconcile_now` and `resources.drain`
functional cells run inside the [authority-owned System frame](#authority-owned-system-frame-3184)
and need its fixture-mode setup, and a lane with an installed authority records the authority's
revision on every cell, so every role must run the candidate. Use the two bring-ups of the core
cells (`KDIVE_WORKER_DEATH_VERIFIER=docker` for `recovery`) and, beside the catalog carrier's
environment (`KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`, an exported
`KDIVE_SYSTEMS_TOML`, the staged lane image), set `LOCAL_AUTHORITY_LANE_CONFIG`.
`inventory.clear_override` needs one declared `[[fault_inject]]` or `[[remote_libvirt]]` host
in that inventory: the cell detaches its cap, clears the entry and waits for the reconciler to
restore the file's cap. The authority lane's fixture inventory already declares its
`[[remote_libvirt]]` authority host, and the cell changes only that row's database values,
never the remote host. On a lane without one, add a `[[fault_inject]]` entry (`name`, `vcpus`,
`memory_mb`, `cost_class`): reconcile creates its row without composing the fault-inject
provider. Adding a `[[remote_libvirt]]` entry instead enables the remote provider for the whole
stack. Run one cell at a time on the lane, as for the frame.

Cells change shared state while they run and restore it before they finish: the queue pause,
the lane host's cordon (`resources.drain`), the declared host's cap, and the coefficient of the
cell-owned class `cov-tool-cells`. A killed run can leave one of them behind. Restore them with an
operator or admin token: `ops.set_queue_paused` with `paused=false`,
`resources.set_scheduling` with `state=schedulable` on the lane host, `inventory.clear_override`
for the declared host (the next reconcile restores its cap). The `cov-tool-cells` coefficient
needs no restore: only cordoned residue hosts carry the class, so no placement prices with it.

What the cells leave behind is history or inert:

- audit, platform-audit and tool-invocation rows;
- allocation and ledger rows in each cell's fresh workload project;
- one cordoned, lease-cleared runtime host per cell that placed an allocation (the retained
  allocation rows keep a foreign key to it), owned by that workload project;
- the `cov-tool-cells` coefficient row: no tool or reconcile deletes a coefficient.

#### System lifecycle tool cells (#3062)

`tests/integration/test_system_tool_cells_live.py::test_system_tool_cell` carries the 120 x86_64
local-libvirt cells of `systems.provision`, `systems.ssh_info`, `systems.authorize_ssh_key`,
`systems.check_ssh_reachable`, `systems.reprovision` and `systems.teardown`. It is the first
provider-lane carrier: the tool-cell harness binds these cells with the guest's catalog identity,
the `kvm` accelerator and the staged image digest, and frames them on a real System. The
[design](../../workflow/specs/2026-10-06-system-tool-cells-design.md) lists what each functional
cell compares and with which independent source. Its lanes, environment and assembly are the
catalog carrier's: the sourced `env.sh`, `KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`,
an exported `KDIVE_SYSTEMS_TOML`, KVM on the host, and the staged `fedora-kdive-ready-44` image,
which every cell boots. Stage the image before writing the bindings: each provider binding carries
the staged bytes' SHA-256, so a rebuilt image needs new bindings. The same file also carries the
remote-libvirt cells (#3080), which block without a provider host, so select the local cells with
`-k local-libvirt` on this lane; the
[remote runbook](remote-live-stack.md#8-remote-system-tool-cells-3080) runs the others.

- A functional cell provisions the image in a fresh `cov-<hex>` project, authorizes a frame key
  and checks the guest over SSH against the catalog entry, calls the tool through the cell's
  exposure, proves its effect against the worker's libvirt domain and the guest, then releases the
  allocation and proves the domain undefined, the disks gone and capacity returned. The
  `systems.provision` cell provisions through the cell's exposure itself.
- A rejection cell aims at one target per stack: a System provisioned, observed, torn down and
  released by the first rejection cell, whose project then must not change. Every role check of
  these tools runs before any state check, so a torn-down System and a released Allocation are
  valid targets. Each rejection record carries the target's guest identity and cleanup proof.

A validation cell mistypes the `system_id` or `allocation_id`. For `systems.provision` and
`systems.reprovision` that matters on `gateway`: `BindingErrorMiddleware` re-envelopes only
failures under their `profile` parameter, so a mistyped id still reaches `tools.invoke`'s own
binding failure with `field_errors`, the evidence the
[ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md) amendment
requires. The ppc64le local cells share the node but are owner #2818's, and are never collected
on an x86_64 host.

A lane takes about eight minutes on a KVM host: twelve functional cells each provision a System.
What the cells leave
is history: torn-down Systems, released allocations, their ledger and audit rows. A cell that
fails before its cleanup proof records a best-effort cleanup attempt instead, and a killed cell
can leave a System and its allocation. `demo-down.sh --wipe --yes` clears all of it; run it after
the proof and check that `virsh list --all` on the worker's libvirt shows no `kdive-` domain.

Last run: candidate `a939e67ba` (server, worker and reconciler at that SHA in both lanes), a
disposable Fedora 44 x86_64 KVM lab guest with SELinux enforcing and no provider authority
installed. Each lane recorded its 60 cells in about eight minutes and skipped the other lane's 60;
the `KDIVE_WORKER_DEATH_VERIFIER=docker` lane proved `recovery`. `qualify` reported all 120
qualified: 24 `success` and 96 `rejection`, including the four `gateway` validation cells of
`systems.provision` and `systems.reprovision`. After `demo-down.sh --wipe --yes` the worker's
libvirt defined no domain. An earlier run at `0539e447b` gave the same result.

#### Run and image tool cells (#3119)

`tests/integration/test_run_tool_cells_live.py::test_run_tool_cell` carries the 96 x86_64
local-libvirt cells of `runs.install`, `runs.boot`, `runs.cancel`, `runs.release_external_boot`
and `images.publish`. Only the release requires the provider `authority` role, and
`images.publish` declares no kernel inputs (operator decision, 2026-10-07). The
[design](../../workflow/specs/2026-10-07-run-tool-cells-design.md) lists what each cell compares.
The prerequisites are the System cells' above, plus a verified `longterm` kernel fixture under
`KDIVE_FIXTURE_ROOT`, built as in the deep-lifecycle section with `--baseline longterm`:

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
export KDIVE_ARTIFACT_DIR=$(mktemp -d)
uv run python -m pytest -m live_stack tests/integration/test_run_tool_cells_live.py -k local-libvirt
```

Run each configuration on a freshly wiped stack (`demo-down.sh --wipe --yes`, then `demo-up.sh`,
with `KDIVE_WORKER_DEATH_VERIFIER=docker` for `recovery`). The staged lane image is registered
again from `KDIVE_SYSTEMS_TOML` at bring-up, so its digest and the bindings stay valid. Then
assemble and qualify as in the System section.

- `runs.install` and `runs.boot` cells provision the lane image, upload the fixture and run the
  deep-lifecycle checks, with the tool under test called through the cell's exposure.
- A `runs.cancel` cell cancels an uploaded Run whose build it never completed. The System stays
  ready and untouched and is freed for a new Run.
- An `images.publish` cell builds and publishes `fedora-kdive-ready-43-cloud` (`direct`) or
  `rocky-kdive-ready-9` (`gateway`) on the worker, then boots it. The worker needs network
  access to the pinned cloud-image URLs. `images.publish` returns a stack's existing job for the
  same image, so a cell stops `blocked` when its image was already published on that stack; this
  is why each configuration starts from a wiped stack. The published rows stay registered until
  the wipe.
- The `runs.release_external_boot` functional cells run only on the installed authority lanes
  ([authority functional tool cells](#authority-functional-tool-cells-3186)). On the demo-up
  lane they stop `blocked` (`missing-prerequisite`), because no authority lane config is named.
  Their rejection cells run on either lane.
- `runs.*` rejection cells aim at one unbound Run created in the stack's System target project.
  `runs.install` and `runs.boot` answer another project's Run with `configuration_error`,
  `runs.cancel` and `runs.release_external_boot` with `not_found`; each matches its answer for an
  absent Run.

The ppc64le local cells share the node but are owner #2818's and are never collected on an
x86_64 host. The carrier also holds the remote-libvirt cells of the
[remote run and image section](remote-live-stack.md#9-remote-run-and-image-tool-cells-3120);
`-k local-libvirt` leaves them out.

Last run: candidate `0024b4145` (server, worker and reconciler at that SHA in both
configurations), a disposable Fedora 44 x86_64 KVM lab guest with SELinux enforcing and no
provider authority installed, the `longterm` fixture `v6.18.54` built on that host. Each
configuration ran on a freshly wiped stack: 48 cells, about 15 minutes. `qualify` reported 88 of
the 96 cells qualified: 12 `success` (`runs.install`, `runs.boot` and `runs.cancel`) and all 76
`rejection` cells. The four `runs.release_external_boot` functional cells were `blocked` as
designed. The four `images.publish` functional cells were `failure`. The worker's build job
dead-lettered with `infrastructure_failure`, because the session libvirt that runs the
customization boot could not read the worker's build workspace ("Cannot access storage file
'/var/lib/kdive/build/images/rootfs-build-<random>/<image>.qcow2' ... Permission denied"). The
build workspace held no leftovers afterwards. After `demo-down.sh --wipe --yes` the worker's
libvirt defined no `kdive-` domain and `KDIVE_INSTALL_STAGING` was empty.

#### Power and snapshot tool cells (#2815)

`tests/integration/test_power_snapshot_tool_cells_live.py::test_power_snapshot_tool_cell` carries
the 100 x86_64 local-libvirt cells of `control.power`, `systems.snapshot`, `systems.restore`,
`systems.list_snapshots` and `systems.delete_snapshot`: 20 `functional` and 80 rejection cells.
Both libvirt providers support snapshots, so these tools have no `capability` cell
([ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md), amended
2026-10-13). The [design](../../workflow/specs/2026-10-08-local-power-snapshot-proof-design.md)
lists what each cell compares. The prerequisites are the run tool cells' above, including the
verified `longterm` fixture; the cells need no provider authority. Run both configurations into
one evidence root, then assemble and qualify as in the System section:

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix KDIVE_ARTIFACT_DIR=$(mktemp -d)
export KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
examples/local-libvirt/demo-up.sh                                     # default
uv run python -m pytest -m live_stack tests/integration/test_power_snapshot_tool_cells_live.py
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery
uv run python -m pytest -m live_stack tests/integration/test_power_snapshot_tool_cells_live.py
```

- Each functional cell provisions `fedora-kdive-ready-44`, boots the fixture kernel through the
  deep-lifecycle checks and then calls only its tool through the cell's exposure. The guest
  holds a value on disk, one in `/dev/shm` and a sentinel process. The disk value is read after
  `sync` and `vm.drop_caches=3`, so a restore that brings back RAM alone cannot pass.
- `systems.snapshot` and `systems.restore` restore a memory checkpoint and require the
  checkpoint's boot ID, both values and the same process. `systems.restore` also restores
  paused (libvirt and the System both `paused`, then `control.power` `resume`), and last a
  disk-only checkpoint: the disk value rolls back in a new boot. A disk-only checkpoint may
  give the domain a new overlay disk, which the cleanup proves absent with the others.
- `systems.list_snapshots` compares two checkpoints, newest first, with libvirt's.
  `systems.delete_snapshot` checks both ledgers and that the name can be used again.
- `control.power` runs `off` (domain `shutoff`, System still `ready`), `on`, `reset` and
  `cycle` (each a new boot), then `resume` from a paused restore.
- Rejection cells aim at the stack's torn-down lane target. Beside the project rows, they
  compare the target's `jobs` rows and check that its domain is still undefined.

The ppc64le local cells share the node but are owner #2818's; the remote cells are #3116's.

Last run: candidate `924de2c59` (server, worker and reconciler at that SHA in both
configurations), a disposable Fedora 44 x86_64 KVM lab guest with SELinux enforcing and no
provider authority installed, the `longterm` fixture `v6.18.54` built on that host. Each
configuration recorded its 50 cells in about 28 minutes and skipped the other's 50. `qualify`
reported all 100 qualified: 20 `success` and 80 `rejection`. After `demo-down.sh --wipe --yes`
the worker's libvirt defined no domain, no overlay disk remained and `KDIVE_INSTALL_STAGING` was
empty.

#### Capture and introspection tool cells (#2814)

`tests/integration/test_capture_tool_cells_live.py::test_capture_tool_cell` carries the 164
x86_64 local-libvirt cells of `control.force_crash`, `vmcore.fetch`, `introspect.from_vmcore`,
`introspect.run`, `introspect.script` and `postmortem.crash`: 28 `functional`, four
`unsupported` and 132 rejection cells, four of them `capability`. `vmcore.fetch` is proven for
`kdump` and `host_dump`; x86_64 refuses `fadump`
([ADR-0757](../../adr/0757-system-capture-method-architecture.md)), so its `unsupported` cell
reads the System's `supported_capture_methods` and its `capability` cell proves the
`capability_unsupported` refusal from a crashed, Run-bound System
([ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md), amended
2026-10-13). The [design](../../workflow/specs/2026-10-13-local-capture-tool-cells-design.md)
lists what each cell compares. The prerequisites are the power and snapshot cells' above, plus
crash(8) and drgn in the MCP server's environment (`postmortem.crash` and
`introspect.from_vmcore` run there; installing them only for the worker is not enough) and the
libguestfs Python binding for the worker's kdump harvest. A tool answering `missing_dependency`
records the cell `blocked`. Run both configurations into one evidence
root, then assemble and qualify as in the System section:

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix KDIVE_ARTIFACT_DIR=$(mktemp -d)
export KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
examples/local-libvirt/demo-up.sh                                     # default
uv run python -m pytest -m live_stack tests/integration/test_capture_tool_cells_live.py
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery
uv run python -m pytest -m live_stack tests/integration/test_capture_tool_cells_live.py
```

- Each functional cell provisions `fedora-kdive-ready-44` with its crash options, boots the
  fixture kernel through the deep-lifecycle checks (which load `loop`) and starts a sentinel
  process named `kcov<hex>`. `kdump` cells add `crashkernel` and wait for
  `/sys/kernel/kexec_crash_loaded`; `host_dump` cells set `debug.preserve_on_crash` instead, so
  the crashed domain stays up for the memory dump.
- `control.force_crash` (admin) drains to `succeeded`, reaches `crashed`, and shows the crash in
  new console parts of the same generation. Those parts hold either the fixture kernel's panic
  with its release on the `CPU:` record (`crash_evidence: console_panic`), or the kdump crash
  kernel's `Linux version <release>` banner whose `Command line:` carries `elfcorehdr=`
  (`crash_evidence: crash_kernel`). A kdump-armed guest normally shows only the second form,
  because the crashing kernel enters the crash kernel before its panic reaches the serial
  console (#3254). The panic text is then only in the dump. That applies outside the harness
  too: console panic matching cannot see a crash on a kdump-armed guest. Check the System's
  `crashed` state or the captured core instead. `vmcore.fetch` drains the capture, checks the
  job, Run and artifact refs, downloads the raw core twice through `artifacts.fetch_raw`
  (identical bytes, advertised size) and requires its VMCOREINFO to name the fixture build ID
  and release.
- `introspect.from_vmcore` and `postmortem.crash` run over a kdump core: the fixture release,
  the guest's node name, `loop` and, for crash(8), the sentinel. `introspect.run` and
  `introspect.script` use a drgn-live session, which they end: the helpers match the guest's
  release, node name and `loop`, and the script finds exactly the sentinel by pid and name.
- Rejection cells aim at one target per stack: a torn-down fixture System with the
  `force_crash` opt-in, its Run and an ended drgn-live session. They compare the project rows,
  the jobs naming the System or Run, and whether its domain is defined.
- Captured cores are Run-owned `vmcore` artifacts that nothing reclaims: a run leaves about
  8 GiB of `host_dump` cores per configuration (2 GiB guests) and a few hundred MiB of kdump
  cores in the object store. `demo-down.sh --wipe --yes` removes them with the store.

The ppc64le local cells share the node but are owner #2818's; the remote cells are #3114's.

Last run: candidate `f9a74e45c` (server, worker and reconciler at that SHA in both
configurations), a disposable Fedora 44 x86_64 KVM lab guest with SELinux enforcing, the
`longterm` fixture `v6.18.54` built on that host. Each configuration recorded its 82 cells in
about 31 minutes and skipped the other's 82. `qualify` reported 144 of the 164 cells qualified:
132 `rejection` (including the four fadump `capability` refusals), 8 `success` (`introspect.run`
and `introspect.script`) and 4 `unsupported`. Twenty did not qualify:

- 16 kdump-based functional cells (`control.force_crash`, `vmcore.fetch` `kdump`,
  `introspect.from_vmcore`, `postmortem.crash`) are `blocked`: the fixture kernel lacks
  `CONFIG_SQUASHFS`, so the guest's `kdump.service` cannot build its initramfs (#3219).
- 4 `vmcore.fetch` `host_dump` functional cells are `failure`: the live libvirt daemon cannot
  write the core into the worker's private spool directory (#3220).

After `demo-down.sh --wipe --yes` the worker's libvirt defined no `kdive-` domain and
`KDIVE_INSTALL_STAGING` was empty.

#### Traffic capture, SysRq and crash-watch tool cells (#3115)

`tests/integration/test_control_tool_cells_live.py::test_control_tool_cell` carries the 60
x86_64 local-libvirt cells of `control.capture_traffic`, `control.diagnostic_sysrq` and
`control.watch_for_crash`: 12 `functional` and 48 rejection cells. local-libvirt supports all
three, so they have no `capability` cell
([ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md), amended
2026-10-13). The [design](../../workflow/specs/2026-10-14-local-control-tool-cells-design.md)
lists what each cell compares. The prerequisites are the capture cells' above: the rejection
cells share their target, which opens a drgn-live session. Run both configurations into one
evidence root, then assemble and qualify as in the System section:

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix KDIVE_ARTIFACT_DIR=$(mktemp -d)
export KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
examples/local-libvirt/demo-up.sh                                     # default
uv run python -m pytest -m live_stack tests/integration/test_control_tool_cells_live.py
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery
uv run python -m pytest -m live_stack tests/integration/test_control_tool_cells_live.py
```

- Each functional cell provisions `fedora-kdive-ready-44`, boots the fixture kernel through the
  deep-lifecycle checks and then calls only its tool through the cell's exposure.
- `control.capture_traffic` captures for 15 s while the guest sends UDP datagrams carrying a
  fresh token to `10.0.2.2:9`. The pcap downloaded through `artifacts.fetch_raw` must hold at
  least five of them, and the domain's QOM tree no `kdive-dump-` filter afterwards. The cleanup
  proves `/var/lib/kdive/pcap/<system>` gone; the stored pcap is Run-owned and retained.
- `control.diagnostic_sysrq` sets `kernel.sysrq=1` and console log level 8 in the guest, then
  runs `show_memory`. The captured console must hold `sysrq: Show Memory` followed by
  `Mem-Info:`, and the guest's `dmesg` one more header than before.
- `control.watch_for_crash` runs a 20 s watch that must end `not_fired` within 30 s, then a
  180 s watch that must fire on `Kernel panic - not syncing: <token>` lines the guest writes to
  `/dev/kmsg` every 2 s. The written signature leaves the System `ready`; no watch job is left
  queued or running.
- Rejection cells aim at the capture cells' torn-down, Run-bound target. The System tools answer
  a foreign target `configuration_error`, the Run-resolved `capture_traffic` `not_found`.

The ppc64le local cells share the node but are owner #2818's; the remote cells are #3116's.

Last run: candidate `cf8275e80` (server, worker and reconciler at that SHA in both
configurations), a disposable Fedora 44 x86_64 KVM lab guest with SELinux enforcing, the
`longterm` fixture `v6.18.54` built on that host. Each configuration recorded its 30 cells in
about 33 minutes and skipped the other's 30. `qualify` reported 56 of the 60 cells qualified:
48 `rejection` and 8 `success` (`control.diagnostic_sysrq` and `control.watch_for_crash`). The
4 `control.capture_traffic` functional cells are `failure`: the worker rejects its own
capture spool directory, which inherits the build workspace's setgid bit, and the failed job
stays `running` until the cell's drain deadline (#3225). After `demo-down.sh --wipe --yes` the
worker's libvirt defined no `kdive-` domain and `KDIVE_INSTALL_STAGING` was empty.

#### GDB debug tool cells (#2813)

`tests/integration/test_debug_tool_cells_live.py::test_debug_tool_cell` carries the 440 x86_64
local-libvirt cells of the 21 `debug.*` tools: 88 `functional` (`debug.start_session` as both
`gdbstub` and `drgn-live`) and 352 rejection cells. Local-libvirt supports both transports, so
no `capability` cell exists. The `debug.list_sessions` `authorization` and `project-isolation`
cells prove denied visibility against a viewer who sees the target session
([ADR-0756](../../adr/0756-filtered-list-boundary-evidence.md), amended 2026-10-14). The
[design](../../workflow/specs/2026-10-14-local-debug-tool-cells-design.md) lists what each cell
compares. The prerequisites are the capture cells' above (drgn for the `drgn-live` variant)
plus `gdb` on the MCP server's PATH, which runs the debugger beside QEMU's loopback gdbstub,
and `objdump` on the test host, which decodes the fixture `vmlinux` for the `debug.advance` and
`debug.disassemble` cells; either missing records the cell `blocked`. Each attach stages a copy
of the Run's `vmlinux` (about 437 MB for `longterm`) in the server's temp directory, and ending
the session removes it (#3229), so that directory needs room for one copy per live session.
Run both configurations into one evidence root:

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix KDIVE_ARTIFACT_DIR=$(mktemp -d)
export KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
examples/local-libvirt/demo-up.sh                                     # default
uv run python -m pytest -m live_stack tests/integration/test_debug_tool_cells_live.py
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery
uv run python -m pytest -m live_stack tests/integration/test_debug_tool_cells_live.py
```

- Each functional cell provisions `fedora-kdive-ready-44` with `debug.gdbstub` (no
  `crashkernel`, so the boot carries `nokaslr`) and boots the fixture kernel through the
  deep-lifecycle checks, which upload its `vmlinux` and load `loop`. Before any attach halts the
  guest, it reads `/proc/version`, `/proc/kallsyms` and `loop`'s `.text` base over SSH, and
  requires `vfs_read` at its fixture `System.map` address.
- A `vfs_read` breakpoint stop is the controlled workload: an SSH client banner written while the
  guest resumes makes guest `sshd` call `read(2)`. Addresses, registers, frames, disassembly,
  the `jiffies_64` watchpoint, `linux_banner` bytes, `loop`'s base and a `loop` function's
  relocated address are compared with the guest's own values, and instruction steps and
  disassembly with the fixture binary's instruction boundaries; session cells compare the
  session rows and prove the guest halts while attached and answers again after the detach.
- The `debug.read_frame` cell requires the stop at `vfs_read`'s entry and compares frame 0's
  arguments with its v6.18 signature (`file`, `buf`, `count`, `pos`) and their values with the
  `rdi`, `rsi`, `rdx` and `rcx` registers, plus a user-half `buf` and a `ret` local (ADR-0781).
  gdb's `<name>@entry` rows are set aside for the name check and must equal the same register.
  The `debug.list_breakpoints` cell compares each listed row's
  number, kind, function and address with the installed breakpoints (ADR-0780).
- The `debug.interrupt` cell leaves the guest running with `debug.continue` `wait=false`
  (ADR-0783), requires `halted_by=interrupt` with `signal-received`, records which path the
  `vfs_read` breakpoint stop took (`resume_reply` or `waiting_continue`), and ends the session
  while the guest runs, requiring it to answer again.
- Rejection cells aim at one target per stack: a torn-down fixture System, its Run and an ended
  gdbstub session. They compare the project rows (including its debug sessions), the jobs naming
  the System or Run, and whether its domain is defined.

The ppc64le local cells share the node but are owner #2818's; the remote cells are #3113's.

Last run: candidate `e5215fc6d` (server, worker and reconciler at that SHA in both
configurations), a disposable Fedora 44 x86_64 KVM lab guest with SELinux enforcing, the
`longterm` fixture `v6.18.54` built on that host. Each configuration ran on a freshly wiped stack
and recorded its 220 cells in about 80 minutes, skipping the other's 220. A first default run
filled the guest's 16 GB quota-limited `/tmp` with leaked debuginfo staging (#3229) and failed 16
cells with `Disk quota exceeded`. Both reported configurations therefore ran with the server's
`TMPDIR` on disk (operator decision, 2026-10-15), and the leaked directories were deleted
before each run. `qualify` reported 420 of the 440 cells qualified: all 352 `rejection` and 68
`success`. Twenty are `failure`:

- 12 known gaps, each after its observable proof: `debug.list_breakpoints` (count only),
  `debug.read_frame` (no arguments or locals) and `debug.interrupt` (the started session's
  guest was already halted), four each.
- 8 `debug.clear_breakpoint` and `debug.clear_watchpoint` cells: their 5 s continue timed out
  and the engine's interrupt dropped the stop reported in its own reply, answering
  `transport_stall` (#3230).

After `demo-down.sh --wipe --yes` neither libvirt defined a `kdive-` domain,
`KDIVE_INSTALL_STAGING` was empty, and no staging directory remained in either temp directory.

A later rerun at the #3229 candidate `9697b4caa` ran 10 `gdbstub` `default`/`direct` functional
cells (`start_session`, `end_session`, `load_module_symbols`, `list_modules`, `read_memory`,
`read_registers`, `resolve_symbol`, `set_breakpoint`, `backtrace`, `disassemble`) with the
server's `TMPDIR` on a tmpfs whose per-user quota was 1200 MB, below three staged copies. All 10
recorded `success`; 11 staging directories came and went, never more than one at a time, and
the temp directory was empty after the cells and after `demo-down.sh --wipe --yes`.

A rerun at the #3242 candidate `424f2fb40` ran the four `debug.list_breakpoints` `gdbstub`
functional cells (`default` and `recovery`, each `direct` and `gateway`) on the same kind of
guest, with the server's `TMPDIR` on the same 1200 MB quota tmpfs; the `recovery` configuration
came up after a plain `demo-down.sh` with `KDIVE_WORKER_DEATH_VERIFIER=docker`. All four recorded
`success`, each listing its `vfs_read` and `vfs_write` breakpoints by the numbers they were set
with, as `breakpoint` rows whose addresses fall inside those functions in `/proc/kallsyms`, and
only the `vfs_write` row after the first was cleared. After `demo-down.sh --wipe --yes` no
`kdive-` domain remained and the temp directory held no staging directory.

#### Break-glass provider tool cells (#3112)

`tests/integration/test_breakglass_tool_cells_live.py::test_breakglass_tool_cell` carries the 136
x86_64 cells of `ops.force_release`, `ops.force_teardown`, `ops.resolve_recovery_orphan` and
`systems.resolve_external_boot_conflict` on both lanes: 68 local-libvirt cells, and 68
remote-libvirt cells on the provider lane of the
[remote runbook](remote-live-stack.md#8-remote-system-tool-cells-3080). The
[design](../../workflow/specs/2026-10-07-breakglass-tool-cells-design.md) lists what each cell
compares. `ops.force_teardown` alone does not require the provider `authority` role
([ADR-0788](../../adr/0788-authority-tool-cells-bind-the-serving-authority.md) amends the
2026-10-07 operator decision for `ops.force_release`).

- An `ops.force_teardown` functional cell provisions the lane image in a fresh `cov-<hex>`
  project. A platform admin who holds no role in that project then tears it down through the
  cell's exposure. The cell proves the System `torn_down`, the domain undefined and its disks
  gone on the provider. It also proves the provider's other `kdive-` domains and the lane's
  staged base untouched, and one `platform_audit_log` row for the call. The frame then releases
  the allocation and proves capacity returned.
- The `ops.force_release`, `ops.resolve_recovery_orphan` and
  `systems.resolve_external_boot_conflict` functional cells run only on the installed authority
  lanes ([authority functional tool cells](#authority-functional-tool-cells-3186)). On the
  demo-up lane they stop `blocked`. A blocked remote cell first probes the provider host, so its
  record carries that host.
- A rejection cell aims at the stack's System target for its provider: a torn-down System and its
  released allocation.
  - The three `ops.*` tools refuse a `platform_operator` with `authorization_denied` before they
    resolve the object.
  - `systems.resolve_external_boot_conflict` refuses a contributor of the target's project. It
    answers another project's System with `not_found`, identical to its answer for an absent
    System.
  - A validation cell mistypes the id.

Run the cells on a stack prepared as for the System cells above, with the remote prerequisites
of the remote runbook's §8. The provider host serves only this run while it lasts: another
stack's remote cells would change its domain set during a teardown cell.

```bash
sha=$(git rev-parse HEAD)
export REMOTE_PROVIDER_SSH=<user>@<provider-host> KDIVE_SYSTEMS_TOML=<the stack's systems.toml>
uv run python -m tests.integration.live_stack.tool_cells bindings --remote --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)        # one evidence root for both configurations
examples/local-libvirt/demo-up.sh             # default configuration
uv run python -m pytest -m live_stack tests/integration/test_breakglass_tool_cells_live.py
examples/local-libvirt/demo-down.sh --wipe --yes
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery configuration
uv run python -m pytest -m live_stack tests/integration/test_breakglass_tool_cells_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

On the demo-up lane, each configuration records 68 cells (4 `success`, 52 `rejection` and 12
`blocked`) and skips the
other configuration's 68. `qualify` then qualifies 112 of the 136 and lists the 24 blocked cells,
so it exits non-zero by design. `demo-down.sh --wipe --yes` clears the stack and the worker's
libvirt, but it does not reach the provider host. So before each wipe, including the one between
the configurations, check the provider: `virsh list --all` there must show no `kdive-` domain,
and the pool must hold no `kdive-` overlay volume. A cell killed mid-run can leave both. While
the stack is still up, release the leftover's allocation as §7 of the remote runbook says, and
let its teardown finish. Anything still on the provider after that, or after a wipe, is removed by
hand: `virsh destroy` and `virsh undefine` on the domain, and `virsh vol-delete --pool <pool>` on
its overlay volume.

Last run: candidate `967755fa0` (server, worker and reconciler at that SHA in both configurations;
later commits change only this runbook). The control plane was a disposable Fedora 44 x86_64 lab
guest with SELinux enforcing. The provider was a separate disposable Rocky Linux 10.2 x86_64 KVM
guest, prepared with `site.yml` (a fresh PKI from `playbooks/pki.yml`) and staging only
`fedora-kdive-remote-base-43`, with no provider authority on either host. One provider-host step
existed only because of an open defect and is not product coverage: firewalld was installed and
enabled before `site.yml` (#3083). Each configuration ran on a freshly wiped stack and recorded
its 68 cells in about three and a half minutes: 4 `success`, 52 `rejection` and 12 `blocked`.
`qualify` reported 112 of the 136 qualified: the 8 `ops.force_teardown` functional cells and all
104 rejection cells. The 24 blocked functional cells were blocked as designed. Their `qualify`
rows also list input-context and digest mismatches, because they stop before any guest is
observed, and the resolve rows list the missing `authority` role. On these hosts the provider's
other-domain set was empty, so the teardown cells' proof that unrelated resources stay rests on
the staged base surviving. Remote records carry the provider host as `rocky:10.2`. No record or
artifact carried a host name or address. After the run, neither the worker's libvirt nor the
provider defined a `kdive-` domain, and the provider pool held only its base volume.

#### Authority-owned System frame (#3184)

`tests/integration/live_stack/authority_lane.py` frames a System on the installed local-authority
lane for the HTTP tool cells that need one. A carrier selects it explicitly through
`on_authority_system`; the ordinary `on_lane_system` and `lane_target` frames do not change. The
frame observes the System on the authority's private daemon and private roots, never over SSH.
A `remote-libvirt` cell runs the remote branch below.

`LOCAL_AUTHORITY_LANE_CONFIG` names an owner-only mode-`0400` or mode-`0600` JSON file
with exactly `installed_revision` (the full 40-character SHA), `authority_service` (the literal
`kdive-external-boot-authority.service`) and `system_mode`. An unset variable stops the cell
`blocked`. `system_mode: "public"` stops `blocked` before any mutation: public authority-owned
provisioning is blocked by #3144 and #3148, and an attempt strands its allocation.
`system_mode: "fixture"` is the runnable path:

1. The frame checks the authority unit, the installed revision, the `/readyz` revision of the
   server and every running fixed worker slot, the installed routes and the harness DSN's read
   access, then lists the authority daemon's domains, the two private roots' entries and the
   worker daemon's domains.
2. It acquires the lane image, requests an allocation and provisions a System over HTTP with the
   fixed rootfs `/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2` pinned to that image's
   registered digest. Stage the lane image's bytes there first, as for the installed carrier.
   The allocated resource must carry no `authority_instance` in the lane's `systems.toml`;
   otherwise the frame releases the allocation and stops `blocked`. Export
   `KDIVE_SYSTEMS_TOML` naming the exact file the running server and fixed workers were started
   with: the frame checks routing in its own process, the test session sandboxes the XDG default,
   and the frame stops `blocked` without the variable. It also stops `blocked`, before any
   allocation, when that file fails to load or routes every `[[local_libvirt]]` entry through an
   `authority_instance`.
3. It moves the System onto the private daemon with `provision-authority-fixture.py`, runs the
   confinement probes and the fixture's read-only verification, then runs the cell body.
4. Cleanup drains every open job of the System and its Runs, closes the Investigations the body
   recorded (a body releases every external boot it starts itself; a held activation fails the
   cleanup), removes the five fixture names, tears the System down, releases the allocation, and
   requires the step-1 listing and the capacity in use to be unchanged. A cleanup failure fails
   the cell.

**Fixture-mode inventory.** A lane has one local-libvirt resource: resource rows are one per
worker libvirt endpoint, so a plain `[[local_libvirt]]` entry cannot sit beside the routed one.
Keep the lane's routed inventory file unchanged and make a separate copy in which only the
`[[local_libvirt]]` entry's `authority_instance` line is removed. Place the copy beside the
routed file with the same ownership and mode: the fixed workers read it, and a start refuses an
inventory a worker slot cannot read. The `[[remote_libvirt]]`
authority tuple stays the same in both, and the copy also declares the registered lane image.
Bring the whole stack up on the copy (`KDIVE_SYSTEMS_TOML` naming it for `stack-services.sh`),
and export the same path to the frame. Until #3144 and #3148 land, this copy is the lane's
working inventory. To switch back, run `stack-services.sh` again with the routed file, and only
when no authority-owned System, job or recovery is open on the lane.

Use the harness DSN (`KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`). Run one authority
scenario at a time on the lane; another scenario's domains change the listing and fail the frame.
The frame's own proof runs with:

```sh
KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" \
KDIVE_SYSTEMS_TOML=~/.config/kdive/systems.toml \
LOCAL_AUTHORITY_LANE_CONFIG=/protected/authority-lane.json \
  uv run python -m pytest tests/integration/test_authority_lane_frame_live.py -m live_stack -v
```

A killed run skips cleanup. Remove its fixture with the exact-name loop under
[fixture cleanup](#installed-local-authority-carrier), then run `systems.teardown` for the System
with an `admin` token for its project and wait for `torn_down`, then `allocations.release`.

##### Local inventory observer prerequisite (#3237)

The local frame requires the proof-only collector from [ADR-0800](../../adr/0800-proof-authority-inventory-collector.md).
Enable `live_vm_host_authority_inventory_observer_enabled` or, for the production host role,
`provider_authority_host_inventory_observer_enabled` in the approved local-lane inputs.
Both default to false and require local authority mutation support and the fixed private roots.
The provider role's configurable authority interpreter does **not** satisfy the collector
prerequisite: `/usr/bin/python3` must separately be immutable system Python 3.14.
Provisioning refuses an older or mutable interpreter; arrange the documented host prerequisites
before retrying. No alternate privileged interpreter or root inventory fallback is used.

Both roles install the same locked, noneditable runtime under `/opt/kdive-authority-inventory`,
with root-owned code/imports, a root-owned systemd listener and the locked
`kdive-authority-observer` identity with no supplementary groups or sudo grant. This adds one
runtime copy and its native build/storage cost, including existing POWER build prerequisites.
The observer can connect only to `/run/kdive-authority-inventory/inventory.sock`; private roots
remain authority-owned mode `0700`. The fixed collector runs as the trusted authority identity.
Its read-only libvirt call is defense in depth; it does not make a compromised authority safe.
The installed collector revision must match the frame's configured candidate before allocation.

Each connection accepts EOF after LF or one canonical UUID plus LF (at most 37 bytes).
A complete response is limited to 1,048,576 UTF-8 bytes; excess fails without truncation.
The outer call and client each have a 120-second monotonic deadline from invocation/connect
preparation, and each accepted service has a 120-second systemd lifetime including backend calls,
with one-second stop escalation. The listener admits four active handlers and backlog 16.
A timeout, refused connection, incomplete facts or oversize response fails the observation and
cleanup evidence. Repair the prerequisite/backend or serialize calls, then rerun the proof;
there is no retry, cache or reduced inventory fallback.

Before changing collector bytes the roles withdraw its socket and wait for all instances to stop.
Readiness failures leave it disabled. Disabling its opt-in withdraws its listener, units and
root-owned runtime/client, preserving the locked account and all authority/guest state. Disable
before rolling back to source without the collector. Re-running unchanged provisioning must
retain the same grants. `authority_host_teardown.yml` withdraws the boundary the same way before it retires the authority.
A connection never starts the authority: while the authority is stopped or failed, each
accepted instance fails instead. The listener stops with the authority, so after an
out-of-band authority stop and start, re-run the role to restore it.
Offline tests and unit-file inspection do not prove these permissions.

**Native proof remains a separately approved operation.** Review an execution packet naming the
exact candidate, one existing idle local lane, native architecture, runtime/install budget and
negative request bytes before running any host commands. Neither these instructions nor a green
offline suite grants host access. Bind caller, collector scripts/imports/revision, authority and
stack to that candidate, and check the host OS/Python/systemd/libvirt prerequisites first.
Within that approved window, record private commands, exits, durations and hashes for:

1. Zero-selection and one existing selected-System observation, compared with independent
   trusted queries in the same quiescent state: complete domain set, both root entry sets and
   active/type facts. No guest creation, start or deletion is needed.
2. Observer denial of direct private RW-socket connection, private authority runtime traversal,
   and `O_RDONLY` opening of representative rootfs/console, journal, credential and proof-control
   files, without reading bytes. `sudo -n -u root -- /usr/bin/true` and
   `sudo -n -u kdive-provider-authority -- /usr/bin/true` must fail as the observer.
3. Literal `destroy UUID\n` and overlong bytes sent only to the collector, yielding EOF/failure
   and fixed `request-rejected` evidence, with unchanged before/after inventory. Never send a
   mutation operation to libvirt. Source/sentinel tests establish the rejection order;
   journal evidence is not an independent syscall trace.
4. Unchanged reconvergence, then approved collector-only disable/re-enable: compare grants,
   prove endpoint withdrawal and restored inventory without changing private permissions.

Report which native arm actually ran. Native x86_64 evidence does not qualify ppc64le; retain
both targets and keep #3185/#3186 fault matrices outside this bounded procedure. Keep host
identifiers and private inventory names out of public summaries.

##### Remote authority branch (#3209)

A `remote-libvirt` cell provisions on the installed remote authority instead. There is no fixture
path: the frame uses public provisioning on the remote resource that routes to the authority.

- `KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG` names the existing remote proof config
  ([its contract](#installed-remote-authority-proof-prerequisite)). The frame replaces its
  `ownership_prefix` with one minted per invocation.
- `resource_name` must be declared in the `KDIVE_SYSTEMS_TOML` inventory with
  `authority_instance` equal to the config's. The control-plane catalog must hold exactly one
  registered public `remote-libvirt` image for the cell's architecture with a digest and an
  inspected root (`root_spec`); admission refuses an authority-owned System without one. Otherwise
  the cell stops `blocked` before any mutation.
- Obtain these catalog facts by declaring/reconciling a public `remote-libvirt` image with
  `source.kind = "staged"` and a **fresh** volume name, then running
  `python -m kdive stage-volume --image IMAGE --arch ARCH --from /path/to/base.qcow2`
  against the lane's configured database and single remote instance. Install `virt-inspector`
  for mandatory root inspection; optional config capture uses `guestfish`. Staging copies one
  qcow2 into a private temporary directory, rejects backing/external-data images, hashes and
  inspects that snapshot, uploads those exact bytes, then commits its digest and `root_spec`
  (ADR-0776).
  Inspection or root-metadata persistence failure fails the command. Existing remote volumes
  are refused; no overwrite/delete is implied. If metadata fails after upload, inspect and
  manually clean up the unverified residue or declare a fresh target before retrying.
  Kernel-config capture/attachment is advisory. Reconcile preserves inspected facts only for
  the unchanged staged volume declaration; repointing invalidates them. Plain staged images
  remain unverified and eligible only for their existing disk/GRUB path.
  These facts attest stage-time bytes, not subsequent remote replacement. Separately install
  the **same digest-pinned base** into the authority's private store using the existing
  [authority base contract](#installed-remote-authority-proof-prerequisite); its manifest root
  identity, private-base digest and ongoing file-identity checks remain mandatory (ADR-0623).
- The operator's batch-mode SSH to `ssh_target` and passwordless `sudo -n -u
  kdive-provider-authority` there are required. As that account, never root, the frame runs one
  fixed program with the authority's interpreter. The program reads
  the pool name from `/etc/kdive/provider-authority.env`, then lists the private daemon's domains
  and that pool's volumes.

The frame gates on the remote unit and its installed revision, the control plane's `/readyz`
revisions and the harness DSN's read access. It then provisions, requires the System's domain to
be running on the private daemon, and runs the body. Cleanup drains the jobs, tears the System down
through the authority, and requires no domain and no pool volume naming the System. It then
releases the allocation and requires the daemon's domains and the pool's volumes unchanged. Add
the config variable to the command above to run the remote arm; run one scenario at a time on the
remote authority.

A killed remote run skips cleanup. Run `systems.teardown` and wait for `torn_down`, then
`allocations.release`. If a `kdive-<system-id>` domain, or a volume whose name contains the System
id (overlays and `kdive-module-<system-id>-*` preparations), is still on the authority's private
daemon, remove it as the authority account, never root (`sudo -n -u kdive-provider-authority
virsh -c` on the authority host), through
`qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock`:
`virsh destroy`, then `virsh undefine` the domain, then `virsh vol-delete --pool <pool>` the volume.

##### Authority recovery faults (#3185)

`tests/integration/test_authority_faults_live.py` runs fault arms inside this frame. Each arm binds
its fault to the frame's System, the Run it activates and one authority operation, and reads the
job, journal head, quarantine rows and the authority's own daemon or receipt before it calls a
recovery tool. A platform admin with no role in the frame's project calls the tools. Enable the
barrier on each authority (above) and add `"barrier_socket":
"/run/kdive/provider-authority/proof-control/control.sock"` to the local lane config. Export
`KDIVE_KERNEL_SRC` naming a pre-built x86_64 tree: the arms upload it without a `vmlinux` and build
no kernel.

A remote arm also uploads an initrd, because remote external boot refuses a build without one
(`remote_external_boot_initrd_required`). The arm builds it on the test client with `dracut`,
which Ubuntu 26.04, Fedora and Rocky install by default. It runs unprivileged and skips
`/etc/dracut.conf` and `/etc/dracut.conf.d`. The distro's `/usr/lib/dracut/dracut.conf.d` drop-ins
still apply and can add dracut modules or files; no dracut option turns them off.
The tree's `.config` must also set `CONFIG_BLK_DEV_INITRD=y` and `CONFIG_RD_GZIP=y`. The initrd
holds userspace only (it resolves `root=UUID=`), so the tree's virtio and root-filesystem drivers
must be built in, as every spine already requires. To build the same initrd by hand, with
`<scratch>` a new directory and `<scratch>/dracut.conf.d` an empty one:

```bash
dracut --conf /dev/null --confdir <scratch>/dracut.conf.d --no-kernel --no-hostonly \
  --no-hostonly-cmdline --no-early-microcode \
  --kver "$(cat "$KDIVE_KERNEL_SRC/include/config/kernel.release")" \
  --modules "base rootfs-block udev-rules systemd systemd-initrd dracut-systemd fs-lib" \
  --gzip --force --tmpdir <scratch> <scratch>/initrd
```

Without root, dracut logs `ldconfig might need uid=0 (root) for chroot()` and still exits 0. The
image then carries no `ld.so.cache`, and glibc's built-in search path finds the libraries.

- **Held teardown (local and remote).** The arm activates and releases one external boot, arms
  `teardown`/`after-provider`, and calls `systems.teardown`. While the effect is held it requires
  the journal head `mutation-started`, the domain gone from the authority daemon, the allocation
  `active`, and `ops.force_release` refused with `external_boot_system_teardown_required`. After
  the barrier is released and the System is `torn_down`, `ops.force_release` must release. A
  remote activation that fails stops the arm `blocked` with the job's phase.
- **Recovery orphan (local).** The arm activates, arms `cleanup`/`before-finalize`, and calls
  `runs.release_external_boot`. While held it requires the head `terminal` for the cleanup, the
  private `cleanup-quarantine.json` present (a fixed read-only root program under
  `/var/lib/kdive/provider-authority/recovery`), and no quarantine row. It then aborts and requires
  exactly one new `quarantined` `recovery-record` row naming that receipt. The release job's next
  attempt finalizes and removes the receipt, so `ops.resolve_recovery_orphan` (`delete`) commits
  the disposition of an already-absent object; the arm requires that row `deleted` by that job and
  every other quarantine row unchanged. If the arm fails after the abort it resolves the rows it
  published before the frame tears down, and names any it could not as residue on the failure.
- **Recovery conflict (local and remote, #3327).** The arm activates, then appends one token to
  the System's persistent kernel command line on the authority daemon, as a host administrator
  would ([ADR-0805](../../adr/0805-authority-observed-conflict-is-recorded-from-its-journal-head.md)).
  Locally it runs as root through the receipt observer's interpreter; remotely it runs as the
  authority account. It then calls `runs.release_external_boot` and requires the job `failed`
  with category `conflict`, the journal head `terminal` for `recover` with outcome `conflict`, and
  the activation in `recovery_conflict`. The frame's project admin then calls
  `systems.resolve_external_boot_conflict` with the head's composite. The added token is a third
  identity the resolution acknowledges, so it restores the recorded source
  ([ADR-0805](../../adr/0805-authority-observed-conflict-is-recorded-from-its-journal-head.md),
  #3330): the arm requires that job to succeed, the journal head `terminal` for `resolve-conflict`
  with outcome `source`, and the activation `recovered`. The frame's teardown then runs from
  `recovered`. Teardown straight from an unresolved remote conflict is not covered: the remote
  provider refuses a definition that is neither the recorded source nor target
  ([ADR-0620](../../adr/0620-authority-owned-system-teardown.md)).
  The deletion of a still-present receipt by `ops.resolve_recovery_orphan` stays unproven, because
  a receipt survives to resolution only if the release job ends terminally, whose recovery is
  #3143.

Every hold waits at most 120 s and checks that the held job is still `running` at the same attempt
with a live lease before the fault, because the worker's authority deadline is five minutes per
attempt. No arm restarts the authority. A killed run can leave one arm, and the next arm then
fails with a stale-arm message: restart the authority once (`sudo systemctl restart` the unit),
which counts against its five starts in 600 s, then run the killed-run cleanup above. A killed
run can also leave an activated Run or a published quarantine row. Before the killed-run
`systems.teardown`, release the Run's external boot (`runs.release_external_boot` with an `admin`
token for the frame's project) and wait for its job, then resolve every `quarantined` row naming
the System with `ops.resolve_recovery_orphan` (`delete`) as a platform admin. Until #3321 and
#3322 are fixed on the lane those steps fail too; roll the lane back to its pre-run snapshot
instead. Add `tests/integration/test_authority_faults_live.py` to the command above in place of
the frame test.

##### Authority functional tool cells (#3186)

The 32 x86_64 functional cells of `runs.release_external_boot`, `ops.force_release`,
`ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict` (both
configurations, both exposures, both providers) run in this frame through
`tests/integration/live_stack/authority_cells.py`, from the ordinary carriers
`test_run_tool_cells_live.py` and `test_breakglass_tool_cells_live.py`. The
[design](../../workflow/specs/2026-10-21-authority-tool-cells-design.md) maps each cell.

- `runs.release_external_boot`: the cell activates the `longterm` fixture on the frame's System
  and a `contributor` of its project releases it through the cell's exposure. It requires the
  release job `succeeded`, the root-release completion evidence, the private cleanup receipt
  absent (local), the domain still defined, and the System's private rootfs entries (local) or
  pool volumes (remote) as they were before the activation.
- `ops.force_release`: the held-teardown arm above, called by an outside platform admin through
  the cell's exposure. After the refusal and the quiescent teardown it requires the allocation
  read `released` and the capacity in use lower than while the effect was held.
- `ops.resolve_recovery_orphan` (local): the recovery-orphan arm above, called the same way. The
  tool admits a local row when its publishing authority is the server's fixed route; the server
  composes no local recovery-object port.
- Remote `ops.resolve_recovery_orphan` runs the recovery-orphan arm and every
  `systems.resolve_external_boot_conflict` cell runs the recovery-conflict arm above, each on the
  frame's own client.
- A local record's `effect` discloses its fixture-mode System: provisioned through the worker
  and moved onto the authority by the fixture script, because of #3144/#3148. Every operation
  under test runs against the real authority.
- A remote cell records the provider host as its host and the bound fedora representative as
  its guest, so the remote authority lane's single registered remote image must be
  `fedora-kdive-remote-base-43` (the same digest-pinned base in its private store). It records the
  remote authority's revision as its `authority` role (ADR-0788).

Prerequisites, beyond the frame's: a verified `longterm` fixture under `KDIVE_FIXTURE_ROOT`
(as for the run cells), the barrier socket in the local lane config, and the remote proof
config. Point `REMOTE_PROVIDER_SSH` at the remote authority host, both for the bindings and for
the run. Run one configuration at a time, as for the other tool cells; restart the stack with
`KDIVE_WORKER_DEATH_VERIFIER=docker` for `recovery`:

```sh
sha=$(git rev-parse HEAD)
uv run python -m tests.integration.live_stack.tool_cells bindings --remote --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" \
LOCAL_AUTHORITY_LANE_CONFIG=/protected/authority-lane.json \
KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG=/protected/remote-authority.json \
  uv run python -m pytest -m live_stack -v -p no:randomly \
  tests/integration/test_run_tool_cells_live.py tests/integration/test_breakglass_tool_cells_live.py \
  -k "functional and (release_external_boot or force_release or resolve_recovery_orphan or resolve_external_boot_conflict)"
```

Then assemble and qualify as for the other tool cells, and report every one of the 32 cells as
qualified, `blocked`, `failure` or not-run.

### `live_vm` (native) — a real kernel on real silicon

```
just test-live         # -m "live_vm and not live_vm_tcg"
```

This needs a KVM/nested-virt host with libvirt, `drgn`, and a kdump-enabled
guest image, plus the per-family env above. Standing up the host reproducibly —
including the short session-runtime paths, the warm image store, and both boot families under
`qemu:///session` — is the
[self-hosted KVM runner runbook](self-hosted-kvm-runner.md); the ppc64le
(POWER) host prerequisites are in the
[cross-platform guide](../../development/cross-platform.md#native-power-host-integration).

The throwaway-domain proofs (snapshot, traffic capture, console inode, preserve-crash attach,
external-boot cmdline) and the local spine (`test_spine_over_the_wire`, the install cmdline sweep,
`test_spine_live_script_over_the_wire`, console parts) boot a guest of the host arch. They take
the machine type from `arch_traits`, so on a POWER host they run as ppc64le guests; on a host
arch kdive cannot provision they skip and name it. On POWER, the spine's `KDIVE_GUEST_IMAGE` must
be a ppc64le qcow2 and `KDIVE_KERNEL_SRC` a built ppc64le tree with `vmlinux` at its root. These
inputs are separate from the ppc64le drivers' `KDIVE_GUEST_IMAGE_PPC64LE` and
`KDIVE_PPC64LE_BUNDLE`. The ppc64le spine drivers in
`test_live_stack.py` run under KVM on a POWER host. `test_pinned_model_is_host_usable` skips
there, because it pins an x86-64-vN CPU rung. Still x86_64-only: the per-family SSH reachability spine
and the SUSE v7.0 kdump spine, whose preflight reads an x86 bzImage (on POWER they skip and name
the host arch, whatever the image env vars hold), and the gdbstub debug proofs under
`tests/mcp/debug` (#2695).

#### Native POWER spine kernel configuration

The spine's kernel preflight reads the tree's `.config` and stops before boot when a proof's
requirement is missing. The direct-boot image has an ext4 root on a virtio disk, so every proof
needs `CONFIG_VIRTIO_PCI=y`, `CONFIG_VIRTIO_BLK=y`, and `CONFIG_EXT4_FS=y`. The proofs that
reach the guest over SSH (console parts, first-boot host keys, and
`test_spine_live_script_over_the_wire`) also need `CONFIG_VIRTIO_NET`; build it in (`=y`), or
with `=m` make sure the guest loads the module before SSH starts, or the SSH banner exchange
times out. The proofs that capture a vmcore (`test_spine_over_the_wire`, the install cmdline
sweep, and `test_spine_live_script_over_the_wire`) need the kdump crash-capture symbols their
preflight names (`ppc64le_defconfig` already sets them). The preflight of
`test_spine_live_script_over_the_wire` also requires `CONFIG_DEBUG_INFO_DWARF4=y` or
`CONFIG_DEBUG_INFO_DWARF5=y` and uploads `vmlinux` with the build, which kdive stages in the guest
for drgn. It no longer requires `CONFIG_DEBUG_INFO_BTF`: no released drgn reads kernel BTF (#3121);
see
[live and host-side debug information](../external-build-upload.md#choosing-your-kernel-config).

The pinned debug fragment, `fixtures/kernel/debug.config`, sets the virtio, ext4 and debug-info
symbols with `VIRTIO_NET=y`, and `ppc64le_defconfig` supplies the kdump ones, so a ppc64le tree
from `scripts/kernel_fixtures.py build --arch ppc64le` passes the kernel preflight of every spine
proof listed above. BTF generation needs host `pahole`; the `local_worker_host` role installs the
package that provides it. The guest side is separate: without matching DWARF debug information
readable inside the guest, the live-script proof stops at `attach-drgn-live`, whose response
carries a `debuginfo_unloadable` warning, whatever the kernel config (#3121).

To validate all four crash-capture methods against such a host, see the
[four-method live run](four-method-live-run.md). Never hand-install a host
dependency for one of these: declare it in the owning Ansible role in the same
change, or the next clean runner reprovision breaks (see the cross-platform and
provisioning-parity notes in [AGENTS.md](../../../AGENTS.md)).

The scheduled native workflow first compares the checkout's lifecycle protocol identity with the
installed persistent-host contract. A mismatch stops before the native reaper or stack startup and
names runner reprovisioning as the remedy. Do not add a workflow install or `sudo` workaround:
Ansible owns that privileged lifecycle contract (ADR-0582).

External-boot authority operations are an opt-in provider path, not another live-test tier.
Read the [authority setup and diagnosis](self-hosted-kvm-runner.md#external-boot-authority-diagnosis)
for its local mutation enablement, readiness, journal, socket, and mutual-TLS prerequisites.
Use the applicable carrier below only after its authority and fixture contract is configured.

### `live_vm_remote` — direct provider ops against a remote `qemu+tls://` host

```
just test-live-remote  # -m live_vm_remote; a carrier skips cleanly with no remote env
```

The AppArmor overlay carrier is the first test in this family. It skips when the remote trigger is
unset; once configured, a failed SSH, privilege, AppArmor, boot, or cleanup operation is red.
The external-boot carrier provisions a fresh System overlay, boots the staged disk image, and then activates
the supplied kernel and initrd through the production remote-libvirt primitives. Immediately
after guest-agent readiness it reads `/proc/cmdline` and compares its exact bytes before invoking
the general kernel-identity observer. A mismatch reports the expected value, observed value, and
first differing byte offset. It also proves preservation of the disk, network, guest-agent,
console, gdbstub, and capture definition components, reconnects to simulate worker-handle loss,
restores disk/GRUB boot twice to prove idempotence, and removes its domain and volumes.

Call `require_live_vm_remote()` **inside** the test function, as the other families
do. A gate at module level (`pytest.skip(..., allow_module_level=True)`) also yields
exit 5, so an absent remote environment would fail this recipe instead of skipping
cleanly; gated inside the test, it is a reported skip and exit 0.

This drives the remote-libvirt family (a sub-selection of `live_vm`, also run by
`just test-live`) directly against an operator-provided `qemu+tls://` host. Set
`KDIVE_LIVE_VM_REMOTE_URI` to the host's control URI, `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE`
to the staged base-image volume name, `KDIVE_LIVE_VM_REMOTE_KERNEL` and
`KDIVE_LIVE_VM_REMOTE_INITRD` to the matching local boot artifacts,
`KDIVE_LIVE_VM_REMOTE_ROOT_DEVICE` to the base image's root block device, and
`KDIVE_LIVE_VM_REMOTE_GDB_ADDR` to the remote host's ACL'd GDB listen address. Set
`KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET`
to the **guest-routable** object store, and `KDIVE_LIVE_VM_REMOTE_RECONCILER` to a
presence marker for a running reconciler (its metrics endpoint, or `1`). The AppArmor carrier
additionally uses `KDIVE_LIVE_VM_REMOTE_SSH` as the noninteractive SSH
destination for the same host. It skips when that carrier-specific variable is unset. Once set,
the account must have key-based SSH and passwordless `sudo` for the fixed proof script; failures
are red. The proof creates only unique scratch volumes beside the staged base, inspects the
generated AppArmor profile, and removes its own artifacts; it never modifies the base.
up the host — mutual TLS, the staged base volume, the gdbstub-port ACL, and
object-store reachability — is the [remote live-stack runbook](remote-live-stack.md).
The URI must be `qemu+tls://` and must not carry `no_verify` (remote mandates
verified mutual TLS, ADR-0076); a wrong scheme or a missing companion fails loud
rather than skipping.

Provision the host from the repository Ansible roles before the first run; do not repair a live
host by hand. Confirm that the configured storage pool contains the named base image and that its
guest has qemu-guest-agent enabled. Run `just test-live-remote` from the exact checkout under test.
An absent URI skips the carrier; a set URI with any missing or malformed companion fails before
libvirt is contacted. After either success or failure, confirm that no `kdive-<uuid>` domain,
overlay, kernel volume, or initrd volume from the invocation remains.

#### Supplied ROOTFS carrier

ADR-0440's worker-local ROOTFS flow has its own narrow gate. Set
`KDIVE_LIVE_VM_REMOTE_ROOTFS` to an absolute local path to a bootable x86_64 qcow2 with
qemu-guest-agent enabled, plus the existing remote URI, base-image volume, and GDB address
variables above. The source is read on the test worker and streamed over libvirt; it need not
exist on the remote host. The operator-staged comparison volume must already exist in the
`default` pool and support disk/GRUB boot with qemu-guest-agent. The repository's
`ubuntu-2404-kdive-remote-base` image definition supplies that prerequisite.
No kernel/initrd, object store, reconciler, SSH account, or running KDIVE stack is needed by
this carrier. The URI must retain verified mutual TLS; `no_verify` and `tls_priority` are
rejected. Existing libvirt client credentials, including an operator-configured `pkipath`,
are used.

Run just this carrier from the checkout under test:

```sh
uv run python -m pytest tests/live_vm/test_remote_supplied_rootfs.py -m live_vm_remote -v
```

An unset ROOTFS input skips all seven cases; once set, missing or malformed inputs fail.
Seven passed cases, not a skip-only exit zero, are the acceptance evidence. The carrier proves
supplied-image upload, guest-agent-ready boot and overlay backing, partial-send and finish
fault cleanup, failed-provision reclaim for supplied and operator profiles, operator-staged
boot without upload, and rejection of non-qcow2 before volume creation. Successful teardown
also has to remove the per-System supplied base.

Each case uses a fresh System UUID, checks initial absence, and observes production cleanup
before its fallback cleanup runs. Fallback cleanup attempts domain removal and each exact
scratch volume independently, preserves an original assertion alongside cleanup failures,
and compares the comparison volume's stable XML, including its identity, size, format, mode,
and modification time. Libvirt's dynamic owner/group, access/change times, and allocated
blocks can change when QEMU opens a read-only backing image; those are excluded. The carrier
never deletes the comparison volume. A killed test process or unreachable TLS service can
prevent cleanup; after restoring connectivity, inspect and remove only the exact
invocation-owned names from the failed run.
Do not delete operator-owned base images. This native x86_64 proof does not claim ppc64le
coverage.

### `live_vm_tcg` — the emulated foreign-arch spine

```
just stack-backends          # the tier runs over the live-stack vehicle
just test-live-tcg     # -m live_vm_tcg; fails loud unless a proof actually passed
```

This runs the four ppc64le provision→boot→crash→retrieve proofs under TCG. It
needs the foreign qemu emulator (e.g. `qemu-system-ppc64`) **and** a running
stack. Without either, the recipe exits non-zero and names the tier: it runs
`scripts/live-vm/preflight-env.sh tcg` first, then requires a real `<N> passed`
summary, so a run where every proof skipped or none was collected is red rather
than a green exit 0 that proved nothing (#2517). Because TCG
needs no `/dev/kvm`, this is the tier that runs on a hosted `ubuntu-latest`
runner. The ppc64le prerequisites and container images are in the
[cross-platform guide](../../development/cross-platform.md).

### ppc64le spine on native POWER

The ppc64le proof drivers also run on a native POWER host: `expected_accel` selects KVM when
native `/dev/kvm` is present, otherwise TCG. Bring up the managed host stack with the
[live-stack runbook](live-stack.md), using the
[POWER prerequisites](../../development/cross-platform.md#native-power-host-integration).
Backends alone are insufficient; server, worker and reconciler must be running at the revision
under test.

The four drivers in `tests/integration/test_live_stack.py` need
`KDIVE_GUEST_IMAGE_PPC64LE` pointing to a prepared ppc64le qcow2. The uploaded-kernel drivers
also need `KDIVE_PPC64LE_BUNDLE`, a directory containing `kernel.tar.gz` and `initrd.img`.
The tar contains the ppc64le ELF at `boot/vmlinuz` and matching `lib/modules/<version>/`;
the initramfs must match that kernel. **When building the bundle from a Fedora ppc64le kernel
RPM, exclude the duplicate `vmlinuz` under `lib/modules/<rel>/vmlinuz`:** that member (~63 MiB)
and `boot/vmlinuz` (~63 MiB) together push the first `.ko.xz` past the 128 MiB module-scan cap,
causing upload validation to reject the bundle. Pass `--exclude='lib/modules/*/vmlinuz'` to your
`tar` invocation. See the
[recorded bundle proof](../../design/2026-07-13-ppc64le-boot-bundle-proof-record-1146.md) for the
artifact shape, and the [external-build contract](../external-build-upload.md) for current
requirements. Image preparation belongs to [image lifecycle](image-lifecycle.md).

With these fixtures and the live-stack environment configured, run `just test-live-tcg`.
The marker name does not force emulation; the expected accelerator is resolved from the host.
Report which drivers passed or skipped. The fadump driver skips non-ppc64le hosts and
non-KVM accelerators (#2398); for a native proof, confirm KVM is selected and the fadump
prerequisites are met. A skip is not capture evidence.

On a native POWER host with `/dev/kvm`, the fadump and kdump capture tests both complete
in about 5 minutes. The
[2026-09-11 record](../../design/2026-09-11-native-power-fadump-kdump-proof-2383.md)
establishes the full crash→capture cycle on POWER9 under KVM-HV.

On an emulated ppc64le host the driver runs rather than skips. The emulated path does not
produce a capture verdict: the
[2026-09-09 record](../../design/2026-09-09-ppc64le-emulated-power-live-proof-2383-proof-record.md)
reached fadump registration under TCG-inside-TCG and then lost the guest to a systemd freeze
before the crash step, after 2 h 51 m. Budget hours per driver there, raise
`KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`, and read a `drain_timeout` on `boot` as a guest that
never signalled readiness rather than as a capture result. The
[platform support page](../platform-support.md#crash-capture-methods-by-arch) records the dated
capture proofs.

## The shared harness

Throwaway-domain tests boot through one context manager instead of
copy-pasting the boot/teardown dance:

```python
from kdive.testing.live_vm import boot_throwaway_domain

with boot_throwaway_domain(
    rootfs, arch=arch, name=unique, mode=uri, wait_for="panic", console_log=log_path
) as domain:
    ...  # run one provider op against a live domain; teardown is guaranteed
```

`boot_throwaway_domain` stages a qcow2 overlay beside the rootfs, resolves the
arch-specific machine type / console / kernel format, waits for
`active` / `panic` / `ssh`, yields the live domain, and tears it down (deleting
its overlay) on exit. `mode` (session/system) is per-test. Two waits carry a
required companion argument, enforced up front: `wait_for="panic"` needs
`console_log` (the panic-wait reads the serial console) and `wait_for="ssh"`
needs `ssh_hostfwd_port` — pass them or the call raises before any domain
boots. The gdbstub debug tests boot through a sibling harness in the same
module, `boot_gdbstub_domain(xml, *, uri, wait_for, console_log=None,
ssh_port=None)`, which takes the caller's already-rendered production domain
XML. It supports the same `active` / `panic` / `ssh` readiness choices while
keeping the transient-domain teardown needed by these tests. The debug
rendering (`render_domain_xml(..., gdb_port=…, debug=…)`) is their subject under
test, so by ADR-0392 the caller keeps rendering it rather than the harness
hiding it.

## Manual proof: the investigation-scoped uploaded rootfs

No pytest tier covers the uploaded-rootfs lifecycle end to end — its four arms
(reuse, isolation, close coupling, reclaim) span the MCP surface, the object
store, the provider staging path, and the reconciler, so it is driven by hand
against a running `live_stack` on a KVM host. Recorded here because the 2026-07-24
run found a defect (#1522) the unit and service suites could not, and because the
setup has several traps worth not rediscovering.

Bring-up is the normal one — `scripts/live-stack/stack-services.sh`, then `just onboard` for a
funded project and a token. The script brings the backends up itself, so `just stack-backends`
ahead of it is redundant. Then, per ADR-0441:

| Arm | Drive | Assert |
|-----|-------|--------|
| Reuse | Two Systems provisioned **concurrently** in one investigation from the same `{"kind":"upload","checksum_sha256":X}` ref | Exactly one object-store `GetObject`; both domains' `<backingStore>` resolve to the same staged base; both boot |
| Isolation | A third System in a *different* investigation, same checksum | `configuration_error` — "checksum is not owned by this System's investigation"; no staging dir created for it |
| Close coupling | `investigations.close` with and without `force` | Bare close refuses and names the live bound Systems; `force` reaps Systems and overlays |
| Reclaim | Close, then let the sweep run | Object, staged base, staging dir, `artifacts` row all gone; `rootfs_cleanup_pending_at` cleared |

Traps this run hit, in the order they bite:

- **Provision concurrently, not serially — but know what that does and does not
  prove.** Two serial provisions prove nothing about deduplication: the second
  short-circuits on the reuse fast path and never reaches the fetch lock. Two
  *concurrent* provisions do satisfy the arm's "exactly one `GetObject`"
  assertion, and on a one-worker stack that is where the assertion is satisfied —
  at the same fast path. It is **not** evidence of fetch-lock contention. A
  worker's claim loop runs one job at a time and the stack starts one worker by
  default, so the second provision sits `queued` until the first has finished
  staging; there is no sibling to block on `pg_advisory_lock`. That still tests
  something real — the fast path collapses the redundant download, and since
  ADR-0443 it verifies the base rather than trusting its presence.

  Be precise about what is left uncovered, because it is narrower than "the lock
  never runs". The arm's *first* provision is a cold fetch, so it does take
  `pg_advisory_lock`, does re-run the staged-base check under it, and does call
  `_unlink_orphan_partials` — uncontended, on one worker, every time. What a
  one-worker stack can never reach are the **contended branches**: the "a sibling
  fetcher finished while we waited on the lock" early return, the "a sibling
  published a staged rootfs base that does not verify" warning, and
  `_unlink_orphan_partials`'s skip of a partial whose `flock` a live writer still
  holds. Each needs a second fetcher that exists at the same moment. To reach
  them, run the [fetch-lock contention arm](#fetch-lock-contention-needs-two-workers)
  below, which needs a second worker process.
- **Compare the uploaded file's byte size with the single-PUT cap.** A qcow2's virtual
  disk capacity is not its stored file size. Measure the file you will PUT; if it exceeds
  the limit, use the gzip transport described by `artifacts.create_investigation_upload`.
  A compressed upload must itself fit the PUT limit. See the
  [artifact guide](../../guide/toolsets/artifacts.md).
- **A multi-kernel rootfs needs a `baseline_kernel` hint.** `fedora-kdive-ready-43`
  carries two kernels, so direct-kernel boot is `not_provisionable` until the
  profile names one (bare version, e.g. `6.18.5-200.fc43.x86_64`).
- **An upload-kind rootfs needs `investigation_id` on the provision call.** The
  allocation does not carry the binding, so omitting it is rejected up front with
  `configuration_error` — "upload-kind rootfs requires a bound investigation_id" —
  before any job is enqueued. The refusal is clear, but it looks like a profile
  problem rather than a missing argument.
- **`systems.provision` returns a job, not a System.** `object_id` is the job id;
  the System id is `data.system_id`. Polling `systems.get` on the returned
  `object_id` answers `not_found` and looks like a failure.
- **Count downloads at the store, not in the logs.** The staging path emits no
  per-download log line, so "exactly one download" is only observable at the
  object store. Use the selected S3 endpoint's access logging on the
  compose network shows the request kind, the object key, and the transferred
  bytes — enough to distinguish one full-object GET from two.
- **The reclaim grace is a day.** `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`
  defaults to `1`, so the close-driven sweep will not select a just-closed
  investigation. Restart the reconciler with it set to `0` to fire the sweep in
  the current pass, and restore the default afterwards.
- **Reclaim is asynchronous and privileged.** Since ADR-0442 the reconciler only
  enqueues; the worker performs the reclaim. Assert on the
  `reclaim_investigation_rootfs` job reaching `succeeded` as well as on the
  post-state, so a job that never ran is not mistaken for a clean sweep.

**2026-07-24 run** (merged `main`, all daemons build-stamped identically): reuse,
isolation, and close coupling passed; reclaim failed. The reconciler runs as the
invoking user while the worker ran under an older privileged launcher, so the stat-based probe
admitted a pass that could not unlink the root-owned staged base — the object was
deleted but the base and its row leaked, with no TTL backstop (#1522, fixed by
ADR-0442). Re-run after that fix: all four arms pass, reclaim drains object, base,
staging dir, row, and marker with no reconciler warnings.

The supported split-user layout now runs every worker through its fixed no-login account and
systemd unit while server and reconciler run as the operator. Shared provider directories are
provisioned explicitly; a stack whose daemons all share a uid (Compose or Helm) will not reproduce
permission defects at that boundary.

### Fetch-lock contention (needs two workers)

The four arms above take the per-(investigation, checksum) fetch lock on every
cold fetch, but always uncontended — a one-worker stack cannot produce two
simultaneous stagings. This arm can, and it is the only local procedure that
reaches a *contended* outcome: the waiter's "a sibling fetcher finished while we
waited on the lock" early return, after really blocking on `pg_advisory_lock`
(ADR-0443).

That is one branch, and claiming more would repeat the defect this arm was
written to fix. Two neighbours stay uncovered locally even here, because each
needs a fault the arm does not engineer. The "a sibling published a staged rootfs
base that does not verify" warning needs the holder to publish a base that fails
the marker or qcow2 gate. `_unlink_orphan_partials` skipping a live writer's
`flock` needs a partial that is still held when a sibling sweeps — but the holder
unlinks its own partial before it releases the fetch lock, and the waiter returns
early without ever reaching the sweep, so a plain two-worker race cannot produce
it. Both are lost-lock and crash paths (ADR-0446), reachable by killing a worker
mid-download or reaping its Postgres backend, not by concurrency alone.

Bring the stack up with two workers. `KDIVE_WORKER_COUNT` is the local stack's
only job-concurrency knob — worker *processes* are the concurrency unit, since a
single worker's claim loop runs one job at a time:

```bash
KDIVE_WORKER_COUNT=2 scripts/live-stack/stack-services.sh
```

Values above 8 are refused, and the refusal is a hard bring-up failure rather than
a clamp. The ceiling is `MAX_WORKER_COUNT` in `scripts/live-stack/lib.sh`; every
worker is a distinct fixed systemd unit with its own database pool and aux health port, so the
bound guards against a transposition typo forking thousands of them. This arm
needs 2.

Confirm two worker processes really came up before drawing any conclusion — this
arm is worthless against one worker, and it fails in exactly the silent way the
old procedure did:

```bash
scripts/live-stack/stack-status.sh
```

The `=== worker lifecycle ===` section must report slots 1 and 2 as `started`, with units
`kdive-live-worker@1.service` and `kdive-live-worker@2.service`. That is the lifecycle witness's
retained view, so it cannot count an unmanaged worker from another checkout. Bring-up refuses an
unmanaged `kdive worker` process rather than adopting it.

The second worker binds its aux health listener on `:9470` rather than
the worker default `:9465`; its logs belong to its exact retained systemd invocation. Leave
`KDIVE_HEALTH_BIND_ADDR` unset: an explicit value applies to every process, so it
cannot coexist with more than one worker, and bring-up refuses the combination
rather than starting workers that die on an exclusive bind.

Drive it as the Reuse arm — one investigation, one uploaded rootfs, two
`systems.provision` calls issued together — with two changes:

- **Use a rootfs no run has staged before, on the identity lane.** Staging is
  content-addressed, so a checksum already present under
  `/var/lib/kdive/rootfs-uploads/<investigation>/` is served by the reuse fast path
  and never takes the lock. Generate a unique image per run, and declare the upload
  with no transport `encoding`. The gzip lane streams the object as a sequence of
  ranged GETs rather than one whole-object GET, so the request count in the third
  assert below stops meaning anything — a correct run would look like hundreds of
  downloads. (This is why the arm does not reuse the oversized catalog image the
  Reuse arm's trap routes onto the gzip lane.)
- **Make the download long enough to observe.** The lock is held across the
  object-store fetch, so the contention window is the download. A ~1.4 GiB
  incompressible qcow2 gives a window of several seconds against local SeaweedFS —
  ample for a sub-second poll. It does not need to be bootable: this arm asserts
  on the fetch, which precedes every boot concern.

While both provisions are in flight, read the advisory lock directly. Postgres
reports the holder and the waiter as two rows on one key:

```bash
docker compose exec -T postgres psql -U kdive -d kdive -c "
SELECT l.classid, l.objid, l.pid, l.granted, a.wait_event_type, a.wait_event
  FROM pg_locks l JOIN pg_stat_activity a USING (pid)
 WHERE l.locktype = 'advisory' AND a.query LIKE '%pg_advisory_lock%'
 ORDER BY l.granted DESC"
```

`classid`/`objid` are how Postgres exposes the advisory key, and the pass rule
below needs them: other session advisory locks run on this cluster (the inventory
reconcile's multi-transaction pass, ADR-0095 console-hosting leadership), so
without the key columns two rows in this output are only co-occurring, not
provably the same lock.

(The compose backend carries `psql`; the host generally does not.)

The arm passes on **all three** of these. The first two are the discriminators —
together they separate contention from serialization, and neither does it alone.
The third is the outcome: contention that did not collapse the second download
would mean the lock ran and achieved nothing.

| Assert | Where | Why it is required |
|--------|-------|--------------------|
| Two rows sharing one `(classid, objid)`: one `granted=t`, one `granted=f` with `wait_event_type=Lock`, `wait_event=advisory` | `pg_locks` ⋈ `pg_stat_activity`, above | A serialized run shows at most one row — the second fetcher has not started, so there is nothing to block |
| Both of **your two** provision jobs `running`, each with a non-NULL `jobs.worker_id`, and the two values **different** | `SELECT id, state, worker_id FROM jobs WHERE id IN ('<job1>', '<job2>')` — the two ids the provisions returned as `object_id` | `worker_id` is `hostname:pid`; one value for both jobs means one worker ran them in sequence and the lock rows above were something else |
| Exactly one `GetObject` for the rootfs key | S3 endpoint access logs, per the "count downloads at the store" trap above | The dedup *outcome* the lock exists to produce, and the only place it is observable |

Name the two job ids explicitly in row 2; do not reach for the newest provision
rows. Every relaxation of that query passes on the serialization the row exists to
catch. Ordering by `heartbeat_at DESC` picks up an unclaimed job first, because it
has a NULL `heartbeat_at` and Postgres sorts NULLs first under `DESC` — and its
`worker_id` is NULL too, so it "differs" from the running job's. Dropping the id
filter lets any other running provision on the stack supply the second row, and
this arm manufactures those: it parks a worker inside a multi-GiB download, a
worker does not act on `SIGTERM` until its job ends, and a re-claimed job whose
lease lapsed is a running provision with a fresh `worker_id`. Both ids, both
`running`, both non-NULL, values different — nothing weaker.

Do not substitute a file count for that third row. Counting `.qcow2` files in the
staging dir proves nothing: both fetchers download into a private
`<token>.<uuid>.partial` and publish by rename, and a fetcher that finds the base
already published discards its own copy without touching `dest`. A waiter that
re-downloaded the whole object therefore still leaves exactly one `.qcow2` with
an unchanged mtime. The store's request count is what separates one download from
two; the waiter's own log is the corroborating signal, since the ADR-0446 "a
sibling published the staged rootfs base … the fetch lock was lost mid-transfer"
warning appears only when it did re-download.

Finish by reclaiming the state. The arm requires a fresh multi-GiB object every
run and by construction can reuse nothing, so repeat runs accumulate in
`/var/lib/kdive/rootfs-uploads/<investigation>/` and in the object store until the
staging filesystem tightens and starts failing *unrelated* provisions with a
free-space shortfall that points at the wrong run. Close the investigation with
`force` (the arm's Systems outlive their provisions and need reaping) and confirm
the sweep collected the object, staged base, staging dir and `artifacts` row —
the same checks the Reclaim arm asserts, including its one-day grace trap.

**2026-07-30 run** (branch `feat/multi-worker-fetch-lock-1551`, both workers
build-stamped identically): all three rows pass.

| | Observed |
|---|---|
| Claim | Both provision jobs held by different workers — `jobs.worker_id` read `sys-R1…:1194523` and `sys-R1…:1194534`, the two live worker pids. (Their `heartbeat_at` was read after both jobs had finished, so it is a last-heartbeat time, not a claim time; the distinct `worker_id` values are the assertion, and the lock timings below are what bound the overlap.) |
| Lock | `18:13:37.009654` pid 244 takes `(classid, objid) = (486701297, 24917193)`, `granted=t`; 237 µs later pid 245 blocks on the same key, `granted=f`, `wait_event_type=Lock`, `wait_event=advisory` |
| Download | One `s3.GetObject` for the rootfs key, `18:13:37.015`→`18:13:39.545`, `200 OK`, ↓ 1.4 GiB in 2.53 s. The whole trace holds exactly one `GetObject` and one `HeadObject` for that key |
| Teardown | `investigations.close` with `force`, then the sweep at `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS=0`: `reclaim_investigation_rootfs` reached `succeeded`, and the staging dir, the object, and the `artifacts` row are gone with `rootfs_cleanup_pending_at` cleared |

So the waiter blocked on the lock for the full duration of the holder's download
and then took the base rather than fetching its own — contention and the dedup
outcome, not one standing in for the other. Both provisions still failed
afterwards at baseline-kernel extraction, because the workspace venv has no
`guestfs` binding; that is downstream of the fetch and does not affect the arm.

The ADR-0482 skew preflight reads the host process table before and after probing.
If it finds more running `python -m kdive worker` processes than worker builds it
can probe, it reports `worker-inventory: unknown`; it cannot certify those
workers as fresh. The same applies when the process table cannot be read or the
worker set changes during the probe. A cached result is rechecked against the
running worker set before reuse. This closes [deferral record 0002](../../debt/0002-skew-preflight-probes-one-worker.md).
For a multi-worker stack, run `scripts/live-stack/worker-lifecycle.sh diagnostics`
from the configured KDIVE checkout and inspect each retained slot's invocation.
See the [lifecycle command limits](../../../scripts/live-stack/README.md) and
the [installed host contract](../../../deploy/systemd/README.md#fixed-live-worker-lifecycle-contract).
The host script's build-stamps block grades only server/reconciler logs; it is
not a per-worker freshness proof. The measurements above describe that dated
experiment's workers and logs, not the current slot identity contract.

## Hard-won quirks

- **`qemu:///session` dodges the root-readback wall.** A `qemu:///system`
  domain writes a root-owned console log a non-root runner cannot read back;
  session mode runs qemu as the invoking user. The installed self-hosted lifecycle
  uses its dedicated session daemon: the workflow calls `load_published_libvirt_uri`
  from `scripts/live-stack/libvirt-uri.sh` and exports that exact root-published URI
  for both boot families, rather than selecting the default session daemon.
- **A long `XDG_CONFIG_HOME` breaks the session-mode QMP socket** (the per-domain
  monitor socket lives under it and hits a 108-byte path limit) — `XDG_RUNTIME_DIR`
  is *not* the lever. The harness redirects it to a short path automatically; the
  quirk bites only code that boots a session-mode domain without the harness.
- **Under `qemu:///system`, the rootfs parent directory's group must include the libvirt QEMU
  user** (`libvirt-qemu` on Ubuntu, `qemu` on Enterprise Linux). The boot creates its overlay
  beside the base image, and without that group access overlay creation fails with
  `Permission denied` (ADR-0052 covers the read side: the qemu user must be able to read the image).
- **A venv running `live_vm` needs the libguestfs binding without shadowing.** Expose only
  `guestfs.py` and `libguestfsmod*.so` from the system install, for example a directory of
  symlinks to those files placed on `PYTHONPATH`. Putting the whole system `dist-packages` on
  `PYTHONPATH` shadows the venv's own packages, and the `live_vm` run then fails because the venv's
  FastMCP server support no longer resolves. See
  [platform support](../platform-support.md) for why the project's `uv` dependency set does not
  supply the binding.
- **Staged images need the right label on an SELinux host** — `virt_image_t` under
  system mode, `svirt_image_t` under session mode (ADR-0640). Ubuntu session guests
  have no per-domain AppArmor confinement (ADR-0741). The rootfs's parent dir must
  be writable, because the boot stages an overlay beside it.
- **`pytest -m live_vm` selects all four families.** If you run a nightly for
  only one, declare which families you intend to run and let the fail-loud gate
  catch a missing declared family, rather than skipping to green.
- **Fakes are blind to what these tiers prove.** `FakeLibvirtConn` / `FakeDomain`
  cannot surface libvirt rejecting domain XML, `filter-dump` emitting no
  packets, a real panic going undetected, snapshot-revert corruption, or
  arch/accel resolving wrong on real silicon. That is the whole reason the live
  tiers exist.

## See also

- Spec: [`2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
- [ADR-0386 — live-test framework and arch-additive runner topology](../../adr/0386-live-test-framework-runner-topology.md)
- [ADR-0353 — the `live_vm_tcg` tier](../../adr/0353-live-vm-tcg-tier.md)
- [ADR-0425 — the remote-libvirt `live_vm` family](../../adr/0425-remote-live-vm-tier.md)
- [ADR-0387 — self-hosted KVM runner host codification](../../adr/0387-selfhosted-kvm-runner-host-codification.md)
- [ADR-0441 — investigation-scoped uploaded rootfs](../../adr/0441-investigation-scoped-uploaded-rootfs.md)
- [ADR-0442 — reclaim the investigation rootfs via a worker job](../../adr/0442-rootfs-reclaim-worker-job.md)
- [live-stack runbook](live-stack.md) · [self-hosted KVM runner](self-hosted-kvm-runner.md) · [POWER host integration](../../development/cross-platform.md#native-power-host-integration) · [four-method live run](four-method-live-run.md)
# External-boot recovery capacity

Each fixed worker has `KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES`, measured in bytes per
activation. The shipped Ansible value is 32 GiB, which accommodates the simultaneous maximum
represented by the current kernel, initrd, module, archive-temporary, and metadata bounds.
Provisioning measures filesystem free bytes after the slot roots are created and
before workers are released. The required free capacity per worker is that ceiling multiplied by
the configured simultaneous-activation count. If the observed value is lower, provisioning stops
and external boot is not advertised. Increase the filesystem capacity or lower admitted
concurrency, then rerun provisioning.
ovisioning.
