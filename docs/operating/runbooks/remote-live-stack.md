# Runbook: remote live-stack end-to-end bring-up

Operator guide for driving the kdive spine against a remote `qemu+tls://`
libvirt/QEMU host the server/worker tier does not share a filesystem with. It mirrors the
[local live-stack runbook](live-stack.md) and runs the same `live_stack` suite
(`tests/integration/test_remote_live_stack.py`), but adds what a remote host needs over the
local one: worker→host TLS, the gdbstub-port ACL, and object-store reachability for the
two-phase vmcore upload. See [ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) (the operator-run
e2e shape), [ADR-0076](../../adr/0076-remote-libvirt-provider-package.md) (the provider package +
portability gate), [ADR-0079](../../adr/0079-remote-live-debug-transport.md) (the gdbstub ACL +
in-guest debug), and [ADR-0084](../../adr/0084-remote-control-two-phase-vmcore-retrieve.md) (the
two-phase KDUMP capture). See [the live-testing map](live-testing.md) for the test tiers.

This is **operator-run, not CI**: the suite is `live_stack`-marked and CI deselects it. The
preflight skips when remote inventory, the base-volume test input, or the required stack
services are absent. With those inputs present, the tests allocate Systems and intentionally
crash guests; use a disposable test deployment. A preflight pass does not validate image contents
or the built kernel tree.

## Prerequisites

- A reachable libvirt/QEMU host exporting `qemu+tls://…/system`, with x509 mutual TLS configured
  on `libvirtd` or `virtproxyd` (server cert signed by a CA the worker trusts; `no_verify` is forbidden).
- An **operator-staged base-OS qcow2 volume** on the remote host's storage pool, carrying:
  qemu-guest-agent (enabled), a kdump-capable base OS, `drgn`, and a matching
  `vmlinux`/debuginfo. Provisioning verifies the volume **exists**, not its contents — these
  image-content obligations are the operator's (ADR-0078/0079).
- The local stack backends up (Postgres + SeaweedFS + mock OIDC) and the host
  `server`/`worker`/`reconciler` running, exactly as in the [local runbook](live-stack.md)
  steps 1–4. The remote variant changes only the **target** of provisioning, not the control
  plane.
- The repo set up (`just setup`).
- An already-built **x86_64** kernel tree at `KDIVE_KERNEL_SRC` on the test client, including
  the boot image, modules, and unstripped `vmlinux` with a GNU build ID. The tests package and
  upload these artifacts; they do not compile the kernel. See the
  [build and upload guide](../external-build-upload.md).

## 1. Worker → host TLS reachability

The remote provider is **opt-in**: composition registers it only when the host URI is set
(`providers/remote_libvirt/config.py`). The TLS client cert, key, and CA are
**secrets-by-reference** — the worker resolves the refs, materializes them into a private per-op
`pkipath`, points the URI at it, and deletes them on every exit path. The connection identity now
lives in a `[[remote_libvirt]]` instance in the `systems.toml` inventory (ADR-0112), pointed at by
`KDIVE_SYSTEMS_TOML`; declaring one is the provider opt-in gate:

```toml
[[remote_libvirt]]
name = "host"
uri = "qemu+tls://host.example/system"          # control transport
gdb_addr = "192.0.2.10"                            # ACL'd gdbstub listen address (see §2)
gdbstub_range = "47000:47099"                    # per-System port range
ssh_addr = "192.0.2.10"                            # optional: ACL'd SSH-forward addr (see §2.1)
ssh_range = "47200:47299"                        # optional: per-System SSH-forward port range
client_cert_ref = "clientcert.pem"               # mutual-TLS client cert (SecretBackend ref)
client_key_ref = "clientkey.pem"                 # mutual-TLS client key  <!-- pragma: allowlist secret -->
ca_cert_ref = "cacert.pem"                       # CA to verify the libvirtd server cert
base_image = "fedora-kdive-remote-base-43"       # an [[image]] name (the staged base volume)
cost_class = "remote"
vcpus = 16
memory_mb = 65536
```

The libvirt storage pool / network / machine knobs that the inventory model does not carry stay
operational env settings (`KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`, `_NETWORK`, `_MACHINE`).

### Delivering the inventory and TLS refs to the fixed workers

