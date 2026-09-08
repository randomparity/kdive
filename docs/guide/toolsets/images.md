# images toolset

Choose the guest rootfs before provisioning. Catalog metadata helps select a suitable image;
it does not prove that your kernel, guest, and provider will support an operation together.
Read each tool's schema for arguments and returned fields.

## Choose and inspect

Use `images.list` to compare visible images by architecture, capability tags, OS, default
kernel, description, and `has_kernel_config`. Profile examples select an image by declaration
order; inspect their `selection_note` and `available_images` before adopting the example.

Use `images.describe` for the selected image's package versions, boot layout, and computed
`capability_signals`:

| Signal | What the recorded evidence establishes |
| --- | --- |
| `kdump` | Whether the recorded makedumpfile version and tooling support the target kernel. Guest crash-capture configuration is still required. |
| `direct_kernel` | Whether the recorded non-rescue kernel count is exactly one, so direct-kernel provisioning can select a baseline kernel. |
| `live_drgn` | Whether the shipped drgn supports introspection from guest BTF. The running kernel must still provide usable type information. |

`unverified` means the signal lacks usable evidence; it neither proves readiness nor diagnoses
a broken image. A present operand's `basis` distinguishes `build_verified` from
`operator_attested`; an operator claim is not a KDIVE verification. Read each signal's status
and note rather than treating a capability tag as an end-to-end guarantee.

When `has_kernel_config` is true, `images.kernel_config` returns a download URL for the image's
recorded kernel config. Use it as a starting point for your own build, then check the requirements
of your target kernel and boot method at resource://kdive/docs/operating/external-build-upload.md.
Without an offered config, the tool returns `kernel_config_unavailable`.

## Guest tools and catalog management

Debug/guest and build-host images serve different purposes; inspect `package_versions` for
what the selected image contains. KDIVE consumes externally built kernels. A build-host image
can provide your build environment, but it does not enable a platform build service.

For additional guest packages, first establish SSH access and check network and disk availability.
Local-libvirt guests have no outbound egress by default; remote guest networking is operator
configured. Follow resource://kdive/docs/guide/toolsets/systems.md instead of assuming package
installation is available. Live introspection prerequisites are at
resource://kdive/docs/guide/toolsets/introspect.md.

Catalog mutations have different roles:

- `images.upload` registers a quarantined project-private image; `images.delete` removes one.
  Both require the owning project's operator role.
- `images.publish` builds and publishes a public base image through a job; it requires
  `platform_operator`.
- `images.extend` changes a private image's retention deadline; `images.prune_expired` runs
  the expired-private-image sweep. Both require `platform_admin`. Extending retention does
  not install packages or extend an Allocation.
