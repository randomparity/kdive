# local-libvirt walkthrough

End-to-end setup for the local-libvirt provider, where the KDIVE worker drives QEMU/KVM
guests on its own host. This page is the linear path from a **bare libvirt-capable host** to a
booted System; for the provider's reference config see
[the local-libvirt provider reference](local-libvirt.md).

> **Deployment:** the KDIVE app processes run as **host processes** on the libvirt host, so the
> worker has native `/dev/kvm` and libvirt access. The app-tier-only
> [docker-compose](../docker-compose.md) worker has no KVM/libvirt access and cannot drive this
> provider — compose is used here only for the **backends** (Postgres, MinIO, OIDC).

> **No `just` required.** `just` is a developer/CI task runner. Everything below uses the
> packaged commands (`python -m kdive …`) and the `scripts/*.sh` helpers directly, so an operator
> never needs `just` or the dev workflow. The examples assume the repo is checked out and the
> project virtualenv is at `.venv` (Step 1).

## 1. Get the code and install host packages

Clone the repo (the host-process deployment runs from this checkout, and the `scripts/`,
`deploy/systemd/`, and example-inventory files live here):

```bash
git clone https://github.com/randomparity/kdive.git ~/kdive
cd ~/kdive
```

Report the host packages KDIVE needs (distro-aware), then install them. The reporter lists a
dev/CI tier (`just`, `prek`, `node`, `npm`) you can ignore for an operator host:

```bash
./scripts/check-setup-deps.sh        # report-only; prints the install hints below
```

On Debian/Ubuntu the operator set is:

```bash
sudo apt-get update
sudo apt-get install -y \
  pkg-config libvirt-dev libvirt-daemon-system libvirt-clients \
  qemu-system-x86 qemu-utils qemu-kvm \
  libguestfs-tools python3-guestfs passt \
  gcc make flex bison bc libssl-dev libelf-dev rsync xz-utils git \
  docker.io docker-compose-v2 gdb
```

