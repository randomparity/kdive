# Proof record — ppc64le banner scan and capture proofs on an emulated POWER host (#2383)

> Historical design or proof for the dated change below. It is retained as decision evidence,
> not as current setup or API guidance. Use the [current documentation index](../README.md).

Date: 2026-09-09
Issue: #2383 · ADR-0636 · Prior: #2382 (ELF banner scan), #2312, #1146 / ADR-0343 (bundle boot),
#1151 / ADR-0349 (fadump opt-in), #1181 / ADR-0363 (fadump RAM floor), #1204 (fadump proof owed)

> **Status: partial — criterion 1 proven, criteria 2 and 3 not established on this host.** The
> banner-scan proof succeeded over the wire. Both capture drivers were blocked before the crash
> step by a guest-side failure that is a property of this emulated host, not of KDIVE. The
> capture proof #1204 is owed therefore remains owed; see [What this record does not
> establish](#what-this-record-does-not-establish).

## Host of record

Charter freeze 3 fixed the host of record as an **emulated ppc64le guest**, not native POWER. It
is a QEMU `pseries` guest with `accel=tcg` on an x86_64 workstation, so every guest the stack
provisions runs under **TCG inside TCG**.

| | |
|---|---|
| guest kernel | `6.19.10-300.fc44.ppc64le`, Fedora Linux 44 (Cloud Edition) |
| device-tree model | `IBM pSeries (emulated by qemu)` |
| cpu | `POWER10 (architected), altivec supported`; 16 logical CPUs |
| memory | `MemTotal 33,358,336 kB` |
| emulator | **QEMU 10.2.2** (`qemu-10.2.2-1.fc44`) — satisfies ADR-0349's `pseries_fadump` floor of ≥ 10.2 |
| libvirt | 12.0.0 |
| containers | Docker 29.7.2, Compose 5.5.0 |
| python | 3.14.7 (GCC 16.1.1) |
| accel | `/dev/kvm` **removed** and `kvm` blacklisted, so `expected_accel()` resolves `tcg` and libvirt offers only `<domain type='qemu'>` |
| access | `ssh -p <REDACTED-PORT> <REDACTED-USER>@<REDACTED-HOST>` |

The `/dev/kvm` removal is deliberate. The node existed and opened by autoload, but `kvm_hv`
failed with `No such device`, so `expected_accel()` said `kvm` while libvirt said `tcg` and
admission and the provisioner disagreed. Blacklisting the module makes the host consistently
unaccelerated, which is also what makes it the interesting host: **the worker's own libguestfs
appliance is emulated too**, which no hosted `live_vm_tcg` runner reproduces.

## What #2383 changed

Freeze 4 amended the charter's no-production-code exclusion after the run proved criteria 1–3
unreachable without it. Three production files and three test files changed.

| Change | Why |
|---|---|
| `host_appliance_multiplier()` in `providers/local_libvirt/lifecycle/deadlines.py`, applied to `_VIRT_CUSTOMIZE_TIMEOUT_S` | ADR-0636. `virt-customize --ssh-inject` writes each System's bootstrap key (ADR-0289/0315) and measured **1474 s** against a fixed **300 s** budget. Keyed off the *worker host's* KVM, not the System's `accel`. |
| `_PPC64LE_PHASE_DEADLINE_S` 7200 s, passed at every ppc64le drain (`tests/integration/test_live_stack.py`) | Pre-authorized. The provision-ready drain cannot cover a 2157 s provision at 600 s, and install and crash-state took the same shared default. Carried per driver rather than by raising `DRAIN_DEADLINE_S`, which also bounds the accelerated hosts every other spine test runs on. |
| `mint_token` grows `lifetime_s`; the spine passes a whole-test bound (`src/kdive/mcp/dev_harness.py`, `spine.py`) | Freeze 5. The mock issuer's default token lifetime is 3600 s. A spine token is minted once per test, so it must outlive every phase of the slowest driver — this one ran 10292 s. |
| `worker_libvirt_uri()`, replacing four hard-coded `qemu:///system` sites (`spine.py`, `test_live_stack.py`) | The native-POWER9 follow-up run below lost all three capture verdicts to this. Every other live-stack consumer already read `KDIVE_LIBVIRT_URI`; the spine did not. One of the four is `_assert_teardown`, where the wrong daemon made acceptance criterion #5 pass vacuously. |

`SLOW_BUILD_TOOL_TIMEOUT_S` has the same defect as `_VIRT_CUSTOMIZE_TIMEOUT_S` and was
deliberately left unscaled: it reaches only rootfs *build* paths, which no #2383 criterion
exercises. It is recorded as a follow-up rather than changed unexercised: #2397.
That follow-up has since scaled it off the worker host's KVM (ADR-0637); the run that would
measure whether the scaled budget is sufficient has not happened.

## Measured cost of an emulated host

| Operation | Wall clock |
|---|---|
| `virt-customize --ssh-inject` against a Fedora 44 ppc64le overlay | **1474 s**, rc=0 (appliance boot → key inject 734.6 s; SELinux relabel a further 573 s) |
| `systems.provision` → `ready`, end to end | **2157 s** (36 min) |
| one full bundle-driver attempt | ~60 min |
| guest boot to `initrd-switch-root.target` | ~1000 s of guest time |

## Criterion 1 — the wire-validated release is the whole module-tree name (PASS)

The bundle was built by `docs/operating/external-build-upload.md`'s recipe from the **scaffold
image's own** `/boot`, so the uploaded kernel and initrd are byte-identical to what the rootfs
already carries — the #1146 construction:

```
tar -czf kernel.tar.gz \
  --exclude='*/build' --exclude='*/source' --exclude='lib/modules/*/vmlinuz' \
  --transform='s|^vmlinuz-6.19.10-300.fc44.ppc64le$|boot/vmlinuz|' \
  -C /boot  vmlinuz-6.19.10-300.fc44.ppc64le \
  -C /      lib/modules/6.19.10-300.fc44.ppc64le
```

`runs.complete_build` accepted it and persisted this `external-boot-evidence-v1` document
(`investigation_builds.canonical_document.external_boot_evidence`):

```json
{
  "architecture": "ppc64le",
  "archive_member_count": 2658,
  "archive_uncompressed_bytes": 106513765,
  "bundle_sha256": "sha256:eb403c8a94b106ce152a1b79287964d90933e397d10b13db0d9e905d05d01bc2",
  "decoded_kernel_size_bytes": 66211760,
  "elf_metadata_bytes": 16777216,
  "gnu_build_id": "06466f9617cff9e5a762af9216bfc23837310b9c",
  "gnu_build_id_size_bytes": 20,
  "initrd": {
    "sha256": "sha256:c08f9fdd40ab046cab344297760861a0a8f5e679f1331869470eec346895ce7d",
    "size_bytes": 40624598
  },
  "module_member_count": 2656,
  "module_source_manifest": "sha256:d4649ecd00ab608bb13f09fd93c9062cdf0030cb23641034c5c2e684b38e545c",
  "module_uncompressed_bytes": 40302005,
  "release": "6.19.10-300.fc44.ppc64le",
  "schema": "external-boot-evidence-v1",
  "vmlinuz_sha256": "sha256:daf5a8fd71682cc8724b4c26369ab4041e18631235134c70b3b804789b84a454",
  "vmlinuz_size_bytes": 66211760
}
```

`release` is the whole 24-character `6.19.10-300.fc44.ppc64le`, identical to the sole
`lib/modules/<release>` tree name — so `complete_build`'s release/module-tree equality check
passed on the full string rather than on a boundary-cut prefix.

**This run covers the ">16 MiB window" half of #2382, not the boundary-cut half.** The `Linux
version` banner sits at byte offset **26,938,408** of the 66,211,760-byte ELF, and
`_BANNER_SCAN_CHUNK_BYTES` is 16,777,216 — the value `elf_metadata_bytes` records. So the banner
is inside chunk 2 and *mid-chunk*: no chunk boundary cuts it. Truncating the same ELF to the old
16 MiB window reproduces the verbatim pre-fix failure #2382 quotes:

```
16 MiB window : REJECTED -> decoded boot/vmlinuz has no bounded Linux release banner
full ELF      : parsed '6.19.10-300.fc44.ppc64le'
```

The boundary-cut half stays covered by the unit alignment sweep
(`test_ppc64le_elf_banner_release_is_whole_across_every_chunk_alignment`), not by this run. It is
stated here rather than glossed, because a reader could otherwise take this record as covering
both halves.

## Criterion 2 — fadump crash-to-capture (RED, blocked before the crash step)

`test_ppc64le_fadump_captures_a_vmcore_under_tcg` ran to completion as a test and **failed**:

```
================= 1 failed, 2 warnings in 10292.09s (2:51:32) ==================
E   tests.integration.live_stack.spine.SpinePhaseError:
        phase 'ppc64le-fadump:boot' failed: drain_timeout
```

The `boot` job never left `running` inside `_PPC64LE_BOOT_DEADLINE_S` (7200 s as raised by
freeze 4). The run never reached `attribute`, `crash`, or `capture`, so **no statement about
fadump capture — positive or negative — can be read out of it.**

Everything KDIVE controls did what its ADRs specify. The profile
(`_ppc64le_fadump_provision_profile`) requested `memory_mb: 4096`, ADR-0363's fadump RAM floor,
with a paired `allocations.request` of `memory_gb=4`; admission accepted it because the host
QEMU 10.2.2 advertises `pseries_fadump` (ADR-0349); and the install cmdline reached the guest
intact, carrying the arch-default reservation rather than the profile's method-signal sentinel:

```
command line: console=hvc0 root=/dev/vda crashkernel=512M fadump=on kdive_proof_token=<REDACTED-TOKEN>
```

That is exactly what the unreached `ppc64le-fadump:attribute` phase asserts — `fadump=on`
present, `crashkernel=512M` (ADR-0346's arch default, not the `256M` sentinel), a `pseries`
machine, and the per-Run staged kernel path. The guest kernel then honored it:

```
[    0.000000] fadump: Reserved 512MB of memory at 0x00000020000000 (System RAM: 4096MB)
[    0.000000] fadump: Initialized [0x20000000, 512MB] cma area from [0x20000000, 512MB] bytes
               of memory reserved for firmware-assisted dump
[   11.947384] rtas fadump: Registration is successful!
```

**Firmware-assisted dump was reserved, initialized and registered with RTAS at 12 seconds of
guest time.** The failure is 19 minutes later and is not a fadump failure.

### Where the boot actually stopped

The guest completed the initrd, switched root, and started the real systemd 259.5-1.fc44, which
loaded the full SELinux policy and relabelled `/dev`, `/run` and `/dev/shm` — then PID 1 froze:

```
[ 1039.179209] systemd[1]: Successfully loaded SELinux policy in 10.594071s.
[ 1055.676514] systemd[1]: Relabeled /dev/, /dev/shm/, /run/ in 5.124706s.
[ 1057.263095] systemd[1]: systemd 259.5-1.fc44 running in system mode ...
[ 1087.954125] systemd[1]: bpf-restrict-fs: LSM BPF program attached
[ 1137.236766] systemd[1]: Failed to fork off sandboxing environment for executing generators:
                           Protocol error
[!!!!!!] Failed to start up manager.
[ 1143.046156] systemd[1]: Freezing execution.
```

`kdive-ready` is a systemd unit, so a frozen PID 1 means readiness can never be signalled and
the `boot` job can only end on its deadline. The worker behaved correctly; there was nothing
left to wait for.

### The memory hypothesis is falsified

The obvious first reading — that the guest is memory-starved, since fadump reserves a
boot-memory region on top of `crashkernel` — does not survive the two runs:

| Run | Guest RAM | Reservation | Outcome |
|---|---|---|---|
| bundle-boot driver | 2048 MiB | `crashkernel=256M` | froze at the identical systemd line |
| fadump driver | 4096 MiB | `crashkernel=512M`, `fadump=on` | froze at the identical systemd line |

Doubling guest RAM and doubling the reservation moved the failure not at all. The signature is
also wrong for exhaustion: `Protocol error` is `EPROTO` returned from systemd's generator-sandbox
fork, not an OOM kill, an allocation failure, or a panic — and a guest short of memory does not
first load a complete SELinux policy and relabel three filesystems. The cause is PID 1 failing to
fork its sandbox under **TCG inside TCG**; the root cause is unproven and is the first thing a
follow-up should establish (`systemd.log_level=debug` on an otherwise identical boot).

### Sizing against published guidance

For the record, the sizes used are the ones the published guidance calls for, so a re-run on
other hardware should not start by changing them. The `ppc64el` recommendation table is
`crashkernel=2G-4G:320M,4G-32G:512M,32G-64G:1024M,64G-128G:2048M,128G-:4096M`; at 4096 MiB the
fadump driver's `512M` is the exact value for its band. Neither that table nor Red Hat's fadump
guide publishes a minimum *system* RAM for enabling fadump — the published method is empirical,
raising `crashkernel` until the crash kernel boots cleanly. ADR-0363's 4 GiB floor is a KDIVE
admission rule derived from #1156 evidence, not a vendor figure.

One mismatch is worth carrying forward as a finding rather than a change: ADR-0346's per-arch
default is a flat `512M` on ppc64le irrespective of guest size, while the table asks for `320M`
below 4 GiB. Every ppc64le guest under 4 GiB is therefore over-reserved against the guidance —
including criterion 3's 2048 MiB kdump profile, which keeps roughly 1.5 GiB for userspace.

## Criterion 3 — kdump crash-to-capture (NOT RUN)

`test_ppc64le_kdump_captures_a_vmcore_under_tcg` was not executed. It shares the guest image,
the host, and the systemd that froze in both prior boots, and its profile is *smaller*
(`memory_mb: 2048`), so on the evidence above it would be expected to reach the same freeze
after roughly three hours. Running it was deferred in favour of executing criteria 2 and 3 on a
host where the guest can boot. **This is a deferral, not a result:** no kdump capture claim on
this host is made or implied.

## Criterion 5 — the deployed tree matches the run checkout (PASS)

Both ADR-0482 build stamps report the run checkout's short HEAD:

```
:9464 {"ready":true,"version":{"version":"0.4.1","commit":"0f63681cd","is_release":false}}
:9466 {"ready":true,"version":{"version":"0.4.1","commit":"0f63681cd","is_release":false}}
```

The worker and the lifecycle-witness report no commit on this host, which the drivers surface as
a `live-stack version skew` `UserWarning` rather than a failure; the worker was installed from
the same checkout by hand (deviation 8).

## What this record does not establish

Stated plainly, because the sections above could otherwise be read as more than they are:

- **No fadump capture.** #1204's crash-to-capture proof is still owed. The fadump *reservation
  and RTAS registration* are demonstrated at 4 GiB under emulation; the crash and the vmcore are
  not.
- **No kdump capture on this host.** Criterion 3 was not run.
- **No verdict on the guest freeze.** The failure is reproduced twice and localized to systemd
  PID 1 under nested TCG, and the memory explanation is excluded. The mechanism behind `EPROTO`
  is not diagnosed.
- **Only the ">16 MiB window" half of #2382**, as [criterion 1](#criterion-1--the-wire-validated-release-is-the-whole-module-tree-name-pass) states.
- **Nothing about native POWER.** This host is emulated end to end; the
  [known limitation](../operating/platform-support.md#known-limitation--native-power-fadump-capture)
  on native fadump capture is unchanged by this record.

## Host deviations from a stock live-stack host

Every one of these was needed to bring the stack up at all, and each is an environment change
with no repository counterpart.

1. **`KDIVE_GUEST_IMAGE_PPC64LE` was built on the x86_64 host, not in the guest.** In-guest
   `build-fs` fails with `virt-tar-out exceeded its timeout {'stage': 'virt-tar-out',
   'timeout_s': 1800}` (`SLOW_BUILD_TOOL_TIMEOUT_S`). The libguestfs appliance runs host-arch, so
   the produced qcow2 is identical either way, and it carries
   `/etc/systemd/system/fadump-capture.service` from the ADR-0345 customize boot. Image digest
   `sha256:4dcc3e74d403e27e53d02494d1391967702b1291a32df172349a1cd79ce56b35`, identical on both
   hosts.
2. **SELinux set permissive** (`setenforce 0`) after four AVC denials under enforcing. Targeted
   relabels (`virt_content_t` on the image, `svirt_home_t` on the worker home) cleared two; more
   appeared, including `entrypoint` denied for `rpc-virtqemud` → `/usr/bin/passt`, the user-mode
   networking helper the SSH-reachability assertion needs.
3. **The slot account's home moved** from `/nonexistent` to `/var/lib/kdive/wh1`. Fedora compiles
   libguestfs's default backend as `libvirt`, which needs a usable `$HOME` for `~/.cache/libvirt`;
   the installer creates the account `--no-create-home --home-dir /nonexistent`. The path also has
   to be *short*: libvirt appends 68 bytes and `sun_path` is 108, so the natural
   `/var/lib/kdive/live-workers/slots/1/home` overflows it.
4. **`/home/<REDACTED-USER>` set to mode 0711.** At 0700 the container uid could not traverse it,
   so Postgres's `docker-entrypoint-initdb.d` bootstrap never ran and every migration failed with
   `password authentication failed for user "kdive-migration"`.
5. **`chcon -Rt container_file_t` on `deploy/`.** The compose bind mounts are declared `:ro` with
   no `:z`, so under enforcing SELinux the same bootstrap failed with `psql: error:
   /docker-entrypoint-initdb.d/010-migration-owner.sql: Permission denied`.
6. **`virtnodedevd.socket` enabled.** `up.sh` manages only `virtqemud`, and onboarding's resource
   discovery crashes without it: `libvirt.libvirtError: Failed to connect socket to
   '/var/run/libvirt/virtnodedevd-sock'` at `providers/local_libvirt/discovery.py:331`.
7. **The session `virtqemud`'s core-file limit raised.** It ran with `Max core file size 0 0`, so
   libvirt could not grant QEMU the unlimited core it requests: `internal error: Process exited
   prior to exec: libvirt: error : cannot limit core file size of process N to
   18446744073709551615: Operation not permitted`. Fixed with `prlimit --core=unlimited` on the
   running daemon rather than a restart, which risks the installer's `_reconcile_libvirt_tuple`
   refusal.
8. **The worker venv was repaired by hand.** `install-live-worker-lifecycle.sh` installs it
   unlocked and without `--group live`, so it had no `drgn` and resolved `grpcio` 1.83.1 against a
   lock that pins 1.81.0. Already-built wheels were staged and installed with `UV_FIND_LINKS` and
   a `grpcio==1.81.0` constraint.
9. **`KDIVE_OIDC_IMAGE` exported by hand.** The `stack-up` recipe tests `${KDIVE_OIDC_IMAGE:-}`
   without sourcing `scripts/live-stack/env.sh`, so ADR-0358's emulated-POWER mirror selection
   never reaches it on the one host it was written for.

None is in #2383's frozen scope. Each is filed:

| Deviation | Issue |
|---|---|
| 1 — in-guest `build-fs` times out on an emulated host | #2397 — scaling mechanism applied (ADR-0637), unit-proven on both branches. Measured over three runs on this host, 2026-09-10 and 2026-09-11 (#2414); see [Deviation 1, measured](#deviation-1-measured) below. The timeout half is **answered**: every `kdive`-owned budget on the `build-fs` path is now measured and cleared. `build-fs` still does not complete, but for a cause outside the deviation — the customization-boot guest's systemd freezes before it starts up, reproducibly and independently of any budget. |
| 6 — `virtnodedevd.socket` enabled by hand | #2401 |
| 8 — worker venv repaired by hand | #2399 |
| 9 — `KDIVE_OIDC_IMAGE` exported by hand | #2400 |

The Red Hat-family host facts behind deviations 2, 3 and 5 belong to epic #2388, which covers
preparing a RHEL/Fedora/SUSE host as a local libvirt server. Deviations 4 and 7 are properties of
this workstation, not of the repository.

### Deviation 1, measured

Three runs, one on 2026-09-10 and two on 2026-09-11, on the emulated-POWER host of record above
— Fedora 44 ppc64le under `qemu-system-ppc64 -machine pseries,accel=tcg -cpu power10 -smp 16
-m 32G`, guest-visible 16 CPUs / 33358336 kB, QEMU 10.2.2, guestfs-tools 1.56.0, `kvm`
blacklisted. In-guest `build-fs --image fedora-kdive-ready-44-ppc64le`, no wrapper and no
`LD_PRELOAD`, so each figure is the tool's own wall clock. Runs 2 and 3 carried the #2419 and
#2420 changes at their pre-merge commits `f590eb50d` and `52505bbf9` on a clean tree; they
merged to `main` as `9e74f51aa` and `f18b5da05`. Run 3 additionally set
`KDIVE_LIBVIRT_URI=qemu:///session`, for the reason recorded under the customization boot below.

**The measurement was not merely unrun — it was unreachable.** `slow_build_tool_timeout_s()`
selected its branch with `os.access`, and on any systemd host `50-udev-default.rules` publishes
`/dev/kvm` at 0666 via `OPTIONS+="static_node=kvm"` whether or not the module ever loads, since
opening the node is what triggers autoload. Recorded on this host before the run: `os.access`
returned `True`, `os.open` raised `OSError 19 ENODEV`, and libvirt advertised zero KVM domains.
So the budget resolved to the unscaled 1800 s on the one host class #2397 exists to scale, and
#2397's scaling had never engaged here at all. Fixed under #2419 by opening the node instead;
`budget_s=18000` was recorded from the corrected path immediately before each launch.

| Stage | Base | Run 1 | Run 2 | Run 3 | Unscaled base | Scaled budget |
|---|---|---|---|---|---|---|
| `virt-tar-out` | 1800 s | **2406 s** | **2888 s** | **2955 s** | exceeds — the #2383 failure | 18000 s, passes at 1.34–1.64x |
| `virt-make-fs` | 1800 s | **2032 s** | **2293 s** | **2256 s** | exceeds | 18000 s, passes at 1.13–1.27x |
| `guestfish` (`rhel.normalize`) | 300 s | killed at 300 s | **1365 s** | **1324 s** | exceeds | 3000 s, passes at 4.41–4.55x |

Timings come from an external process-table sampler on a 10 s interval, because `build-fs`
emits no per-stage timing at `INFO`; every figure carries that sampling error. The sampler is
independently corroborated at one point: it read 303 s for the run-1 `guestfish` call that
`build-fs` itself killed at a 300 s timeout, agreeing with a known ground truth to within one
interval. Run-to-run spread on the same host with no configuration change between runs reaches
20% (`virt-tar-out`, 2406 s to 2955 s), which is why each stage is reported as a range rather
than a point. The two stages measured three times each agree closely enough that the spread
looks like ambient host load rather than anything structural.

**What this establishes.** Scaling is required rather than precautionary: every stage measured
exceeds its unscaled base, so an unfixed probe fails this host at `virt-tar-out` exactly as
deviation 1 records. The scaled budgets are sufficient for all three, and the multiplier now has
a second measured point beside ADR-0636's 1474 s `virt-customize --ssh-inject` figure.

**Headroom is not uniform, and the `guestfish` stage is the reason to say so.** The two
1800 s-based stages ask 1.13–1.64x of their base, well inside the 10x ADR-0341 supplies. The
300 s-based `guestfish` stage asks **4.41–4.55x**. A run-1 reading of "303 s against a 300 s
budget" invites the conclusion that the budget was marginally short; it was not. 303 s was the
point at which `build-fs` killed the call, which bounds the work from below and says nothing
about its length. Allowed to finish under #2420's scaled budget, the same stage took 1365 s and
1324 s. A timeout kill is a lower bound, never a measurement, and the two differ here by a
factor of four.

The mechanism is that a smaller base pays the same fixed appliance boot over less work, so it
asks a larger ratio. Read the 1.1–1.6x figures as a property of the two longest bases, not as
the multiplier's working range: at the tightest stage measured the 10x margin is closer to 2x.

#### The customization boot does not complete, for a reason no budget reaches

Run 2 failed in `run_customization_boot` with `Unable to open file:
/var/lib/kdive/console/<uuid>.log: Permission denied`. That was a property of how the run was
invoked rather than of the code: it used the default `KDIVE_LIBVIRT_URI=qemu:///system`, so the
opener was the system `virtqemud`, which is neither the operator nor a member of
`kdive-live-libvirt`, while the console directory is provisioned `2770
operator:kdive-live-libvirt` for the operator-owned *session* daemon that
`lifecycle/rootfs/customization_boot.py` documents. Run 3 set `qemu:///session` and the console
log opened.

Run 3 then reached the customization boot proper and **the guest froze**:

```
[ 1035.822766] audit: avc: denied { execute_no_trans } for pid=660 comm="(exec-inner)"
    path="/usr/lib/systemd/system-generators/cloud-init-generator"
    scontext=system_u:system_r:kernel_t:s0 tcontext=system_u:object_r:unlabeled_t:s0 permissive=1
[ 1081.029038] systemd[1]: Failed to fork off sandboxing environment for executing generators:
    Protocol error
[!!!!!!] Failed to start up manager.
[ 1083.237850] systemd[1]: Freezing execution.
```

`build-fs` reported `boot_timeout` — *customization boot did not reach the ok marker within the
window* — after the full scaled window (1800 s base x 10; ~19555 s wall including poll
overhead). **That report is misleading about the cause.** The guest had been frozen since
~1081 s of its own uptime; `kdive` then polled a dead domain for more than five hours. No
increase in the window, and no change to the multiplier, would alter the outcome: systemd PID 1
never starts up, so the ok marker can never appear.

This is **not** a defect introduced by #2414, #2419 or #2420, and not a property of how these
runs were invoked. The identical failure — same message, same `Freezing execution`, same
systemd 259.5-1.fc44 on ppc64le, at ~1114 s instead of ~1081 s — is present in the console log
of the #2383-era run on this host from 2026-09-09, taken under a different worker identity and a
different code state. It reproduces across both.

Two observations worth carrying to whoever picks this up, neither of them a diagnosis: the
rootfs is `unlabeled_t` and systemd PID 1 runs as `kernel_t`, so the SELinux transition to
`init_t` has not happened at generator time; and sandboxed generator execution is a recent
systemd feature, which makes a ppc64le-under-TCG interaction plausible without establishing one.

**What none of the three runs establishes.** No `virt-builder` or `virt-customize` stage ran, so
neither is measured here. #2397's acceptance bullet "an emulated host completes in-guest
`build-fs`" is **not met**, and on this host it is not reachable by budget work alone: every
`kdive`-owned budget on the path is now cleared, and the run still stops at a guest that freezes
before it can be customized.


---

## Appendix — native POWER9 follow-up run (2026-09-09, <REDACTED-HOST>)

A second run against the same branch (`HEAD: 0330b05fc`) was executed on a **native POWER9 host**
(Ubuntu 26.04.1, kernel `7.0.0-31-generic`, `/dev/kvm` present). This host was not the charter's
host of record, but the charter's host-of-record decision fixed it as an emulated ppc64le guest —
not as the only permitted run host. This run adds supplementary evidence on a host where provision
takes ~30 s (KVM) rather than ~36 min (TCG inside TCG).

| | |
|---|---|
| host kernel | `7.0.0-31-generic`, Ubuntu 26.04.1 LTS (Resolute Raccoon) |
| cpu | POWER9, altivec supported |
| memory | available via `/dev/kvm` |
| QEMU | 10.2.1 (Debian `1:10.2.1+ds-1ubuntu3.2`) |
| libvirt | 12.0.0 |
| containers | Docker 29.1.3, Compose (stack-up); python 3.14.4 |
| accel | `/dev/kvm` present; `expected_accel("ppc64le")` resolves `kvm` |
| access | `<REDACTED-HOST>` |
| stack | same three-role host stack (`server`/`worker`/`reconciler`) at commit `0330b05fc` |

**Note:** The worker on this host uses a dedicated kdive session libvirtd
(`qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock`) rather than the
system libvirtd. The test's `attribute` phase calls `virsh -c qemu:///system dumpxml` (system
libvirtd), which cannot see domains the worker created in the session daemon. All three
`test_ppc64le_*_over_the_wire` tests failed at `:attribute` for this reason, not because KDIVE
itself failed. The underlying KDIVE operations (provision, upload, `runs.complete_build`,
`runs.install`, `runs.boot`) all succeeded and are recorded in the DB.

