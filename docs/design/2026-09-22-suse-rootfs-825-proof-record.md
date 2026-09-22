# Proof record — openSUSE rootfs catalog and v7.0 recovery (#825)

> Historical design or proof for the dated change below. It is retained as decision evidence,
> not as current setup or API guidance. Use the [current documentation index](../README.md).

Date: 2026-09-22
Issue: #825 · Epic: #822
Tested source: `3178fd4feaa495d9f64ed73293a06be20c2b2db0`

## What this proves

The two cataloged x86_64 openSUSE debug images build from their digest-pinned official sources,
boot under KVM through the host-process HTTP stack, answer authenticated SSH, boot KDIVE's v7.0
test kernel, and disclose the existing incomplete-core recovery instead of publishing a
makedumpfile artifact that its own converter warns may be incomplete.

This proof does not establish SLES, ppc64le, build-host, remote-libvirt, hosted-CI, or third-party
package support.

## Built artifacts

Both artifacts were built from the tested source commit through `python -m kdive build-fs`.

| Catalog row | Pinned source | Built image digest | Recorded guest evidence |
|---|---|---|---|
| `opensuse-tumbleweed-kdive-ready` | Tumbleweed snapshot 20260919, source SHA-256 `bddab63272d36e2893b6b50561278511bb29e17000fb21c135fecf0e8553518d` | `sha256:e10d417ac1a1ffa753afae5d9f21b03e5dc21b48fff465653f02b4ec88855464` | kernel `7.2.6-1-default`; makedumpfile 1.7.7; drgn 0.1.0; SSH, AppArmor, kdump, and drgn capabilities |
| `opensuse-leap-kdive-ready-15.6` | Leap 15.6 Build19.146, source SHA-256 `0a5720416d423f98aacaa793a57d56ec045e3dd25cd88713952660ad00da53bd` | `sha256:75c5c7029ff596f26a50b19875b422058deb353a82ab42713b78745ea0357251` | kernel `6.4.0-150600.23.100-default`; makedumpfile 1.7.4; no distribution drgn package; SSH, AppArmor, and kdump capabilities |

The build sidecars also recorded one non-rescue boot kernel, whole-disk ext4 qcow2 layout, and the
expected distro identity for each row.

## v7.0 test-kernel contract

The lifecycle proof used a kernel tree whose `make kernelversion` result was `7.0.0` and whose
`kernelrelease` and bzImage header both reported `7.0.0-1-default`. Because the live helper boots
the uploaded kernel without an initrd, its configuration included these built-ins:

- `CONFIG_VIRTIO_PCI=y`
- `CONFIG_VIRTIO_BLK=y`
- `CONFIG_EXT4_FS=y`

The test additionally required the running libvirt domain XML to name the per-Run staged kernel
and carry a release-specific command-line proof token before forcing the crash.

## Live results

The final artifacts and runtime roles used the tested source commit.

| Proof | Tumbleweed | Leap 15.6 | Aggregate duration |
|---|---|---|---|
| baseline provision + authenticated SSH authorization | passed | passed | 73.04 s |
| upload/install/boot v7.0 + force-crash + kdump classification | passed | passed | 1275.92 s |

Each crash case observed `ready`, reached `crashed` two seconds after the forced crash, waited for
the named domain to shut off, and then required `vmcore.fetch` to terminate with:

- status `failed`;
- error category `readiness_failure`;
- failure reason `kdump_core_incomplete`;
- remediation naming `method="host_dump"`.

No successful vmcore artifact satisfies this assertion. The final command completed with `2
passed`; the SSH command also completed with `2 passed`.

The quiet pytest invocation emitted one duration for each two-parameter command, not a duration
for each parameter. Per-case stopwatches were not captured, so the table retains only the observed
aggregate durations rather than inventing a split; each row's independent pass result and the
two-second ready-to-crashed transition were captured separately.

## Live-diagnosed false-success cause

Before the final fix, both images produced a vmcore and a final README status of `saved
successfully`, but makedumpfile printed both of these warnings to the crash console:

- `The kernel version is not supported.`
- `The makedumpfile operation may be incomplete.`

SUSE's save script bases the README status on exit status and does not copy vmcore-conversion
stderr into the README. The image now uses SUSE's `MAKEDUMPFILE_OPTIONS` field to retain that
stderr inside the disposable capture initramfs. Its fixed postscript renames the core to
`vmcore-incomplete` when either exact warning is present, even if the converter exits zero. The
same two-case live proof then changed from two false-success failures to two expected
incomplete-core passes.

The portable stack emitted its existing build-stamp warnings for the intentionally absent
lifecycle-witness role and a worker package without embedded Git metadata. Server and reconciler
reported the tested commit, and the worker lifecycle installation was rebuilt from the same
verified checkout before the run.
