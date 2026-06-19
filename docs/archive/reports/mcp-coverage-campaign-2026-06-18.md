# MCP Tool Coverage Campaign — Rerun 2026-06-18

Rerun of the MCP tool coverage campaign per
`docs/operating/runbooks/mcp-coverage-campaign-rerun.md`, against the **current 112-tool
surface** (image `ghcr.io/randomparity/kdive:sha-b45aa02`). Supersedes
`mcp-coverage-campaign-2026-06-14.md` (which covered an older 91-tool surface). Closes #572.

## Deployments

| Deployment | Providers registered | Identity gate | Notes |
|---|---|---|---|
| D2 k8s (`kdive-demo`, k3s `kdive-dev`) | remote-libvirt | PASS (admin + 3 platform roles) | helm `kdive-0.4.0` / app `0.3.0`, image `sha-b45aa02`; bundled backends; MCP via `kubectl port-forward svc/kdive-kdive-server 8000`. **112 tools** advertised. |

Only **remote-libvirt** is registered on this deployment (`KDIVE_LOCAL_LIBVIRT_ENABLED=false`,
no `KDIVE_FAULT_INJECT`), matching the operator's single-host setup (`ub24-big`,
`qemu+tls://`). local-libvirt and fault-inject were out of scope for this environment and are
shown blank in the grid. The remote host carries the ephemeral_libvirt build host
(`ub24-big-build`) declared in `systems.toml`.

Tokens were minted in-pod against the in-cluster demo issuer (so `iss` matches what the server
validates) and presented over the port-forward; the committed `scripts/coverage_campaign/drive.py`
mints a test-keypair token the k8s demo issuer does not trust, so a small run-local driver
(`artifacts/coverage-campaign/{mcpcall,arc}.py`, gitignored) was used instead.

## Result — pass/fail per provider

| Provider | pass | fail | blocked | cells driven |
|---|---|---|---|---|
| remote-libvirt | 47 | 2 | 5 | 54 |

54 distinct `(tool, remote-libvirt)` cells driven of the 112-tool census (86 implemented,
26 partial — every `partial` tool now carries an ADR-0175 `maturity_detail`, new this surface).
Full grid below (driven rows only; ★ = destructive-capable).

### Arc status

- **Read sweep (Arc 1):** 44/44 reachable read tools respond over the live transport — **0
  transport failures**. 20 return a clean `ok` with no arguments; the rest correctly return a
  structured envelope (or a FastMCP missing-argument validation) for an id-less probe. The new
  #571 `debug.get_session` / `debug.list_sessions` and the #566/#567 discoverability tools
  (`runs.profile_examples`, `systems.profile_examples`) are present and answer.
