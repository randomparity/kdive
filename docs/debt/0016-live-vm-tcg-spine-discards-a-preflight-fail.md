# 0016 — The hosted live_vm_tcg spine still discards a preflight FAIL

## Status

> **Resolved by #2626** (2026-09-21)

## Concern

ADR-0666 makes the local-libvirt preflight's severity the caller's declaration: `onboard.sh` reads
`ONBOARD_PREFLIGHT`, and the caller whose next step provisions declares `required`. Issue #2568
applied that to `scripts/live-vm/mint-system.sh`, the caller the reported failure came from.

Two of `onboard.sh`'s four callers provision. The other is the hosted `live_vm_tcg` spine at
`.github/workflows/live.yml:549`, whose same shell goes on to run `scripts/live-vm/preflight-env.sh
tcg` (`:563`) and `pytest -m live_vm_tcg` (`:570`), and whose following job step captures a
provision-evidence target. It stays on `advisory`, so on that tier a preflight `FAIL` is still
downgraded to a `WARN` and a dependent later step still surfaces a generic failure — the exact
shape #2568 reports, unclosed.

The spine's own guard at `live.yml:550-553` does not cover it: it fires only when `onboard.sh`
minted no `KDIVE_TOKEN`. A preflight `FAIL` that still permits the mint passes that check.

The residual is narrow but real. The specific `/boot` trigger from #2568 does not reach this tier —
`live.yml:100` runs `sudo chmod 0644 /boot/vmlinuz-*` on the hosted runner — but every other
blocking check in `scripts/operations/check-local-libvirt.sh` can still `FAIL` there and be
discarded.

## Why deferred

`.github/workflows/live.yml` is listed in issue #2568's approved scope charter (token
`q2568-e003bba4`) as read but not changed. Converting the spine is a workflow change with its own
review surface, and #2568 was split out of #2544 precisely to keep a CI-provable fix from growing
host- and workflow-dependent obligations.

Deferring costs little because the call site is already the right shape: `live.yml:549` uses
`onboard_wiring="$(scripts/live-stack/onboard.sh)"`, a bare assignment whose command-substitution
status propagates under `errexit`. Unlike `mint-system.sh:30`, it needs no call-shape change — only
the declaration.

## Non-regression boundary

- `onboard.sh` must keep `advisory` as its default, so this tier's present behaviour is the
  documented one rather than an accident.
- `live.yml:549` must keep the bare-assignment capture. Rewriting it as `eval "$(onboard.sh | ...)"`
  would reintroduce the `errexit` hole ADR-0666 removes from `mint-system.sh`, and would make this
  record more expensive to resolve than it is now.

## What would resolve it

Declare the severity at the hosted spine's call site:

```sh
onboard_wiring="$(ONBOARD_PREFLIGHT=required scripts/live-stack/onboard.sh)"
```

then confirm on a hosted `live_vm_tcg` run that a stubbed or genuine preflight `FAIL` takes the job
red at the preflight, naming the preflight, rather than at a later proof. Done when that call site
declares `required` and ADR-0666's `## Consequences` no longer names this tier as an open residual.

## Provenance

target: .github/workflows/live.yml
Deferred while implementing issue #2568 on 2026-09-16, under that issue's approved scope charter,
which lists `.github/workflows/live.yml` as read but not changed. Recorded because two independent
design reviews and the scope audit each raised the residual, and the issue closes with the same
pull request that writes ADR-0666.