> **On a POWER (`ppc64le`) host** the set above targets an `x86_64` host. Swap `qemu-system-x86`
> for `qemu-system-ppc` — the native POWER emulator, which `./scripts/check-setup-deps.sh` names
> for you — and install a **Rust toolchain** before the `uv sync` below: `pydantic-core` and the
> `just`/`prek` CLIs have no `ppc64le` wheels and build from source
> ([rustup](https://rustup.rs); [ADR-0360](../../adr/0360-arch-aware-rust-dep-check.md)).
> A ppc64le guest on a POWER host runs native under KVM-HV. The
> [cross-platform development guide](../../development/cross-platform.md) and the
> [POWER host bring-up runbook](../runbooks/power-host-bringup.md) cover the POWER path end to end.

Add yourself to the `libvirt`, `kvm`, and `docker` groups, then **start a new login shell** so the
membership takes effect:

```bash
sudo usermod -aG libvirt,kvm,docker "$USER"   # log out/in (or `exec su -l "$USER"`) afterwards
```

Build the project virtualenv with [`uv`](https://docs.astral.sh/uv/) (no published wheel; the venv
is built from this checkout). `libvirt-python` compiles against `libvirt-dev`, installed above:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is not already present
uv sync                                           # creates .venv with kdive importable
.venv/bin/python -m kdive --help                  # sanity check
```

Create the host directories the worker stages into (world-traversable, **not** under a private
`$HOME` — `0700` hides the staged kernel from the `qemu` user that boots the VM):

```bash
sudo install -d -o "$USER" -m 0755 /var/lib/kdive/install /var/lib/kdive/console \
  /var/lib/kdive/rootfs/local /var/lib/kdive/build
```

> **Debian/Ubuntu libguestfs notes** (needed for the Step 6 image build, harmless otherwise):
> - libguestfs builds an appliance from the **host** kernel, which Debian/Ubuntu ship `root:0600`.
>   Make them readable or the appliance fails with `cp: cannot open '/boot/vmlinuz-…'`:
>   `sudo chmod 0644 /boot/vmlinuz-*` (re-apply after a kernel upgrade, or use `dpkg-statoverride`).
> - libguestfs uses `passt` for the appliance's network (needed by `virt-builder --install`). On
>   Ubuntu 24.04 this can fail with `libguestfs error: passt exited with status 1`. Unloading the
>   `passt` AppArmor profile (`sudo apparmor_parser -R /etc/apparmor.d/usr.bin.passt`) clears one
>   cause, but a libguestfs/passt version mismatch may still block it; if so, build the rootfs on a
>   host with a working libguestfs appliance, or stage a prebuilt bootable qcow2 (see Step 6).
> - Both failures now report an actionable `configuration_error` from `build-fs` instead of a raw
>   tool dump, and the kernel-readability case is flagged by the preflight (Step 2) when run as the
>   worker user — see [ADR-0222](../../adr/0222-ubuntu-build-fs-libguestfs-diagnostics.md).

## 2. Run the preflight

Check the host (report-only; it never changes anything). Point it at the venv interpreter:

```bash
KDIVE_PYTHON="$PWD/.venv/bin/python" ./scripts/check-local-libvirt.sh
```

Fix what it reports. One failure is expected to remain on most hosts and is **not** fatal for the
core lifecycle: the `import guestfs, drgn` check is only needed for the **kdump capture** method
(Step 5). See [kdump capture prerequisites](#kdump-capture-prerequisites) for the `drgn`/libguestfs
wiring and a Python-version caveat.

The preflight also flags an unreadable host kernel (`/boot/vmlinuz-*`), which blocks the Step 6
`build-fs` image build on Debian/Ubuntu; fix it with the `chmod` above. Run the preflight as the
worker user, since it checks readability as whoever invokes it.

## 3. Bring up the backends and start the host processes

Bring up the backing services with compose (backends only — not the app tier):

```bash
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
```

The host processes read their backend connection from `KDIVE_*` environment variables. The
backends above publish on host ports, so the **host-process** values are (these mirror the comment
block at the top of `docker-compose.yml`; note OIDC is published on **8090**):

```bash
cat > ~/kdive/.kdive-host.env <<'EOF'
export KDIVE_DATABASE_URL=postgresql://kdive:kdive@localhost:5432/kdive   # pragma: allowlist secret - local demo only
export KDIVE_OIDC_ISSUER=http://localhost:8090/default
export KDIVE_OIDC_JWKS_URI=http://localhost:8090/default/jwks
export KDIVE_OIDC_AUDIENCE=kdive
export KDIVE_S3_ENDPOINT_URL=http://localhost:9000
export KDIVE_S3_BUCKET=kdive-artifacts
export KDIVE_S3_REGION=us-east-1
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin     # pragma: allowlist secret - local demo only
export KDIVE_PYTHON=$HOME/kdive/.venv/bin/python
EOF
source ~/kdive/.kdive-host.env
```

Apply the schema, then start the three processes (a real deployment runs them under systemd — see
[systemd](../systemd.md); shown here as plain processes for clarity):

```bash
.venv/bin/python -m kdive migrate
.venv/bin/python -m kdive server     &   # MCP HTTP API
.venv/bin/python -m kdive worker     &   # runs provision/build/install/capture jobs
.venv/bin/python -m kdive reconciler &   # drift-repair AND provider discovery
```

The **reconciler runs discovery**: it enumerates `qemu:///system`, creates the local-libvirt
resource row, and probes its vcpus/memory_mb ceiling. It must be running for the resource to exist.

### Guest-CPU visibility and pinning

Discovery advertises the host's native CPU (`host_cpu`) and the per-arch set of CPU models the host
can pin (`selectable_cpus`) on `resources.describe`. An existing host must be **re-discovered** (let
the reconciler enumerate it again) to gain these fields after upgrading to this version; until then
they are absent and a CPU pin is rejected. An agent pins a guest CPU per-System with the local-libvirt
profile's `cpu.model`, chosen from `selectable_cpus[arch]` — pin a portable `x86-64-vN` rung for a
deterministic reproducer. Admission validates only that the **host** can deliver the model, not that
the rootfs **image** can run on it: a model below the image's ISA floor (`x86-64-v2` for
EL9/RHEL-family) produces a non-booting System. `systems.get` reports the System's actual booted CPU
in `resolved_cpu` — a live reading of the running domain for local Systems (a host-passthrough guest
resolves to the host CPU; a TCG machine-default the host does not expand reads `null`).

### Worker privilege under `qemu:///system`

Provisioning a System to `ready` works with a **non-root** worker — it never reads the guest
console. But every **post-boot** plane needs the worker to read files that QEMU/`virtlogd` write as
`root:0600` under `qemu:///system`, so the worker must run as **root** (or be given group
read/remove access to the paths below — or use `qemu:///session`, where the worker owns the QEMU
process and its files). Two seams require it:

- **Build → boot confirmation.** The boot-readiness preflight tails the guest console log that
  `virtlogd` writes `root:0600` to detect boot-to-multiuser; a non-root worker gets a
  `PermissionError` and the boot step fails with `configuration_error` — a host identity/permission
  misconfiguration, not a transient infrastructure failure ([ADR-0223](../../adr/0223-local-libvirt-worker-readability-diagnostics.md)).
  Because every later plane is gated on a confirmed boot, this blocks **debug, introspection, and all
  capture methods** at once.
- **host_dump capture.** `virDomainCoreDumpWithFormat` runs as the QEMU/root process and writes the
  core to a `root`-owned temp file; the worker must read and remove it. A non-root worker gets
  `configuration_error` with the same operator-fix guidance (ADR-0223), not an opaque `[Errno 13]`.

So a non-root worker can provision but cannot confirm a boot or capture a host_dump. This is the
worker-privilege gap tracked in [#699](https://github.com/randomparity/kdive/issues/699);
`scripts/check-local-libvirt.sh` now prints a non-failing advisory when it detects a non-root worker
under `qemu:///system`, so the constraint is surfaced before a run. The kdump-only note under
[Declare your inventory](#kdump-capture-prerequisites) is one instance of this broader requirement,
not a kdump-specific one.

The kernel itself is built off-worker: you compile it locally and upload the artifacts on the
build lane (see [Build lane](../external-build-upload.md)), so the worker never compiles kernel
source and needs no build toolchain.

## 4. Onboard the project

A fresh database has no quota or budget, so the first `allocations.request` would dead-end on
`quota_exceeded`. Seed the demo project's budget and quota:

```bash
.venv/bin/python -m kdive seed-project \
  --project demo --limit-kcu 1000000 \
  --max-concurrent-allocations 4 --max-concurrent-systems 4
```

`seed-project` is the token-less bootstrap (raw inserts, no audit row). To onboard through the
audited, role-gated admin tools instead, use `./scripts/setup-local-libvirt.sh` with
`KDIVE_SETUP_AUDITED=1` and a project-`admin` token from a claims-asserting issuer — see
[Project onboarding](../project-onboarding.md). (That script re-runs the Step 2 preflight and
aborts if **any** check fails, including the kdump-only `guestfs`/`drgn` one, so prefer the direct
`seed-project` above unless you have wired those deps.)

## 5. Declare your inventory

The reconciler's discovery already created a **grantable** local-libvirt resource (it carries the
seeded `local` cost class, which is priced), so after Step 4 an `allocations.request` is granted.
What `systems.toml` adds is the rest of the inventory the **lifecycle** needs: the **image** a
System boots and (optionally) a custom cost class. A fresh or broken file fails the reconcile pass
with a `configuration_error`, so it is still worth getting right.

`systems.toml` is the single declarative source of truth for the inventory the app loads into the
database (ADR-0112). Its default path is the per-user XDG location
`~/.config/kdive/systems.toml` (there is no working-directory fallback; set `KDIVE_SYSTEMS_TOML`
to point elsewhere).

Start from the minimal, local-only example —
[`examples/systems-local-libvirt.toml`](examples/systems-local-libvirt.toml). It declares one
image and one priced cost class for the `qemu:///system` host; the full multi-provider reference
is `systems.toml.example` at the repo root.

```bash
mkdir -p ~/.config/kdive
cp docs/operating/providers/examples/systems-local-libvirt.toml ~/.config/kdive/systems.toml
.venv/bin/python -m kdive reconcile-systems --check   # validate only (no DB/S3 writes); exits 0 when valid
.venv/bin/python -m kdive reconcile-systems           # apply: creates the image and cost class
```

`reconcile-systems` creates the image and cost-class coefficient, and **binds** the discovered
local-libvirt resource (matched by `host_uri`) to your declared `name`, `cost_class`, and
`concurrent_allocation_cap`. The local-libvirt resource row itself is created and sized by
discovery (Step 3), not by this file; if you reconcile before discovery has enumerated the host,
the overlay logs a benign `no discovered local-libvirt host … overlay deferred` warning and
converges on the next reconciler pass.

The image is declared with a **`staged-path`** source (ADR-0228): the source is the rootfs FILE on
local disk (the path `build-fs` writes to in Step 6), so its catalog row seeds **`registered`**
(bootable) immediately — no object-store upload. That makes it discoverable from the MCP surface
alone: `fixtures.list` / `systems.profile_examples` list it, and a System can be provisioned with a
`catalog` reference (`{kind = "catalog", provider = "local-libvirt", name = "fedora-kdive-ready-44"}`)
rather than a host path (Step 6). The file need not exist yet at reconcile time (declared, not
probed) — provisioning re-validates it against the provider `allowed_roots`, so build it before you
provision. (Declare an `s3` source instead only if you publish the qcow2 to the object store; an
`s3` row with no `digest` stays `defined` until published.)

### kdump capture prerequisites

Provisioning the inventory above is enough to provision and boot. **kdump vmcore capture** (the
`kdump` method) needs extra one-time host setup, because the capture is host-side: the guest's
kdump writes `/var/crash/<ts>/vmcore` (booting its crash kernel via `kexec`), then the worker
force-stops the domain and harvests the core from the qcow2 overlay with libguestfs. Concretely:

- **Run the worker as `root`** (or grant the equivalent group access) — see
  [Worker privilege under `qemu:///system`](#worker-privilege-under-qemusystem). kdump is one of the
  several post-boot planes that need it; it is also the natural identity for `kexec` and libguestfs.
- **Wire `drgn` + `libguestfs` into the worker venv** — `uv sync --group live` pulls `drgn`; the
  system `guestfs` binding is wired separately. **Caveat:** the binding is an ABI-locked system
  package built for the **distro** Python (e.g. 3.12 on Ubuntu 24.04), while `uv` builds the venv
  on the **project** Python (3.14); the symlink wiring in the runbook only works when those minor
  versions match. Absence is a `missing_dependency`, not a silent skip.
- **Prepare the install-staging and console host directories** (done in Step 1).

These are detailed in the four-method runbook's
[§4b kdump](../runbooks/four-method-live-run.md#4b-kdump) section. The kernel-config symbols a kdump
build must carry to actually arm are tracked in
[#688](https://github.com/randomparity/kdive/issues/688); the example's `kdump` build-config
already includes that arming set.

### drgn-live introspection prerequisites

`introspect.run` (live drgn over the guest's own `/proc/kcore`) and a `drgn-live`
`debug.start_session` reach the guest over a loopback-forwarded SSH port (ADR-0218/0219), not over
any capture-method machinery. drgn-live is therefore **orthogonal** to a System's capture method —
a kdump, gdbstub, host_dump, or plain console System can each carry it.

**No credential setup is required (ADR-0315).** The loopback SSH NIC and `hostfwd` are rendered on
**every** local domain (ADR-0281), and the SSH transport authenticates with the System's own
bootstrap private key (ADR-0289): a unique ed25519 keypair generated at provision, whose public half
is injected into that System's overlay and whose private half the server loads from
`system_bootstrap_keys` for the duration of the SSH call. So a `drgn-live` `debug.start_session`
works on any ready local System with no profile field to set and no key file to stage — it fails
closed with `configuration_error` (`reason="no_bootstrap_key"`) only if a System has no bootstrap
key at all. Catalog images bake no credential; every provisioned System carries its own key
regardless of which debug rootfs it boots.

The debug rootfs also supplies the two guest-side pieces drgn needs, all automatic for the
catalog debug image and a from-source build (listed here so a failure is diagnosable): the
`kdive-drgn` helper at `/usr/local/sbin/` and `drgn` itself (ADR-0220), plus the per-Run DWARF
`vmlinux` that the install step stages at `/usr/lib/debug/lib/modules/<ver>/vmlinux` so `drgn -k`
can resolve typed kernel symbols (ADR-0221). A System missing the helper returns
`debug_attach_failure`; one missing the vmlinux raises a drgn `ObjectNotFoundError` for the first
typed symbol.

## 6. Test the lifecycle

The lifecycle steps below are MCP tool calls, but they need one **host-side prerequisite**: a
bootable, kdive-ready rootfs qcow2 on disk at the path your `staged-path` `[[image]]` (Step 5)
declares. Build that first, then connect a client and drive the MCP calls.

### Build and install the rootfs image(s)

Build images from the declarative rootfs catalog with `build-fs --image <name>` (ADR-0251). The
catalog (`fixtures/local-libvirt/rootfs_catalog.toml`) ships these debug-guest entries:

| `--image` | base | default `kdump` capture for a v7.0-class kernel |
|---|---|---|
| `fedora-kdive-ready-44` | Fedora 44 (makedumpfile 1.7.9) | **complete filtered core** |
| `fedora-kdive-ready-43` | Fedora 43 (1.7.8) | incomplete → use `method="host_dump"` |
| `rocky-kdive-ready-8` / `-9` / `-10` | Rocky 8/9/10 | incomplete → use `method="host_dump"` |
| `centos-stream-kdive-ready-9` / `-10` | CentOS Stream 9/10 | incomplete → use `method="host_dump"` |
| `debian-kdive-ready-12` / `-13` | Debian 12/13 (makedumpfile 1.7.2 / 1.7.6) | incomplete → use `method="host_dump"` |

Only Fedora 44 ships a makedumpfile new enough (≥ 1.7.9) to filter a v7.0 vmcore via the default
`kdump` method; the others disclose `kdump_core_incomplete` and capture via `host_dump` instead (the
rest of the lifecycle — provision/build/install/boot — is identical). The full per-release table is
in the [image-lifecycle runbook](../runbooks/image-lifecycle.md).

`--image` resolves the row's pinned base, the family's package set (the EL-version-aware `rhel`
customizer for Fedora/Rocky/CentOS, the apt-based `debian` customizer for Debian), and **destination**
(`/var/lib/kdive/rootfs/local/<name>.qcow2` — exactly the `staged-path` your inventory declares), so
no `--dest` is needed. Point `--workspace` at a **user-writable** path (the default
`/var/lib/kdive/build/images` is root-owned); the build stages there and publishes the finished
qcow2 to the catalog destination:

```bash
# the kdump-capable default; re-run per distro you want to exercise
.venv/bin/python -m kdive build-fs --image fedora-kdive-ready-44 \
  --workspace ~/.local/share/kdive/build/images
.venv/bin/python -m kdive build-fs --image rocky-kdive-ready-9 \
  --workspace ~/.local/share/kdive/build/images
.venv/bin/python -m kdive build-fs --image debian-kdive-ready-12 \
  --workspace ~/.local/share/kdive/build/images
```

The build needs the Step 1 libguestfs tooling and network access — the EL-family images
`dnf install` their crash toolchain at customize time (Rocky 8 enables EPEL for `drgn`
automatically), and the Debian images `apt install` theirs (`kdump-tools`, `python3-drgn`, `crash`).
The Debian build is otherwise the same flow and needs no distro-specific workaround. (On Ubuntu
24.04 the libguestfs `--install` step may be blocked by the passt/libguestfs mismatch noted in
Step 1; build on a Fedora host or stage a prebuilt qcow2.)

**Label the image for `qemu:///system` — the easy step to miss.** When `--workspace` is under
`$HOME`, the cross-filesystem publish move can leave the qcow2 with the home SELinux type
(`data_home_t`), which the `qemu` user **cannot read** under SELinux Enforcing — provisioning then
fails to open the disk with a permission error. Give the rootfs directory a persistent
`virt_image_t` rule once, then relabel:

```bash
sudo semanage fcontext -a -t virt_image_t '/var/lib/kdive/rootfs/local(/.*)?'
sudo restorecon -Rv /var/lib/kdive/rootfs/local
```

`semanage fcontext` makes the rule durable across a full system relabel (a bare `chcon` does not);
`semanage` is in `policycoreutils-python-utils`. `restorecon` skips files already at a customizable
virt type — both `virt_image_t` and `virt_content_t` are qemu-readable and left alone — and corrects
a stray `data_home_t`. The published file is mode `0644` (world-readable), so its build-user
ownership is fine for a read-only base image; only if libvirt's dynamic ownership complains on first
provision would you also `sudo chown qemu:qemu` the file.

**Register each image you built.** Add one `staged-path` `[[image]]` block per rootfs to
`~/.config/kdive/systems.toml` (the Step 5 pattern) — `name` = the catalog `--image`, `path` =
`/var/lib/kdive/rootfs/local/<name>.qcow2` — then re-run `reconcile-systems`. The repo-root
`systems.toml.example` carries all the RHEL-family rows as a copy-paste reference. Each then seeds a
`registered` catalog row a System can boot by `catalog` reference.

### Connect an MCP client

The server speaks streamable HTTP and expects an `Authorization: Bearer <token>` header. Point
your MCP client at it with a `.mcp.json` in the tree you want to drive (your kernel source
checkout); the token is read from `${KDIVE_TOKEN}` at connect time, so the file itself holds no
secret:

```json
{
  "mcpServers": {
    "kdive": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer ${KDIVE_TOKEN}" }
    }
  }
}
```

Mint the token from the **host-process** mock issuer with
[`examples/local-libvirt/mint-token.sh`](../../../examples/local-libvirt/mint-token.sh) (this is
the host-mode equivalent of the live-stack runbook's mint; the Kubernetes `scripts/demo-token.sh`
does not apply here). The token must grant the **same project you seeded in Step 4** — `demo` in
this walkthrough — or every project-scoped call dead-ends on `quota_exceeded`:

```bash
export KDIVE_TOKEN=$(KDIVE_PROJECT=demo examples/local-libvirt/mint-token.sh)
```

The token carries `roles={demo: admin}` plus the platform roles, so it reaches every tool; it
expires after `KDIVE_TOKEN_TTL` seconds (default 12h). The client expands `${KDIVE_TOKEN}` **once,
when it connects**, so after a token expires you must re-export it and then **reconnect** the
`kdive` server in your client (in Claude Code: `/mcp` → reconnect), not just re-run the export.

The [`examples/local-libvirt/`](../../../examples/local-libvirt/) helpers automate this end to
end: `up.sh` installs the `.mcp.json` into `KDIVE_KERNEL_SRC` (merging, not clobbering, any
existing file) and starts the trio; see that example's README for the full bring-up. Note the
example seeds and tokenises a project named `local` by default — set `KDIVE_PROJECT=demo` to match
this walkthrough, or seed `local` instead of `demo` in Step 4.

**Request an allocation.** With the project onboarded and the resource discovered, this is granted:

```text
allocations.request project=demo request={
  "vcpus": 2, "memory_gb": 2, "disk_gb": 10,
  "resource": {"mode": "kind", "kind": "local-libvirt"}
}
# → status=granted; suggested_next_actions: [allocations.get, systems.provision, allocations.release]
```

**Provision a System.** Provision boots a qcow2 rootfs from disk, so it needs the **bootable image**
you built and labeled in [Build and install the rootfs image(s)](#build-and-install-the-rootfs-images)
above (or any prebuilt bootable qcow2 staged at a world-readable, `virt_image_t`-labeled path under
`/var/lib/kdive/rootfs/local/`). The `staged-path` `[[image]]` you reconciled (Step 5) points at
exactly that file, so once it exists the catalog row resolves it. Local-libvirt provisioning uses `boot_method: direct-kernel`
(`kernel_source_ref` is required by the schema but only used by a later build Run; provision
extracts and boots the rootfs's own baseline kernel via direct-kernel boot). `disk_gb` must equal
the allocation's (ADR-0205).

**Recommended — provision by `catalog` reference** (what a host-shell-free agent does: discover the
name via `fixtures.list` / `systems.profile_examples`, then paste it):

```text
systems.provision allocation_id=<granted id> profile={
  "schema_version": 1, "arch": "x86_64", "vcpu": 2, "memory_mb": 2048, "disk_gb": 10,
  "boot_method": "direct-kernel", "kernel_source_ref": "/path/to/linux",
  "provider": {"local-libvirt": {"rootfs":
    {"kind": "catalog", "provider": "local-libvirt", "name": "fedora-kdive-ready-44"}}}
}
# → status=queued; the worker define+starts a tagged libvirt domain. Poll systems.get until ready.
```

The `local` host-path form still works when you have not declared an `[[image]]` (it needs no
inventory, but the path is invisible to an agent without host access):

```text
  "provider": {"local-libvirt": {"rootfs":
    {"kind": "local", "path": "/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2"}}}
```

> **On a POWER (`ppc64le`) host**, set `"arch": "ppc64le"` in the profile and boot the catalog's
> `fedora-kdive-ready-44-ppc64le` image; the rest of the flow is identical. The domain's machine
> type (`pseries`), console (`hvc0`), and CPU model are derived from the profile arch. The guest
> runs native under KVM-HV on POWER. A **cross-arch** `ppc64le` guest on an `x86_64` host instead
> runs under TCG emulation, where the provider scales boot-readiness deadlines by
> `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10.0`) — see
> [Cross-architecture guests](../install.md#cross-architecture-guests).

A successful provision yields a running `kdive-<system-id>` domain (`virsh -c qemu:///system list`)
and a System in state `ready`. For the deep build → boot → debug steps (the four capture methods
and the canonical dcache `dhash_entries` verification) follow the
[four-method live run](../runbooks/four-method-live-run.md) and
[live stack](../runbooks/live-stack.md) runbooks. Tear the System down and release the allocation
with `systems.teardown` and `allocations.release` when done.
