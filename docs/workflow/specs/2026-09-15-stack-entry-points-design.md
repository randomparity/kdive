# Stack entry points named for the state they leave

## Problem

Four entry points bring up parts of the live stack and none of their names says where it
stops. `just stack-up` starts backends only. `scripts/live-stack/up.sh` stops short of a
usable system and says so at `up.sh:229`. `just onboard` funds a project.
`examples/local-libvirt/up.sh` is the only complete path and lives under `examples/`.

Four in-repo sources disagree about `just stack-up`: `live.yml:523-524` says `up.sh` "replaces
`just stack-up` outright"; `runbooks/live-stack.md` presents them as sequential steps;
`scripts/live-stack/README.md:47` assigns it to a suite it cannot serve; `AGENTS.md:123`
prescribes a redundant ordering. All three CI live gates call `up.sh` alone.

The backends are brought up twice under two readiness contracts. `stack-up` runs
`seaweedfs-init` through `run --rm`, so a bucket failure fails the recipe; `up.sh` starts it
inside `up -d` and polls only postgres, so the same failure is silent on the CI path.

## Scope

Rename each entry point for the state it leaves, and give every layer below funding one
implementation. Decision: [ADR-0655](../../adr/0655-stack-entry-points-named-for-end-state.md).

| Current | Becomes | Leaves you with |
|---|---|---|
| `just stack-up` | `just stack-backends` | backends, bucket, schema |
| `scripts/live-stack/up.sh` | `scripts/live-stack/stack-services.sh` | + libvirt, host processes, inventory |
| `scripts/live-stack/down.sh` | `scripts/live-stack/stack-down.sh` | stopped, state kept |
| `scripts/live-stack/status.sh` | `scripts/live-stack/stack-status.sh` | a health report |
| `just onboard` | unchanged | + funding, token on stdout |
| `examples/local-libvirt/up.sh` | `examples/local-libvirt/demo-up.sh` | + preflight, `.mcp.json` |
| `examples/local-libvirt/down.sh` | `examples/local-libvirt/demo-down.sh` | demo stopped |

`stack-services.sh` takes `--stage backends|services`, default `services`. The `backends`
stage pre-builds the mock-OIDC image when `KDIVE_OIDC_IMAGE` is unset, runs
`up -d --wait --wait-timeout 120` over `postgres seaweedfs oidc`, runs `seaweedfs-init` via
`run --rm` with its exit status propagated, and applies migrations. The `services` stage
continues through role bootstrap, libvirt, host processes, inventory reconcile, status, and
the existing "next: fund a project" guidance. `just stack-backends` is
`stack-services.sh --stage backends`. Existing flags `--skip-libvirt`, `--skip-obs` and
`--reset-db` keep their behavior and are rejected with a clear message under
`--stage backends`, which reaches none of them.

`KDIVE_BACKEND_SERVICES` stays in `lib.sh` naming all four services for `stack-status.sh`. The
backends stage names its three long-running services separately, because the one-shot must sit
outside the `--wait` set.

Old names are replaced outright — internal developer entry points, no external consumer, no
alias. Every live reference is updated in this change.

The file map is generated, not asserted. Three hand-written patterns during design produced
three different counts (25, 26, 39) because each missed a form: hidden directories, and bare
`up.sh` references carrying no path prefix. The plan's first task regenerates it with

```
rg -l --hidden '\b(up|down|status)\.sh\b|\bstack-up\b' --glob '!.git/**' --glob '!CHANGELOG.md'
```

and classifies every hit against the rule below before any rename. The sweep test in
Validation is what proves completeness; no count is frozen here.

**Sweep rule.** Update a reference when the file is live surface: code, configuration, tests,
CI, provisioning, `AGENTS.md`, and the operating guides under `docs/operating/` and
`docs/guide/`. Never update an immutable record: `docs/adr/` and `docs/debt/` are append-only
under the `records` gate, and `docs/archive/`, `docs/superpowers/`, `docs/design/` proof
records and merged `docs/workflow/` plans are point-in-time. They keep citing the old paths;
ADR-0655 is where a reader learns the current names.

Out of scope: the onboarding-script collapse and the worker-lifecycle-contract consolidation
(separate cycles); `just compose-up` and the containerized tier; `docker-compose.yml` service
definitions, whose only match is a comment naming `down.sh`; Kubernetes bring-up;
`onboard.sh`'s own behavior and stdout contract.

## Failure model

