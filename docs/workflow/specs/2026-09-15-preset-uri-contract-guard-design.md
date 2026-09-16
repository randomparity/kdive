# Preset endpoint contradicting the published contract — design (#2509)

## Problem

`resolve_libvirt_uri` (`scripts/live-stack/libvirt-uri.sh`) short-circuits on any preset
`KDIVE_LIBVIRT_URI` and never reads the published contract. On a provisioned host a preset
disagreeing with `/etc/kdive/live-worker-libvirt.env` therefore puts the operator's shell and the
worker processes on different daemons with no message — the #2480 split, invisible until failure.

## Scope

`resolve_libvirt_uri`'s `[[ -n "${KDIVE_LIBVIRT_URI:-}" ]]` branch, the one point holding both
values (ADR-0659): it loads the published endpoint, compares, and on disagreement writes a report
to stderr naming both values and the way back, then honours the preset.

Both halves of a loader failure must be caught, because this branch reads no contract today and
must stay silent when there is nothing valid to compare against. Its stderr is discarded, since an
invalid contract writes a metadata or allowlist refusal *before* returning 1; and its status is
taken with an explicit `|| published=''`, since under the callers' `set -euo pipefail` a bare
assignment from a failing command substitution aborts the sourcing shell — on a dev box with a
preset and no contract, every live-stack entry point. With no published value the preset is
honoured exactly as today.

**It reports; it does not refuse**
([ADR-0661](../../adr/0661-a-contradicting-preset-libvirt-endpoint-is-reported-not-refused.md)):
the override is the documented escape hatch (`docs/operating/runbooks/live-testing.md:93`).

### Failure model

- **Actors and deployments** — a local operator at a live-stack entry point on a provisioned host;
  the CI live runner, which presets the published value (`.github/workflows/live.yml`).
- **Invariants at stake** — every consumer a live-stack entry point starts shares one endpoint;
  the deliberate-override path stays usable where the contract is broken or absent.
- **Accepted failure classes** — an operator who reads the report and proceeds still gets the split
  (refusing is what ADR-0661 rejects); a preset read straight by Python, where no resolver runs;
  `stack-status.sh` sources `lib.sh` and `env.sh`, so it reports twice (ADR-0661).
- **Covered elsewhere** — wrong daemon behind a valid contract: #2515, #2516; further
  `LIBVIRT_OPTIONAL` conversions: ADR-0659, unassigned.

## Success

1. Preset disagreeing with a valid published contract: stderr carries both URIs and names
   `KDIVE_LIBVIRT_URI` as the way back; the preset is still exported; exit 0.
2. Preset equal to the published value: no report.
3. Preset with an absent or invalid contract: no output at all, preset exported, exit 0.

## Validation

`focused-test` entries in `tests/scripts/test_live_stack_scripts.py`, green via `just test-verbose`:

- Success 1 — `test_a_preset_contradicting_the_contract_is_reported`; red today, because the branch
  never loads the contract and stderr is empty.
- Success 2 — `test_a_preset_matching_the_contract_is_not_reported`; red on a presence-keyed guard.
- Success 3 — `test_a_preset_is_honoured_silently_without_a_valid_contract`, four inputs across the
  loader's three refusals (two reach `require_exact_libvirt_env`), asserting exit 0 and empty
  stderr; red without either half of the loader-failure handling.
- Existing preset expectations, now meeting the guard — the `("qemu:///system", True, …)` case of
  `test_live_stack_env_resolves_one_libvirt_endpoint`, which now asserts the report, and
  `test_lifecycle_uri_is_parsed_as_literal_data`.