### Criterion 1 — the wire-validated release is the whole module-tree name (PASS, native POWER)

All three bundle uploads on this run persisted the same `external_boot_evidence` document.
Extracted from `investigation_builds.canonical_document`:

```json
{
  "schema": "external-boot-evidence-v1",
  "release": "6.19.10-300.fc44.ppc64le",
  "architecture": "ppc64le",
  "elf_metadata_bytes": 16777216,
  "vmlinuz_size_bytes": 66211760,
  "gnu_build_id": "06466f9617cff9e5a762af9216bfc23837310b9c",
  "bundle_sha256": "sha256:7ef6f385815745deabc0b003a43a4663a5753538dd71a6a99d0953b6d25a4a33"
}
```

`release` is the full 24-character `6.19.10-300.fc44.ppc64le`. The vmlinuz is 66,211,760 bytes;
the banner lies past the 16 MiB (`elf_metadata_bytes`) read window added by #2382, confirming
the ">16 MiB window" half of the fix. Criterion 1 is proven on both hosts.

### Criteria 2 and 3 — fadump and kdump crash→capture (BLOCKED, wrong libvirt URI in test attribute step)

All three boot jobs (`runs.boot`) succeeded with `state=succeeded`:

| run | state | boot job | method |
|---|---|---|---|
| `61891dbe` (bundle) | succeeded | `03088575` | `kdump`, cmdline `crashkernel=512M` |
| `38876443` (fadump) | succeeded | `fae79b4a` | `fadump`, cmdline `fadump=on crashkernel=512M` |
| `dad5614a` (kdump) | succeeded | `d9223251` | `kdump`, cmdline `crashkernel=512M` |

