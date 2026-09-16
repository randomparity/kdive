# Onboard preflight severity — design (#2568)

## Problem

`scripts/live-stack/onboard.sh:52-56` runs the local-libvirt preflight in `if ! ...; then`, prints a
`WARN`, and continues. On a native `live_vm` job its precise diagnosis (unreadable `/boot` kernel,
ADR-0222) was discarded, resurfacing eight seconds later as a generic `infrastructure_failure`.

## Scope

Per ADR-0666 the preflight's severity is the **caller's** declaration. `onboard.sh` gains
`ONBOARD_PREFLIGHT`: `advisory` (default — today's `WARN`) or `required`, which stops before
`migrate`, non-zero, with the preflight's own `FAIL` text on stderr plus a line attributing it.
Any other value is refused. Unprefixed, per ADR-0659's category.

`mint-system.sh` gets both halves: it declares `required` **and** captures `onboard.sh` before
`eval` (the `live.yml:549` shape), since `eval "$(cmd)"` discards a non-zero exit.

Changed: `onboard.sh`, `mint-system.sh`, `tests/scripts/test_onboard.py`,
`tests/scripts/test_mint_system.py`. `check-local-libvirt.sh` is reused unchanged: no owner moves.

Out of scope (operator-approved 2026-09-16): Fedora/RHEL relabel behaviour; retrofitting broken
hosts; the `postinst.d` relabel hook (#2567); `just ci` recipe coverage (#2582).

### Failure model

- **Actors, deployments** — an operator at `just onboard`; the `demo-up.sh` workstation demo; the
  hosted `live_vm_tcg` and self-hosted native `live_vm` CI jobs.
- **Invariants at stake** — the advisory default for callers that only fund a project; a demo with
  no provisionable libvirt still onboarding; the stop precedes `migrate`, stranding no rows. Two
  boundaries: `ONBOARD_PREFLIGHT` entering `onboard.sh`, closed by a two-value allowlist; and the
  failure re-emit into a public CI log, filtering `export KDIVE_TOKEN=` (the grep's complement).
- **Accepted classes** — `required` gates all nine blocking checks, not only those the next step
  uses; the three probing `qemu:///system` pass on the native host (`libvirt_stack` starts the
  system sockets, `libvirt_pool_net` activates `default`, `live_vm_host`'s `verify.yml:40-45`
  gates on `libvirt` group). Exporting it globally reaches `demo-up.sh:66` (category cost).
- **Covered elsewhere** — kernel upgrades: #2567. The advisory `live_vm_tcg` spine (`live.yml` out
  of scope): `docs/debt/0016-live-vm-tcg-spine-discards-a-preflight-fail.md`.

## Success

1. Under `required`, a failing preflight stops `onboard.sh` before `migrate`, attributed on stderr.
2. Unset, empty or `advisory` reproduces today's `WARN`-and-continue.
3. A passing preflight proceeds under both values.
4. An unrecognised value is refused, not treated as `advisory`.
5. A non-zero `onboard.sh` aborts `mint-system.sh` at the preflight, not at the token check.

## Validation

The first four are `focused-test` cases green via `uv run pytest tests/scripts/test_onboard.py -q`:

- **`required` + failing preflight stops before `migrate`** — `test_required_aborts`: non-zero, the
  stub's `FAIL` text and attributing line on stderr, no `migrate` logged; red today (exit 0).
- **Default stays advisory** — existing `test_preflight_failure_is_advisory` plus an explicit
  `advisory` case, both asserting `WARN` on stderr; red under a blanket hard-fail.
- **Passing preflight proceeds under `required`** — `test_required_proceeds`; red if inverted.
- **Invalid value refused** — `test_invalid_preflight_refused`: non-zero, value echoed; red today.
- **`mint-system.sh` aborts on a non-zero `onboard.sh`** — `focused-test` in
  `tests/scripts/test_mint_system.py` (`-q`): non-zero without `did not mint a token`, staging never
  reached; red against the present `eval` shape.
