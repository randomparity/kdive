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

1. A new sourced shell helper under `examples/local-libvirt/` applies the label with one
   `semanage fcontext -a`, which adds a missing rule and **rewrites** an existing one on the same
   pattern instead of skipping it.
2. `examples/local-libvirt/install-host.sh` calls it for `/var/lib/kdive/rootfs` and for
   `/var/lib/kdive/install`. It does not create the latter — step 6 already runs
   `deploy/systemd/install-live-worker-lifecycle.sh`, which creates it — and it does not touch the
   nested `/var/lib/kdive/rootfs/local` rule, which `build-image.sh` owns and migrates itself.
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
no labeling at all — no `sefcontext` or `setype` task exists anywhere in it — so two comments there
are corrected as prose: `roles/live_vm_host/tasks/main.yml:1929`, which names `virt_image_t` as
"the SELinux equivalent RHEL required", and `inventory/group_vars/live_vm_runners.yml:3`, which
claims `/var/lib/kdive/live-vm` and `/var/lib/kdive/install` are "Both labeled virt_image_t" — an
assertion `main.yml:1926-1929` directly contradicts. No task, variable or value changes.

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
  images, where `virt_image_t` allowed only read. This applies to the default `qemu:///system`
  deployment as well as the session-mode one — at rest, images now restore to `svirt_image_t:s0`
  (writable by any `svirt_t` domain) where they previously restored to `virt_image_t:s0`
  (read-only). Accepted: it requires the operator to configure another domain against a
  kdive-owned path.
- A host whose fcontext was applied by something other than these scripts keeps its own label.
  Accepted: bounded — provisioning fails closed with the same `Permission denied` this change
  fixes, and re-running the installer recovers it.
- An overlay left at `svirt_image_t:s0:c<i>,c<j>` by a privileged daemon that died without
  restoring is skipped by a later plain `restorecon`, because `svirt_image_t` is a customizable
  type. Accepted: bounded and recoverable with `restorecon -F`, which ADR-0639 records.

**Covered elsewhere.**

- Guest-side SELinux posture — ADR-0484 (guest images ship permissive).
- **The build-time customization boot has the same defect, one path over, and is not fixed here.**
  `lifecycle/rootfs/customization_boot.py:163` opens `config.require(LIBVIRT_URI)` — under the
  example stack, the session daemon — against a workspace defaulting to
  `$XDG_DATA_HOME/kdive/build/images`. Measured on both RedHat-family targets 2026-09-11:
  `svirt_t` gets neither `write` nor `map` on that path's policy default `data_home_t`, and
  `svirt_home_t` grants `write` but **not `map`**, so a direct-kernel customization boot cannot map
  its `kernel`/`initrd` under either. This is a second instance of the #2424 denial class on the
  build path. It is out of scope for this change — the charter's surface is host preparation for
  the provisioning path — and is reported as a follow-up. Its consequence here is procedural: the
  live proof stages a prebuilt image rather than building on the target, so a pre-existing build
  failure cannot masquerade as a failure of this change.
- The `cannot limit core file size` failure observed on `kdive-build-*` domains during design is an
  `RLIMIT_CORE` failure and unrelated to labeling. A related condition is already owned by
  `docs/operating/providers/local-libvirt.md:87-91`; whether this is that condition or a distinct
  one is not established. Owner: reported as a follow-up candidate in the implementing PR, not
  fixed here. It is a risk to the live proof — if it recurs on a target, that arm cannot complete
  there, and the PR body says so rather than reporting the criterion met.

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
   exactly one rule of type `svirt_image_t` for each of the two patterns it owns —
   `/var/lib/kdive/rootfs(/.*)?` and `/var/lib/kdive/install(/.*)?` — and re-running
   `build-image.sh` leaves exactly one for `/var/lib/kdive/rootfs/local(/.*)?`.
4. The `qemu:///system` default keeps working: the rendered domain XML is byte-identical to what
   `main` renders, and a privileged daemon's relabel-and-restore cycle from a static
   `svirt_image_t` base is unchanged.
5. No operator-facing text still tells a reader that provisioning fails under SELinux, or
   prescribes `virt_image_t` for a path this change relabels.
6. The repository guardrail suite (`just ci`) is green.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| A pattern with no rule gets one `svirt_image_t` rule | `focused-test` | `tests/scripts/test_selinux_label.py::test_labels_the_directory` |
| A pattern carrying a stale `virt_image_t` rule is rewritten by the same single call, not duplicated | `focused-test` | `…::test_issues_exactly_one_semanage_call` |
| The helper no-ops off an enforcing host | `focused-test` | `…::test_noop_when_not_enforcing` |
| A missing `semanage` reports and returns 0 | `focused-test` | `…::test_reports_missing_semanage` |
| A failing `semanage` or `restorecon` aborts rather than reporting success | `focused-test` | `…::test_aborts_when_semanage_fails`, `…::test_aborts_when_restorecon_fails` |
| The domain XML is unchanged by this work (Success 4) | `focused-test` | the existing `tests/adversarial/test_provider_xml.py` and `tests/providers/local_libvirt/lifecycle/test_xml.py` suites pass untouched; this change adds no test there because it adds no behavior there |
| The privileged daemon's relabel/restore cycle is unchanged by the new static label (Success 4) | design-time probe, recorded | measured on Fedora 44 / libvirt 12.0.0, 2026-09-11: a disk at `svirt_image_t:s0` attached to a `qemu:///system` domain went to `svirt_image_t:s0:c51,c883` while running and back to `svirt_image_t:s0` after `virsh destroy`. Recorded in ADR-0639's Decision section. No repeatable arm: the live proof below exercises the session daemon, and standing up a privileged-daemon kdive deployment is outside this change. |
| `install-host.sh` labels both paths it owns | `task-test-not-applicable` | the installer's own gate harness (`tests/scripts/test_install_host_gates.py`) stops the script at the `sudo` preflight, far above these calls, and driving the whole installer needs a real enforcing host with root — which is the live proof below. The labeling logic itself is covered by the helper tests above, which is where the branching lives. |
| Prose corrections | `task-test-not-applicable` | documentation and help text with no executable consumer; `just docs-links`, `just docs-paths` and `just config-docs-check` cover link, path and generated-table integrity |
| End-to-end provisioning under enforcing | live proof | Fedora 44 and Rocky 10.2: run the installer, provision a System, assert `ready`, assert QEMU is `svirt_t`, assert no AVC under the kdive image directories |

The live proof is a completion criterion of the frozen charter, not an optional arm; the operator
authorized both hosts. Record which arms ran in the PR body.