The worker logs confirm the install cmdlines reached the guest:

```
install: run 38876443 resolved cmdline 'console=hvc0 root=/dev/vda crashkernel=512M fadump=on
         kdive_proof_token=[REDACTED]' (method fadump)
install: run 61891dbe resolved cmdline 'console=hvc0 root=/dev/vda crashkernel=512M
         kdive_proof_token=[REDACTED]' (method kdump)
```

However, the tests failed at the `:attribute` phase (before the crash step) with:

```
virsh -c qemu:///system dumpxml kdive-<system_id>: returned non-zero exit status 1
```

The test uses the system libvirtd URI. This host's worker uses a dedicated session daemon at a
different socket. The test runner can connect to that socket (the runner's account is in the
`kdive-live-libvirt` group), but the hard-coded `qemu:///system` in the attribute assertion
cannot see the domains. Since the test never reached the `crash` phase, **no capture verdict
can be read from this run**, positive or negative.

**Fixed after this run.** The four hard-coded sites now read `KDIVE_LIBVIRT_URI`, which is what
every other live-stack consumer already did (`scripts/live-stack/lib.sh`, `down.sh`, `status.sh`).
So this blocker does not stand between a re-run on this host and a capture verdict. This record
states what the run that happened established; it does not predict the re-run's outcome. Criteria
2 and 3 stay unproven until a run reaches the crash step and reports one.

Provision timing on native POWER9 with KVM: ~30 s (vs 2157 s under TCG-inside-TCG on the
emulated host). Boot timing: ~72–92 s. The entire 4-test suite completed in 17 min 25 s.
