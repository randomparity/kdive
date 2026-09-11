# Static `svirt_image_t` label for kdive images — #2424

- **Issue:** #2424
- **Decision record:** [ADR-0639](../../adr/0639-static-svirt-image-label-for-session-mode-domains.md)

## Problem

Provisioning fails on an SELinux-enforcing RedHat-family host: QEMU cannot open the per-System
overlay and the System goes to `error`. The kdive image directories are labeled `virt_image_t`,
which `svirt_t` may read but not write or map; the unprivileged session libvirt daemon never
performs the dynamic relabel that covers that on a privileged daemon. ADR-0639 holds the analysis.

## Scope

Change the static label kdive's host preparation applies. Nothing in the rendered domain XML
changes: a first design cycle proposed declaring `relabel='no'` per disk, and the review retired
it — see ADR-0639's rejected alternatives and the re-frozen charter on the issue.

1. A new sourced shell helper under `examples/local-libvirt/` applies the label and **migrates** an
   existing rule on the same pattern instead of skipping it.
2. `examples/local-libvirt/install-host.sh` calls it for `/var/lib/kdive/rootfs`, for the nested
   `/var/lib/kdive/rootfs/local` rule `build-image.sh` owns, and for `/var/lib/kdive/install`
   (creating that root, which the installer does not create today).
3. `examples/local-libvirt/build-image.sh` calls it in place of its own `label_for_qemu` body.
4. Operator-facing text that names `virt_image_t` for a path this change relabels is corrected:
   `examples/local-libvirt/README.md`, `docs/operating/providers/local-libvirt.md` (including its
   `## Known limitation — SELinux and per-System overlays` section, which this change resolves),
   `src/kdive/config/core_settings.py` help text, the matching row in
   `docs/guide/reference/config.md`, and the remediation string in
   `src/kdive/providers/local_libvirt/lifecycle/install.py`.

**Deliberately unchanged, with reasons.** `docs/operating/runbooks/live-testing.md` and
`docs/operating/runbooks/image-lifecycle.md:91` name `virt_image_t` for `qemu:///system` paths,
where a privileged daemon relabels dynamically and the old label stays correct; so do
`src/kdive/testing/live_vm.py` and `tests/live_vm/__init__.py`. The `deploy/ansible/` tree performs
no labeling at all, and `inventory/group_vars/live_vm_runners.yml:3` describes
`/var/lib/kdive/live-vm` and `/var/lib/kdive/install` on an Ubuntu/AppArmor runner — this change
relabels neither on that host, so that comment is left alone. Only
`roles/live_vm_host/tasks/main.yml:1929`, which names `virt_image_t` as "the SELinux equivalent
RHEL required", is corrected.

Out of scope per the frozen charter: the Ubuntu/AppArmor path; any behavioral change under
`deploy/ansible/`; `security_driver = "none"`; the remote-libvirt provider; a custom SELinux
policy module.

### Failure model

**Actors and deployments.** A local operator running `examples/local-libvirt/install-host.sh` and
`build-image.sh` on a single-tenant RedHat-family workstation or lab host, and the kdive worker
account on that host driving the operator-owned session daemon. Designed for Fedora 44 and Rocky
10.2 with SELinux enforcing, and for the existing Debian/AppArmor and `qemu:///system`
deployments, which must keep working unchanged. Multi-tenant hosts, and hosts sharing the kdive
image directories with non-kdive workloads, are not named deployments.

**Invariants and assets at stake.**

- sVirt confinement of kdive domains stays on; `security_driver` is untouched.
- The default `qemu:///system` deployment keeps working — nothing may disable the privileged
  daemon's dynamic relabel.
- The confinement boundary between a kdive domain and the rest of the host is not widened.
- A shared base image stays usable by every System that backs onto it.

**Accepted failure classes.**

- Under the session daemon, MCS does not separate one kdive System's images from another's, since
  all kdive images share `svirt_image_t:s0`. Accepted: the relabel that would provide that
  separation was never running there, so this documents existing state rather than reducing it.
  Systems in one kdive deployment already share a worker, an image tree and a base image.
- A non-kdive confined domain the operator points at a kdive image directory could write those
  images, where `virt_image_t` allowed only read. Accepted: it requires the operator to configure
  another domain against a kdive-owned path, and is outside the named single-tenant deployments.
- A host whose fcontext was applied by something other than these scripts keeps its own label.
  Accepted: bounded — provisioning fails closed with the same `Permission denied` this change
  fixes, and re-running the installer recovers it.
