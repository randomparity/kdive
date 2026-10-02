# Remote deep lifecycle on a separate provider host — design

Issue: #2810 (epic #2803 entry 7). Governing records:
[ADR-0686](../../adr/0686-independent-coverage-obligations.md) (contract, qualifier) and
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md) (evidence identity).
No new ADR: every choice below stays inside those two records and the #2809 precedent
([local design](2026-10-01-deep-lifecycle-matrix-design.md)).

## Problem

The contract gives #2810 444 cells: 12 `deep-lifecycle/remote-libvirt/...` cells (8 x86_64,
4 ppc64le) and 432 remote tool cells. No test produces any of them. The remote spine
(`tests/integration/test_remote_live_stack.py::test_remote_spine_over_the_wire`) checks neither
the running build, a loaded module, a reconnect, nor remote-side cleanup. Remote tool scenario IDs
are provider-neutral (`tool/<tool>/<variant>/...`), so a local-only node mapped by #3062 would mark
the remote tool cells implemented.

## Scope

1. **Ownership** (`scripts/coverage_campaign/contract.py`, operator decision 2026-10-02).
   - Lifecycle group (owner 3062) cells: ppc64le on any provider → 2818; remote x86_64 → 3080;
     local x86_64 stays 3062.
   - Deep cells: ppc64le → 2818 on both providers; remote x86_64 → 2810; local x86_64 → 2809.
   - Every `remote-libvirt` tool cell's `scenario_id` becomes `tool/remote-libvirt/<tool>/
     <variant>/<kind-or-boundary>`. Cell IDs, local and service scenario IDs are unchanged.
   - `obligations.toml` maps `deep-lifecycle/remote-libvirt/{longterm,stable}` to the new node.
     The ppc64le remote cells share those scenario IDs (as local ppc64le shares the local node)
     and report `missing-result` for #2818 until a ppc64le remote host runs them.
   - Remote deep cells are **not** authority-routed: disk-image install runs the in-guest helper
     and never calls the optional remote authority binding. Roles stay server/worker/reconciler.
2. **Provider-neutral seams** (`tests/integration/live_stack/`), each a small widening:
   - `image_smoke.Endpoint(host, port)`; `ssh`, `scenario.ssh_probe`, `probe_new_boot`,
     `ssh_endpoint` and `authorize_ssh` carry it instead of a loopback port. `ssh_endpoint` keeps
     the `worker_loopback` assertion and uses the returned `host` (127.0.0.1 locally).
   - `deep_body`'s `staged_kernel(system_id) -> path` hook becomes `installed_kernel(system_id,
     endpoint, key, release) -> (sha256, owned_path | None)`, called after the reconnect and the
     boot-identity proof. Local: the domain XML `<kernel>` file (owned, as today). Remote: the
     guest's `/boot/vmlinuz-<release>` read over SSH as root (inside the guest disk, so nothing
     extra is owned). `release` is the fixture manifest's value, already asserted equal to
     `uname -r`.
   - `deep_body`'s `entry` and `image_smoke.os_matches` take a `GuestIdentity` protocol
     (`distro`, `version`, `arch` properties) that a catalog entry and a remote image both meet.
   - `cleanup.release_and_verify` gains `absent: Callable[[str], bool] = disk_absent`;
     `scenario._cleanup_attempt` takes the frame's cleanup coroutine.
3. **Remote frame and inputs** (`tests/integration/live_stack/remote_lifecycle.py`):
   - `REMOTE_REPRESENTATIVES` (`RemoteImage(name, distro, version, arch)`): fedora →
     `fedora-kdive-remote-base-43` (`fedora:43`), enterprise → `rocky-10-kdive-remote-base`
     (`rocky:10`), both x86_64, the `kdive_image_catalog` rows of
     `deploy/ansible/inventory/group_vars/all.yml`. `REMOTE_BLOCKED`: debian → #3081 (no Debian
     install helper), suse → #3082 (no SUSE remote image). A unit test requires every remote deep
     cell's family to be in exactly one map and every representative to match the Ansible
     catalog's distro/version.
   - **Observer** (`REMOTE_PROVIDER_SSH`, an operator `user@host` SSH destination, validated
     `^[A-Za-z0-9][A-Za-z0-9._@-]*$`; unset → `blocked`; unprefixed because it is a test-only
     input, which `env-docs-check` would otherwise require in the product config catalog): the
     test's own channel to the provider host, independent of the worker's TLS identity.
     `host_probe` runs one fixed command over `ssh -o BatchMode=yes` → `os-release` identity,
     `uname -m`, `systemd-detect-virt`. `observer()` opens
     `qemu+ssh://<dest>/system?no_tty=1` read through libvirt for the domain XML, volume lookups
     and the base volume's streamed SHA-256 (cached per volume per process).
   - `on_remote_system(run, base_url, issuer, db_url, *, project, family, body)`: a blocked
     family, a provider host whose arch is not the cell's, anything but exactly one
     `[[remote_libvirt]]` instance, or no staged `[[image]]` for the family's representative →
     `blocked` before any mutation. It sets `run.observed` `host_os`/`host_arch` from the probe
     (overriding the control-plane identity `CellRun.context` would otherwise record) and
     `image_sha256` from the observer, then allocates `{"mode": "kind", "kind":
     "remote-libvirt"}`; provisions the disk-image profile with that volume; reads the remote
     domain XML (accelerator from `type`, owned disks from `<source file>`); runs `body`; proves
     `cleanup` with `release_and_verify(connect=observer, absent=volume_absent)`. A volume is
     absent only when every active pool has been refreshed and no pool resolves its path.
     Released capacity is proven twice: kdive's accounting (`resources.availability`, as #2809)
     returns to its pre-allocation value, and on the provider host the set of defined `kdive-*`
     domains (`remote_kdive_domains(conn)`) returns to the set observed before the allocation.
     The `provider_host` artifact records `os`, `arch` and `virtualization`
     (`systemd-detect-virt`, e.g. `kvm` for a nested lab host).
   - `bindings` command: the expected `Context` of each #2810 cell (host from the same
     probe, guest from the representative, `kvm`, image digest, kernel inputs as #2809); blocked
     families get null guest fields.
