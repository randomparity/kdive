# Runbook: live-stack end-to-end bring-up

Operator guide for standing up the M1.2 live stack and running the `live_stack` suite.
The suite drives the full kdive spine over the real MCP HTTP transport against a running
`server`/`worker`/`reconciler` and the containerized backing services. See
[ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) for the decision and
[`docs/archive/plans/m1.2-implementation.md`](../../archive/plans/m1.2-implementation.md) for the epic.

The `server`, `worker`, and `reconciler` run **on the host** (not in containers) against
the `docker-compose.yml` backends, so qemu disk-image and kernel-tree paths resolve where
`libvirtd` runs. Containerizing them is a deferred follow-on (ADR-0042 §2).

For the **remote** `qemu+tls://` variant — driving the spine against a host the worker tier does
not share a filesystem with — see [remote-live-stack.md](remote-live-stack.md); it reuses this
bring-up and adds worker→host TLS, the gdbstub ACL, and object-store reachability for the
two-phase vmcore upload.

The `just` recipes below are source-tree conveniences. Installed-package deployments use
`python -m kdive migrate` and `python -m kdive seed-project`, then run the app tier from the
compose reference (`docker compose up -d migrate server worker reconciler`); see
[`docs/operating/local-stack.md`](../local-stack.md) and
[`deploy/compose/README.md`](../../../deploy/compose/README.md). For a **Kubernetes / Helm**
deployment (the production-shaped path), see
[`kubernetes-deploy.md`](kubernetes-deploy.md).

## Prerequisites

- A KVM / nested-virt host with `libvirt` and a running `libvirtd`.
- Docker with a reachable daemon and **pullable** compose images. The compose file pins
  `ghcr.io/navikt/mock-oauth2-server:3.0.3`; if that tag no longer resolves on ghcr.io,
  re-pin it to a current tag before `just stack-up`.
