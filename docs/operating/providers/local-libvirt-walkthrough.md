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

A root worker must **not** compile kernel source as root. The local build lane runs
operator/agent-supplied `git clone` + `make` — arbitrary code — so when the worker is root it
**requires** `KDIVE_BUILD_USER` set to an unprivileged account and drops to it for every build
subprocess, keeping root only for the libvirt/console/`kexec` operations (ADR-0214). A root worker
with `KDIVE_BUILD_USER` unset (or naming an unknown or root account) **refuses** the build with a
`configuration_error` rather than building as root. The build account also needs the build
workspace (`KDIVE_BUILD_WORKSPACE`, default `/var/lib/kdive/build`) traversable (`o+x`) and the warm
tree (`KDIVE_KERNEL_SRC`) readable. So a build-and-capture-capable worker runs as **root with
`KDIVE_BUILD_USER`** set — for example:

```bash
sudo KDIVE_KERNEL_SRC=/home/you/src/linux KDIVE_BUILD_USER=you .venv/bin/python -m kdive worker
```

See [`resource://kdive/docs/operating/build-source-staging.md`](../build-source-staging.md) for the
full `KDIVE_BUILD_USER` resolution table.

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
System boots, the **kdump build-config**, and (optionally) a custom cost class. A fresh or broken
file fails the reconcile pass with a `configuration_error`, so it is still worth getting right.

`systems.toml` is the single declarative source of truth for the inventory the app loads into the
database (ADR-0112). Its default path is the per-user XDG location
`~/.config/kdive/systems.toml` (there is no working-directory fallback; set `KDIVE_SYSTEMS_TOML`
to point elsewhere).

Start from the minimal, local-only example —
[`examples/systems-local-libvirt.toml`](examples/systems-local-libvirt.toml). It declares one
image, one priced cost class for the `qemu:///system` host, and one kdump fragment; the full
multi-provider reference is `systems.toml.example` at the repo root.

```bash
mkdir -p ~/.config/kdive
cp docs/operating/providers/examples/systems-local-libvirt.toml ~/.config/kdive/systems.toml
.venv/bin/python -m kdive reconcile-systems --check   # validate only (no DB/S3 writes); exits 0 when valid
.venv/bin/python -m kdive reconcile-systems           # apply: creates the image, cost class, build config
```

`reconcile-systems` creates the image, the cost-class coefficient, and the build-config, and
**binds** the discovered local-libvirt resource (matched by `host_uri`) to your declared `name`,
`cost_class`, and `concurrent_allocation_cap`. The local-libvirt resource row itself is created and
sized by discovery (Step 3), not by this file; if you reconcile before discovery has enumerated the
host, the overlay logs a benign `no discovered local-libvirt host … overlay deferred` warning and
converges on the next reconciler pass.

The image is declared with an `s3` source and **no `digest`**, so its catalog row stays `defined`
(expected) until the object is published — this does not block allocation. The rootfs a System
actually boots comes from the provisioning profile (Step 6), not this row's digest.

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
any capture-method machinery. Opting a System into drgn-live is therefore **orthogonal** to its
capture method — a kdump, gdbstub, host_dump, or plain console System can each carry it. Two
one-time pieces of setup arm it:

1. **Set `ssh_credential_ref` in the provisioning profile.** A non-`null` value is what makes
   provisioning render the loopback SSH NIC and `hostfwd` into the domain XML; without it the guest
   has no NIC and drgn-live cannot connect. It is an opaque **reference** — a filename, never the
   key value:

   ```jsonc
   "provider": {"local-libvirt": {
     "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2"},
     "ssh_credential_ref": "drgn-ssh"
   }}
   ```

2. **Stage that reference under the secrets root.** `debug.start_session` resolves the reference
   through the file-ref secret backend **before** opening the transport, confined to
   `KDIVE_SECRETS_ROOT` (default `/var/lib/kdive/secrets`). The file must exist and be readable by
   the **server** process — drgn-live sessions and `introspect.run` run server-side, not in the
   worker — or the session fails with a `configuration_error`. The reference's *contents* are not
   the SSH credential (see below), so stage the managed **public** key, which keeps no new private
   key at rest; its filename must match the `ssh_credential_ref` above:

   ```bash
   sudo install -d -o "$USER" -m 0750 /var/lib/kdive/secrets
   install -m 0644 ~/.local/share/kdive/ssh/id_kdive_ed25519.pub \
     /var/lib/kdive/secrets/drgn-ssh
   ```

