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
preflight. `KDIVE_ONBOARD_PREFLIGHT` takes `advisory` (the default, today's `WARN`) or `required`,
which stops `onboard.sh` before `migrate` with the preflight's own `FAIL` text already on stderr as
the stated reason. `scripts/live-vm/mint-system.sh` declares `required`.**

The default is unchanged behaviour because the header's advisory intent is correct for the callers
it was written for. `required` is set by the one caller whose next step provisions, which is the
path the observed failure came from.

The knob is `KDIVE_`-prefixed and therefore published in `src/kdive/config/external_env.py` and the
generated reference: unlike ADR-0659's `LIBVIRT_OPTIONAL`, this **is** an operator knob — an
operator running `just onboard` on a host they intend to provision from has the same reason to
insist as CI does. `scripts/guards/check_env_documented.py` enforces the publication.

## Consequences

`just onboard` and `demo-up.sh` are unchanged. That matters for `demo-up.sh`: it already hard-gates
the same preflight at `:53-54`, but with `KDIVE_PREFLIGHT_KDUMP` defaulted to `optional`, and that
assignment is command-scoped, so it does not reach the `onboard.sh` invocation at `:66`. A blanket
hard-fail would therefore have failed a first-run workstation on the guestfs/drgn probe the demo
had just deliberately downgraded — the case #2568 requires not to break.

The native `live_vm` job now stops at the preflight with the actionable diagnosis as its last
words, instead of reaching `systems.get`. Its failure moves earlier and its message improves; a
host that was going to fail still fails.

The two severities are not a classification of `FAIL` entries, so a caller declaring `required`
gets **every** required check as a gate, including ones its next step does not use. That is the
cost of not teaching the preflight about its callers, and it is bounded: `check-local-libvirt.sh`
already splits blocking from advisory itself (`note_fail` sets `fail=1`; `note_warn` does not), and
`KDIVE_PREFLIGHT_KDUMP=optional` already downgrades the one check with a documented soft case.

A fifth caller added later inherits `advisory` silently. That is the same default this record
preserves, and it fails the way `onboard.sh` fails today rather than in a new way.

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
- **Do nothing; let the reader correlate the two log entries.** judgment: that is the present
  behaviour, and #2568 is the report that it does not work.
