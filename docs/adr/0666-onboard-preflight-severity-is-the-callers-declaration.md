# 0666 — Onboard's preflight severity is the caller's declaration

## Status

Accepted (2026-09-16)

## Context

`scripts/live-stack/onboard.sh:52-56` runs the local-libvirt preflight inside `if ! ...; then`,
prints `WARN: local-libvirt preflight reported problems; funding the project anyway.`, and
continues. Its header at `:9-14` documents that as deliberate: "Advisory (warn, non-fatal): the
provider preflight, the seed's resource-discovery side effect, and the token mint."

On a scheduled native `live_vm` job the preflight correctly reported an unreadable host kernel
under `/boot` (the libguestfs build-fs appliance, ADR-0222). The run continued, and eight seconds
later the same defect resurfaced as `systems.get error: infrastructure_failure — libguestfs failed
extracting the baseline kernel from the rootfs base`. The reader sees a generic code at the bottom
of the log and an accurate diagnosis thirty lines above it, disconnected (#2568).

Issue #2568 offers two shapes and leaves the choice here: hard-fail on any preflight `FAIL`, or
fail only on the `FAIL` entries the following steps depend on. Both assume `onboard.sh` can tell
which entries those are. It cannot. Its own hard gates are `migrate` and `verify-project`, which
need the database and no libvirt at all — ADR-0659 already classifies `onboard.sh` as a
libvirt-free entry point. By `onboard.sh`'s own reckoning **no** preflight `FAIL` blocks its work,
and the advisory default is right. What depends on the provider is whatever the caller does next,
and `onboard.sh` has four of them: `just onboard` (`justfile:63`),
`examples/local-libvirt/demo-up.sh:66`, the hosted `live_vm_tcg` spine at
`.github/workflows/live.yml:549`, and `scripts/live-vm/mint-system.sh:30`, which the native
`live_vm` job reaches at `live.yml:777`.

## Decision

**The preflight's severity is declared by the caller, not decided by `onboard.sh` or by the
preflight. `ONBOARD_PREFLIGHT` takes `advisory` (the default, today's `WARN`) or `required`, which
stops `onboard.sh` before `migrate` with the preflight's own `FAIL` text already on stderr, plus a
line attributing the stop to it. `scripts/live-vm/mint-system.sh` declares `required` **and** takes
the call shape that can observe it.**

The default is unchanged behaviour because the header's advisory intent is correct for the callers
it was written for. `required` is set by the provisioning caller inside this change's surface, the
path the observed failure came from.

The declaration is unprefixed for exactly ADR-0659's reason. It is what that record describes — "a
declaration an entry point makes about itself and its children, not an operator knob" — and
`scripts/guards/check_env_documented.py:35-36` sweeps `src tests scripts deploy` for
`KDIVE_[A-Z0-9_]+`, so a prefixed name would have to be published in
`src/kdive/config/external_env.py` and the generated reference as the operator knob it is not.

The second half is not decoration. `mint-system.sh:30` reads
`eval "$("${here}/../live-stack/onboard.sh" | grep '^export KDIVE_TOKEN=')"`, and a command
substitution in an argument list does not fire errexit — so a `required` stop would reach
`mint-system.sh:31` and terminate with `die "onboard.sh did not mint a token"`, naming a cause that
never occurred. The caller therefore captures `onboard.sh` into a variable first, the shape
`.github/workflows/live.yml:549` already uses on this same script, and dies naming the preflight.

## Consequences

`just onboard` and `demo-up.sh` are unchanged. That matters for `demo-up.sh`: it already hard-gates
the same preflight at `:53-54`, but with `KDIVE_PREFLIGHT_KDUMP` defaulted to `optional`, and that
assignment is command-scoped, so it does not reach the `onboard.sh` invocation at `:66`. A blanket
hard-fail would therefore have failed a first-run workstation on the guestfs/drgn probe the demo
had just deliberately downgraded — the case #2568 requires not to break.

The native `live_vm` job now stops at the preflight with the actionable diagnosis as its last
words, instead of reaching `systems.get`. Its failure moves earlier and its message improves; a
host that was going to fail still fails.

The hosted `live_vm_tcg` spine provisions too, and stays advisory. `live.yml:549` already has the
capture shape, so it would honour `required`, but `live.yml` is outside #2568's surface and its
guard at `:550-553` catches only a missing token — so a preflight `FAIL` that still permits the
mint keeps the #2568 shape on that tier until it declares `required`. That is a residual this
record leaves open, not one it closes.

The two severities are not a classification of `FAIL` entries, so a caller declaring `required`
gets **all nine** of `check-local-libvirt.sh`'s blocking checks as gates, including ones its next
step does not use. `KDIVE_PREFLIGHT_KDUMP=optional` downgrades exactly one of the nine, so it is
not the bound; the bound is that the only caller declaring `required` provisions a real domain
through the local-libvirt provider and needs every one of them. #2568's quoted native-runner
preflight output carries a single `FAIL`, which is evidence the other eight pass on that host
today.

A fifth caller added later inherits `advisory` silently. That is the same default this record
preserves, and it fails the way `onboard.sh` fails today rather than in a new way. In the other
direction, an operator who exports `ONBOARD_PREFLIGHT=required` in their shell has it inherited by
every `onboard.sh` a later script runs, including `demo-up.sh:66` — the cost ADR-0659's category
carries and `scripts/live-stack/README.md:54` already warns about for `LIBVIRT_OPTIONAL`.

## Considered & rejected

- **Hard-fail on any preflight `FAIL` (#2568 direction 1).** verified: `demo-up.sh:53-54` sets
  `KDIVE_PREFLIGHT_KDUMP` as a command-scoped assignment on the `check-local-libvirt.sh` call only,
  and invokes `onboard.sh` separately at `:66`; `bash -c 'FOO=bar true; echo "${FOO:-unset}"'`
  prints `unset`. So `onboard.sh`'s own preflight run reads the `required` default, and a
  first-run box that `demo-up.sh` deliberately passed would fail inside `onboard.sh`.
- **Teach `check-local-libvirt.sh` which `FAIL` entries block (#2568 direction 2).** verified:
  the split already exists — `note_fail` sets `fail=1` and the summary exits 1, `note_warn` does
  not — so the preflight's exit code *is* the blocking signal. It does not help, because the
  entries that block depend on what the caller does after `onboard.sh` returns, which the preflight
  cannot see. The proposed new surface would encode a caller's needs in a report-only script.
- **Default to `required`, let the two non-provisioning callers opt out.** judgment: it inverts a
  documented default and breaks `just onboard` on a provider-less box — the header's own
  "no schedulable resource" case — for everyone who never reads the knob.
- **Drop the knob; hard-gate the preflight in `mint-system.sh` itself.** judgment: it fixes the
  native job and leaves `onboard.sh` still discarding the diagnosis for every other caller, which
  is the defect #2568 names.
- **Declare `required` at `mint-system.sh:30` and leave its call shape alone.** verified: a stub
  harness carrying `mint-system.sh`'s exact `:9` (`set -euo pipefail`), `:30` and `:31` shapes,
  against an `onboard.sh` stub printing a `FAIL` line to stderr and exiting 1, reached the statement
  after the `eval` with rc 0 and terminated on `die "onboard.sh did not mint a token"` (GNU bash
  5.3.9, Fedora 44) — a command substitution in an argument list does not fire errexit. The
  repository documents the same trap 25 lines above its own call site, at `live.yml:751-752`.
- **Publish the declaration as a `KDIVE_`-prefixed operator knob.** verified:
  `scripts/guards/check_env_documented.py:35-36` scans `src tests scripts deploy` for
  `KDIVE_[A-Z0-9_]+`, so the prefix alone would pull `src/kdive/config/external_env.py` and the
  generated `docs/guide/reference/config.md` into the change. judgment: it buys generated
  documentation for a need nothing states — no completion criterion names an operator who wants to
  insist at `just onboard`, and an operator can set an unprefixed name just as easily.
- **Do nothing; let the reader correlate the two log entries.** judgment: that is the present
  behaviour, and #2568 is the report that it does not work.
