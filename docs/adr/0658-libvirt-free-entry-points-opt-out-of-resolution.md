# 0658 — Libvirt-free entry points opt out of source-time resolution

## Status

Accepted (2026-09-15)

## Context

`resolve_libvirt_uri` (`scripts/live-stack/libvirt-uri.sh`) is fail-closed: anything occupying
`/etc/kdive/live-worker-libvirt.env` but failing validation returns non-zero rather than falling
back to `qemu:///system`. That is deliberate (#2480) — a silent downgrade puts the server on one
daemon and the lifecycle worker on another, which is the split the file exists to prevent.

`lib.sh:66-68` and `env.sh:13-15` both source the resolver and call it **unconditionally at source
time**, and every live-stack entry point sources one of them before doing any of its own work.
Under the callers' `set -euo pipefail` a broken contract therefore aborts the whole invocation —
including entry points that touch no libvirt at all (`stack-services.sh --skip-libvirt`,
`apply-migrations.sh`, `onboard.sh`) and the two an operator reaches for *because* the host is
broken: `stack-down.sh` and `stack-status.sh`. `stack-down.sh` reads `$KDIVE_LIBVIRT_URI` only at
`:83-84`, inside `if [[ "$wipe" == "1" ]]`; plain teardown needs no libvirt whatever.

Issue #2504 names two directions and leaves the choice to this record: resolve lazily at first
libvirt use, or keep source-time resolution and let libvirt-free entry points opt out.

## Decision

**Fail-closed source-time resolution stays the default. An entry point that can do useful work
without libvirt declares itself by setting `KDIVE_LIBVIRT_OPTIONAL=1` before sourcing `lib.sh` or
`env.sh`; on a broken contract that entry point continues with `KDIVE_LIBVIRT_URI` left
deliberately *unset*, and each operation inside it that genuinely needs libvirt fails closed at
the point of use through `require_libvirt_uri`.**

The sentinel is an unset variable, not an empty string. `virsh -c ''` connects to libvirt's
probed default URI and exits 0, so an empty value would reintroduce the silent downgrade this
decision preserves. Left unset, every consumer that reads `$KDIVE_LIBVIRT_URI` without a `:-`
default dies with `unbound variable` under `set -u` — loud, and in the right direction.

`KDIVE_LIBVIRT_OPTIONAL` is a declaration an entry-point script makes about itself, not an
operator knob: it gets no row in the generated config reference, and no entry point that will
touch libvirt sets it.

## Consequences

`stack-status.sh` reports on a broken host instead of aborting, and says the endpoint is
unresolved and why rather than printing a probe against a wrong daemon. `stack-down.sh` performs
plain teardown on a broken host; `--wipe` still refuses, and now refuses **before** stopping
anything, because the libvirt reap it cannot perform is the reason an operator asked for `--wipe`.

The obligation this creates: an entry point that opts out owns every `$KDIVE_LIBVIRT_URI` read
inside it. A read added later without `require_libvirt_uri` or a `:-` default aborts that entry
point under `set -u` — a defect, but a visible one.

This decision keeps resolution at source time, so issue #2509's guard — reporting a preset
`KDIVE_LIBVIRT_URI` that contradicts the published contract — belongs in `resolve_libvirt_uri`,
in the `[[ -n "${KDIVE_LIBVIRT_URI:-}" ]]` branch, where both values are in hand at one point.
Had resolution gone lazy there would have been no such point, and the guard would have had to be
replicated per consumer.

Two `stack-down.sh` defects are untouched and stay owned elsewhere: a URI naming a daemon holding
no kdive domains reaps nothing (#2515), and `sudo virsh` against a per-user session socket fails
silently behind `|| true` (#2516).

## Considered & rejected

- **Resolve lazily at first libvirt use (#2504 direction 1).** verified: `rg -n
  'KDIVE_LIBVIRT_URI' scripts/ examples/` returns eight top-level reads outside any function —
  `stack-services.sh:176,184,189,214,221`, `stack-status.sh:55`, `examples/local-libvirt/
  demo-up.sh:42,46,123` — each of which would need its own guard, and `lib.sh:300,304`
  forks the server and reconciler with the inherited environment, so the export has to have
  happened before that fork rather than at a later first use. The blast radius is every consumer,
  not the resolver.
- **Degrade to `KDIVE_LIBVIRT_URI=''` instead of leaving it unset.** verified: `virsh -c '' list`
  exits 0 against the probed default connection (virsh 12.0.0, Fedora 44 host) — the empty value
  reads as "no URI given" to libvirt, so a wrong-daemon query would succeed silently.
- **Degrade to `qemu:///system`.** verified: this is the exact fallback #2480 removed; the
  resolver's own comment records that it puts the server on a daemon holding no kdive domains
  while the worker uses the published session URI.
- **Do nothing and let operators export `KDIVE_LIBVIRT_URI` by hand.** judgment: the override
  already exists and the abort message already names it, yet the recovery tools remain unusable
  until an operator knows which of two allowlisted values their host publishes — which is the
  fact the broken contract just made unreadable.
- **Make every entry point libvirt-free by default and opt *in* to resolution.** judgment: it
  inverts a fail-closed default that is correct for the majority of entry points, so the cost of
  a forgotten declaration lands on the side that matters.
