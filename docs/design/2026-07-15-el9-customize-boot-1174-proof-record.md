# Proof record — EL9 customize-boot end-to-end + firstboot serial-verdict fix (#1174)

Date: 2026-07-15
Issue: #1174 · Epic: #1139 · Follows #1152
(`docs/design/2026-07-14-ppc64le-catalog-parity-1152-proof-record.md`)
ADR: 0364 (firstboot verdict from a captured status, not a serial-console write)

## What this proves

A **Rocky 9 (x86_64, KVM)** customize boot reaches `kdive-customize-ok` and **publishes**,
recorded here — the acceptance criterion's composed-path EL9 proof, previously unmet because no
EL9 (CentOS Stream / Rocky) customize boot had ever reached the ok marker on any arch. Blocker 2
(the undiagnosed "silent" Rocky 9 x86_64 firstboot exit) is diagnosed to a real code defect in the
ADR-0345 firstboot script, fixed (ADR-0364), and the fix is proven end-to-end below.

## Environment

- Host: x86_64, libvirt 12.0.0 / QEMU (KVM), `/dev/kvm` present. Build driven with
  `KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs` (worker-owned virtlogd console).

## Blocker 2 — root cause (live-diagnosed on local x86_64 KVM)

Reproduced the pre-fix failure with `build-fs --image rocky-kdive-ready-9`: the customization
boot fired `kdive-customize-failed` ~12 s in, with the dnf metadata phase visible on the console
(BaseOS + AppStream downloaded) but **no install output and no error** — the reported symptom.

Isolating the failing command on the Rocky 9.8 GenericCloud base (NoCloud seed, serial capture):

- `dnf -y install epel-release` with stdout pointed **at the serial console** →
  exit **120**, output truncated; the *same* command with stdout to a **file** →
  exit **0**, full transaction (`Complete!`) — the install actually succeeds.
- Decoupled from dnf: a trivial Python program writing a large volume to `/dev/ttyS0` exits
  **120**; the identical program writing to a **file** or through a **pipe** exits **0**.

Root cause: `dnf` is a Python program. The firstboot script pointed the whole script's stdout at
the serial tty (`exec > /dev/<console> 2>&1`) under `set -e`. A large volume to the slow serial
tty overruns it, CPython's final stdout flush at interpreter shutdown fails, and CPython exits
**120** even though the transaction succeeded. `set -e` + the `EXIT` trap turned that benign
flush error into the fail marker. The same serial-write loss discards the buffered dnf output,
hence the "silent" failure. (Even `tee`/`cat` writing that volume to the serial tty can return an
error, so *any* verdict derived from a serial write is unsafe — confirmed live: a `tee` pipe under
`pipefail` failed a build whose dnf succeeded.)

Incidental finding: on Rocky 9.8, `drgn` installs from **AppStream** (`drgn-0.0.33-2.el9`), not
EPEL; the EPEL enable is still correct for the CentOS Stream / EL10 path but is not what blocked
Rocky 9.

## Fix (ADR-0364)

`render_firstboot_script` now runs the exec steps in a `( set -e … ) > /run/kdive-customize.log
2>&1` subshell, captures `rc=$?`, dumps the log to the console **best-effort** (so a failed
step's error still reaches the host — #1147), and selects the `ok`/`failed` marker from `rc`. A
serial-write error can no longer flip the verdict. Validated live before the real build: OK case
reaches the ok marker with no spurious 120; FAIL case (a nonexistent package) shows the dnf
`Error: Unable to find a match` on the console AND fires the fail marker. `sh -n` + `shellcheck`
clean.

## Live proof — Rocky 9 x86_64 (KVM), all fixes composed

`KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image rocky-kdive-ready-9`
**built + published** end-to-end:

- Customization boot reached **`kdive-customize-ok`** (console
  `/var/lib/kdive/console/69723239-1f76-48f7-ba3d-ab8deb489cbb.log`), with the compact dnf
  transactions now visible on the console (`epel-release … Complete!`, `drgn`/`libkdumpfile`
  install), then a clean poweroff, offline seal, and publish.
- Published image `digest=sha256:38fd884f14bacba0c3340e0d2caef462107f156b7a43a153c843c4be2ff0e93b`.
  Provenance: `arch=x86_64`, `os_release={id=rocky, version_id=9.8, pretty_name="Rocky Linux 9.8
  (Blue Onyx)"}`, installed `drgn-0.0.33 / kexec-tools-2.0.29 / keyutils-1.6.3 /
  openssh-server-9.9p1`, `makedumpfile_version=1.7.6`, `drgn_version=0.0.33`,
  `boot_kernel_count=1`, `layout=whole-disk-ext4-qcow2`, capabilities include `drgn`.
- The shipped image's ext4 feature set contains **no `orphan_file`** (ADR-0351 fix still holds on
  the sealed EL9 image).

## Blocker 1 — ppc64le under TCG (unchanged, environmental)

The ppc64le EL9 customize-boot proof remains gated on the CentOS mirror-CDN `dnf4` metadata stall
under the TCG/SLIRP emulated network (not kdive code) — see #1152's proof record. It is solvable
only on native POWER10 (`ssh -p 2223 dave@192.168.2.8`, KVM-HV), not on this x86_64 host. The
acceptance criterion is satisfied by the x86_64 composed-path proof above ("on at least one arch");
the ppc64le arch proof is left to the native-POWER validation track.

## Reproduction

```
# pre-fix failure (fail marker fired though dnf succeeded):
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image rocky-kdive-ready-9
# post-fix end-to-end proof (built + published, reached kdive-customize-ok):
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image rocky-kdive-ready-9
```
