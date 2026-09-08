# Remote-libvirt guest helpers

Remote-libvirt invokes these fixed guest programs through qemu-guest-agent. The operator
installs them in the base image; KDIVE does not inject them into an already-running guest.
The [Ansible image catalog](../ansible/README.md#image-catalog-inventorygroup_varsallyml--host_vars)
owns automated package/helper installation. Build the image from the matching checkout and
verify its userland before provisioning.

## Entrypoints and source owners

| Installed program | Operations | Provider consumer |
| --- | --- | --- |
| `/usr/local/sbin/kdive-install-kernel` | `install`, `boot-id`, `boot`, `kdump-status` | [remote install/boot](../../src/kdive/providers/remote_libvirt/lifecycle/install.py) |
| `/usr/local/sbin/kdive-capture-vmcore` | `inspect`, `upload` | [remote retrieve](../../src/kdive/providers/remote_libvirt/retrieve/) |
| `/usr/local/sbin/kdive-drgn` | `tasks`, `modules`, `sysinfo`, `run-script` | [remote introspection](../../src/kdive/providers/remote_libvirt/debug/introspect.py) |

The executable helpers in this directory own argv/stdout details and pass `just lint-shell`.
When changing a helper protocol, update its provider consumer and tests together.

## Baked-in units

Not everything here is an entrypoint. `fadump-capture.service` is a systemd unit baked into
fadump-capable (ppc64le) debug images, not a program a provider invokes. It fires on the fadump
capture-kernel boot, where `kdumpctl` cannot rebuild the fadump initrd, and writes the vmcore
itself. It lives in this directory because both provisioning paths need one authoritative copy:
the [build-fs family customizer](../../src/kdive/images/families/rhel.py) uploads it from the
source tree, and the
[`guest_base_image` role](../ansible/roles/guest_base_image/tasks/build_one.yml) uploads the copy
this directory is staged as on the remote build host. The unit's own header documents the
`/var/crash` layout it owes the
[offline harvest](../../src/kdive/providers/local_libvirt/retrieve/guestfs.py) — change one side
and the `test_fadump_capture_unit_writes_only_paths_the_harvest_globs_match` test fails.

`kdive-install-kernel install` fetches the kernel bundle and installs a deterministic GRUB
entry; `boot` selects that entry for one boot and starts a detached reboot. `boot-id` reads
the guest boot identity. `kdump-status` reports reserved crash memory and whether the capture
kernel is loaded. The provider skips that arming check for older helpers returning nonzero
or unparseable status; absence of the check is not proof that crash capture is ready.

Install exit `75` identifies a fetch failure the worker may retry with a fresh signed URL;
missing executables, invalid input, and other failures map to `install_failure`. Older helpers
that return `1` for every failure retain that non-retryable classification. Rebuild the image
to update the helper behavior. See [ADR-0489](../../docs/adr/0489-guest-helper-exit-code-names-transience.md)
for the decision history.

The capture helper's `inspect` reports core presence, checksum, size, build ID, and bounded
dmesg evidence; `upload` sends the raw core using the signed PUT and required headers.
A missing core is different from a failed transfer. Follow the
[postmortem guide](../../docs/guide/toolsets/postmortem.md) for the user-facing capture flow.

The drgn fixed reports produce JSON. `run-script <timeout>` consumes the caller's script
from stdin and runs it under the supplied timeout. The helper explicitly selects readable
`/sys/kernel/btf/vmlinux`; otherwise it falls back to drgn's debug-info search. Live analysis
needs a working drgn with usable BTF or matching kernel debug information, plus the guest
access prerequisites in the [introspection guide](../../docs/guide/toolsets/introspect.md).
Shared report producers live under
[`providers/shared/debug_common`](../../src/kdive/providers/shared/debug_common/).

## Image and network prerequisites

Use the image-building roles to install executables, their system packages, ownership, modes,
and labels. The install path needs fetch/archive, module, initramfs, and GRUB tools; capture
needs its checksum/build-ID tools and configured kdump support. The actual role package lists
and helper programs define the requirements for the selected image family.

Guest images use SELinux permissive mode under
[ADR-0484](../../docs/adr/0484-guest-images-ship-selinux-permissive.md); a per-domain permissive
rule is insufficient for all helper children. Preserve the host's separate confinement policy.

Signed bundle GET and core PUT URLs must be reachable from the guest. A loopback object-store
address points back into that guest, not to the control plane. Configure the endpoint and guest
network using the [remote-libvirt setup](../../docs/operating/providers/remote-libvirt.md).