**What actually authenticates the SSH** is the kdive-**managed** private key
(`id_kdive_ed25519`, under `~/.local/share/kdive/ssh/` or `KDIVE_SSH_KEY_DIR`), whose public half
`build-fs` injects into the debug rootfs at image-build time (ADR-0052/0219) — **not** the
`ssh_credential_ref` contents, which the transport resolves only to gate the session and register
the value for log redaction. drgn-live therefore works only on a System booted from a rootfs that
carries the managed key: the `--kind debug` image from Step 6 does; a generic base qcow2 does not.

The debug rootfs also supplies the two guest-side pieces drgn needs, all automatic for the
`--kind debug` image and a from-source build (listed here so a failure is diagnosable): the
`kdive-drgn` helper at `/usr/local/sbin/` and `drgn` itself (ADR-0220), plus the per-Run DWARF
`vmlinux` that the install step stages at `/usr/lib/debug/lib/modules/<ver>/vmlinux` so `drgn -k`
can resolve typed kernel symbols (ADR-0221). A System missing the helper returns
`debug_attach_failure`; one missing the vmlinux raises a drgn `ObjectNotFoundError` for the first
typed symbol.

## 6. Test the lifecycle

The lifecycle steps are MCP tool calls. Issue them from an MCP client (e.g. an agent session)
that connects to the server's HTTP transport at `http://127.0.0.1:8000/mcp`, authenticated with
a project-scoped bearer token.

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

**Provision a System.** Provision boots a qcow2 rootfs from disk, so it needs a **bootable image**
on the host — the minimal example's `s3` image is digest-less and does not provide one. Get a
bootable qcow2 by either:

- building a kdive-ready rootfs with `build-fs` (uses libguestfs/virt-builder — needs the Step 1
  Debian/Ubuntu libguestfs fixes; on Ubuntu 24.04 the `virt-builder --install` step may still be
  blocked by the passt/libguestfs mismatch noted there):

  ```bash
  .venv/bin/python -m kdive build-fs --kind debug --distro fedora \
    --workspace /var/lib/kdive/build \
    --dest /var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2
  ```

- or staging any prebuilt bootable qcow2 (e.g. a Fedora/Cloud base image) at a world-readable path
  under `/var/lib/kdive/rootfs/local/` and pointing `rootfs.path` at it. (The clean-room validation
  of this page used a staged base qcow2, because `build-fs` was blocked by the Ubuntu 24.04
  passt/libguestfs issue above.)

Then provision against whichever rootfs you staged. Local-libvirt provisioning uses `boot_method: direct-kernel`
(`kernel_source_ref` is required by the schema but only used by a later build Run; provision boots
the rootfs's own kernel from disk). `disk_gb` must equal the allocation's (ADR-0205):

```text
systems.provision allocation_id=<granted id> profile={
  "schema_version": 1, "arch": "x86_64", "vcpu": 2, "memory_mb": 2048, "disk_gb": 10,
  "boot_method": "direct-kernel", "kernel_source_ref": "/path/to/linux",
  "provider": {"local-libvirt": {"rootfs":
    {"kind": "local", "path": "/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2"}}}
}
# → status=queued; the worker define+starts a tagged libvirt domain. Poll systems.get until ready.
```

A successful provision yields a running `kdive-<system-id>` domain (`virsh -c qemu:///system list`)
and a System in state `ready`. For the deep build → boot → debug steps (the four capture methods
and the canonical dcache `dhash_entries` verification) follow the
[four-method live run](../runbooks/four-method-live-run.md) and
[live stack](../runbooks/live-stack.md) runbooks. Tear the System down and release the allocation
with `systems.teardown` and `allocations.release` when done.
