# Static `svirt_image_t` label for session-mode domains — #2424

- **Issue:** #2424
- **Decision record:** [ADR-0639](../../adr/0639-static-svirt-image-label-for-session-mode-domains.md)

## Problem

Provisioning fails on an SELinux-enforcing RedHat-family host: QEMU cannot open the per-System
overlay and the System goes to `error`. The kdive rootfs tree is labeled `virt_image_t`, which
`svirt_t` may read but not write or map; the unprivileged session libvirt daemon never performs
the dynamic relabel that would fix that on a privileged daemon. ADR-0639 holds the analysis and
the decision.

## Scope

Change the static label kdive's host preparation applies, and state the static-label contract in
the domain XML.

1. `examples/local-libvirt/install-host.sh` — the persistent fcontext for
   `/var/lib/kdive/rootfs(/.*)?` becomes `svirt_image_t`, and an existing rule on that pattern is
   **replaced** rather than skipped, so re-running the installer migrates a host installed before
   this change.
2. `examples/local-libvirt/build-image.sh` — the same change for the rootfs directory it labels
   when publishing a base image.
3. `src/kdive/providers/local_libvirt/lifecycle/xml.py` — `_append_root_disk` emits
   `<seclabel model='selinux' relabel='no'/>` inside `<source>`. Both renderers
   (`render_domain_xml`, `render_customization_domain_xml`) route through it, so one change covers
   System and build domains.
4. Prose that names `virt_image_t` as the RedHat equivalent is corrected where it describes the
   session-mode path: `examples/local-libvirt/README.md`, the two comments named in the charter
   under `deploy/ansible/`.

Out of scope, per the frozen charter: the Ubuntu/AppArmor path; any behavioral change under
`deploy/ansible/`; `security_driver = "none"`; the remote-libvirt provider; authoring a custom
SELinux policy module. Also excluded on evidence: `KDIVE_INSTALL_STAGING`
(`src/kdive/config/core_settings.py`) and the `live_vm` harness docstrings, which describe the
install-staging directory and the **system-mode** boot path respectively — a privileged daemon
relabels dynamically there, so `virt_image_t` is correct and changing it would introduce an error.

### Failure model

**Actors and deployments.** A local operator running `examples/local-libvirt/install-host.sh` and
`build-image.sh` on a single-tenant RedHat-family workstation or lab host; the kdive worker
account on that same host driving the operator-owned session daemon. Designed for Fedora 44 and
Rocky 10.2 with SELinux enforcing, and for the existing Debian/AppArmor and `qemu:///system`
deployments, which must keep working unchanged. Multi-tenant hosts and hosts sharing the kdive
rootfs tree with non-kdive workloads are not named deployments.

**Invariants and assets at stake.**

- sVirt confinement of kdive domains stays on: the domain runs as `svirt_t` with per-domain MCS
  categories, and `security_driver` is untouched.
- The confinement boundary between a kdive domain and the **rest of the host** is not widened.
- A shared base image stays usable by every System that backs onto it.
- The `qemu:///system` paths (self-hosted runner, snapshot) keep working.

**Accepted failure classes.**

- MCS no longer distinguishes one kdive domain's images from another kdive domain's, since all
  kdive images share `svirt_image_t:s0`. Accepted: the relabel that would have provided that
  isolation was never running on the session daemon, so this is a documented property of the
  existing state rather than a reduction. Systems within one kdive deployment are already mutually
  trusted — they share one worker, one rootfs tree, and one base image.
- A non-kdive confined domain that the operator points at the kdive rootfs tree could read and
  write those images. Accepted: reaching that state requires the operator to configure another
  domain against a kdive-owned path, and it is outside the named single-tenant deployments.
- A host whose fcontext was applied by something other than the installer keeps its own label.
  Accepted: bounded — provisioning fails closed with the same `Permission denied` this change
  fixes, and `restorecon -R` on the tree recovers it.

**Covered elsewhere.**

- Guest-side SELinux posture — ADR-0484 (guest images ship permissive).
- The `cannot limit core file size` failure seen on `kdive-build-*` domains on the Fedora host is
  unrelated to labeling and not addressed here; reported as a follow-up candidate.

### Threat model

**Boundary inventory.** This change adds no boundary. It changes the object label at one existing
boundary — the host filesystem objects a confined QEMU domain opens — and it adds a declaration
(`relabel='no'`) at the libvirt/domain-XML boundary that kdive already controls. No new entry
point, no parsing of foreign input, no secret handling, no dependency change.

**Actor model.** The untrusted party is the **guest kernel under test**, which is expected to
crash and may be hostile-by-accident: kdive exists to run crashing kernels. It is confined by
QEMU plus sVirt (`svirt_t` + MCS). The operator and the worker account are trusted. There is no
anonymous or network-reachable actor on this path.

**Control per boundary.**

- Guest → host filesystem: QEMU's own `-sandbox on` plus the SELinux type transition to `svirt_t`.
  Unchanged by this work. The type system still confines the domain to `svirt_image_type` objects;
  what changed is that the files kdive gives it now carry such a type.
- kdive → libvirt: `relabel='no'` asserts that kdive, not libvirt, owns the image label. Its
  failure mode is a start-time error from libvirt, not a silent widening.
- Host preparation → filesystem: `semanage fcontext` plus `restorecon`, scoped to the single path
  pattern `/var/lib/kdive/rootfs(/.*)?`.

**Explicitly out of scope.** Isolation between two kdive Systems on one host (accepted above);
isolation of a multi-tenant host (not a named deployment); anything reachable only by an operator
who already has root on the host.

## Success

1. On a host in the named deployments, with SELinux enforcing, a System provisions to `ready`
   through the ordinary worker path — no `setenforce 0`, no `security_driver` change.
2. The domain's QEMU process runs as `svirt_t` with MCS categories, and the start produces no
   `denied` AVC for any path under the kdive rootfs tree.
3. Re-running `install-host.sh` on a host that already carries a `virt_image_t` fcontext rule for
   the kdive rootfs pattern leaves exactly one rule for that pattern, of type `svirt_image_t`.
4. Every disk `render_domain_xml` and `render_customization_domain_xml` emit carries
   `<seclabel model='selinux' relabel='no'/>` within `<source>`, and the rendered XML is accepted
   by libvirt.
5. The repository guardrail suite (`just ci`) is green.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| `_append_root_disk` emits the per-disk seclabel | `focused-test` | `tests/providers/local_libvirt/test_xml.py` — assert `source/seclabel` with `model="selinux"`, `relabel="no"`, for both renderers |
| Rendered XML stays libvirt-acceptable | `focused-test` | same file — the existing render assertions must still pass; plus the live `virsh define` in criterion 4 below |
| `install-host.sh` replaces an existing rule | `focused-test` | `tests/examples/test_install_host_selinux.sh` via the repo's shell-test path — seed a `virt_image_t` rule, run the block, assert one `svirt_image_t` rule remains |
| `build-image.sh` labels `svirt_image_t` | `task-test-not-applicable` | the changed line is a single literal in a `sudo semanage` argv guarded by `getenforce`; no executable observation exists that does not require an SELinux-enforcing host with root, which is the live proof below |
| Prose corrections | `task-test-not-applicable` | documentation wording; `just docs-links` and `just ci` cover link and format integrity, and no executable consumer reads the text |
| End-to-end provisioning under enforcing | live proof | Fedora 44 and Rocky 10.2: run the installer, provision a System, assert `ready`, assert QEMU is `svirt_t`, assert no AVC under the rootfs tree |

The live proof is a completion criterion of the frozen charter, not an optional arm; the operator
authorized both hosts. Record which arms ran in the PR body.