**Actors and deployments.** A local operator at a terminal; the self-hosted KVM runner and the
hosted TCG runner, both non-interactive, invoking `stack-services.sh` from a checkout at
`live.yml:413`, `:527`, `:770`. No network caller; these scripts are never reachable from the
served MCP surface.

**Invariants and assets at stake.** The bring-up path must never start the compose app tier —
host processes own it, and a compose `server` holding port 8000 produces a 401 that reads as
an auth bug. A bring-up that reports success must mean the artifacts bucket exists and is
versioned. Renaming must not leave a dangling invocation in CI or provisioning, where the
failure is a red gate rather than a wrong result. `onboard.sh`'s stdout contract must survive
unchanged, because `live.yml:553` evaluates it.

**Accepted failure classes.** A `--wait` timeout reports generically rather than naming the
unhealthy service; accepted for the terminal operator, who reads `docker compose ps` next.
For the CI actors this is a real loss of signal, recorded in ADR-0655's Consequences and
mitigated only by the job log. Worst-case backend wait grows 30s → 120s: bounded, and only on
a stack that is already failing. Obs-profile failures stay warn-only, unchanged.

**Covered elsewhere.** Migration drift: ADR-0015 and `--reset-db`. Store protocol
compatibility: the worker's own startup check. Stale references in historical records:
ADR-0655, by decision, not by sweep.

## Success

1. `scripts/live-stack/stack-services.sh --stage backends` and `--stage services` both exist,
   and `just stack-backends` invokes the former.
2. No file outside the not-swept set references `scripts/live-stack/up.sh`,
   `just stack-up`, `scripts/live-stack/{down,status}.sh`, or
   `examples/local-libvirt/{up,down}.sh`.
3. A `seaweedfs-init` that exits non-zero fails `stack-services.sh --stage backends`, and so
   the `services` stage, which runs that stage first through the same code path.
4. The app-tier guard reads the renamed script and does not match the retained `--profile obs`
   line or a comment.
5. `just ci` passes, and the three `live.yml` call sites name existing paths.

## Validation

- **Backends stage propagates the one-shot failure** — Mode: `focused-test`. Contract: a
  non-zero `seaweedfs-init` fails the stage. `tests/scripts/test_live_stack_scripts.py`, new
  case driving `stack-services.sh --stage backends` with a stubbed `docker` on `PATH` that
  exits 7 for `run --rm seaweedfs-init`; assert non-zero exit. Red against today's `up.sh`,
  which exits 0. Green: `uv run python -m pytest tests/scripts/test_live_stack_scripts.py -q`.
- **The one-shot stays outside the `--wait` set** — Mode: `focused-test`. Contract: `--wait` is
  applied only to the three long-running backends. Same file, stubbed `docker` recording argv;
  assert the `--wait` invocation names `postgres seaweedfs oidc` and not `seaweedfs-init`. Red
  if the one-shot is folded back in.
- **Stage selection** — Mode: `focused-test`. Contract: `--stage backends` stops before role
  bootstrap; an unsupported stage and a `--skip-libvirt` under `--stage backends` both exit
  non-zero. Same file, stubbed `docker`; assert on recorded argv and exit status.
- **App-tier guard follows the rename and discriminates** — Mode: `focused-test`.
  `tests/live_stack/test_up_invariants.py` retargeted at `stack-services.sh`, with the
  predicate narrowed to non-comment lines matching `compose … up` that do not carry
  `--profile obs`. Red while it reads `up.sh`; red on a naive predicate, which matches
  `up.sh:91`.
- **No dangling old references** — Mode: `focused-test`. Contract: Success criterion 2.
  `tests/scripts/test_live_workflow_shape.py`, new case walking tracked files outside the
  not-swept set and asserting no match for the old names. Red before the sweep.
- **CI call sites resolve** — Mode: `focused-test`. Same file, assert every
  `scripts/live-stack/*.sh` path named in `.github/workflows/live.yml` exists on disk. Red if
  a rename misses a call site.
- **Generated config reference** — Mode: `focused-test`. `just config-docs-check`, already a CI
  gate, fails until `docs/guide/reference/config.md` is regenerated from the edited
  `external_env.py`.
- **Runbook and AGENTS.md prose** — Mode: `task-test-not-applicable`. `just docs-links` and
  `just docs-paths` check their references, and criterion 2's sweep test covers the old names;
  the remaining prose has no executable consumer, and asserting on wording would test the
  snapshot rather than a contract.
