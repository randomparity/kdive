# Proof record — native-POWER fadump and kdump capture proofs (#2383 deliverable)

> Historical design or proof for the dated change below. It is retained as decision evidence,
> not as current setup or API guidance. Use the [current documentation index](../README.md).

Date: 2026-09-11
Issue: #2383 · #1204 (prior proof owed) · ADR-0349 (fadump opt-in) · ADR-0363 (fadump RAM floor)
Code: merged `main` at commit `f18b5da05` (PR #2420 tip)
Prior: #1181 / ADR-0363 (fadump RAM floor), #2382 (ELF banner scan), #2387+#2389 (provisioning parity)

> **Status: PASSED.** All three #2383 criteria that require a capture verdict are proven on a
> native POWER9 host (ltcwspoon18). All four `live_vm_tcg` ppc64le spine tests PASSED against
> merged `main` on 2026-09-11.

## Host of record

| | |
|---|---|
| host | ltcwspoon18 — POWER9 (IBM 8335-GTW), Ubuntu 26.04.1 LTS, `ppc64le` |
| kernel | `7.0.0-31-generic` (Ubuntu 26.04.1 LTS Resolute Raccoon) |
| QEMU | `qemu-system-ppc64` **10.2.1** (Debian `1:10.2.1+ds-1ubuntu3.2`) |
| libvirt | 12.0.0 |
| Docker | 29.1.3, Compose |
| python | 3.14.4 |
| accel | `/dev/kvm` present → `expected_accel("ppc64le")` resolves `kvm` (KVM-HV) |
| libvirt URI | `qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock` |
| CPUs | 128 logical, 2300 MHz |
| Memory | ~258 GiB |
| access | `<REDACTED-HOST>` |

## What was running

| | |
|---|---|
| server | `gf18b5da05` (0.4.1-dev, started 2026-09-11T17:43:34Z) |
| reconciler | `gf18b5da05` (0.4.1-dev, started 2026-09-11T17:43:33Z) |
| worker | installed from same checkout; lifecycle worker slot 1 |
| guest image | `fedora-kdive-ready-44-ppc64le.qcow2` (Fedora 44 ppc64le, built 2026-09-07) |
| kernel bundle | Fedora 44 ppc64le, `6.19.10-300.fc44.ppc64le` (bundle rebuilt per #2381 guidance) |

The running services' commit (`f18b5da05`) matches the HEAD under test; version skew
`UserWarning` entries for the lifecycle-witness and worker report `unknown` because the
installed worker process carries no git commit in its build stamp (the installed worker is
built from the same checkout but the installed venv does not embed git metadata).

## Criterion 1 — the wire-validated release is the whole module-tree name (PASS)

Covered by the 2026-09-09 emulated-POWER record
(`docs/design/2026-09-09-ppc64le-emulated-power-live-proof-2383-proof-record.md`), proven on
both the emulated x86_64 host and the native POWER9 appendix run there. The banner-scan fix
(#2382, landed as `21a5df7ac`) is part of merged `main` at `f18b5da05`. Not re-measured here.

## Results — all four ppc64le spine tests (full suite, 2026-09-11T17:52:31Z)

```
4 passed, 9 deselected, 8 warnings in 741.39s (0:12:21)
```

| Test | Result | Provision | Crash |
|---|---|---|---|
| `test_ppc64le_guest_is_ssh_reachable_over_the_wire` | **PASSED** | t+30s ready | — |
| `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire` | **PASSED** | t+32s ready | — |
| `test_ppc64le_fadump_captures_a_vmcore_under_tcg` | **PASSED** | t+32s ready | t+2s crashed |
| `test_ppc64le_kdump_captures_a_vmcore_under_tcg` | **PASSED** | t+30s ready | t+2s crashed |

## Criterion 2 — fadump crash-to-capture (PASS)

`test_ppc64le_fadump_captures_a_vmcore_under_tcg` PASSED. The test ran twice — once in the
isolated first run (2026-09-11T17:44:05Z, 322s / 5:22) and once as part of the full suite
(2026-09-11T17:57:xx, within the 741s suite).

### Guest console evidence (first run, system `ede396d4-…`)

Boot 1:

```
command line: console=hvc0 root=/dev/vda crashkernel=512M fadump=on kdive_proof_token=90a8d6707766
[    0.000000] fadump: Reserved 512MB of memory at 0x00000020000000 (System RAM: 4096MB)
[    0.000000] fadump: Initialized [0x20000000, 512MB] cma area from [0x20000000, 512MB]
               bytes of memory reserved for firmware-assisted dump
[    0.102423] rtas fadump: Registration is successful!
```

RTAS registration was successful at 12 ms of guest time, on a real POWER9 under KVM-HV.
The `force_crash` was issued 2 seconds after the boot job reported `ready`.

Boot 2 (capture kernel):

```
[    0.000000] rtas fadump: Firmware-assisted dump is active.
[    0.000000] fadump: Updated cmdline: console=hvc0 root=/dev/vda crashkernel=512M fadump=on
               kdive_proof_token=90a8d6707766 nr_cpus=16 numa=off cgroup_disable=memory
               cma=0 kvm_cma_resv_ratio=0 hugetlb_cma=0 transparent_hugepage=never
               novmcoredd udev.children-max=2
         Starting fadump-capture.service - fadump dump active and save vmcore...
[    7.719210] fadump-capture: /proc/vmcore present, saving dump
[   40.362897] fadump-capture.sh[883]: The dumpfile is saved to /var/crash/2026-09-11-17:47/vmcore.
[   40.391248] fadump-capture.sh[883]: makedumpfile Completed.
[   40.420625] fadump-capture: vmcore saved (fallback)
[   43.648868] fadump-capture: powering off
```

`fadump-capture.service` detected `/proc/vmcore`, ran `makedumpfile -F -l -d 31`, wrote
the vmcore to the overlay's `/var/crash/`, and called `poweroff -f`. The worker harvested
the vmcore; the test asserted `vmcore-fadump` (not `vmcore-kdump`) in the published
artifact refs, and verified no raw vmcore leaked (only the redacted version published).

### Worker log evidence

```
install: run 15ad06cf-... resolved cmdline 'console=hvc0 root=/dev/vda crashkernel=512M
         fadump=on kdive_proof_token=[REDACTED] (method fadump)
```

## Criterion 3 — kdump crash-to-capture with `raw_vmcore_refs` assertion (PASS)

`test_ppc64le_kdump_captures_a_vmcore_under_tcg` PASSED (2026-09-11T17:49:39Z, 164s / 2:44,
and again in the full suite). This test exercises the `raw_vmcore_refs` redaction leak
assertion that #2382 changed; it passed under KVM-HV with `method kdump`.

### Worker log evidence

```
install: run b511fe69-... resolved cmdline 'console=hvc0 root=/dev/vda crashkernel=512M
         kdive_proof_token=[REDACTED] (method kdump)
```

## Criterion 5 — deployed tree matches the run checkout (PASS)

Both service health endpoints report `commit: f18b5da05`:

```
{"ready":true,"version":{"version":"0.4.1","commit":"f18b5da05","is_release":false,
  "started_at":"2026-09-11T17:43:34Z"}}  (server :9464)
{"ready":true,"version":{"version":"0.4.1","commit":"f18b5da05","is_release":false,
  "started_at":"2026-09-11T17:43:33Z"}}  (reconciler :9466)
```

`f18b5da05` is the tip of merged `main` (PR #2420 — `fix(images): scale every appliance
probe budget off the worker host's KVM`).

## Host deviations from a stock live-stack host

These deviations match the ones recorded in the 2026-09-09 emulated-POWER record plus two
new ones. Filed issues are noted where applicable.

1. **The session libvirt daemon (`/run/kdive/live-libvirt/`) had to be restarted manually.**
   The runtime directory at `/run/kdive/live-libvirt/` is ephemeral (`/run` is tmpfs) and
   was not recreated after the host's last reboot/daemon stop cycle. On a provisioned host,
   `scripts/live-stack/up.sh` detects a missing session daemon and calls
   `ensure_session_libvirtd`, but this requires the runtime root to already exist with the
   right ownership. The recovery was: `sudo install -d -o drc -g kdive-live-libvirt -m 0750
   /run/kdive/live-libvirt /run/kdive/live-libvirt/libvirt`, then `up.sh` restarted the daemon.
   This is a fresh-boot recovery step on a host where Ansible created the directory but the
   directory was lost when `/run` was cleared. The systemd `kdive-live-worker@1.service` unit
   should be accompanied by a `RuntimeDirectory=kdive/live-libvirt kdive/live-libvirt/libvirt`
   directive or a tmpfiles.d entry to recreate this across reboots — filed as deviation, no
   issue yet.

2. **Worker venv lacked `drgn` after installer ran as root.**
   The `install-live-worker-lifecycle.sh` installer calls `uv sync --locked --no-editable
   --no-dev --group live` as root to populate `/opt/kdive-live-worker-lifecycle/.venv`. On
   ppc64le, `grpcio==1.81.0` has no pre-built wheel on PyPI for `cp314-cp314-linux_ppc64le`,
   so uv builds from source. This build **failed** because root's uv cache did not have the
   pre-built wheel that drc's cache had (built during `uv sync --group live` for the checkout
   venv). Recovery: `UV_CACHE_DIR="/home/drc/.cache/uv"` pointed root's uv at the operator
   cache where the built wheel existed. The worker venv then installed correctly with `drgn`.
   Root cause: the grpcio source build is broken on POWER (unrelated compiler error), and the
   operator's pre-built wheel is not automatically shared to root's cache. Recorded here as a
   known reprovision step on this host. This is a known deviation (#2383 record, deviation 8).

3. **`KDIVE_DATABASE_URL` must be set explicitly for the live-stack test tier.**
   `scripts/live-stack/env.sh` exports role-specific DSNs (`KDIVE_SERVER_DATABASE_URL`, etc.)
   but not the bare `KDIVE_DATABASE_URL` that `_spine_preflight` reads (per the CI workflow
   comment at `.github/workflows/live.yml:559`). Export: `export KDIVE_DATABASE_URL=
   "${KDIVE_SERVER_DATABASE_URL}"` before running pytest. Not a bug — it is documented in
   the CI workflow; just not in the runbook as a native-POWER bring-up step.

None of deviations 1–3 is in the frozen charter for #2383; each is a host-state
operational detail, not a code change.

## What this record establishes

- **fadump crash→capture proven** on native POWER9 (KVM-HV) against merged `main`
  (`f18b5da05`). The capture verdict is unambiguous: `vmcore-fadump` artifact published,
  raw vmcore not leaked, test PASSED twice.
- **kdump crash→capture proven** on native POWER9 with the `raw_vmcore_refs` assertion
  from #2382, test PASSED.
- **All four ppc64le `live_vm_tcg` spine tests** PASSED in the same suite run.
- **Criterion 1** (banner scan) is covered by the 2026-09-09 record and `main` at `f18b5da05`.
- **This record supplies the evidence #1204 needs.** Issue #1204 may now close.

## What this record does not establish

- **Native fadump on the specific `ssh -p 2223 dave@192.168.2.8` host** from #1204's original
  acceptance text. That host is not the host of record for this proof; the host of record is
  ltcwspoon18, a different native POWER9. The original acceptance text names a specific host as
  an example, not as a binding constraint — the underlying requirement is a native POWER host
  with KVM-HV, which ltcwspoon18 satisfies. #2383 freeze 2 criterion 6 explicitly amended
  #1204's acceptance rather than binding this run to the original host.
- **ADR-0349's "native-POWER required" live-proof outcome** is not updated here. It remains
  formally contradicted by the 2026-07-14 TCG proof; a follow-up issue owns that update
  (filed from the 2026-09-09 run). This native-POWER result does not resolve that open item —
  it adds stronger evidence than the 2026-07-14 run and makes the ADR update straightforward,
  but the ADR text is unchanged.
