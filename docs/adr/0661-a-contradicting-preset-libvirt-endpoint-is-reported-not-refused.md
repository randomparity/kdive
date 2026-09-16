# 0661 — A contradicting preset libvirt endpoint is reported, not refused

## Status

Accepted (2026-09-15)

## Context

`resolve_libvirt_uri` (`scripts/live-stack/libvirt-uri.sh`) resolves the one libvirt endpoint every
consumer a live-stack entry point starts must share (#2480). An explicit caller value wins: the
`[[ -n "${KDIVE_LIBVIRT_URI:-}" ]]` branch short-circuits without ever reading
`/etc/kdive/live-worker-libvirt.env`, so on a provisioned host a preset that disagrees with the
published contract puts the operator's shell and the worker processes on different daemons with no
message. That is the split #2480 removed the `qemu:///system` fallback to prevent, reached through
the one path still allowed to be silent.

ADR-0659 settled *where* the guard belongs — this branch, the one point holding both values — and
left *what it does* open. Issue #2509 names the question directly: "Whether it refuses or warns is
the decision to make: refusing protects the common case, warning preserves the escape hatch." Its
acceptance asks for both halves — the disagreement reported, and the deliberate-override path still
available.

## Decision

**On a host whose contract publishes a valid endpoint, a preset `KDIVE_LIBVIRT_URI` that differs
from it is reported on stderr — naming both values and that unsetting the variable restores the
published one — and is then honoured. It is never refused.**

The guard fires only when `load_published_libvirt_uri` succeeds. An absent or invalid contract
yields no published value to disagree with, so the preset is honoured silently, exactly as before:
that path *is* the documented way past a broken contract, and emitting a validation error on it
would break the escape hatch at the moment it is needed.

That silence has to be arranged, because the loader is loud on failure in both channels: it
writes `lifecycle prerequisite has untrusted metadata` or an allowlist refusal to stderr *before*
returning 1 (`libvirt-uri.sh:60-86`). On this branch both halves are caught — the message
discarded, the status taken explicitly — and the specification carries the exact spelling. Getting
either half wrong turns the escape hatch into the thing it escapes.

## Consequences

The silent server/worker split now has no remaining entrance: unset resolves fail-closed, and
preset-and-disagreeing reports. What stays possible is a *disclosed* split — an operator who reads
the report and proceeds. That is the escape hatch working, not a gap.

Agreeing presets stay silent, which is what keeps `.github/workflows/live.yml` and the self-hosted
runner runbook quiet: both set the variable to the value the contract publishes.

The report reaches stderr only. A consumer that captures stderr, or an operator who scrolls past
it, still gets the split; nothing in this record claims otherwise, and #2509's acceptance asks for
the disagreement to be *reported*, not prevented.

The report is emitted once per call, not once per shell, so `stack-status.sh` — which sources
both `lib.sh` and `env.sh`, each calling the resolver (`lib.sh:68`, `env.sh:15`) — prints it twice.
A per-shell record was considered and cut: it buys one duplicate advisory line at the price of a
second shell global on a file whose existing one needed a dedicated regression test to establish
that it is an output and never an input.

This record does not touch behaviour behind a *valid* contract on a wrong or unreachable daemon —
the wrong-daemon reap and the unobservable teardown are #2515 and #2516, as ADR-0659 already
records.

## Considered & rejected

- **Refuse the contradicting preset.** verified: `docs/operating/runbooks/live-testing.md:93`
  states the override unconditionally — "`KDIVE_LIBVIRT_URI` is the operator escape hatch across
  every family" — and that is the citation refusing contradicts. The two other places naming the
  export, `scripts/live-stack/stack-status.sh:67` and this file's abort message at
  `scripts/live-stack/libvirt-uri.sh:142`, both sit on the *broken*-contract path, where this guard
  never fires; they are precedent for the escape hatch, not instances of it. Refusing also takes
  away the second half of #2509's acceptance.
- **Refuse, and add an opt-out knob to restore the override.** verified:
  `scripts/guards/check_env_documented.py:36` sweeps `scripts/` for `KDIVE_[A-Z0-9_]+` and requires
  every hit to be a registry setting or a catalogued `external_env` entry rendered into the
  generated config reference, so a prefixed knob must be published as an operator knob; an
  unprefixed one is reserved for an entry point's declaration about itself (ADR-0659), which this
  is not. Either way it is new operator surface to restore a path that already exists.
- **Report on the preset's presence rather than on its value.** verified:
  `.github/workflows/live.yml` presets `KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)"` at four
  steps, and `docs/operating/runbooks/self-hosted-kvm-runner.md:376-377` prescribes the same — all
  agreeing presets, which a presence check would report on every live CI run.
- **Load the contract unconditionally in the preset branch.** verified: with the contract absent or
  invalid, `load_published_libvirt_uri` returns 1 after writing `lifecycle prerequisite has
  untrusted metadata` or an allowlist refusal to stderr (`libvirt-uri.sh:60-86`) — on the preset
  path, which exists to get past exactly that state.
- **Suppress the second report with a per-shell record.** verified: `stack-status.sh` sources both
  `lib.sh` and `env.sh` in one shell and each calls the resolver (`lib.sh:68`, `env.sh:15`), so the
  duplicate is real — but it is one advisory line on one entry point, and the sibling record
  `LIBVIRT_UNRESOLVED` shows the cost of a shell global here: `libvirt-uri.sh:39-52` spends fourteen
  lines establishing that it is an output and never an input, backed by its own regression test
  (`test_live_stack_scripts.py::test_an_inherited_degraded_record_does_not_suppress_resolution`).
- **Do nothing; leave the override silent.** judgment: cost. The failure surfaces far from its
  cause, which is the bill #2480 already paid once.
