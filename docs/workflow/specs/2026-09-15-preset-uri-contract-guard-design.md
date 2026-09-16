# Preset endpoint contradicting the published contract — design (#2509)

## Problem

`resolve_libvirt_uri` (`scripts/live-stack/libvirt-uri.sh`) short-circuits on any preset
`KDIVE_LIBVIRT_URI` and never reads the published contract. On a provisioned host a preset
disagreeing with `/etc/kdive/live-worker-libvirt.env` therefore puts the operator's shell and the
worker processes on different daemons with no message — the #2480 split, invisible until failure.

## Scope

`resolve_libvirt_uri`'s `[[ -n "${KDIVE_LIBVIRT_URI:-}" ]]` branch, the one point holding both
values (ADR-0659). It loads the published endpoint, compares, and on disagreement writes a report
to stderr naming both values and the way back, then honours the preset. The load is guarded: an
absent or invalid contract yields no published value, so the guard stays silent and the preset is
honoured exactly as today. Reported once per shell, so `stack-status.sh` — which sources `lib.sh`
and `env.sh`, each calling the resolver — does not report twice.

**It reports; it does not refuse**
([ADR-0661](../../adr/0661-a-contradicting-preset-libvirt-endpoint-is-reported-not-refused.md)):
the override is the documented operator escape hatch (`docs/operating/runbooks/live-testing.md`)
and the recovery path both `stack-status.sh` and this file's own abort message name, so refusing
it needs a second opt-out knob — new operator surface.

Out of scope: wrong-daemon behaviour behind a *valid* contract (#2515, #2516); converting further
entry points to `LIBVIRT_OPTIONAL` (ADR-0659, unassigned).

### Failure model

- **Actors and deployments** — a local operator at a live-stack entry point on a provisioned host;
  the CI live runner, which presets the published value (`.github/workflows/live.yml`).
- **Invariants at stake** — every consumer a live-stack entry point starts shares one endpoint;
  the deliberate-override path stays usable where the contract is broken or absent.
- **Accepted failure classes** — an operator who ignores the stderr report still gets the split
  (the override is deliberate by construction, and refusing it is what ADR-0661 rejects); a preset
  read directly by Python (`kdive.config`), unreported, because no shell resolver runs there.
- **Covered elsewhere** — wrong or unreachable daemon behind a valid contract: #2515, #2516.

## Success

1. Preset disagreeing with a valid published contract: reported on stderr, preset still exported.
2. Preset equal to the published value: no report.
3. Preset with an absent or invalid contract: no report, preset exported, exit 0.
4. Exactly one report when `lib.sh` and `env.sh` are both sourced in one shell.
5. `just ci` green.

## Validation

Every entry is `focused-test` in `tests/scripts/test_live_stack_scripts.py`, green via
`just test-verbose tests/scripts/test_live_stack_scripts.py`:

- Success 1 — `test_a_preset_contradicting_the_contract_is_reported`; red today because the branch
  never loads the contract, so stderr is empty.
- Success 2 — `test_a_preset_matching_the_contract_is_not_reported`; red if the guard keys on the
  contract's presence rather than on its value.
- Success 3 — `test_a_preset_is_honoured_silently_without_a_valid_contract`; red if unguarded.
- Success 4 — `test_a_contradicting_preset_is_reported_once_per_shell`; red without the record.
- Existing caller expectation that a preset always wins silently — the `("qemu:///system", True,
  …)` case of `test_live_stack_env_resolves_one_libvirt_endpoint`, now asserting the report beside
  the unchanged resolved value.
