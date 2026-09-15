# 0655 — Stack entry points are named for the state they leave

## Status

Accepted (2026-09-15)

## Context

Four entry points bring up parts of the live stack, and none of their names says where it
stops.

`just stack-up` starts the three compose backends, creates the bucket, and migrates. It does
not start a stack. `scripts/live-stack/up.sh` additionally starts libvirt and the host
processes, then prints, at `up.sh:229`, "The stack is up but no project is funded yet" — the
script states its own name is wrong. `just onboard` funds a project and mints a token.
`examples/local-libvirt/up.sh` is the only path that leaves a usable system, and it is
filed under `examples/`.

Four documents give four accounts of `just stack-up`. `.github/workflows/live.yml:523-524`
says "up.sh owns the whole bring-up (backends + bucket, migrations, libvirt, host processes,
inventory reconcile), so it replaces `just stack-up` outright", and all three live gates
(`live.yml:413`, `:527`, `:770`) call `up.sh` with no `stack-up` before it.
`docs/operating/runbooks/live-stack.md` presents them as sequential steps 1 and 4.
`scripts/live-stack/README.md:47` calls `stack-up` the path "for the `just test-live-stack`
suite" — a suite that needs the host processes `stack-up` does not start. AGENTS.md instructs
`just stack-up`, then `scripts/live-stack/up.sh`, which is the runbook's ordering and is
redundant, because `up.sh:82` brings the backends up itself.

Every real consumer runs `up.sh` and `onboard.sh` as a pair: `live.yml:548` captures
`onboard.sh` output and fails the job at `:550` if no token is minted, and
`examples/local-libvirt/up.sh:60,66` calls both. Nobody wants to stop where `up.sh` stops.

The backends are also brought up twice, by two different readiness contracts. `stack-up` runs
`seaweedfs-init` as `docker compose run --rm` (`justfile:355`), so a bucket or versioning
failure fails the recipe. `up.sh` lists the one-shot among the services it starts with a plain
`up -d` and then polls only postgres, so the same failure is silent on the path CI uses.

## Decision

Name every entry point for the state it leaves behind, and give the layers below funding one
implementation.

`scripts/live-stack/up.sh` becomes `scripts/live-stack/stack-services.sh`, taking
`--stage backends|services` (default `services`). Its `backends` stage brings up and verifies
the backends and applies migrations; `services` continues through role bootstrap, libvirt,
host processes, and inventory reconcile. `just stack-up` becomes `just stack-backends`,
implemented as `stack-services.sh --stage backends`, so there is one implementation and one
readiness contract rather than two.

That single contract is the stronger of the two existing ones: pre-build the mock-OIDC image
when `KDIVE_OIDC_IMAGE` is unset, `up -d --wait --wait-timeout 120` over the three
long-running backends, then `seaweedfs-init` via `run --rm` with its exit status propagated.
The one-shot stays outside the `--wait` set because `--wait` treats any container exit as a
wait failure, so a healthy stack would otherwise report failure.

`scripts/live-stack/down.sh` and `status.sh` become `stack-down.sh` and `stack-status.sh`, and
`examples/local-libvirt/{up,down}.sh` become `demo-{up,down}.sh` — the demo pair being the
only one that reaches a usable system. `just onboard` keeps its name, which is already
accurate, and keeps returning shell exports on stdout.

These are internal developer entry points with no external consumer, so the old names are
replaced outright rather than aliased. A compatibility shim would preserve the ambiguity this
removes.

## Consequences

`stack-services.sh` gains failures `up.sh` did not have. A failed bucket creation now fails
bring-up directly instead of surfacing later as a worker store check, and `--wait` hard-fails
on an unhealthy `seaweedfs` where the replaced code polled only postgres. The worst-case wait
grows from 30s to 120s. An operator whose object store was quietly failing will see bring-up
start failing where it previously continued; that is the intent, not a regression.

A `--wait` timeout reports generically rather than naming the unhealthy service, where the
replaced postgres poll named its own. Because two of the three named deployments are
non-interactive CI actors that cannot run the obvious remedy, the bring-up function dumps
`docker compose ps` to stderr on a non-zero `--wait` rather than leaving the job log with a
bare timeout.

Merged ADRs, proof records under `docs/design/`, and archived plans keep citing
`scripts/live-stack/up.sh` and `just stack-up`. They are point-in-time records and are not
swept; this ADR is where a reader learns the current names.

The text guards in `tests/live_stack/test_up_invariants.py` read one file — `up.sh` — by path,
and match `compose\b.*\bup\b`. They must follow the rename, and they must also start reading
`lib.sh`, because the backend `compose up` moves there and the invariant they protect is that
no bring-up file starts the app tier. Reading `lib.sh` is what makes a discriminator
necessary: `lib.sh:229` is a comment matching that regex whose text contains
`KDIVE_WORKER_COUNT`, so a case-insensitive `worker` check fails on it. Skip comments; do not
skip the observability line, which carries no app-tier name today and must stay guarded
against one arriving.

`just stack-backends` inherits three preconditions the `stack-up` recipe never had, because it
now delegates to the bring-up script: a refusal to run as UID 0, a `.venv` interpreter check,
and `docker compose rm -sf migrate server worker reconciler`. The first is a behavior change
on a path that worked as root. The third is destructive and reaches the containerized tier, so
it belongs to the `services` stage — host processes and compose containers contend for port
8000, which is a services concern — and is gated there rather than run for a backends-only
bring-up. The observability profile is likewise a `services` phase: `just stack-up` never
started prometheus, and `just stack-backends` must not either.

## Considered & rejected

- **Keep two implementations sharing one `lib.sh` function.** verified: CI never invokes
  `just stack-up` — `rg -n 'stack-up|live-stack/up\.sh' .github/workflows/` returns `up.sh` at
  `live.yml:413`, `:527`, `:770` and a comment at `:523` stating it "replaces `just stack-up`
  outright". They were never peers, so a shared function would have preserved a distinction
  the working configuration does not make.
- **Make `up.sh` honest by folding funding into it.** verified: `live.yml:553` runs
  `eval "$(grep '^export KDIVE_TOKEN=' <<<"$onboard_wiring")"`, so `onboard.sh`'s stdout is a
  consumed contract; and its funding path uses the demo mock issuer, which would become
  mandatory in every bring-up.
- **Rename only `just stack-up` and the example pair.** judgment: leaves the script CI calls
  three times still claiming to leave a usable stack, which is the misleading name that
  matters most.
- **Alias the old names for a deprecation window.** judgment: no external consumer exists, and
  an alias keeps both names answerable to "what does this leave me with".
- **Do nothing.** verified: four in-repo sources — `live.yml:523`, `live-stack.md` §1/§4,
  `scripts/live-stack/README.md:47`, and `AGENTS.md:123` — give four different accounts of
  what `just stack-up` is for, and the script's own `up.sh:229` contradicts its name.
