# Runbook: live-stack end-to-end bring-up

Operator guide for standing up the live stack and running the `live_stack` suite.
The suite drives the full kdive spine over the real MCP HTTP transport against a running
`server`/`worker`/`reconciler` and the containerized backing services. See
[ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) for the original decision and
[the live-testing map](live-testing.md) for the test tiers and prerequisites.

The `server` and `reconciler` run as ordinary operator-owned host processes. Workers run in the
fixed `kdive-live-worker@1..8.service` units through the installed lifecycle socket. All use the
`docker-compose.yml` backends, so qemu disk-image and kernel-tree paths resolve on the libvirt
host. The supported worker path has no direct-process fallback (ADR-0574).

For the **remote** `qemu+tls://` variant — driving the spine against a host the worker tier does
not share a filesystem with — see [remote-live-stack.md](remote-live-stack.md); it reuses this
bring-up and adds worker→host TLS, the gdbstub ACL, and object-store reachability for the
two-phase vmcore upload.

Run the `just` recipes below from the checkout. For an app-tier Compose deployment, follow
[the Compose operating guide](../../../deploy/compose/README.md); for Kubernetes, follow the
[Helm deployment runbook](kubernetes-deploy.md). The local-libvirt flow on this page runs
workers on the host so they can access KVM and libvirt.

## Prerequisites

- A KVM / nested-virt host with the provisioned libvirt daemon and socket for its distro.
  Use the installed session endpoint described below.
- Docker with a reachable daemon and access to the Compose images and build dependencies.
  The mock OIDC service uses the in-repo mirror, built locally or selected by
  `KDIVE_OIDC_IMAGE`; the wrapper defaults to a pinned mirror on emulated POWER. See
  [image selection](../../../deploy/mock-oidc/README.md#using-the-image).
- The repo set up: `just setup` (or `uv sync --locked`).
- Local-libvirt kdump capture needs drgn and libguestfs in the **installed worker environment**,
  `/opt/kdive-live-worker-lifecycle/.venv`, supplied by the lifecycle host provisioning below.
  The checkout preflight probes `KDIVE_PYTHON`; passing it does not verify that worker interpreter.
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
— to be **healthy**, runs the one-shot `seaweedfs-init` to completion (creating the
`kdive-artifacts` bucket, enabling bucket-wide versioning, and verifying `Enabled`, MFA Delete
off, and no MinIO prefix/folder exclusions), and applies database migrations.

> The recipe scopes `docker compose up --wait` to the long-running backends and runs
> `seaweedfs-init` separately, because `--wait` treats a run-to-completion service's exit as a
> wait failure. `seaweedfs-init`'s exit code still propagates, so a bucket creation, version enable,
> or version-policy verification failure fails `just stack-up` before any KDIVE process starts.

For an external bucket, the runtime identity needs `s3:GetObjectVersion`,
`s3:GetBucketVersioning`, `s3:ListBucketVersions`, and `s3:DeleteObjectVersion`. Complete the
[object-store preflight](../install.md#object-store-preflight) before starting processes.
This checkout requires fresh protocol-4 resources; the
[release-compatibility boundary](../install.md#release-compatibility) applies to this host stack
as well. Historical versioning-adoption instructions are not a migration path for existing
protocol-3 state.

### Required: abort-incomplete-multipart-upload lifecycle rule

Chunked external-build uploads larger than the 5 GiB single-PUT ceiling are reassembled
server-side with a multipart upload (ADR-0104). A `kdive` process that crashes between
`CreateMultipartUpload` and `Complete`/`Abort` leaves one in-progress multipart upload that
`ListObjectsV2` — and therefore the reconciler's prefix reaper — cannot see. Configure the
bucket with an `AbortIncompleteMultipartUpload` lifecycle rule so the store reclaims such an
orphan on its own. Add this rule after the bucket exists (one-day incomplete-upload age shown). Preserve any
existing bucket lifecycle rules; do not add noncurrent-version expiry, which can remove
completed object versions still pinned by KDIVE records:

```bash
# MinIO
mc ilm rule add local/kdive-artifacts --incomplete-multipart-days 1

```

For S3, merge the following rule into the bucket's existing lifecycle configuration before
applying the complete policy. `put-bucket-lifecycle-configuration` replaces the policy; a
standalone rule must not discard existing retention settings.

```json
{
  "ID": "abort-incomplete-mpu",
  "Status": "Enabled",
  "Filter": { "Prefix": "" },
  "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 1 }
}
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
target DB), then mints a token with the configured `KDIVE_TOKEN_TTL` lifetime and prints the **binding contract** (`projects`, `roles`, and
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

The spine boots a real guest and uploads an already-built kernel. Prepare a guest image and
build the kernel on the test client before running the suite:

```bash
python -m kdive build-fs --image fedora-kdive-ready-44 \
  --workspace ~/.local/share/kdive/build/images
export KDIVE_GUEST_IMAGE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2
# checks out the pinned kernel source tree; prints the checkout path on stdout
export KDIVE_KERNEL_SRC="$(bash scripts/fetch-kernel-tree.sh /var/lib/kdive/build/linux)"
```

The fetch helper only checks out source. Compile the kernel yourself using the configuration
and artifact requirements in the [external-build guide](../external-build-upload.md), then
keep `KDIVE_KERNEL_SRC` pointed at that built tree. The test harness stages modules and packages the
boot image; debug exercises also need the unstripped `vmlinux` with a GNU build ID.

`build-fs --image` selects an entry from the rootfs catalog and produces the guest qcow2 plus
its provenance sidecar. The [image-lifecycle runbook](image-lifecycle.md) owns image selection,
customization, output-directory permissions, and host labeling requirements. The
[local example](../../../examples/local-libvirt/README.md) automates building and registering
these images through `build-image.sh`.

Point `KDIVE_GUEST_IMAGE` at the resulting qcow2 and `KDIVE_KERNEL_SRC` at the built kernel tree.
The presence of either path does not prove that the image or kernel is usable; read the failed
phase's diagnostics if packaging, provisioning, or boot fails.

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

Use the [local-libvirt example](../../../examples/local-libvirt/README.md) for guided host
setup and client connection, then follow the [core workflow](../../guide/core-path.md).
The Compose app tier described above does not provide local-libvirt guest access; do not
use it as a substitute for that host setup.

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
