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
unreachable without it. Two production files and three test files changed.

| Change | Why |
|---|---|
| `host_appliance_multiplier()` in `providers/local_libvirt/lifecycle/deadlines.py`, applied to `_VIRT_CUSTOMIZE_TIMEOUT_S` | ADR-0636. `virt-customize --ssh-inject` writes each System's bootstrap key (ADR-0289/0315) and measured **1474 s** against a fixed **300 s** budget. Keyed off the *worker host's* KVM, not the System's `accel`. |
| `DRAIN_DEADLINE_S` 600 → 7200 (`tests/integration/live_stack/spine.py`) | Pre-authorized. The provision-ready drain cannot cover a 2157 s provision at 600 s. |
| `_PPC64LE_BOOT_DEADLINE_S` 1800 → 7200 (`tests/integration/test_live_stack.py`) | Pre-authorized, same reason. |
| `mint_role_token` sets `exp` from `DRAIN_DEADLINE_S` (`spine.py`) | Freeze 5. The mock issuer's default token lifetime is 3600 s while the spine's own per-phase budget is 7200 s, so the credential expired before the budget it must outlive. |

`SLOW_BUILD_TOOL_TIMEOUT_S` has the same defect as `_VIRT_CUSTOMIZE_TIMEOUT_S` and was
deliberately left unscaled: it reaches only rootfs *build* paths, which no #2383 criterion
exercises. It is recorded as a follow-up rather than changed unexercised.

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

Deviations 1, 8 and 9, and the Red Hat-family facts behind 2, 3, 5 and 6, are filed as follow-up
work; none is in #2383's frozen scope.
