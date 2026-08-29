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
counts, and manifests represented in the result.

Every retry classifies the nonce-owned root-directory names before writing: `D` is the release
destination, `N` the staged replacement, and `O` the displaced destination. These are the complete
accepted states; any other phase/name/manifest combination is `RECOVERY_CONFLICT` and performs no
further mutation.

| Durable phase | Accepted durable state | Resume action |
|---|---|---|
| `captured` | original `D`; no `N` or `O` | write `staging-intent` |
| `staging-intent` | original `D`; no `O`; absent or partial owned `N` | remove `N`, sync, rebuild and index it |
| `replacement-ready` | original `D` + complete `N`; or absent `D` + original `O` + complete `N`; or installed `D` + original `O` | resume or finish the rename sequence, verify, write `installed` |
| `installed` | installed `D`; no `N`; absent or matching original `O` | rewrite `installed`, then remove matching `O` |
| `restore-ready` | installed `D` + complete captured `N`; or absent `D` + installed `O` + complete captured `N`; or captured `D` + installed `O` | resume or finish the reverse rename sequence, verify, write `restored` |
| `restored` | exact captured `D`, or absent `D` for an absent capture; no `N`; absent or matching installed `O` | rewrite `restored`, then remove matching `O` |

The absent-capture form uses the same states without a captured tree or original `O` during
installation, and restores by removing the installed destination. `O` is never removed until the
corresponding terminal checkpoint is durable.

The entry and uncompressed-content ceilings are 200,000 entries and 8 GiB per tree. Scratch is a
fixed 10 GiB volume per System/Run recovery point. A limit failure is terminal `LIMIT_EXCEEDED`;
the operator reduces the existing tree or rebuilds the System baseline. Every durable phase fsyncs
changed files and directories and syncs the root and scratch filesystems. Flush, unmount, shutdown,
or detach failure is incomplete, never success.

Observed installed/recovery xattr names and values share a 32 MiB per-tree budget. `depmod` has a
300-second monotonic process deadline per capture/install attempt; expiry is `DEPMOD_FAILURE`, makes
no replacement checkpoint durable, and recovery is to retry the same operation after reducing or
repairing the source module tree.

## Image v1

`build_image.py` accepts only an architecture (`x86_64` or `ppc64le`), a kernel, and a prepared
runtime root. It emits a normalized tar bundle containing the kernel and a bootable newc initramfs.
The runtime root must supply Python and appliance-owned `depmod`; the builder rejects symlinks and
includes no shell or socket module. Python runs with site initialization disabled, and the builder
rejects `.pth`, `sitecustomize`, and `usercustomize` startup hooks. Every archive member has fixed
ownership, modes, order, and
timestamp, so identical inputs produce identical bytes. The output SHA-256 is the appliance image
identity stored in operation and result documents.

The `remote_libvirt_module_appliance` Ansible role is wired into `deploy/ansible/site.yml` behind
`remote_libvirt_module_appliance_enabled`. Enabling it requires an immutable URL and a lowercase
64-character SHA-256 for both architecture bundles. Production inventories normally use HTTPS;
an operator may instead stage immutable bundles on each host and use `file://` URLs. This split is
intentional: the repository owns the deterministic build and pinned installation contract, while
artifact publication remains an operator/release action. Ansible verifies every digest during
acquisition and again from the installed read-only file before the artifact is usable.