4. **Test** `tests/integration/test_remote_deep_lifecycle_live.py::test_remote_deep_lifecycle`,
   `live_stack`, parametrized over the 8 cells owned by #2810 (remote x86_64); #2818's ppc64le
   remote cells never enter it. `run_cell` →
   `on_remote_system` → `deep_body` (upload with `root_fs="xfs"`; both representatives are XFS).
5. **Docs**: a "Remote deep lifecycle" section in `docs/operating/runbooks/remote-live-stack.md`
   (topology, observer access, a source-restricted firewalld rule opening `ssh_addr:ssh_range` to
   the control plane, image staging, fixtures, bindings, run, assemble, qualify); one
   sentence in `docs/development/coverage-qualification.md`.

## Failure model

1. Actors and deployments: an operator running the stack (server, worker, reconciler) on a
   control-plane host at the candidate SHA, with a separate x86_64 remote libvirt host reachable
   over `qemu+tls://` and over operator SSH; pytest runs on the control-plane host. CI runs only
   unit tests and `just coverage-check`.
2. Invariants and assets: a cell counts only with every assertion backed by an artifact and the
   deployed roles equal to the candidate; the remote domain, its disks and capacity are
   reclaimed; the base volume and other guests on the remote host are untouched; published
   evidence holds no host names, addresses, SSH destinations, URIs or volume paths.
3. Accepted failure classes:
   - A killed pytest leaks its remote allocation until lease expiry; the runbook names the manual
     release.
   - A fixture kernel that cannot boot a representative's userspace or start its guest agent is a
     fixture defect: filed, recorded as that cell's failure, fixture unchanged here.
   - Another kdive allocation on the same provider host during a cell fails its domain-set
     check: the lane requires exclusive ownership of the provider host (#2803 req 4).
   - The observer trusts the provider host's libvirt and sshd answers; a compromised provider host
     is out of scope.
   - The qualifier trusts producer digests (ADR-0686 limit).
4. Covered elsewhere: remote tool cells #3080; ppc64le remote #2818; Debian helper #3081; SUSE
   image #3082; capture/introspection #2814; remote quiescence #2816; release gate #2819.

## Threat model

- Boundaries: guest SSH output (untrusted) → assertions; provider-host SSH and libvirt answers
  → assertions; operator env (`REMOTE_PROVIDER_SSH`) → an `ssh` argv and a libvirt URI.
- Controls: commands sent to guest and host are literals or the fixture's release (validated
  against `uname -r`, `shlex.quote`d); the destination must match the regex above, so it cannot
  start with `-` and be parsed as an `ssh` option; module paths stay confined to the
  upload's `modstage`; evidence artifacts carry counts, digests and OS identities only, and a
  cleanup error records its exception type.
- Out of scope: provider-host compromise (accepted above); the worker's TLS material is never
  read by the test.

## Success

- `just coverage-check` passes; the 8 remote x86_64 deep cells are owned by #2810 and mapped;
  remote ppc64le deep/tool cells by #2818; remote x86_64 tool cells by #3080; no remote tool
  cell shares a scenario ID with a local or service cell.
- Local image smoke and local deep lifecycle behave as before (unit tests unchanged in intent).
- On lab hosts at the deployed candidate, each of the 8 x86_64 remote cells writes one record:
  Fedora and Rocky on both baselines with their real outcomes, Debian and SUSE `blocked`; the
  PR and runbook record the per-cell outcomes, sanitized, with a linked issue for every failure.
