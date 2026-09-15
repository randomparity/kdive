# Live-stack backend bring-up has one owner

## Problem

`just stack-up` and `scripts/live-stack/up.sh` each bring up the same three compose backends,
duplicating the mock-OIDC pre-build and diverging on readiness. `stack-up` runs `seaweedfs-init`
as `run --rm`, so a bucket or versioning failure fails the recipe; `up.sh` starts it inside
`up -d` and polls only postgres, so the same failure is silent on the path the `live_stack` and
`live_vm_tcg` suites use. Nothing states which bring-up path serves which purpose.

## Scope

Add one function to `scripts/live-stack/lib.sh`: pre-build `kdive-mock-oidc:dev` when
`KDIVE_OIDC_IMAGE` is unset, `up -d --wait --wait-timeout 120` over `postgres seaweedfs oidc`,
then `seaweedfs-init` via `run --rm` with its exit status propagated. Both callers use it and
keep no backend bring-up of their own. It stops at the backends; each caller applies migrations
itself. `KDIVE_BACKEND_SERVICES` keeps naming all four services for `status.sh`. Decision:
[ADR-0655](../../adr/0655-one-owner-for-live-stack-backend-bring-up.md). Add a bring-up layer
model to AGENTS.md, each layer with its one owner: backends `just stack-up`; host contract the
Ansible play or systemd installer; processes `scripts/live-stack/up.sh`; project funding `just
onboard`; client wiring `examples/local-libvirt/up.sh`.

Out of scope: onboarding-script collapse and worker-lifecycle consolidation (separate cycles);
`just compose-up`; `docker-compose.yml`; `up.sh`'s reset-db, app-tier removal, obs,
role-bootstrap, libvirt and host-process phases; Kubernetes; `down.sh`.

### Failure model

- **Actors and deployments.** A local operator at a terminal and a self-hosted CI runner,
  running these scripts from a checkout against a local Docker daemon. No network caller.
- **Invariants at stake.** `up.sh` must never start the compose app tier — host processes own
  it. A reported-healthy backend set must mean the artifacts bucket exists and is versioned.
- **Accepted failure classes.** A `--wait` timeout reports generically rather than naming the
  unhealthy service: bounded, the operator reads `docker compose ps` next. Obs-profile failures
  stay warn-only, unchanged.
- **Covered elsewhere.** Migration drift: ADR-0015 and `up.sh --reset-db`. Store protocol
  compatibility: the worker's own startup check.

## Success

1. Neither `up.sh` nor the `stack-up` recipe contains a backend `compose … up` or an oidc
   pre-build; both call the shared function.
2. A failing `seaweedfs-init` fails `scripts/live-stack/up.sh`.
3. The app-tier guard reads the file holding the compose invocation.
4. AGENTS.md names the five bring-up layers and each layer's owner.

## Validation

- **One-shot failure propagates** — Mode: `focused-test`.
  `tests/live_stack/test_up_invariants.py`, new case asserting `lib.sh` runs `seaweedfs-init`
  through `run --rm`, outside the `--wait` set. Red before the function exists. Green:
  `uv run python -m pytest tests/live_stack/test_up_invariants.py -q`.
- **Callers keep no bring-up** — Mode: `focused-test`. Same file: neither `up.sh` nor the
  `stack-up` recipe body matches a backend `compose … up` or the oidc pre-build. Red today.
- **App-tier guard follows the code** — Mode: `focused-test`. Extend
  `test_up_never_starts_the_app_tier` to read `lib.sh` too; red while it reads only `up.sh`.
- **AGENTS.md layer model** — Mode: `task-test-not-applicable`. Its references are checked by
  `just docs-links` and `just docs-paths`; the prose has no executable consumer, and asserting
  on wording would test the snapshot, not a contract.