- An overlay left at `svirt_image_t:s0:c<i>,c<j>` by a privileged daemon that died without
  restoring is skipped by a later plain `restorecon`, because `svirt_image_t` is a customizable
  type. Accepted: bounded and recoverable with `restorecon -F`, which ADR-0639 records.

**Covered elsewhere.**

- Guest-side SELinux posture — ADR-0484 (guest images ship permissive).
- The `cannot limit core file size` failure observed on `kdive-build-*` domains during design is
  unrelated to labeling; reported as a follow-up candidate, not addressed here.

### Threat model

**Boundary inventory.** No boundary is added. The change alters the object label at one existing
boundary: the host filesystem objects a confined QEMU domain opens. No new entry point, no parsing
of foreign input, no secret handling, no dependency change, no domain-XML change.

**Actor model.** The untrusted party is the **guest kernel under test**, which is expected to
crash and may be hostile by accident — kdive exists to run crashing kernels. It is confined by
QEMU plus sVirt (`svirt_t` + MCS). The operator and the worker account are trusted. No anonymous
or network-reachable actor is on this path.

**Control per boundary.**

- Guest → host filesystem: QEMU's `-sandbox on` plus the SELinux type transition to `svirt_t`,
  both unchanged. The type system still confines the domain to `svirt_image_type` objects; what
  changed is that the files kdive gives it now carry such a type.
- Host preparation → filesystem: `semanage fcontext` plus `restorecon`, each scoped to one
  explicit path pattern, invoked only when `getenforce` reports `Enforcing`.

**Explicitly out of scope.** Separation between two kdive Systems on one host under the session
daemon (accepted above); multi-tenant host isolation (not a named deployment); anything reachable
only by an operator who already has root.

## Success

1. On a host in the named deployments, with SELinux enforcing, a System provisions to `ready`
   through the ordinary worker path — no `setenforce 0`, no `security_driver` change.
2. The domain's QEMU process runs as `svirt_t` with MCS categories, and the start produces no
   `denied` AVC for any path under the kdive image directories.
3. Re-running `install-host.sh` alone on a host carrying the old `virt_image_t` rules leaves
   exactly one rule of type `svirt_image_t` for each of the three patterns it owns or migrates:
   `/var/lib/kdive/rootfs(/.*)?`, `/var/lib/kdive/rootfs/local(/.*)?`, and
   `/var/lib/kdive/install(/.*)?`.
4. The rendered domain XML is byte-identical to what `main` renders — this change adds no
   `<seclabel>` and must not perturb the `qemu:///system` default.
5. No operator-facing text still tells a reader that provisioning fails under SELinux, or
   prescribes `virt_image_t` for a path this change relabels.
6. The repository guardrail suite (`just ci`) is green.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| A pattern with no rule gets one `svirt_image_t` rule | `focused-test` | `tests/scripts/test_selinux_label.py::test_adds_rule_when_absent` |
| A pattern carrying a stale `virt_image_t` rule is migrated, not duplicated | `focused-test` | `…::test_migrates_stale_rule` |
| The helper no-ops off an enforcing host | `focused-test` | `…::test_noop_when_not_enforcing` |
| A missing `semanage` reports and returns 0 | `focused-test` | `…::test_reports_missing_semanage` |
| The domain XML is unchanged by this work (Success 4) | `focused-test` | the existing `tests/adversarial/test_provider_xml.py` and `tests/providers/local_libvirt/lifecycle/test_xml.py` suites pass untouched; this change adds no test there because it adds no behavior there |
| `install-host.sh` labels all three paths | `task-test-not-applicable` | the installer's own gate harness (`tests/scripts/test_install_host_gates.py`) stops the script at the `sudo` preflight, far above these calls, and driving the whole installer needs a real enforcing host with root — which is the live proof below. The labeling logic itself is covered by the helper tests above, which is where the branching lives. |
| Prose corrections | `task-test-not-applicable` | documentation and help text with no executable consumer; `just docs-links`, `just docs-paths` and `just config-docs-check` cover link, path and generated-table integrity |
| End-to-end provisioning under enforcing | live proof | Fedora 44 and Rocky 10.2: run the installer, provision a System, assert `ready`, assert QEMU is `svirt_t`, assert no AVC under the kdive image directories |

The live proof is a completion criterion of the frozen charter, not an optional arm; the operator
authorized both hosts. Record which arms ran in the PR body.
