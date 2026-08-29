# Remote module-restoration appliance

This directory is the versioned appliance contract selected by ADR-0585. The appliance is a
transient direct-kernel domain. It has no network interface, persistent definition, graphics,
host filesystem share, shell entry point, or caller-selected device. Future provider lifecycle
code supplies exactly three virtio disks (`vda` root read-write, `vdb` source read-only, and `vdc`
scratch read-write) and the bounded console described by the two architecture-specific
`domain-v1-*.xml` configurations.

## Protocol v1

The source volume contains one canonical UTF-8 JSON document named `operation-v1.json`. It is
limited to 16 KiB and must validate against `operation-v1.schema.json`; unknown fields are errors.
Only `capture_install` and `restore` are caller operations. Capture, manifest calculation, staging,
installation, `depmod`, flush, and restoration are fixed internal phases. The caller cannot name a
path, device, mount option, command, environment variable, or destination.

The appliance writes `result-v1.json` and durable phase checkpoints to the scratch volume. Results
validate against `result-v1.schema.json`. A failure always carries exactly one stable error code;
success never carries one. Console output contains only the phase, stable error code, identities,
counts, and manifests represented in the result. The complete recovery and retry table remains the
normative one in ADR-0585.

The entry and uncompressed-content ceilings are 200,000 entries and 8 GiB per tree. Scratch is a
fixed 10 GiB volume per System/Run recovery point. A limit failure is terminal `LIMIT_EXCEEDED`;
the operator reduces the existing tree or rebuilds the System baseline. Every durable phase fsyncs
changed files and directories and syncs the root and scratch filesystems. Flush, unmount, shutdown,
or detach failure is incomplete, never success.

## Image v1

`build_image.py` accepts only an architecture (`x86_64` or `ppc64le`), a kernel, and a prepared
runtime root. It emits a normalized tar bundle containing the kernel and a bootable newc initramfs.
The runtime root must supply Python and appliance-owned `depmod`; the builder rejects symlinks and
includes no shell or socket module. Every archive member has fixed ownership, modes, order, and
timestamp, so identical inputs produce identical bytes. The output SHA-256 is the appliance image
identity stored in operation and result documents.

The `remote_libvirt_module_appliance` Ansible role installs both architecture bundles from
operator-pinned HTTPS URLs. A 64-character SHA-256 is mandatory for each image; Ansible verifies it
during download and again from the installed file before publishing the read-only artifact.
