# Runbook: live-stack end-to-end bring-up

Operator guide for standing up the M1.2 live stack and running the `live_stack` suite.
The suite drives the full kdive spine over the real MCP HTTP transport against a running
`server`/`worker`/`reconciler` and the containerized backing services. See
[ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) for the decision and
[`docs/archive/plans/m1.2-implementation.md`](../../archive/plans/m1.2-implementation.md) for the epic.

The `server` and `reconciler` run as ordinary operator-owned host processes. Workers run in the
fixed `kdive-live-worker@1..8.service` units through the installed lifecycle socket. All use the
`docker-compose.yml` backends, so qemu disk-image and kernel-tree paths resolve on the libvirt
host. The supported worker path has no direct-process fallback (ADR-0574).

For the **remote** `qemu+tls://` variant — driving the spine against a host the worker tier does
not share a filesystem with — see [remote-live-stack.md](remote-live-stack.md); it reuses this
bring-up and adds worker→host TLS, the gdbstub ACL, and object-store reachability for the
two-phase vmcore upload.

The `just` recipes below are source-tree conveniences. Installed-package deployments use
`python -m kdive migrate` and `python -m kdive seed-project`, then run the app tier from the
compose reference (`just compose-up`); see
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
  `scripts/operations/check-local-libvirt.sh` flags the gap with the fix.
