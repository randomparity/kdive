# local-libvirt walkthrough

End-to-end setup for the local-libvirt provider, where the KDIVE worker drives QEMU/KVM
guests on its own host. For the provider's prerequisites and config see
[the local-libvirt provider reference](local-libvirt.md); this page is the linear path from a
prepared host to a verified run.

> **Deployment:** the KDIVE app processes run as **host services** on the libvirt host (see
> [systemd](../systemd.md)), so the worker has native `/dev/kvm` and libvirt access. The
> app-tier-only [docker-compose](../docker-compose.md) worker has no KVM/libvirt access and
> cannot drive this provider — use compose only for the backends (Postgres, MinIO, OIDC).

## 1. Prepare

Run the read-only preflight; fix anything it reports before continuing:

```bash
just check-local-libvirt
```

## 2. Install

Bring up the backends, then run the three KDIVE processes as host services:

```bash
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
```

Install and start the host services as described in [systemd](../systemd.md), then apply the
schema with `python -m kdive migrate`. See [Local stack administration](../local-stack.md)
for the package-on-host layout.

## 3. Onboard the project

A fresh database has no quota or budget, so the first `allocations.request` would dead-end on
`quota_exceeded`. Seed the demo project's budget and quota. Run this from the repo checkout
with the project venv active (or set `KDIVE_PYTHON=/opt/kdive/.venv/bin/python`), so `kdive`
resolves:

```bash
just setup-local-libvirt
```

By default this runs `python -m kdive seed-project`, which writes the budget/quota rows with no
token. To onboard through the audited, role-gated admin tools instead (the production-style
path), set `KDIVE_SETUP_AUDITED=1` and supply a project-`admin` token in `KDIVE_TOKEN` — this
path needs an OIDC issuer configured to assert the project-`admin` claims, and the local
script does **not** mint a token for you (unlike the remote one):

```bash
KDIVE_SETUP_AUDITED=1 \
  KDIVE_MCP_BASE=http://localhost:8000/mcp \
  KDIVE_TOKEN="$(your-issuer-mint-command)" \
  just setup-local-libvirt
```

See [Project onboarding](../project-onboarding.md) for the audited-onboarding rationale and
why `kdivectl` cannot perform these writes.

## 4. Declare your inventory

Onboarding (§3) seeds the project's budget and quota, but `allocations.request` still dead-ends
until the **inventory** exists: a local-libvirt resource, a priced cost class, an image, and a
kdump build-config. Most of these are declared in `systems.toml` and reconciled into the catalog
(the local-libvirt resource itself is created by discovery — see below) — a fresh or broken file
produces `configuration_error` / no grantable resource with no breadcrumb back to the file, so this
step is not optional.

`systems.toml` is the single declarative source of truth for the inventory the app loads into the
database (ADR-0112). Its default path is the per-user XDG location
`~/.config/kdive/systems.toml` (there is no working-directory fallback; set `KDIVE_SYSTEMS_TOML`
to point elsewhere).

Start from the minimal, local-only example —
[`examples/systems-local-libvirt.toml`](examples/systems-local-libvirt.toml). It declares the
four entities a beginner needs (one image, one `qemu:///system` host, one priced cost class, one
kdump fragment); the full multi-provider reference is `systems.toml.example` at the repo root.

```bash
mkdir -p ~/.config/kdive
cp docs/operating/providers/examples/systems-local-libvirt.toml ~/.config/kdive/systems.toml
kdive reconcile-systems --check   # validate only (no DB/S3 writes); exits 0 when the file is valid
kdive reconcile-systems           # apply: creates the image, cost class, and build config
```

The image is declared with an `s3` source and **no `digest`**, so its catalog row stays `defined`
(expected) until the object is published — this does not block the lifecycle. The rootfs a System
actually boots comes from the provisioning profile, not this row's digest.

The local-libvirt **resource** is not created by this file — **discovery** creates and sizes it.
The running reconciler enumerates `qemu:///system`, inserts the resource row, and probes its
vcpus/memory_mb ceiling; `reconcile-systems` then binds the host (matched by `host_uri`) to your
declared `name`, `cost_class`, and `concurrent_allocation_cap`. So the host services from §2 (which
include the reconciler) must be running. If you reconcile before discovery has enumerated the host,
the overlay logs a benign `no discovered local-libvirt host … overlay deferred` warning and
converges on the next reconciler pass once the host is discovered.

### kdump capture prerequisites

Provisioning and booting need only the inventory above. **kdump vmcore capture** (the `kdump`
method in the lifecycle below) needs extra one-time host setup, because the capture is host-side:
the guest's kdump writes `/var/crash/<ts>/vmcore` (booting its crash kernel via `kexec`), then the
worker force-stops the domain, harvests the core from the qcow2 overlay with libguestfs, and reads
the guest console log that `virtlogd` writes as `root:0600`. Concretely:

- **Run the worker as `root`** — the natural identity for managing `qemu:///system` domains,
  libguestfs, and kexec, and the simplest way to read the `root:0600` console log.
- **Wire `drgn` + `libguestfs` into the worker venv** — `drgn` (the `live` dependency group) and
  the system `guestfs` binding must both be importable by the worker's interpreter; absence is a
  `missing_dependency`, not a silent skip.
- **Prepare the install-staging and console host directories** for the worker and `qemu` users.

These are detailed in the four-method runbook's
[§4b kdump](../runbooks/four-method-live-run.md#4b-kdump) section (worker-venv wiring and
host-directory prep included). The kernel-config symbols a kdump build must carry to actually arm
are tracked in [#688](https://github.com/randomparity/kdive/issues/688); the example's `kdump`
build-config already includes that arming set.

## 5. Test the lifecycle

With the project onboarded and the inventory reconciled, request an allocation and drive a System
through its lifecycle:

```bash
# allocations.request → provision → build → boot → verify → teardown → release
```

Issue these as MCP tool calls from an agent session or a scripted client. For the deep
build→boot→debug steps and the canonical dcache `dhash_entries` verification, follow the
[four-method live run](../runbooks/four-method-live-run.md) and
[live stack](../runbooks/live-stack.md) runbooks. A successful run reaches a ready System via
`provision` (minimum) and ideally completes teardown and release.