- **remote-libvirt lifecycle (Arc 2):** `allocations.request` → `systems.provision` (System
  reaches **ready**) → `investigations.open` → `runs.create` all **pass**. The arc then blocks
  at **`runs.build`** (#584); install / boot / capture / debug-attach are blocked behind it.
  This is a regression in reachability versus 2026-06-14 (where the build plane was proven and
  install was the open frontier) — and the cause is infrastructure state, not the tool surface:
  see findings #583 / #584.
- **fault-inject / local-libvirt lifecycle:** not configured on this deployment; not driven.

### Lifecycle arc trace (remote-libvirt)

| Phase | Tool | Verdict |
|---|---|---|
| allocate | `allocations.request` | ✅ granted (vcpus/memory advertised — admission ceiling OK) |
| provision | `systems.provision` | ✅ System → `ready` |
| open | `investigations.open` | ✅ |
| create | `runs.create` | ✅ |
| build | `runs.build` | ❌ `configuration_error` (#584) |
| install/boot | `runs.install` / `runs.boot` | ⏭ blocked behind build (#584) |
| attach | `debug.start_session` | ⏭ blocked behind boot (#584) |
| crash | `control.force_crash` | ⏭ blocked behind boot (#584) |
| capture | `vmcore.fetch` | ⏭ blocked behind crash (#584) |

## Findings

### New this run

- **#582** — `ops.jobs_list` raises a server-side `ToolResponse` validation error (`is_error`,
  null structured content) instead of returning an envelope, whenever a `failed` job exists in
  scope: a per-job item is built with `status='failed'` but no `error_category`, which the
  post-#430 invariant rejects. Breaks the job listing exactly when an operator most needs it.
- **#583** — the worker's `/livez` aux endpoint is starved during a remote build (event-loop
  blocking on the build hot path), so the chart's liveness probe (~30s grace) SIGKILLs the
  worker (`exit=137`) and crash-loops it mid-build. Worked around for this run by patching the
  live worker deployment's liveness `failureThreshold` to 180 (reverted afterward).
- **#584** — after the worker is killed mid-build, the ephemeral build VM `kdive-build-<run_id>`
  is **not reaped** (the build-session cleanup runs in `__exit__`, skipped by SIGKILL).
  Subsequent `runs.build` jobs fail in `_wait_for_network` with `qemu-guest-agent is not
  usable on this build host` and `Storage volume not found: kdive-build-<run_id>.qcow2`. The
  remote build plane stays wedged until the host is cleaned by hand. Same class as #372, for
  *build* VMs. `runs.build` also returns an empty-`detail` `configuration_error` to the client.

### Positive signals worth recording

- **Read plane is fully reachable** (44/44) on the 112-tool surface — no transport/auth gaps.
- **Discoverability tools are accurate:** `runs.profile_examples` returned the exact valid
  build-profile shape per build host (`source:"server"`, structured `kernel_source_ref.git`,
  `build_host`), which is what unblocked the build submission. The #566/#567/#570 work
  (nested schemas, upload-declaration schema, `maturity_detail`) is live and correct.
- **Allocation admission ceiling is fixed:** the remote resource advertises `vcpus`/`memory_mb`
  in its capabilities, so `allocations.request(kind=remote-libvirt)` is granted — the universal
  wall from earlier campaigns is gone.

## Setup deltas worth folding into the runbook / descriptor

- `scripts/coverage_campaign/systems.py render-env` expects a `[campaign.workstation]` section;
  the current `systems.toml` (schema v2, app-inventory) carries only `[campaign.k3s]`, so the
  D1 render path does not apply to a k8s-only rerun. Drive D2 directly via port-forward + an
  in-pod-minted token, as done here.
- The cluster ran a **pre-merge** image (`sha-a757346`); the rerun first rolled it to
  `sha-b45aa02` via `helm upgrade -f <captured-values> --set image.tag=sha-b45aa02` (migrate
  0044 applied, server/worker/reconciler rolled). Confirm `resources.list` shows the provider
  before driving.
- **Leaked-state hygiene (still relevant — #371/#372 + the new #584):** every interrupted
  lifecycle run leaks an `active` allocation (recover with `ops.force_release <id> --reason …`)
  and, for build interruptions, an orphaned `kdive-build-*` VM the reconciler does not reap.

## Reproduce

See `docs/operating/runbooks/mcp-coverage-campaign-rerun.md`. Census + grid:

```
uv run python -c "from scripts.coverage_campaign.gridgen import generate_rows; print(len(generate_rows()))"
# 112
```

Full coverage grid (driven rows; remote-libvirt the only configured provider):

| Tool | Plane | Maturity | Annotation | local-libvirt | remote-libvirt | fault-inject |
|---|---|---|---|---|---|---|
| `accounting.estimate` | accounting | implemented | read_only | — | ✅ | — |
| `accounting.report_all_projects` | accounting | implemented | read_only | — | ✅ | — |
| `accounting.report_granted_set` | accounting | implemented | read_only | — | ✅ | — |
| `accounting.usage_project` | accounting | implemented | read_only | — | ✅ | — |
| `allocations.get` | allocations | implemented | read_only | — | ✅ | — |
| `allocations.list` | allocations | implemented | read_only | — | ✅ | — |
| `allocations.request` | allocations | implemented | mutating | — | ✅ | — |
| `artifacts.expected_uploads` | artifacts | implemented | read_only | — | ✅ | — |
| `artifacts.get` | artifacts | partial | read_only | — | ✅ | — |
| `artifacts.list` | artifacts | partial | read_only | — | ✅ | — |
| `artifacts.search_text` | artifacts | partial | read_only | — | ✅ | — |
| `audit.query` | audit | implemented | read_only | — | ✅ | — |
| `build_hosts.list` | build_hosts | implemented | read_only | — | ✅ | — |
| `buildconfig.get` | buildconfig | implemented | read_only | — | ✅ | — |
| `control.force_crash`★ | control | partial | destructive | — | ⏭(#584) | — |
| `debug.get_session` | debug | implemented | read_only | — | ✅ | — |
| `debug.list_breakpoints` | debug | partial | read_only | — | ✅ | — |
| `debug.list_sessions` | debug | implemented | read_only | — | ✅ | — |
| `debug.read_memory` | debug | partial | read_only | — | ✅ | — |
| `debug.read_registers` | debug | partial | read_only | — | ✅ | — |
| `debug.start_session` | debug | partial | mutating | — | ⏭(#584) | — |
| `fixtures.list` | fixtures | implemented | read_only | — | ✅ | — |
| `fixtures.validate` | fixtures | implemented | read_only | — | ✅ | — |
| `images.list` | images | implemented | read_only | — | ✅ | — |
| `introspect.from_vmcore` | introspect | partial | read_only | — | ✅ | — |
| `introspect.run` | introspect | partial | read_only | — | ✅ | — |
| `inventory.list` | inventory | implemented | read_only | — | ✅ | — |
| `investigations.get` | investigations | implemented | read_only | — | ✅ | — |
| `investigations.list` | investigations | implemented | read_only | — | ✅ | — |
| `investigations.open` | investigations | implemented | mutating | — | ✅ | — |
| `jobs.get` | jobs | implemented | read_only | — | ✅ | — |
| `jobs.list` | jobs | implemented | read_only | — | ✅ | — |
| `ops.export_cost_classes` | ops | implemented | read_only | — | ✅ | — |
| `ops.jobs_list` | ops | implemented | read_only | — | ❌(#582) | — |
| `postmortem.crash` | postmortem | partial | read_only | — | ✅ | — |
| `postmortem.triage` | postmortem | partial | read_only | — | ✅ | — |
| `projects.list` | projects | implemented | read_only | — | ✅ | — |
| `resources.availability` | resources | implemented | read_only | — | ✅ | — |
| `resources.describe` | resources | implemented | read_only | — | ✅ | — |
| `resources.list` | resources | implemented | read_only | — | ✅ | — |
| `runs.boot` | runs | partial | mutating | — | ⏭(#584) | — |
| `runs.build` | runs | partial | mutating | — | ❌(#584) | — |
| `runs.create` | runs | implemented | mutating | — | ✅ | — |
| `runs.get` | runs | implemented | read_only | — | ✅ | — |
| `runs.install` | runs | partial | mutating | — | ⏭(#584) | — |
| `runs.profile_examples` | runs | implemented | read_only | — | ✅ | — |
| `secrets.list` | secrets | implemented | read_only | — | ✅ | — |
| `shapes.list` | shapes | implemented | read_only | — | ✅ | — |
| `systems.get` | systems | implemented | read_only | — | ✅ | — |
| `systems.list` | systems | implemented | read_only | — | ✅ | — |
| `systems.profile_examples` | systems | implemented | read_only | — | ✅ | — |
| `systems.provision` | systems | partial | mutating | — | ✅ | — |
| `vmcore.fetch` | vmcore | partial | mutating | — | ⏭(#584) | — |
| `vmcore.list` | vmcore | partial | read_only | — | ✅ | — |
