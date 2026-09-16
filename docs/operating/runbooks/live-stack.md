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
  runner (`virtqemud-sock` is selected on the modular-daemon family). `scripts/live-stack/env.sh`
  and `scripts/live-stack/lib.sh` read that published value, so every host-local libvirt consumer
  a bring-up entry point starts — server, reconciler, workers, the `virsh` gates, teardown — lands
  on one endpoint rather than splitting by entry point (#2480). A server on a different endpoint
  than the worker finds no domain for a healthy System, and `systems.ssh_info` then reports
  `system_domain_not_found` — the reason that names this fault, and whose message points at the
  endpoint check (#2502, ADR-0658). Before that change the same symptom reported
  `ssh_not_provisioned`, which is what made #2480 hard to diagnose.

  **Leave `KDIVE_LIBVIRT_URI` unset on a provisioned host.** It is honored verbatim and is the one
  thing that still splits the two: `worker-lifecycle.sh` reads the published contract directly and
  ignores the override, so presetting it moves the daemons off the worker's socket and reproduces
  that failure with nothing to warn you. The override exists for a host whose contract file is
  broken, where it is the only way to run teardown at all.
- The VM fixtures built (below).
- If you run a **published** kdive image from `ghcr.io/randomparity/kdive` rather than a
  locally built one, verify its signature first. The release workflow cosign-signs each
  released digest keyless/OIDC and attaches an SBOM (ADR-0088 decision 8); the consumer
  `cosign verify` check is in
  [`deploy/compose/README.md`](../../../deploy/compose/README.md#image-provenance--verify-before-you-run-a-published-image).

## 1. Bring up the backends

```bash
just stack-backends
```

This is not a prerequisite for step 4: `scripts/live-stack/stack-services.sh` brings the backends
up itself through the same code path, so running both is redundant. Use `just stack-backends` when
the backends are all you want — an in-network Compose app tier, a schema inspection, a bucket
check — and go straight to step 4 otherwise.

It waits for the three long-running backends — Postgres, SeaweedFS, and the mock OIDC issuer
— to be **healthy**, runs the one-shot `seaweedfs-init` to completion (creating the
`kdive-artifacts` bucket, enabling bucket-wide versioning, and verifying `Enabled`), and applies
database migrations.

> The bring-up path scopes `docker compose up --wait` to the long-running backends and runs
> `seaweedfs-init` separately, because `--wait` treats a run-to-completion service's exit as a
> wait failure. `seaweedfs-init`'s exit code still propagates, so a bucket creation or versioning
> verification failure fails `just stack-backends` before any KDIVE process starts. That
> scoping lives in `live_stack_backends_up` in `scripts/live-stack/lib.sh`, which is the one
> implementation both this recipe and `stack-services.sh` reach (ADR-0655).

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

For an external S3 backend, merge the following rule into the bucket's existing lifecycle
configuration before
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
The bundled SeaweedFS account is `kdive` / `kdive-demo-secret`, so those must be exported as the
`AWS_*` vars or every artifact `put`/`get` fails with an access error that looks like a code bug.
The `KDIVE_S3_*` vars carry only the endpoint, bucket, and region.

| var | value | consumed by |
|-----|-------|-------------|
| `KDIVE_MIGRATION_DATABASE_URL` | migration-owner member DSN | migration and role bootstrap only |
| `KDIVE_SERVER_DATABASE_URL` | server-member DSN, member only of `kdive_server` | server |
| `KDIVE_WORKER_DATABASE_URL` | worker-member DSN, member only of `kdive_worker` | fixed workers |
| `KDIVE_RECONCILER_DATABASE_URL` | reconciler-member DSN, member only of `kdive_reconciler` | reconciler |
| `KDIVE_OIDC_ISSUER` | `http://localhost:8090/default` | `mcp/auth.py` |
| `KDIVE_OIDC_JWKS_URI` | `http://localhost:8090/default/jwks` | `mcp/auth.py` |
| `KDIVE_OIDC_AUDIENCE` | `kdive` | `mcp/auth.py` |
| `KDIVE_S3_ENDPOINT_URL` | `http://localhost:8333` | `store/objectstore.py` |
| `KDIVE_S3_BUCKET` | `kdive-artifacts` | `store/objectstore.py` |
| `KDIVE_S3_REGION` | `us-east-1` | `store/objectstore.py` |
| `AWS_ACCESS_KEY_ID` | `kdive` | boto3 default chain |
| `AWS_SECRET_ACCESS_KEY` | `kdive-demo-secret` | boto3 default chain |

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
scripts/live-stack/stack-services.sh
```

Systemd retains each exact worker invocation, while server and reconciler remain ordinary host
processes. Each process waits up to ten seconds at start for its first database connection and
exits if it cannot get one, so `stack-services.sh` waits past that budget and fails if either ordinary daemon
exits or the requested worker slots do not report `started`. If it fails, read
`.live-stack-logs/*.log` for server/reconciler and run
`scripts/live-stack/worker-lifecycle.sh diagnostics` for the retained worker invocations:
`no database connection within` there means the backend was unreachable, or its
credentials or database name are wrong. Recovery is re-running `stack-services.sh` once the backend answers.

`stack-services.sh` is idempotent and also ensures the backends and libvirt are up; a no-VM API-only loop uses
`scripts/live-stack/stack-services.sh --skip-libvirt` through the same installed worker units. It also runs
one synchronous `reconcile-systems` pass before starting the host processes, so a completed `stack-services.sh`
guarantees the catalog is populated and every on-disk `<name>.config` sibling is uploaded with
`kernel_config_key` set (ADR-0336) — rather than waiting for the reconciler daemon's next loop.

Use the lifecycle scripts as one supported host flow:

```bash
scripts/live-stack/stack-services.sh
scripts/live-stack/stack-status.sh
scripts/live-stack/stack-down.sh
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
> [`scripts/live-stack/stack-services.sh`](../../../scripts/live-stack/stack-services.sh) — the path at the top of this
> section, and the one both `live.yml` gates use.

### Recovering a wedged worker slot

`stack-services.sh` fails, or a lifecycle request comes back with
`retry_action=operator_recovery`. The usual cause is `code=conflict` after a
`kdive-live-worker@N.service` unit was restarted outside the lifecycle contract — a
`needrestart` sweep, an unattended upgrade, a manual `systemctl restart`. The unit comes back
with a new `InvocationID` while the retained slot state still names the previous one, the gate
exits, and the unit is left `failed` holding that identity, so the next `start` is refused.
`retry_action` is what tells you a retry will not clear it; the values are defined in
[the systemd unit reference](../../../deploy/systemd/README.md#lifecycle-retry-actions).

**Read the cause first.** The client prints one JSON line, so pipe it:

```bash
scripts/live-stack/worker-lifecycle.sh diagnostics | python3 -m json.tool
```

The command exits 4 when a slot was withheld; the pipe is what keeps the output readable under
`set -e`. A withheld slot carries `"code": "diagnostics_withheld"` and
`"message": "withheld: <reason>"`, with its `"phase"` beside them:

| Reason | What it means | What to do |
|---|---|---|
| `state_unreadable` | the slot's retained state could not be read at all | check ownership and mode under `/var/lib/kdive/live-workers` |
| `slot_unusable` | the slot holds no usable diagnostic state: no exact invocation, or redaction sources rejected as unsafe | run `recover` below; if it persists, check the count, per-value size, ownership, and mode of the slot's redaction sources under the same directory — more than 32 values, or any value over 4096 bytes, is rejected. Do not print their contents: those files hold the worker's database URL and object-store keys |
| `acquisition_failed` | systemd, the journal, or the request deadline did not answer in time, or acquisition failed unexpectedly | check `systemctl status` and `journalctl` for the unit, then re-run `diagnostics` once; if the same reason returns, read the unit's journal on the host and check the witness log for the exception type |
| `redaction_refused` | the report held a value the redactor may not emit, or no safe substitute could render it, so the whole report was dropped | read the unit's journal on the host directly; re-running `diagnostics` will not clear it |
| `peer_redaction_refused` | the report held a value another slot registered, so it was dropped for the same reason | read the unit's journal on the host directly |
| `internal_error` | the slot's redaction-source file could not be read, or a failure in the slot's diagnostic preconditions that is none of the above | check ownership and mode under `/var/lib/kdive/live-workers`; the witness log names the exception type |

A slot that carries no reason is a different thing. Read it off the slot result rather than off
the emitted text: a slot whose `"code"` is `"ok"` but which contributes no `=== slot N ===` block
to the diagnostics was never captured, because the run had already spent the acquisition or
emission budgets above. That is truncation, not withholding, and the response stays `ok` for it.
Do not look for `[aggregate diagnostics truncated]` as the cue — the marker is itself suppressed
when it collides with one of the host's redaction sources, and on some paths it is never emitted
at all. Read those units' journals on the host directly. The reason text is accounted inside
those same budgets, so the numbers given above are unchanged.

**Recover.** Unlike `diagnostics`, this operation first requires the installed lifecycle
environment to match your checkout. If it prints
`installed lifecycle protocol does not match this checkout; reprovision the runner` or
`installed lifecycle protocol is unavailable; reprovision the runner`, reprovision the host per
the Prerequisites above before retrying. A third string,
`checkout lifecycle compatibility probe failed`, points the other way — at this checkout's own
environment rather than at the installed runner, so run `uv sync` here first.

```bash
scripts/live-stack/worker-lifecycle.sh recover
```

For each slot it observes the unit. A slot whose cgroup still holds live processes is refused:
the response is `code=conflict` with `retry_action=operator_recovery` and a per-slot
`recovery_refused`. Stop the work that unit is doing rather than forcing past it. For a slot
proven dead it publishes terminal evidence derived from that same observation, clears the on-disk
slot facts, releases the `worker_incarnations` fence, and runs `reset-failed` on a unit systemd
still accounts for. It never fabricates a termination outcome — see
[ADR-0657](../../adr/0657-a-successor-invocation-is-terminal-evidence.md).

**Then bring the stack back up.** Re-run `scripts/live-stack/stack-services.sh`; it is
idempotent.

**The residual slots `recover` now reaches.** A slot whose `state.json` is absent or malformed,
whose retained binding drifted from its `worker_incarnations` row, or whose termination evidence
that row rejected, is retired by a `recover` pass: the row is named by the slot's derived
incarnation prefix and released with the binding it actually stores, then the on-disk facts are
cleared. Such a slot reports `retired the residual worker slot`.

**The one slot `recover` still refuses.** If systemd reports no invocation for the unit on the
*retained* boot, nothing can prove the registered invocation ended — absence within a boot is not
termination evidence — so recovery refuses that slot rather than clearing it, with the per-slot
code `recovery_refused_unreadable_identity`. The rest of the sweep still runs. The remedy is a
reboot, which yields a different boot ID and therefore real evidence.

A slot reported `recovery_refused_incoherent_row` is a different problem, and a reboot will not
clear it: the database holds an active `worker_incarnations` row over that slot which does not
describe this slot on this host — its stored binding names a different unit, or a different host.
Recovery refuses the whole slot rather than releasing that row or clearing the slot's files around
it. A slot that had only a *prepared* generation is the exception: that generation holds no fence,
and it is discarded before the refusal is decided. Read the row's `authority_binding` and reconcile
it with the host that actually owns it; the warning in the lifecycle journal names the slot and
unit, not the foreign host.
See
[ADR-0657](../../adr/0657-a-successor-invocation-is-terminal-evidence.md) and
[ADR-0667](../../adr/0667-recovery-names-the-fence-row-by-the-slot-derived-incarnation.md).

**When `recover` refuses the whole sweep.** If any `kdive worker` process is running outside the
eight fixed unit cgroups, `recover` refuses before touching any slot and returns `code=conflict`
with `retry_action=operator_recovery` and an **empty slot list** — no slot was examined, so none is
reported. Cgroup membership cannot see such a process, and recovery releases fences, so it takes
the same guard `start` takes rather than the weaker check. The lifecycle journal carries a warning
naming the count. Stop those processes, then re-run `recover`.

Do not hand-edit the slot files or the `worker_incarnations` row in any of these cases.

### The app tier does not hot-reload — re-run `stack-services.sh` after editing source

The three host processes are plain Python; they load your source once, at start. Editing a file
under `src/kdive/` does **not** reach a running server, worker or reconciler. Driving the suite
against a process that predates your own fix produces a green (or a red) that means nothing —
this is the local half of issue #1630. Re-run `scripts/live-stack/stack-services.sh` after any source change;
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
| `stale_restart` | at `HEAD`, but an **uncommitted** `src/kdive` change is newer than its start | **skips** — run `scripts/live-stack/stack-services.sh` |
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
scripts/live-stack/stack-down.sh          # stop host processes + backends, keep state
scripts/live-stack/stack-down.sh --force  # also SIGKILL host processes left after the grace period
scripts/live-stack/stack-down.sh --wipe   # full reset: drop DB/SeaweedFS volumes AND reap kdive-* domains/overlays
```

`stack-down.sh --force` is an operator recovery when graceful lifecycle stop cannot converge. It can end
remaining host processes, but it cannot publish exact worker termination evidence. The retained
database incarnation and any artifact fences may therefore be stranded until an operator repairs
or explicitly reconciles them. Prefer restoring the failed dependency and retrying plain
`stack-down.sh`; force recovery trades cleanup for lost evidence.

`stack-down.sh --wipe` drops the Postgres and SeaweedFS volumes and reaps all `kdive-*` libvirt domains
and their overlay disks, so the next `stack-services.sh` starts from a clean schema and an empty bucket.
