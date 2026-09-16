# Onboard preflight severity — design (#2568)

## Problem

`scripts/live-stack/onboard.sh:52-56` runs the local-libvirt preflight in `if ! ...; then`, prints a
`WARN`, and continues. On a native `live_vm` job its precise diagnosis (unreadable `/boot` kernel,
ADR-0222) was discarded, resurfacing eight seconds later as a generic `infrastructure_failure`.

## Scope

Per ADR-0666 the preflight's severity is the **caller's** declaration. `onboard.sh` gains
`KDIVE_ONBOARD_PREFLIGHT`: `advisory` (default — today's `WARN`, unchanged) or `required`, which
stops before `migrate`, non-zero, with the preflight's own `FAIL` text on stderr as the stated
reason. Any other value is refused. `mint-system.sh:30` declares `required` — the failure's caller.

Changed: `onboard.sh`, `mint-system.sh`, `src/kdive/config/external_env.py` and
`docs/guide/reference/config.md` (publishing a `KDIVE_` knob, enforced by
`scripts/guards/check_env_documented.py`), `tests/scripts/test_onboard.py`.
`check-local-libvirt.sh` is reused unchanged — its `note_fail` / `note_warn` split already makes
exit status the blocking signal. A clean extension: no owner moves and no caller migrates.

Out of scope (operator-approved 2026-09-16): Fedora/RHEL relabel behaviour; retrofitting broken
hosts; the `postinst.d` relabel hook (#2567); `just ci` recipe coverage (#2582).

### Failure model

- **Actors, deployments** — an operator at `just onboard`; the `demo-up.sh` workstation demo; the
  hosted `live_vm_tcg` and self-hosted native `live_vm` CI jobs.
- **Invariants at stake** — the advisory default for callers that only fund a project; a demo with
  no provisionable libvirt still onboarding. The stop precedes `migrate`, stranding no rows.
- **Accepted classes** — `required` gates every required check, not only those the caller's next
  step uses (`onboard.sh` cannot see that step; bounded by `KDIVE_PREFLIGHT_KDUMP=optional`). A
  fifth caller inherits `advisory` silently (unchanged behaviour). No trust boundary moves — the
  invoker already controls the script and `required` only narrows — so no threat model is warranted.
- **Covered elsewhere** — surviving a kernel upgrade: #2567.

## Success

1. Under `required`, a failing preflight stops `onboard.sh` before `migrate`, named on stderr.
2. Unset or `advisory` reproduces today's `WARN`-and-continue.
3. A passing preflight proceeds under both values.
4. An unrecognised value is refused, not treated as `advisory`.
5. `mint-system.sh` invokes `onboard.sh` under `required`.

## Validation

The first four are `focused-test` cases green via `uv run pytest tests/scripts/test_onboard.py -q`:

- **`required` + failing preflight stops before `migrate`** —
  `test_required_preflight_failure_aborts_before_migrate`; red today: exit 0, `migrate` logged.
- **Default stays advisory** — the existing `test_preflight_failure_is_advisory` plus an explicit
  `advisory` case; red under a blanket hard-fail.
- **Passing preflight proceeds under `required`** — `test_required_preflight_success_proceeds`; red
  if the gate reads the exit status inverted.
- **Invalid value refused** — `test_invalid_preflight_mode_is_refused`, asserting non-zero and the
  offending value echoed; red today, which accepts anything.
- **`mint-system.sh` declares `required`** — `focused-test`: a structural case in
  `tests/scripts/test_mint_system.py`; `uv run pytest tests/scripts/test_mint_system.py -q`.
- **The knob is a published operator setting** — `focused-test`: `just env-docs-check`, red on a
  `KDIVE_*` token under `scripts/` absent from `external_env.py`.