The live stack's workers are the fixed `kdive-live-worker@N` slots (ADR-0574). Their accounts have
no home directory, so they never see the XDG default `~/.config/kdive/systems.toml`. When the
operator's inventory declares a `[[remote_libvirt]]` instance, `scripts/live-stack/worker-lifecycle.sh
start` sends the path the operator's server and reconciler resolve to the root lifecycle witness.
The witness writes it into each slot's environment as `KDIVE_SYSTEMS_TOML`. An inventory with no
remote instance is not sent, so the request a local-only stack sends is unchanged.

Place the inventory where the slot accounts can read it but cannot write it, and point the server
and reconciler at it:

```bash
sudo install -o root -g root -m 0644 systems.toml /etc/kdive/systems.toml
export KDIVE_SYSTEMS_TOML=/etc/kdive/systems.toml
```

Each check below fails before anything starts or stops:

- **Launcher.** It refuses an inventory that any `kdive-worker-N` account cannot read, for example
  one under a 0700 or 0750 home.
- **Witness.** It inspects only metadata and never opens the file. It returns `invalid_request` /
  `correct_request` when the path:
  - is relative or not normalized;
  - traverses a symlink;
  - is not a regular file;
  - can be written by a slot account, through any slot group or `kdive-live-libvirt`, or by other
    users. This covers the file and every ancestor directory, except a sticky one such as `/tmp`.

Adding or removing a `[[remote_libvirt]]` block takes effect at the next `worker-lifecycle.sh
start`. Host entries are re-read on every operation, so replace the file atomically (write a
sibling, then rename) rather than editing it in place. The delivered inventory also governs the
fixed workers' other inventory-driven behavior, such as `[[local_libvirt]] guest_egress`.

The TLS refs resolve under the fixed secrets root `/var/lib/kdive/secrets`. Workers never take
another root, and the launcher refuses to start remote workers while `KDIVE_SECRETS_ROOT` names a
different directory. Keep the remote client material in its own subdirectory:

```text
/var/lib/kdive/secrets                    root:root            0711
/var/lib/kdive/secrets/remote-libvirt     root:kdive-live-libvirt 0750
/var/lib/kdive/secrets/remote-libvirt/*   root:kdive-live-libvirt 0440
```

The Ansible `local_worker_host` role creates the secrets root. On an installer-only host, create it
yourself with `sudo install -d -o root -g root -m 0711 /var/lib/kdive/secrets`. The installer adds both the slot accounts and the operator to `kdive-live-libvirt`. The refs then
read `client_cert_ref = "remote-libvirt/clientcert.pem"`, and likewise for the key and CA. The
launcher also checks that every slot account can traverse the secrets root.

Confirm the worker host can actually reach libvirtd over TLS before running the spine:

```bash
virsh -c "qemu+tls://host.example/system" list --all
```

A failure here surfaces in the spine as a `transport_failure` at the provision or discovery
phase — fix the URI, the cert chain, or host/CA hostname mismatch first.

## 2. The gdbstub-port ACL

The gdb-MI debug tier connects **directly over TCP** from the worker to the host's QEMU gdbstub
port — `qemu+tls://` does not tunnel it. The gdbstub is unauthenticated and unencrypted, so the
**ACL is the auth**: bind it to the worker pool's source only, and one System's port must be
unreachable by other tenants/guests. Each running System gets a distinct port the provisioning
profile allocates and records in the domain XML; the Connect port reads it back.

The `[[remote_libvirt]]` instance's `gdb_addr` is the ACL'd listen address (e.g. `192.0.2.10`) — the
security boundary — and `gdbstub_range` (e.g. `47000:47099`) is the per-System port range. Both
are required instance fields.

`gdb_addr` is a required field and provisioning **fails closed** without it, so the remote
preflight requires it — an unset address skips the suite rather than letting it fail at the
provision phase. Restrict the address + port range to the worker pool's source at the host
firewall; this is a security boundary, not a convenience note.

## 2.1 The SSH-forward ACL (optional agent SSH parity, ADR-0291)

Setting **both** `ssh_addr` and `ssh_range` turns on agent SSH parity with local-libvirt: each
provisioned System gets a per-System QEMU user-mode `hostfwd` bound to `ssh_addr`, its port
allocated from `ssh_range` and recorded in the domain XML. The worker injects the per-System
bootstrap key over the guest agent at provision, so `systems.ssh_info` returns a reachable
`(ssh_addr, port)` and `systems.authorize_ssh_key` appends an agent's key over that forward.

- Both fields are optional; leaving them unset keeps remote at guest-agent-only (no SSH forward,
  no key injected). Setting exactly one is a `configuration_error`. When `ssh_addr == gdb_addr`,
  `ssh_range` must not overlap `gdbstub_range` (they would contend for one host socket).
- `ssh_addr` is a security boundary exactly like `gdb_addr`: restrict `ssh_addr:ssh_range` to the
  worker pool (and any agent host that must SSH in) at the host firewall. The worker→guest SSH does
  not verify the guest host key (`StrictHostKeyChecking=no`), so the ACL is the trust boundary —
  see ADR-0291.
- The base image must carry the SSH-forward return route (ADR-0721). Without it, the provider
  host accepts TCP on the forward but the guest never answers, and `systems.authorize_ssh_key`
  fails `transport_failure`. Rebuild a Fedora or Rocky image staged before that change with
  `force_image_rebuild=true` (see the
  [host setup's image section](remote-libvirt-host-setup.md#2-prepare-guest-images)). The Ubuntu
  24.04 and bare images do not carry it yet (#3091).

## 3. Object-store reachability for the presigned PUT

The guest downloads the installed kernel with a presigned GET and uploads a kdump vmcore
with a presigned PUT. It must reach the endpoint embedded in those URLs, not only the worker.
Follow the [host setup's object-store reachability requirements](remote-libvirt-host-setup.md#3-register-remote-libvirt-on-the-deployment),
including the loopback restriction and guest-to-store firewall route. No standing object-store
credential is installed in the guest.

## 4. The base-image volume (a test/runbook input)

The operator-staged qcow2 base volume is declared as a `staged` `[[image]]` in `systems.toml` and
referenced by the `[[remote_libvirt]]` instance's `base_image` field (ADR-0112). The test also
requires `KDIVE_REMOTE_BASE_IMAGE_VOLUME`; it passes this value directly as the provision
profile's `base_image_volume`. Set it to the same storage-pool volume:

```toml
[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "kdive-base-fedora.qcow2"   # the operator-staged libvirt volume name
```

```sh
export KDIVE_REMOTE_BASE_IMAGE_VOLUME=kdive-base-fedora.qcow2
```

## 5. Run the suite

```bash
just test-live-stack
```

This runs `pytest -m live_stack`, which now collects both the local
(`test_live_stack.py`) and the remote (`test_remote_live_stack.py`) spines. The remote spine
drives allocate(`remote-libvirt`) → provision(disk-image) → upload external build → install → boot →
attach(gdb-MI direct TCP) → force-crash → two-phase KDUMP capture →
introspect(`from_vmcore`) → release → reconciler teardown → accounting report, each step under a
per-project role token.

Three operational notes:

- **The app tier does not hot-reload.** Only the *libvirt host* is remote here; the kdive
  server, worker and reconciler are the same host processes from the
  [live-stack runbook](live-stack.md) §4, and they load your source once, at start. A source fix
  does not reach a running worker until you re-run `scripts/live-stack/stack-services.sh`. A remote spine
  driven against a worker that predates its own fix produced a meaningless green during #1610;
  the version-skew preflight (ADR-0482, live-stack runbook §5) now names that case at preflight
  time instead. `KDIVE_STACK_SKEW_POLICY=off` disables it.
- **Capture budget.** The capture phase drains a job that waits out a ~300s server-side readiness
  window while the guest reboots out of the kdump capture kernel, then uploads. The spine budgets
  900s for it; if the operator's reboot is slower, raise `_CAPTURE_DEADLINE_S` in the remote test.
- **Completion evidence.** A successful run writes `remote-accounting-report.json` to the artifact
  dir (`KDIVE_ARTIFACT_DIR`, or an out-of-tree temp default) — attach it as the record that the
  remote spine completed end-to-end.

## 6. Four-method capture capstone

The remote provider advertises four capture methods —
`{console, host_dump, gdbstub, kdump}` — pinned by the drift guard in
`tests/scripts/test_provider_capture_coverage.py`. The capstone exercise
(`test_remote_four_method_capture_over_the_wire`) proves all four against the live remote spine.
It runs under the same `live_stack` gate as the spine above: configure the prerequisites in steps
1–4, then `just test-live-stack` collects it.

The test chooses two Systems with distinct bound Runs. Both vmcore methods require a CRASHED
System, but `ensure_method_match` binds the stored capture method to the **Run**, not globally
to the System. The topology below is this test's setup, not a requirement that every method
use a different System. Use the Run ID for `vmcore.fetch`; see the
[postmortem guide](../../guide/toolsets/postmortem.md).

| method | System | what it proves |
|--------|--------|----------------|
| `host_dump` | **A**, with a bound Run — provisioned to `ready`, then crashed | host-side `virDomainCoreDumpWithFormat` → storage-pool volume → stream-download ([ADR-0094](../../adr/0094-remote-host-dump-via-coredump-volume.md)); **no** in-guest kdump kernel needed |
| `gdbstub` | **B** — booted | direct-TCP gdb-MI attach to a running System ([ADR-0083](../../adr/0083-remote-connect-debug-plane.md)) |
| `kdump` | **B** — booted, then crashed | the two-phase in-guest capture kernel → presigned-PUT upload ([ADR-0084](../../adr/0084-remote-control-two-phase-vmcore-retrieve.md)) |
| `console` | **B** — boot→crash lifetime | the reconciler-hosted `virDomainOpenConsole` collector ([ADR-0095](../../adr/0095-reconciler-remote-console-collector.md)); the single artifact assembles on teardown-finalize, so it is asserted **after** System B is `torn_down` |

Operator notes:

- **Two crashes, two cores.** A crashed kernel exports VMCOREINFO reliably; an absent VMCOREINFO
  is the documented `configuration_error`, **not** a 4/4 pass — do not accept a missing-build-id
  skip as success.
- **Metering.** The exercise seeds the project for two concurrent allocations/Systems; no extra
  quota staging is needed.
- **Capture budget.** Each vmcore drain reuses the spine's 900s `_CAPTURE_DEADLINE_S`; the kdump
  leg additionally waits out the guest's crash→reboot→upload window.
- **Record.** Attach the run log (the per-phase names identify any failing leg) as the recorded
  evidence that the remote spine reached 4/4.

## 7. Remote deep lifecycle (#2810)

`tests/integration/test_remote_deep_lifecycle_live.py::test_remote_deep_lifecycle` has one
parameter for each `deep-lifecycle/remote-libvirt/x86_64` contract cell: four families times the
two pinned baselines in `fixtures/kernel/baselines.toml`. Fedora runs on
`fedora-kdive-remote-base-43` and Enterprise Linux on `rocky-10-kdive-remote-base`
(`REMOTE_REPRESENTATIVES` in `tests/integration/live_stack/remote_lifecycle.py`). Debian and SUSE
record `blocked`: the in-guest install helper is Fedora/RHEL-only (#3081) and there is no SUSE
remote base image (#3082). A parameter provisions the representative, uploads its baseline's
fixture kernel, completes the build, installs and boots it in the guest, reconnects over the SSH
forward with the same key, checks the running release and GNU build ID against the fixture, reads
the digest of the guest's `/boot/vmlinuz-<release>`, loads the `loop` module and compares its
bytes with the uploaded copy, then releases and proves on the provider host that the domain is
undefined, its volumes are gone and no new `kdive-*` domain remains, with kdive's capacity back to
its starting value. The ppc64le remote cells share the node and stay `missing-result` for #2818.

Topology and prerequisites, in addition to steps 1–4:

- The control plane runs the stack at the candidate SHA, and pytest runs there: the evidence reads
  the deployed role revisions from that host. The provider is a separate x86_64 host, used by
  this lane alone while it runs (another allocation there fails the domain-set check).
- Prepare the provider with the `libvirt_tls` and `libvirt_pool_net` roles and
  `deploy/ansible/playbooks/image.yml` with
  `host_images: [fedora-kdive-remote-base-43, rocky-10-kdive-remote-base]`, and stage both as
  `[[image]]` entries. Images built before #3094 fail the `enterprise` cells at boot (the
  [host setup's image section](remote-libvirt-host-setup.md#2-prepare-guest-images) says why):
  rebuild them with `force_image_rebuild=true`. Declare exactly one `[[remote_libvirt]]`
  instance, with `ssh_addr` and `ssh_range` (§2.1). Allow `ssh_addr:ssh_range` from the control
  plane in the provider's firewall (a source-restricted firewalld rich rule, as `gdbstub_acl`
  writes for the gdbstub range).
- `REMOTE_PROVIDER_SSH=user@host` gives the test its own access to the provider host: an `ssh`
  destination that works non-interactively from the control plane (key and known host entry),
  whose user is in the provider's `libvirt` group. The test reads the host's `os-release`,
  `uname -m` and `systemd-detect-virt` over it, hashes the base volume there with
  `virsh vol-download`, and opens `qemu+ssh://<destination>/system` to observe domains and
  volumes. It never reads the worker's TLS
  material, and the evidence never records the destination.
- Fixtures as in the [live-testing runbook](live-testing.md#deep-lifecycle-across-representative-guests-2809).

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix REMOTE_PROVIDER_SSH=<user>@<provider-host>
export KDIVE_SYSTEMS_TOML=<the stack's systems.toml>  # live tests ignore the XDG default
uv run python -m tests.integration.live_stack.remote_lifecycle bindings --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)
uv run python -m pytest -m live_stack tests/integration/test_remote_deep_lifecycle_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

An unset or malformed `REMOTE_PROVIDER_SSH`, an unreachable provider host, a provider of another
architecture, other than one `[[remote_libvirt]]` instance, an unstaged representative, or a
missing or invalid fixture is `blocked` (`missing-prerequisite`). A cell blocked before the
provider host is observed records the control-plane host, so `qualify` lists context-mismatch
reasons beside `missing-prerequisite` for it; the pytest line names the actual cause. After an
interrupted run, release the leftover allocation with `allocations.release` (or let the lease
expire) and check the provider for a leftover `kdive-*` domain and its overlay volume.

Last run: candidate `b5c5141c8` (server, worker and reconciler at that SHA; both base images rebuilt
from it) on snapshot-capable disposable lab test hosts: an Ubuntu 26.04 x86_64 control plane and a
separate Rocky Linux 10.2 x86_64 provider host, itself a KVM guest (`systemd-detect-virt` `kvm`;
domains run with the `kvm` accelerator, nested). Fixtures `v6.18.54` (longterm) and `v7.2.8`
(stable). Two provider-host steps existed only because of open defects and are not product coverage:
firewalld installed and enabled before `site.yml` (#3083), and a runtime `DOCKER-USER` rule
accepting forwarded traffic to and from `virbr0` so guests reach the object store past docker's
`FORWARD` drop policy (#3093).

| Cell (`deep-lifecycle/remote-libvirt/x86_64/…`) | Guest | Outcome | Failing assertion |
|---|---|---|---|
| `fedora/longterm`, `fedora/stable` | `fedora:43` | success | — |
| `enterprise/longterm`, `enterprise/stable` | `rocky:10` | success | — |
| `debian/longterm`, `debian/stable` | — | blocked | no Debian install helper (#3081) |
| `suse/longterm`, `suse/stable` | — | blocked | no SUSE remote image (#3082) |

`qualify` accepted the Fedora and Enterprise cells; the four blocked cells do not qualify, and
also list context-mismatch reasons, because they stop before the provider host is observed.

## 8. Remote System tool cells (#3080)

`tests/integration/test_system_tool_cells_live.py::test_system_tool_cell` also carries the 120
x86_64 remote-libvirt cells of `systems.provision`, `systems.ssh_info`,
`systems.authorize_ssh_key`, `systems.check_ssh_reachable`, `systems.reprovision` and
`systems.teardown`: two configurations, two exposures, one functional and four rejection cells
each. Their parameter ids contain `remote-libvirt`, so `-k remote-libvirt` selects them and leaves
the local cells of the [live-testing runbook](live-testing.md#system-lifecycle-tool-cells-3062)
out. The frame, the per-tool effects and the rejection boundaries are the local cells'; the
[design](../../workflow/specs/2026-10-06-remote-system-tool-cells-design.md) lists what differs.
Every remote cell boots `fedora-kdive-remote-base-43` on the provider host. Its domain XML, its
volumes and the provider's `kdive-*` domain set are read over the §7 observer, and the cell
records the provider host's OS and architecture as its host. `systems.ssh_info` must answer the
domain's own `hostfwd` address and port, which is `ssh_addr` here.

A rejection cell aims at one target per stack and provider: a remote System provisioned, observed,
torn down and released by the first remote rejection cell. That cell's project then must not
change. Each rejection record carries the target's provider-host, guest and image identity and its
cleanup proof.

Prerequisites, in addition to §7's topology, observer access and SSH-forward rule:

- `fedora-kdive-remote-base-43` is staged on the provider and declared as an `[[image]]`. The
  Rocky representative is not needed.
- The control-plane stack runs the tool-cell lanes of the live-testing runbook: the sourced
  `env.sh`, `KDIVE_DATABASE_URL="$KDIVE_MIGRATION_DATABASE_URL"`, an exported
  `KDIVE_SYSTEMS_TOML` that declares the `[[remote_libvirt]]` instance, and the `default` and
  `KDIVE_WORKER_DEATH_VERIFIER=docker` (`recovery`) bring-ups.

```bash
sha=$(git rev-parse HEAD)
export REMOTE_PROVIDER_SSH=<user>@<provider-host> KDIVE_SYSTEMS_TOML=<the stack's systems.toml>
uv run python -m tests.integration.live_stack.tool_cells bindings --remote --candidate "$sha" --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)        # one evidence root for both lanes
examples/local-libvirt/demo-up.sh             # default configuration
uv run python -m pytest -m live_stack tests/integration/test_system_tool_cells_live.py -k remote-libvirt
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery configuration
uv run python -m pytest -m live_stack tests/integration/test_system_tool_cells_live.py -k remote-libvirt
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

`bindings --remote` observes the provider host first and exits 2 naming the blocker when it
cannot. Each pytest run records the 60 cells of its configuration and skips the other 60. A
blocked remote prerequisite (§7's list) blocks the cell. A cell blocked before the provider host
is observed records the control-plane host, so `qualify` adds context-mismatch reasons beside
`missing-prerequisite`. After the run, `demo-down.sh --wipe --yes` clears the stack. Then check
the provider: `virsh list --all` shows no `kdive-` domain, and the pool holds no `kdive-` overlay
volume.

Last run: candidate `ee349c788` (server, worker and reconciler at that SHA in both lanes; later
commits change only this record). The control plane was a disposable Fedora 44 x86_64 lab guest
with SELinux enforcing. The provider was a separate disposable Rocky Linux 10.2 x86_64 host, itself
a KVM guest whose domains ran with the `kvm` accelerator (nested), prepared with `site.yml` and
`image.yml` for `fedora-kdive-remote-base-43` alone. One provider-host step existed only because
of an open defect and is not product coverage: firewalld was installed and enabled before
`site.yml` (#3083). The §7 `DOCKER-USER` rule (#3093) was not needed, because these cells never
send guest traffic to the object store. Each lane recorded its 60 cells in about eight minutes and
skipped the other lane's 60. The `KDIVE_WORKER_DEATH_VERIFIER=docker` lane proved `recovery`.
`qualify` reported all 120 qualified: 24 `success` and 96 `rejection`, every record carrying the
provider host as `rocky:10.2` and the guest as `fedora:43`. No record or artifact carried a host
name or address. After the run the provider defined no `kdive-` domain and held only its base
volume, and `demo-down.sh --wipe --yes` cleared the stack.

## 9. Remote run and image tool cells (#3120)

`tests/integration/test_run_tool_cells_live.py::test_run_tool_cell` also carries the 96 x86_64
remote-libvirt cells of `runs.install`, `runs.boot`, `runs.cancel`, `runs.release_external_boot`
and `images.publish`. Their parameter ids contain `remote-libvirt`, so `-k remote-libvirt` selects
them. The bodies and rejection boundaries are the local cells' of the
[live-testing runbook](live-testing.md#run-and-image-tool-cells-3119), on §8's remote lane; the
[design](../../workflow/specs/2026-10-07-remote-run-tool-cells-design.md) lists what differs.

- `runs.install` and `runs.boot` cells provision `fedora-kdive-remote-base-43`, upload the
  `longterm` fixture and run §7's checks, with the tool under test called through the cell's
  exposure. The install is in-guest, so the installed kernel is the digest of the guest's
  `/boot/vmlinuz-<release>`.
- A `runs.cancel` cell cancels an uploaded Run whose build it never completed. The System stays
  ready, the guest keeps its `boot_id`, and the System is freed for a new Run.
- The four `runs.release_external_boot` functional cells stop `blocked`: no runbook provisions a
  remote provider authority, and an authority on the instance would route every remote install
  and boot through external boot.
- The four `images.publish` functional cells stop `blocked`: the `IMAGE_BUILD` handler builds
  catalog images for local-libvirt only. Remote base images are staged with
  `deploy/ansible/playbooks/image.yml`.
- Both blocked kinds first probe the provider host, so their records carry it as their host.
- `runs.*` rejection cells aim at one unbound `remote-libvirt` Run in the stack's remote System
  target project. `images.publish` rejection cells aim at `fedora-kdive-remote-base-43` with
  provider `remote-libvirt`; their snapshot holds that name's catalog rows and build jobs.

Prerequisites, in addition to §8's:

- A verified `longterm` kernel fixture under `KDIVE_FIXTURE_ROOT`, built as in the
  [live-testing runbook](live-testing.md#deep-lifecycle-across-representative-guests-2809).
- `KDIVE_S3_ENDPOINT_URL` set, before the bring-up, to an object-store address the remote guests
  reach (§3): the install downloads the kernel in the guest. If the provider host runs Docker,
  its `FORWARD` drop policy also needs §7's `DOCKER-USER` rule (#3093).

```bash
sha=$(git rev-parse HEAD)
export KDIVE_FIXTURE_ROOT=$HOME/kfix REMOTE_PROVIDER_SSH=<user>@<provider-host>
export KDIVE_SYSTEMS_TOML=<the stack's systems.toml> KDIVE_S3_ENDPOINT_URL=<guest-reachable URL>
uv run python -m tests.integration.live_stack.tool_cells bindings --remote --candidate "$sha" \
  --out inputs.json --kernel-baseline longterm
export KDIVE_ARTIFACT_DIR=$(mktemp -d)        # one evidence root for both lanes
examples/local-libvirt/demo-up.sh             # default configuration, on a wiped stack
uv run python -m pytest -m live_stack tests/integration/test_run_tool_cells_live.py -k remote-libvirt
examples/local-libvirt/demo-down.sh --wipe --yes
KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh  # recovery configuration
uv run python -m pytest -m live_stack tests/integration/test_run_tool_cells_live.py -k remote-libvirt
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

Each configuration records 48 cells: 6 `success`, 38 `rejection` and 4 `blocked`, and skips the
other configuration's 48. `qualify` then qualifies 88 of the 96 and lists the 8 blocked cells,
so it exits non-zero by design. After an interrupted run, release the leftover allocation as §7
says before the wipe. After the run, `demo-down.sh --wipe --yes` clears the stack. Then check the
provider: `virsh list --all` shows no `kdive-` domain, and the pool holds no `kdive-` overlay
volume.

Last run: candidate `da633d359` (server, worker and reconciler at that SHA in both
configurations; later commits change only this record). The control plane was a disposable
Fedora 44 x86_64 lab guest with SELinux enforcing, with the `longterm` fixture `v6.18.54` built
there. The provider was a separate disposable Rocky Linux 10.2 x86_64 host with SELinux
enforcing, itself a KVM guest whose domains ran with the `kvm` accelerator (nested), prepared
with `site.yml` (a fresh PKI from `playbooks/pki.yml`) and `image.yml` for
`fedora-kdive-remote-base-43` alone. One provider-host step existed only because of an open
defect and is not product coverage: firewalld was installed and enabled before `site.yml`
(#3083). The provider runs no Docker, so §7's `DOCKER-USER` rule (#3093) was not needed; the
guests reached the object store at the control plane's LAN address. Each configuration recorded
its 48 cells on a freshly wiped stack in about ten minutes and skipped the other 48. The
`KDIVE_WORKER_DEATH_VERIFIER=docker` lane proved `recovery`.

`qualify` qualified 88 of the 96 cells: 12 `success` (`runs.install`, `runs.boot` and
`runs.cancel`, both exposures, both configurations) and all 76 `rejection` cells, each record
carrying the provider host as `rocky:10.2` and the guest as `fedora:43`. The eight blocked
functional cells recorded the provider host but no guest or kernel, so `qualify` lists
`input-digest-mismatch` and `input-context-mismatch` beside `missing-prerequisite` for them, and
`deployed-role-missing` for the four release cells, which require the `authority` role. One
`images.publish` call for `remote-libvirt` with a name no image carries, made on the default
stack after its cells, enqueued an `IMAGE_BUILD` job that failed on its first attempt with
`configuration_error`, "provider-specific image build request is not implemented", provider
`remote-libvirt`. No record or artifact carried a host name or address. After each configuration
and after the final `demo-down.sh --wipe --yes`, the provider defined no `kdive-` domain and its
pool held only the base volume.
