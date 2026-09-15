# 0655 — One owner for live-stack backend bring-up

## Status

Accepted (2026-09-15)

## Context

Two paths bring up the same three compose backends. `just stack-up` waits with
`docker compose up -d --wait --wait-timeout 120` over `postgres seaweedfs oidc`, then runs
the `seaweedfs-init` one-shot separately as `docker compose run --rm` so its exit code
propagates — the recipe fails when bucket creation or versioning verification fails.
`scripts/live-stack/up.sh` instead lists `seaweedfs-init` among the services it starts with a
plain `up -d` (`KDIVE_BACKEND_SERVICES`, `lib.sh:16`), then polls only postgres health for 30
seconds. It never reads the one-shot's exit status and never names the bucket.

Both also carry the same `kdive-mock-oidc:dev` pre-build block, with the same rationale
comment, in two files.

So the weaker readiness contract sits on the path the `live_stack` and `live_vm_tcg` suites
use: a failed bucket creation is silent at bring-up there and surfaces later as a worker store
check. The duplication is what let the two drift apart without either changing.

## Decision

`scripts/live-stack/lib.sh` owns one function that brings up and verifies the backends, and
both callers use it. It carries the `stack-up` contract: pre-build the mock-OIDC image when
`KDIVE_OIDC_IMAGE` is unset, `up -d --wait --wait-timeout 120` over the three long-running
backends, then `run --rm seaweedfs-init` as a separate step whose exit status propagates.

The one-shot stays outside the `--wait` set because `--wait` treats any container exit as a
wait failure, so a healthy stack would report failure with the init included.

The function stops at the backends. Each caller applies migrations itself, so `up.sh` keeps
its ADR-0015 recovery guidance and `stack-up` keeps its plain call.

`KDIVE_BACKEND_SERVICES` continues to name all four services for `status.sh`, which reports on
the one-shot too.

## Consequences

`scripts/live-stack/up.sh` gains bucket-creation failure detection it did not have: a broken
object store now fails bring-up directly instead of a later worker readiness check. This is a
behavior change on the live-suite path, and an operator whose store was quietly failing will
see bring-up start failing where it previously continued.

The text guards in `tests/live_stack/test_up_invariants.py` read `up.sh` to prove it never
starts the app tier. The compose invocation moves to `lib.sh`, so those guards must read the
file that now holds it or they pass vacuously.

## Considered & rejected

- **Adopt `up.sh`'s contract instead.** verified: `stack-up` runs `docker compose run --rm
  seaweedfs-init` (`justfile:355`) whose exit status fails the recipe; the `up -d` form does
  not surface the one-shot's exit at all. Unifying downward would remove the only bucket
  verification either path has.
- **Share only the duplicated oidc pre-build.** judgment: removes the visible copy while
  leaving the divergent failure contracts that are the actual defect.
- **Put migrations inside the shared function.** judgment: couples a caller-specific concern —
  `up.sh`'s ADR-0015 recovery message — to a function whose other caller does not want it.
- **Do nothing.** verified: `rg` over `scripts/live-stack/up.sh` returns no match for
  `seaweedfs`, `bucket`, or `init` outside the shared service array, so the gap does not close
  on its own.