- The fixed systemd worker contract must be installed. Persistent self-hosted runners get it from
  `deploy/ansible/roles/live_vm_host`; apply the runner playbook with the revision to install:

  ```bash
  ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
    ansible-playbook deploy/ansible/playbooks/runner.yml \
    -i deploy/ansible/inventory/hosts.yml --limit <runner> \
    -e "live_vm_repo_version=$(git rev-parse HEAD)"
  ```

  A disposable hosted runner uses `deploy/systemd/install-live-worker-lifecycle.sh` after `uv
  sync`, passing the witness-member DSN only on standard input. That installer is the hosted-only
  provisioning step; persistent hosts use Ansible so accounts, directories, socket DAC, installed
  revision, and verification stay converged.

  ```bash
  witness_password=kdive-witness-local # pragma: allowlist secret — disposable local flow
  witness_dsn="postgresql://kdive-witness-member:${witness_password}@localhost:5432/kdive"
  printf '%s\n' "$witness_dsn" | sudo env "PATH=$PATH" \
    deploy/systemd/install-live-worker-lifecycle.sh \
      --operator "$(id -un)" --source "$PWD"
  ```

  This root installation is part of disposable-host provisioning, before the operator runs the
  stack. The live-stack commands themselves remain non-root socket clients.
  On a **reused hosted runner** (the `live_vm_tcg` gate's ephemeral VM image), an earlier job can
  leave `/run/kdive/live-libvirt` behind: an operator-owned session daemon plus its socket/pid
  residue. A self-contradictory scene makes the installer exit 1 by design
  (`_reconcile_libvirt_tuple` refuses a "contradictory selected libvirt tuple") — the installer
  must never paper over it — so the workflow runs a fail-soft **pre-clean step immediately
  before the install step** (#2033). What it may do, and why it is safe:

  - It stops the daemon recorded in
    `/run/kdive/live-libvirt/libvirt/libvirtd.pid` **as the operator who owns it**, and only
    after verifying the live pid is that operator's own `libvirtd`; anything else is left for
    the installer to diagnose loudly.
  - It then removes the stale runtime hierarchy under `/run/kdive/live-libvirt` so the
    installer's `_lock_libvirt_runtime` recreates it idempotently as a clean slate. The scope is
    the `/run` runtime hierarchy only: it never touches the `/var/lib/kdive` state roots and
    never follows symlinks.
  - It is pre-install hygiene on a single-tenant ephemeral box: removals are logged, missing
    state is a no-op, and it does not mask install-step failures — the install step itself stays
    strictly fail-closed.
- The installed contract publishes one explicit operator-owned session URI:
  `qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock` on the Debian-family
  runner (`virtqemud-sock` is selected on the modular-daemon family). Use the published value from
  `/etc/kdive/live-worker-libvirt.env`; worker accounts must not fall back to `qemu:///system`.
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
`kdive-artifacts` bucket, enabling bucket-wide versioning, and verifying `Enabled`, MFA Delete
off, and no MinIO prefix/folder exclusions), and applies database migrations.

> The recipe scopes `docker compose up --wait` to the long-running backends and runs
> `minio-init` separately, because `--wait` treats a run-to-completion service's exit as a
> wait failure. `minio-init`'s exit code still propagates, so a bucket creation, version enable,
> or version-policy verification failure fails `just stack-up` before any KDIVE process starts.

For an external bucket, the runtime identity also needs `s3:GetObjectVersion`, `s3:GetBucketVersioning`,
`s3:ListBucketVersions`, and `s3:DeleteObjectVersion`. First adoption is stop-old-first: quiesce
all old processes, grant and verify IAM, verify whole-bucket/no-exclusions/MFA-off policy, enable
versioning, wait for activation, migrate, and start only the version-aware image. Suspending
versioning and live rollback to a pre-ADR-0524 image are unsupported. The complete procedure is in
[Installing KDIVE](../install.md).

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

A second read-only check reports every stored System whose provisioning-profile provider section
is not exactly one section keyed by its bound Resource's kind (ADR-0579) — pre-ADR-0549 residue
that ADR-0549 stopped admitting but never repaired. Worth running after upgrading past ADR-0549.

It resolves its target from `KDIVE_DATABASE_URL`, which the two environments supply differently.

**Source tree.** `env.sh` deliberately exports no shared `KDIVE_DATABASE_URL` — one DSN per
database authority (#1929) — so source it, alias the server DSN, and scrub the other roles from
the child, exactly as `onboard.sh` does for its `verify-project` step (#2046):

```bash
source scripts/live-stack/env.sh
KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}" \
  env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
      -u KDIVE_RECONCILER_DATABASE_URL \
  uv run python -m kdive verify-profile-kinds
```

**Installed-package deployment**, where `KDIVE_DATABASE_URL` is already set by the unit
environment or the chart:

```bash
python -m kdive verify-profile-kinds
```

It writes nothing and the report is the whole answer: remediation is manual. Once it can reach
the database it exits `0` whether or not it finds residue, so a non-zero exit means it could not
run — not that the database is clean.

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
| `KDIVE_MIGRATION_DATABASE_URL` | migration-owner member DSN | migration and role bootstrap only |
| `KDIVE_SERVER_DATABASE_URL` | server-member DSN, member only of `kdive_server` | server |
| `KDIVE_WORKER_DATABASE_URL` | worker-member DSN, member only of `kdive_worker` | fixed workers |
| `KDIVE_RECONCILER_DATABASE_URL` | reconciler-member DSN, member only of `kdive_reconciler` | reconciler |
| `KDIVE_OIDC_ISSUER` | `http://localhost:8090/default` | `mcp/auth.py` |
| `KDIVE_OIDC_JWKS_URI` | `http://localhost:8090/default/jwks` | `mcp/auth.py` |
| `KDIVE_OIDC_AUDIENCE` | `kdive` | `mcp/auth.py` |
| `KDIVE_S3_ENDPOINT_URL` | `http://localhost:9000` | `store/objectstore.py` |
| `KDIVE_S3_BUCKET` | `kdive-artifacts` | `store/objectstore.py` |
| `KDIVE_S3_REGION` | `us-east-1` | `store/objectstore.py` |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | boto3 default chain |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | boto3 default chain |

The root lifecycle service separately reads the witness-member DSN from
`/etc/kdive/credentials/live-worker-witness.dsn`; that login is member only of
`kdive_lifecycle_witness` and is never delivered to a worker or operator request.

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
export KDIVE_KERNEL_SRC="$(bash scripts/fetch-kernel-tree.sh /var/lib/kdive/build/linux)"
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

The RBAC-gated `kdivectl images publish` operator verb (M2.4/7) enqueues an
`IMAGE_BUILD` job that runs the same plane and publishes the result to the catalog; this inline
`build-fs` is the local-disk fixture path for the live-stack suite.

Point `KDIVE_GUEST_IMAGE` and `KDIVE_KERNEL_SRC` at the build output and the kernel checkout.
The `live_stack` preflight skips with an actionable reason when either is missing.

## 4. Start the host processes

From a source checkout, run the convenience wrapper:

```bash
scripts/live-stack/up.sh
```

Systemd retains each exact worker invocation, while server and reconciler remain ordinary host
processes. Each process waits up to ten seconds at start for its first database connection and
exits if it cannot get one, so `up.sh` waits past that budget and fails if either ordinary daemon
exits or the requested worker slots do not report `started`. If it fails, read
`.live-stack-logs/*.log` for server/reconciler and run
`scripts/live-stack/worker-lifecycle.sh diagnostics` for the retained worker invocations:
`no database connection within` there means the backend was unreachable, or its
credentials or database name are wrong. Recovery is re-running `up.sh` once the backend answers.

`up.sh` is idempotent and also ensures the backends and libvirt are up; a no-VM API-only loop uses
`scripts/live-stack/up.sh --skip-libvirt` through the same installed worker units. It also runs
one synchronous `reconcile-systems` pass before starting the host processes, so a completed `up.sh`
guarantees the catalog is populated and every on-disk `<name>.config` sibling is uploaded with
`kernel_config_key` set (ADR-0336) — rather than waiting for the reconciler daemon's next loop.

Use the lifecycle scripts as one supported host flow:

```bash
scripts/live-stack/up.sh
scripts/live-stack/status.sh
scripts/live-stack/down.sh
```

Serialize the complete interval from `up` through `down`: one live-stack flow owns one host. The
socket's request lock only rejects overlapping requests; it is not a cross-flow lease. A later
`start` replaces the current fleet and `stop` targets that fleet.

Every lifecycle request has a 120-second monotonic deadline; worker stop uses 45 seconds within
that budget. Diagnostics has a 30-second acquisition budget, reads at most 320 KiB per slot and
1.25 MiB total, and emits at most 256 KiB per slot and 1 MiB total with a truncation marker. A
database or systemd failure retains the exact unit, invocation, state, credential, and active
incarnation row. Restore the named dependency, inspect `status` or `diagnostics`, and retry the
same `stop`; do not remove the retained files to make the command pass.

Installed package — migrate and seed on the host, then run the app tier from the compose
reference ([`deploy/compose/README.md`](../../../deploy/compose/README.md)):

```bash
python -m kdive migrate
python -m kdive seed-project --project demo
just compose-up
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

### The app tier does not hot-reload — re-run `up.sh` after editing source

The three host processes are plain Python; they load your source once, at start. Editing a file
under `src/kdive/` does **not** reach a running server, worker or reconciler. Driving the suite
against a process that predates your own fix produces a green (or a red) that means nothing —
this is the local half of issue #1630. Re-run `scripts/live-stack/up.sh` after any source change;
it is idempotent and restarts the app tier in place.

The suite now checks this for you (§5).

## 5. Run the suite

```bash
just test-live-stack
```

This runs `pytest -m live_stack`. The `live_stack` preflight skips cleanly with an
actionable reason when the fixtures or the stack are absent — so the recipe is safe to run
on any host.

### The version-skew preflight (ADR-0482)

Every `live_stack` test goes through `require_stack()`, which reads the build each app process
reports on its aux `/readyz` (`127.0.0.1:9464`/`9465`/`9466`) and grades it against your
checkout. Read a stale stack straight out of the skip/warning instead of rediscovering it as a
confusing test failure:

| verdict | meaning | what happens |
|---|---|---|
| `fresh` | the process is at `HEAD` and started after your last edit | runs, silently |
| `stale_restart` | at `HEAD`, but an **uncommitted** `src/kdive` change is newer than its start | **skips** — run `scripts/live-stack/up.sh` |
| `behind` | the deployed commit is an ancestor of `HEAD` | warns, names the commit distance |
| `diverged` | not an ancestor of `HEAD` (other branch, or `HEAD` rewritten) | warns |
| `unknown` | the process reports no build, or is not answering | warns |

Only `stale_restart` skips, because its remedy is one command. It is deliberately narrow: the
timestamp of a file that still matches `HEAD` proves nothing (a `git worktree add`, a branch
round-trip or a stash pop rewrites mtimes without changing content), so only an *uncommitted*
change newer than the process start counts. `behind` and `diverged` warn, so a deliberate run
against an older deployment is never blocked.

One limit worth knowing: the comparison is against the checkout the **tests** run from. If you
run the suite from a git worktree while the stack was started from a different checkout, a real
commit difference shows up as `behind`/`diverged`, but two checkouts sitting on the same commit
are indistinguishable.

Override with `KDIVE_STACK_SKEW_POLICY`:

```bash
KDIVE_STACK_SKEW_POLICY=warn just test-live-stack     # never skip, warn only
KDIVE_STACK_SKEW_POLICY=strict just test-live-stack   # skip on anything but fresh
KDIVE_STACK_SKEW_POLICY=off just test-live-stack      # do not probe at all
```

A stack whose processes predate this feature reports `unknown` and only warns, so the preflight
never blocks an older deployment. When **no** `live_stack` test is collected yet (the marked spine driver lands
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
just compose-up
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
scripts/live-stack/down.sh --force  # also SIGKILL host processes left after the grace period
scripts/live-stack/down.sh --wipe   # full reset: drop DB/MinIO volumes AND reap kdive-* domains/overlays
```

`down.sh --force` is an operator recovery when graceful lifecycle stop cannot converge. It can end
remaining host processes, but it cannot publish exact worker termination evidence. The retained
database incarnation and any artifact fences may therefore be stranded until an operator repairs
or explicitly reconciles them. Prefer restoring the failed dependency and retrying plain
`down.sh`; force recovery trades cleanup for lost evidence.

`down.sh --wipe` drops the Postgres and MinIO volumes and reaps all `kdive-*` libvirt domains
and their overlay disks, so the next `up.sh` starts from a clean schema and an empty bucket.