- The repo set up: `just setup` (or `uv sync --locked`).
- For **local-libvirt `kdump`** capture, the worker venv additionally needs `drgn`
  (`uv sync --group live`) and the system `guestfs` binding wired in; this is a one-time step
  documented in the
  [four-method runbook §4b](four-method-live-run.md#wire-the-worker-venv-drgn--libguestfs).
  `scripts/check-local-libvirt.sh` flags the gap with the fix.
- The install-staging directory (`KDIVE_INSTALL_STAGING`, default `/var/lib/kdive/install`) and the
  console-log directory (`/var/lib/kdive/console`) must be prepared for the worker user (and the
  `qemu` user under `qemu:///system`) — a one-time host step needed for **every** local install/boot.
  See [four-method runbook §4b](four-method-live-run.md#prepare-the-worker-host-directories-install-staging--console);
  `scripts/check-local-libvirt.sh` flags an unwritable staging directory with the fix.
- The VM fixtures built (below).
- If you run a **published** kdive image from `ghcr.io/randomparity/kdive` rather than a
  locally built one, verify its signature first. The release workflow cosign-signs each
  released digest keyless/OIDC and attaches an SBOM (ADR-0088 decision 8); the consumer
  `cosign verify` check is in
  [`deploy/compose/README.md`](../../../deploy/compose/README.md#image-provenance--verify-before-you-run-a-published-image).

## 1. Bring up the backends

```bash
just stack-up
```

This waits for the three long-running backends — Postgres, MinIO, and the mock OIDC issuer
— to be **healthy**, runs the one-shot `minio-init` to completion (creating the
`kdive-artifacts` bucket), and applies database migrations.

> The recipe scopes `docker compose up --wait` to the long-running backends and runs
> `minio-init` separately, because `--wait` treats a run-to-completion service's exit as a
> wait failure. `minio-init`'s exit code still propagates, so a genuine bucket-creation
> failure fails `just stack-up`.

### Required: abort-incomplete-multipart-upload lifecycle rule

Chunked external-build uploads larger than the 5 GiB single-PUT ceiling are reassembled
server-side with a multipart upload (ADR-0104). A `kdive` process that crashes between
`CreateMultipartUpload` and `Complete`/`Abort` leaves one in-progress multipart upload that
`ListObjectsV2` — and therefore the reconciler's prefix reaper — cannot see. Configure the
bucket with an `AbortIncompleteMultipartUpload` lifecycle rule so the store reclaims such an
orphan on its own. Run once after the bucket exists (1-day expiry shown):

```bash
# MinIO
mc ilm rule add local/kdive-artifacts --expire-delete-marker --noncurrent-expire-days 1
mc ilm rule add local/kdive-artifacts --incomplete-multipart-days 1

# Real S3 (equivalent), via a lifecycle configuration with:
#   AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 }
aws s3api put-bucket-lifecycle-configuration --bucket "$KDIVE_S3_BUCKET" \
  --lifecycle-configuration '{"Rules":[{"ID":"abort-incomplete-mpu","Status":"Enabled",
  "Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
```

## Fund the demo project — `just onboard`

`allocations.request` is funding-walled until a project has a budget **and** a quota row, keyed
by the same string the token's `projects`/`roles` claim carries. `just onboard` collapses that
into one idempotent command against the same `env.sh` database the stack uses:

```bash
just onboard                 # project "demo" (override with KDIVE_PROJECT=acme)
```

It runs an advisory provider preflight, then `migrate` → `seed-project` → `verify-project` (the
hard funding gate — it fails loudly if the rows are absent and echoes the credential-redacted
target DB), then mints a 24 h token and prints the **binding contract** (`projects`, `roles`, and
the `project` arg, all the same string). Export the printed `KDIVE_TOKEN` and re-run when it
expires. This is the dev/demo path; production onboards via the audited admin tools
([project onboarding](../project-onboarding.md)). It can run any time after the backends and
migrations are up (it does not need the host processes).

## 2. Review the host-process env

The source-tree wrappers source `scripts/live-stack/env.sh`, which exports the local
defaults before starting KDIVE. The full set of `KDIVE_*` variables is in
[the config reference](../../guide/reference/config.md); the live-run subset is below.

**The most error-prone step:** the object store reads S3 **credentials from boto3's
default chain** (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`), **not** from `KDIVE_S3_*`.
MinIO's root user/password are `minioadmin`/`minioadmin`, so those must be exported as the
`AWS_*` vars or every artifact `put`/`get` fails with an access error that looks like a
code bug. The `KDIVE_S3_*` vars carry only the endpoint, bucket, and region.

| var | value | consumed by |
|-----|-------|-------------|
| `KDIVE_DATABASE_URL` | `postgresql://kdive:kdive@localhost:5432/kdive` | `db/pool.py` | <!-- pragma: allowlist secret — local dev only -->
| `KDIVE_OIDC_ISSUER` | `http://localhost:8090/default` | `mcp/auth.py` |
| `KDIVE_OIDC_JWKS_URI` | `http://localhost:8090/default/jwks` | `mcp/auth.py` |
| `KDIVE_OIDC_AUDIENCE` | `kdive` | `mcp/auth.py` |
| `KDIVE_S3_ENDPOINT_URL` | `http://localhost:9000` | `store/objectstore.py` |
| `KDIVE_S3_BUCKET` | `kdive-artifacts` | `store/objectstore.py` |
| `KDIVE_S3_REGION` | `us-east-1` | `store/objectstore.py` |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | boto3 default chain |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | boto3 default chain |

Installed-package deployments usually write these defaults to `/etc/kdive/local.env` and
source that file before running commands:

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
python -m kdive seed-project --project demo
```

## 3. Build the VM fixtures

The spine boots a real guest and builds a real kernel, so the suite needs an
operator-provided guest image and kernel tree:

```bash
python -m kdive build-fs --image fedora-kdive-ready-44 \
  --workspace ~/.local/share/kdive/build/images
export KDIVE_GUEST_IMAGE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2
# checks out the pinned kernel source tree; prints the checkout path on stdout
export KDIVE_KERNEL_SRC="$(bash scripts/fetch-kernel-tree.sh)"
```

The kernel-tree fetch helper lives under `scripts` (the `fetch-kernel-tree.sh` fixture script);
clone the pinned source there and point `KDIVE_KERNEL_SRC` at it.

`build-fs` drives the in-process `RootfsBuildPlane` (the Python successor to the removed
bash rootfs builder): it runs the unprivileged libguestfs stages (`virt-builder` customize →
`virt-make-fs` whole-disk ext4 qcow2 → fstab/crypttab/SELinux normalize), records the pinned
inputs (distro, releasever, packages, source-image digest) as provenance, prints the qcow2 content
digest, and moves the image to `--dest` (default
`/var/lib/kdive/rootfs/local/<image>.qcow2`). `--image` selects a catalog row such as
`fedora-kdive-ready-44` (debug guest) or `fedora-kdive-build-44` (build host); pass `--package`
only to add packages on top of the catalog kind's default set, or `--workspace` to stage under a
user-writable path (no privileged `mkdir`). See [the image-lifecycle runbook](image-lifecycle.md)
for the full catalog list. For the default root-owned `--dest`
an OS admin pre-prepares the output directory once and makes it writable by the build user; the
per-build write and the final `chmod 0644` are unprivileged. The image is left `0644` so the
separate `qemu` user can read it under `qemu:///system`. Under SELinux the file also needs the
`virt_image_t` label (the standard label for libvirt-managed images); this is the host-side file
label and is independent of the guest-internal SELinux the plane disables.

The RBAC-gated, publish-backed `kdivectl images build` operator verb (M2.4/7) enqueues an
`IMAGE_BUILD` job that runs the same plane and publishes the result to the catalog; this inline
`build-fs` is the local-disk fixture path for the live-stack suite.

Point `KDIVE_GUEST_IMAGE` and `KDIVE_KERNEL_SRC` at the build output and the kernel checkout.
The `live_stack` preflight skips with an actionable reason when either is missing.

## 4. Start the host processes

From a source checkout, run the convenience wrapper:

```bash
scripts/live-stack/up.sh
```

`up.sh` is idempotent and also ensures the backends and libvirt are up; for a no-VM, no-sudo
API-only loop use `KDIVE_WORKER_AS_ROOT=0 scripts/live-stack/up.sh --skip-libvirt`. It also runs
one synchronous `reconcile-systems` pass before starting the host processes, so a completed `up.sh`
guarantees the catalog is populated and every on-disk `<name>.config` sibling is uploaded with
`kernel_config_key` set (ADR-0336) — rather than waiting for the reconciler daemon's next loop.

Installed package — migrate and seed on the host, then run the app tier from the compose
reference ([`deploy/compose/README.md`](../../../deploy/compose/README.md)):

```bash
python -m kdive migrate
python -m kdive seed-project --project demo
docker compose up -d migrate server worker reconciler
```

The default MCP URL is `http://127.0.0.1:8000/mcp`. Override the bind address with
`KDIVE_HTTP_HOST` / `KDIVE_HTTP_PORT` if `127.0.0.1:8000` is taken; keep
`KDIVE_STACK_BASE_URL` in sync.

> **The compose app tier cannot serve the host-side suite (§5) or the `local-libvirt`
> provider.** Two independent reasons, both by design:
>
> - **One issuer, two identities.** The mock issuer derives `iss` from the request host, so a
>   token minted from the host carries `iss=http://localhost:8090/default` while the compose
>   `server` is configured `iss=http://oidc:8080/default`. `JWTVerifier` enforces `iss`, so every
>   host-side call returns `401 Unauthorized` even though the signature is valid.
> - **No VM access.** The kdive image is built to drive the remote-libvirt and fault-inject
>   providers over the network; `local-libvirt` is deliberately not containerized. The compose
>   services get no `/dev/kvm`, no libvirt socket and no privileged flag.
>
> Use the compose app tier for in-network clients only. For the suite, the CLI, or anything that
> provisions a local VM, run the app tier as **host processes** via
> [`scripts/live-stack/up.sh`](../../../scripts/live-stack/up.sh) — the path at the top of this
> section, and the one both `live.yml` gates use.

## 5. Run the suite

```bash
just test-live-stack
```

This runs `pytest -m live_stack`. The `live_stack` preflight skips cleanly with an
actionable reason when the fixtures or the stack are absent — so the recipe is safe to run
on any host. When **no** `live_stack` test is collected yet (the marked spine driver lands
in a later sub-issue), the recipe reports `no live_stack tests collected — skipping
cleanly` and exits 0.

## 6. Kernel debugging demo smoke check

The default installed-package flow is:

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
python -m kdive seed-project --project demo
docker compose up -d migrate server worker reconciler
```

Expected defaults:

- MCP URL: `http://127.0.0.1:8000/mcp`
- Kernel source: `~/src/linux` unless `KDIVE_KERNEL_SRC` is set
- Build workspace: `/var/lib/kdive/build`
- Component roots: `/var/lib/kdive/build/components:/etc/kdive/fixtures`
- Fixture catalog: `/etc/kdive/fixtures/local-libvirt`
- Fedora kdive-ready rootfs: `/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2`
- Busybox rootfs: `/var/lib/kdive/rootfs/local/busybox-bare.qcow2`

After the stack is up, use the live-stack harness to call MCP tools for:

- `accounting.set_budget`
- `accounting.set_quota`
- `resources.list`
- `allocations.request`
- `systems.provision` with
  `rootfs: {"kind": "catalog", "provider": "local-libvirt", "name": "fedora-kdive-ready-44"}`
- `runs.create`, then `artifacts.create_run_upload` + PUT your locally-built kernel, then
  `runs.complete_build`
- `runs.install`
- `runs.boot`
- `artifacts.list(system_id=...)`

Vulnerable kernels should produce a console artifact instead of an empty `boot_timeout`.
Patched kernels can boot and reach the readiness marker.

## 7. Teardown

```bash
scripts/live-stack/down.sh          # stop host processes + backends, keep state
scripts/live-stack/down.sh --wipe   # full reset: drop DB/MinIO volumes AND reap kdive-* domains/overlays
```

`down.sh --wipe` drops the Postgres and MinIO volumes and reaps all `kdive-*` libvirt domains
and their overlay disks, so the next `up.sh` starts from a clean schema and an empty bucket.
