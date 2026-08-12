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
**skips cleanly** when its environment is absent — but a tier whose env is set
*wrong* fails loud rather than skipping (see [Skip vs. fail](#skip-vs-fail-a-skip-must-not-look-like-a-pass)).

The `live_vm` tier spans four families (below); `just test-live` runs all of them
(each gated), and one — the remote-libvirt family, which drives a remote
`qemu+tls://` host rather than local silicon — additionally has a focused
`just test-live-remote` recipe (`-m live_vm_remote`) for driving it on its own —
which has no carriers yet and so fails by design (see the `live_vm_remote`
section below).

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
| Provisioned (`live_vm_provisioned`) | `KDIVE_LIVE_VM_SYSTEM_ID` + `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` | `qemu:///system` | an externally provisioned System through the live stack |
| Remote (`live_vm_remote`) | `KDIVE_LIVE_VM_REMOTE_URI` (a `qemu+tls://` host) + `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE` + `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` + `KDIVE_LIVE_VM_REMOTE_RECONCILER` | `qemu+tls://` (operator-named; no default host) | direct provider ops against a genuinely remote libvirt host (ADR-0425) |

The env reads live in `tests/live_vm/__init__.py` (kept out of `src/` so the
ADR-0087 config-env guard is not tripped by test-only vars). That module also
exposes the `require_live_vm_throwaway` / `require_live_vm_bzimage` /
`require_live_vm_vmlinux` / `require_live_vm_provisioned` /
`require_live_vm_remote` gates — the `live_vm`
analogue of the `require_issuer` / `require_stack` / `require_guest_arch` gates
the stack tiers use.

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
- **Environment variables** are read in one place per family (the table above),
  never per module. S3 *credentials* for the provisioned family are **not** env
  vars — they are file-based under `KDIVE_SECRETS_ROOT`; the resolver checks
  only that the endpoint + bucket env is present.
- **The session-mode QMP socket path is length-limited (108 bytes), and the
  lever is `XDG_CONFIG_HOME`.** Session-mode libvirt derives each domain's QMP
  monitor socket under `$XDG_CONFIG_HOME`, so a deep pytest tmp path overflows
  it. The harness redirects `XDG_CONFIG_HOME` to a short `/tmp/kdive-cl-<hex>`
  path for the duration of a session-mode boot and restores it in teardown
  (`prepare_session_runtime`) — a test that boots through the harness need not
  manage it. (Separately, the self-hosted runner keeps `XDG_RUNTIME_DIR` short
  for the session libvirt daemon's own socket; see its runbook.)
- **libvirt runs as modular daemons** (`virtqemud` / `virtnetworkd`), not the
  monolithic `libvirtd`.
- **Guest confinement is named per environment.** Under **system mode** on the
  RHEL-family self-hosted runner, staged images must be relabeled SELinux
  `virt_image_t`; under system mode on an Ubuntu host, AppArmor's `libvirt-qemu`
  profile applies. **Session mode engages neither** — qemu runs as the invoking
  user with no sVirt relabel — so a session-mode tier sidesteps both.
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

## Running each tier

### `live_stack` — drive the running stack over HTTP

```
just stack-up          # backends healthy + schema migrated + host-process env
just test-live-stack   # runs -m live_stack; skips cleanly if the stack is absent
```

`just stack-up` reuses the compose backends (Postgres + MinIO + mock-OIDC) and
keeps the host `server`/`worker`/`reconciler` outside compose. Full bring-up,
including the host-process env block, is in the
[live-stack runbook](live-stack.md). To drive a genuinely remote
`qemu+tls://` libvirt host instead, use the
[remote live-stack runbook](remote-live-stack.md).

### `live_vm` (native) — a real kernel on real silicon

```
just test-live         # -m "live_vm and not live_vm_tcg"
```

This needs a KVM/nested-virt host with libvirt, `drgn`, and a kdump-enabled
guest image, plus the per-family env above. Standing up the host reproducibly —
including the short session-runtime paths, the warm image store, and both boot families under
`qemu:///session` — is the
[self-hosted KVM runner runbook](self-hosted-kvm-runner.md); the ppc64le
(POWER) north-star host is the [POWER host bring-up runbook](power-host-bringup.md).
To validate all four crash-capture methods against such a host, see the
[four-method live run](four-method-live-run.md). Never hand-install a host
dependency for one of these: declare it in the owning Ansible role in the same
change, or the next clean runner reprovision breaks (see the cross-platform and
provisioning-parity notes in [AGENTS.md](../../../AGENTS.md)).

### `live_vm_remote` — direct provider ops against a remote `qemu+tls://` host

```
just test-live-remote  # -m live_vm_remote; a carrier skips cleanly with no remote env
```

**This family has no carriers yet, so the recipe currently fails by design (#1627).**
ADR-0425 shipped the marker, the `RemoteContract`, and `require_live_vm_remote()`
ahead of the first remote proof. Until one is marked, `-m live_vm_remote` collects
nothing (pytest exit 5) and the recipe reports that rather than exiting 0 — an empty
run proves nothing, so it must not read as a pass.

Call `require_live_vm_remote()` **inside** the test function, as the other families
do. A gate at module level (`pytest.skip(..., allow_module_level=True)`) also yields
exit 5, so an absent remote environment would fail this recipe instead of skipping
cleanly; gated inside the test, it is a reported skip and exit 0.

This drives the remote-libvirt family (a sub-selection of `live_vm`, also run by
`just test-live`) directly against an operator-provided `qemu+tls://` host. Set
`KDIVE_LIVE_VM_REMOTE_URI` to the host's control URI, `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE`
to the staged base-image volume name, `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET`
to the **guest-routable** object store, and `KDIVE_LIVE_VM_REMOTE_RECONCILER` to a
presence marker for a running reconciler (its metrics endpoint, or `1`). Standing
up the host — mutual TLS, the staged base volume, the gdbstub-port ACL, and
object-store reachability — is the [remote live-stack runbook](remote-live-stack.md).
The URI must be `qemu+tls://` and must not carry `no_verify` (remote mandates
verified mutual TLS, ADR-0076); a wrong scheme or a missing companion fails loud
rather than skipping.

### `live_vm_tcg` — the emulated foreign-arch spine

```
just stack-up          # the tier runs over the live-stack vehicle
just test-live-tcg     # -m live_vm_tcg; skips cleanly without the foreign emulator
```

This runs the four ppc64le provision→boot→crash→retrieve proofs under TCG. It
needs the foreign qemu emulator (e.g. `qemu-system-ppc64`) **and** a running
stack; it skips cleanly (pytest exit 5 tolerated) without either. Because TCG
needs no `/dev/kvm`, this is the tier that runs on a hosted `ubuntu-latest`
runner. The ppc64le prerequisites and container images are in the
[cross-platform guide](../../development/cross-platform.md).

## The shared harness

Throwaway-domain tests boot through one context manager instead of
copy-pasting the boot/teardown dance:

```python
from kdive.testing.live_vm import boot_throwaway_domain

with boot_throwaway_domain(rootfs, arch=arch, name=unique, mode=uri,
                           wait_for="panic", console_log=log_path) as domain:
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

Bring-up is the normal one — `just stack-up`, then `scripts/live-stack/up.sh`,
then `just onboard` for a funded project and a token. Then, per ADR-0441:

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
- **The catalog images are too big to upload as-is.** They are 6 GiB virtual,
  over the 5 GiB single-PUT cap, so a declaration is rejected at
  `artifacts.create_investigation_upload`. `qemu-img convert -O qcow2 <src> <dst>`
  yields a ~1.9 GiB compact copy that exercises the identity lane. Uploading the
  original requires the gzip transport lane instead.
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
  object store. `mc admin trace` from a throwaway `minio/mc` container on the
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
KDIVE_WORKER_COUNT=2 scripts/live-stack/up.sh
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
scripts/live-stack/status.sh
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
  incompressible qcow2 gives a window of several seconds against local MinIO —
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
| Exactly one `GetObject` for the rootfs key | `mc admin trace` at the store, per the "count downloads at the store" trap above | The dedup *outcome* the lock exists to produce, and the only place it is observable |

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
| Claim | Both provision jobs held by different workers — `jobs.worker_id` read `homer…:1194523` and `homer…:1194534`, the two live worker pids. (Their `heartbeat_at` was read after both jobs had finished, so it is a last-heartbeat time, not a claim time; the distinct `worker_id` values are the assertion, and the lock timings below are what bound the overlap.) |
| Lock | `18:13:37.009654` pid 244 takes `(classid, objid) = (486701297, 24917193)`, `granted=t`; 237 µs later pid 245 blocks on the same key, `granted=f`, `wait_event_type=Lock`, `wait_event=advisory` |
| Download | One `s3.GetObject` for the rootfs key, `18:13:37.015`→`18:13:39.545`, `200 OK`, ↓ 1.4 GiB in 2.53 s. The whole trace holds exactly one `GetObject` and one `HeadObject` for that key |
| Teardown | `investigations.close` with `force`, then the sweep at `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS=0`: `reclaim_investigation_rootfs` reached `succeeded`, and the staging dir, the object, and the `artifacts` row are gone with `rootfs_cleanup_pending_at` cleared |

So the waiter blocked on the lock for the full duration of the holder's download
and then took the base rather than fetching its own — contention and the dedup
outcome, not one standing in for the other. Both provisions still failed
afterwards at baseline-kernel extraction, because the workspace venv has no
`guestfs` binding; that is downstream of the fetch and does not affect the arm.

One limit to record: the ADR-0482 skew preflight probes one worker. Its URL set is
built from the registered per-process ports, so workers 2..N are outside it and a
`fresh` verdict grades only worker 1 — tracked in
[deferral record 0002](../../debt/0002-skew-preflight-probes-one-worker.md). On a
multi-worker stack, read the `=== build stamps ===` block instead: it prints a row
per worker log and a live worker count in its header, so a row without a live
worker behind it is visible as a stale log rather than read as a graded process.

## Hard-won quirks

- **`qemu:///session` dodges the root-readback wall.** A `qemu:///system`
  domain writes a root-owned console log a non-root runner cannot read back;
  session mode runs qemu as the invoking user. This is why the self-hosted
  runner exports `KDIVE_LIBVIRT_URI=qemu:///session` for both boot families.
- **A long `XDG_CONFIG_HOME` breaks the session-mode QMP socket** (the per-domain
  monitor socket lives under it and hits a 108-byte path limit) — `XDG_RUNTIME_DIR`
  is *not* the lever. The harness redirects it to a short path automatically; the
  quirk bites only code that boots a session-mode domain without the harness.
- **Staged images need the right label under system mode** — `virt_image_t`
  (SELinux) or the `libvirt-qemu` AppArmor profile — and the rootfs's parent
  dir must be writable, because the boot stages an overlay beside it.
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
- [live-stack runbook](live-stack.md) · [self-hosted KVM runner](self-hosted-kvm-runner.md) · [POWER host bring-up](power-host-bringup.md) · [four-method live run](four-method-live-run.md)
