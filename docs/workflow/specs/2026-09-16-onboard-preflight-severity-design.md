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

`scripts/live-vm/mint-system.sh` gets both halves: it declares `required` **and** captures
`onboard.sh` before `eval`, the `live.yml:549` shape. Its present `eval "$(onboard.sh | grep ...)"`
at `:30` discards a non-zero exit, so the stop would arrive as a token-mint failure that never was.

Changed: `onboard.sh`, `mint-system.sh`, `tests/scripts/test_onboard.py`,
`tests/scripts/test_mint_system.py`. `check-local-libvirt.sh` is reused unchanged: no owner moves.

Out of scope (operator-approved 2026-09-16): Fedora/RHEL relabel behaviour; retrofitting broken
hosts; the `postinst.d` relabel hook (#2567); `just ci` recipe coverage (#2582).

### Failure model

- **Actors, deployments** — an operator at `just onboard`; the `demo-up.sh` workstation demo; the
  hosted `live_vm_tcg` and self-hosted native `live_vm` CI jobs.
- **Invariants at stake** — the advisory default for callers that only fund a project; a demo with
  no provisionable libvirt still onboarding. The stop precedes `migrate`, stranding no rows.
- **Accepted classes** — `required` gates all nine blocking checks, not only those the caller's next
  step uses (that caller provisions and needs every one; #2568's quoted runner output carries one
  `FAIL`, so the other eight pass there). The `live_vm_tcg` spine provisions too and stays advisory
  (`live.yml` is out of scope). Exporting `ONBOARD_PREFLIGHT=required` globally reaches
  `demo-up.sh:66` (known cost of the category — `scripts/live-stack/README.md:54`). No trust
  boundary moves, so no threat model.
- **Covered elsewhere** — surviving a kernel upgrade: #2567.

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
