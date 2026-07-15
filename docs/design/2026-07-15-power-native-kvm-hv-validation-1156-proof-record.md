# Proof record — POWER native KVM-HV validation (#1156)

Date: 2026-07-15
Issue: #1156 · Epic: #1139 · Design: `2026-07-13-ppc64le-full-support.md` decision 3 · ADR-0355

> **Status: NATIVE KVM-HV CONFIRMED on real POWER hardware.** On a POWER9 host (`ltcwspoon18`,
> Ubuntu 26.04 ppc64el, `qemu-system-ppc64` 10.2, `/dev/kvm` present), a Fedora 44 ppc64le guest
> boots end-to-end under **native KVM-HV** and the full crash→kdump→retrieve spine completes with a
> real kdump-compressed vmcore read by drgn. This un-gates the "POWER-native proof" that the design
> deferred (decision 3) and that ADR-0354 recorded as "live POWER proof deferred to #1156." The
> validation host is a POWER9; the KVM-HV path is architecturally identical on POWER10, so the
> runbook and this record are POWER-generic.

## What this proves (and how it maps to acceptance)

Decision 3 of the ppc64le design gated native validation on hardware. #1156's acceptance:

1. **Runbook reproducible from a clean host** — `docs/operating/runbooks/power-host-bringup.md`,
   driven to the `check-local-libvirt.sh` "host is ready" exit criterion. Every step is a fix the
   check emitted on a clean install.
2. **Documented native run of the full spine, plus the x86_64-under-TCG direction** — below.
3. **Any KVM-HV-vs-TCG behavioral difference folded back into code/ADRs** — the `accel=kvm`
   difference (§Results) is folded into the proofs (`expected_accel`, this branch); the ppc64le
   backend-image gap and the drgn/libkdumpfile requirement are folded into the runbook and ADR-0355.

## Host

| | |
|---|---|
| host | POWER9, Ubuntu 26.04 LTS ppc64el, kernel 7.0.0-27-generic, 128 cores / 251 GiB |
| virt | `qemu-system-ppc64` 10.2, libvirt 12.0 (monolithic `libvirtd`), `/dev/kvm` + `kvm_hv` |
| guest | Fedora 44 Cloud (`fedora-kdive-ready-44-ppc64le`), kernel `6.19.10-300.fc44.ppc64le` |
| fixture | `fedora-kdive-ready-44-ppc64le.qcow2`, content digest `sha256:588dc4d26249c0be6e22ee2e37053a8342bbda6c942c928c69763f4429f4d7e7` |

The fixture was built on the host itself by `build-fs`, whose customization boot booted a real
ppc64le guest under native KVM (`Fedora Linux 44 (Cloud Edition), Kernel 6.19.10-300.fc44.ppc64le
on ppc64le (hvc0)`) — native guest boot is proven by the build step alone.

## Results — the spine under native KVM-HV

The four #1144/#1146/#1148/#1151 proofs run over the `live_stack` vehicle. On this host the ppc64le
guest is **native**, so they exercise KVM-HV instead of TCG.

| proof | native KVM-HV result |
|---|---|
| ssh reachability (`..._is_ssh_reachable_over_the_wire`) | **PASS** — provision→boot→SSH; System row `accel=kvm`, `arch=ppc64le`, `boot_method=direct-kernel`; `ready` in ~30 s |
| uploaded-kernel boot / kdump capture (`..._kdump_captures_a_vmcore_under_tcg`) | **PASS (453 s)** — boot uploaded bundle → `force_crash` → kdump → `makedumpfile` → retrieve; drgn+libkdumpfile read the kdump-compressed vmcore |
| fadump capture (`..._fadump_captures_a_vmcore_under_tcg`) | **PARTIAL** — boots `crashkernel=512M fadump=on` under KVM (past the TCG RTAS Oops of #1151), but the run-readiness check fails at the profile's 2 GiB guest RAM (`System booted but a run-readiness check failed`) |
| x86_64-under-TCG direction | available — the foreign x86_64 emulator is present (`qemu-system-x86_64`), so admission resolves `accel=tcg` for an x86_64 guest on this POWER host (the ADR-0354 inverted case), scaled by `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` |

### The load-bearing difference: `accel=kvm`

`systems.get` after a native provision returns `'accel': 'kvm'` (vs `'tcg'` on the x86_64 CI host).
The proofs asserted the *persisted* accel against a hard-coded `"tcg"`, so the ssh-reachability
proof failed `assert 'kvm' == 'tcg'` before the fix. Folded back on this branch: `expected_accel`
resolves the accel the host produces (native arch + usable `/dev/kvm` → `kvm`, else `tcg`), mirroring
the production `guest_arch_accel` probe. Verified: the proof passes under native KVM-HV **and** is
unchanged on the x86_64 host.

### fadump: the one step not green natively

Under TCG, fadump *skips* (RTAS unsupported). On native POWER it gets further — the `fadump=on`
kernel boots under KVM — but readiness fails at 2 GiB guest RAM, where fadump's boot-memory
reservation plus `crashkernel=512M` leaves too little for the guest to reach the kdive-ready marker
in the tuned window. This is a documented native-POWER limitation: a native fadump capture needs a
larger guest-memory profile or a fadump-specific readiness accommodation. Filed as follow-up; not a
regression (kdump, the spine, is green). This extends the #1151 fadump verdict (registration under
TCG) with the native-POWER boot result.

## Clean-host findings folded into the runbook

Discovered while bringing the host from a clean install to "host is ready" — corrections to the
shared four-method §4b and new POWER-specific dependencies:

- **`python3-guestfs`**, not `python3-libguestfs`, on Ubuntu 26.04; installs to the dpkg path
  `/usr/lib/python3/dist-packages/`, not the `purelib` path the §4b symlink snippet computes.
- **drgn needs `libkdumpfile-dev` at build time.** No ppc64le wheel → drgn builds from source; without
  libkdumpfile it reads ELF cores (so boot/ssh proofs pass) but fails on kdump-compressed vmcores
  (`ValueError: drgn was built without libkdumpfile support`), which surfaces only at capture.
- **Host kernel is `/boot/vmlinux-*`** (ELF, no `z`) on ppc64le, mode 0600 → libguestfs supermin
  fails to read it. `check-local-libvirt.sh` globbed only `vmlinuz-*` and passed vacuously on POWER
  (fixed this branch to probe both).
- **Monolithic `libvirtd`** (no `virtqemud.socket`); **`docker-compose-v2`** not preinstalled;
  drgn source build needs autotools + `libelf-dev`/`libdw-dev`.
- **ppc64le backend-image gap:** `mock-oauth2-server` and `grafana` publish no ppc64le manifest.
  Emulating the JVM issuer under qemu-user deadlocks (segfault); running its portable jib bytecode on
  a native ppc64le JDK works. See ADR-0355.

## Re-proof trigger

Re-run §7 of the runbook after a QEMU/libvirt/drgn bump on a POWER host, or when the fixture is
re-captured (update the digest above and the runbook). The kdump-capture pass is the regression
signal for native KVM-HV; a native fadump pass closes the follow-up.
