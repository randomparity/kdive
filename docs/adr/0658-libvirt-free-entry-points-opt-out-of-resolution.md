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
`:105-106`, inside `if [[ "$wipe" == "1" ]]`; plain teardown needs no libvirt whatever.

The abort also crosses a process boundary. `stack-down.sh:79` spawns `worker-lifecycle.sh stop`
before it stops anything, and that script has its own `set -euo pipefail` and sources both
`lib.sh:7` and `env.sh:9` — so the child aborts too, and teardown exits 1 having stopped nothing.

Issue #2504 names two directions and leaves the choice to this record: resolve lazily at first
libvirt use, or keep source-time resolution and let libvirt-free entry points opt out.

## Decision

**Fail-closed source-time resolution stays the default. An entry point that can do useful work
without libvirt declares itself by exporting `LIBVIRT_OPTIONAL=1` before sourcing `lib.sh`
or `env.sh`; on a broken contract that entry point continues with `KDIVE_LIBVIRT_URI` left
deliberately *unset*, and each operation that genuinely needs libvirt fails closed at the point of
use through `require_libvirt_uri`.**

The sentinel is an unset variable, not an empty string. `virsh -c ''` connects to libvirt's probed
default URI and exits 0, so an empty value would reintroduce the silent downgrade this decision
preserves. Left unset, every consumer that reads `$KDIVE_LIBVIRT_URI` without a `:-` default dies
with `unbound variable` under `set -u` — loud, and in the right direction.

The flag is **exported**, not a shell-local assignment, because a libvirt-free entry point drives
libvirt-free children: `stack-down.sh` and `stack-status.sh` both invoke `worker-lifecycle.sh`,
whose `stop`, `status` and `diagnostics` operations touch no libvirt. What makes exporting safe is
that the flag governs `resolve_libvirt_uri` alone. `worker-lifecycle.sh:188` reaches the endpoint
through `load_published_libvirt_uri` directly, on the `start` path only, and that function does
not consult the flag — so a libvirt-requiring operation still fails closed even when it inherits
the declaration.

`LIBVIRT_OPTIONAL` is a declaration an entry point makes about itself and its children, not an
operator knob, and it carries no `KDIVE_` prefix for the reason `LIBVIRT_ENV` does not
(`libvirt-uri.sh:9-11`): `scripts/guards/check_env_documented.py` sweeps `scripts/` for
`KDIVE_[A-Z0-9_]+` and requires every hit to be a registry setting or a catalogued entry in
`src/kdive/config/external_env.py`, which renders into the generated config reference. A prefixed
name would therefore have to be published there as an operator knob, which is what it is not. No
entry point that will touch libvirt sets it.

## Consequences

`stack-status.sh` reports on a broken host instead of aborting: it skips the endpoint banner and
probe at `:64-75` — `libvirt_ok` reads `$KDIVE_LIBVIRT_URI` unguarded at `lib.sh:380`, and `set -u`
is not suppressed inside an `if` condition — printing the endpoint as unresolved instead. The
`provision_prereqs_ok` report at `:76-80` reads no libvirt and keeps running, which is the part a
broken host still needs. `stack-down.sh` performs plain teardown, including its compose `down`.

`--wipe` still refuses, and now refuses **before** stopping anything. Refusing it wholesale rather
than dropping the volumes and skipping the reap is what `stack-down.sh:5-8` already prescribes:
the domains and overlays live outside compose, so "a DB wipe alone would orphan them". The two
halves are one operation, and half of it on a host that cannot reach libvirt is the orphaning the
flag's own documentation exists to avoid. The refusal bounds the unresolved-endpoint case only:
behind a *valid* contract a wrong-daemon or unreachable-daemon reap still orphans, which is #2515
and #2516, not this record.

The obligation this creates is transitive: an entry point that opts out owns every
`$KDIVE_LIBVIRT_URI` read it can reach, including reads inside `lib.sh` functions it calls. A
reachable read added later without `require_libvirt_uri` aborts that entry point under `set -u` —
a defect, but a visible one. A `:-` default does **not** discharge the obligation; it is the
silent-downgrade shape this record rejects, and `lib.sh:422` and
`scripts/operations/check-local-libvirt.sh:49` are existing instances, neither reachable from an
opted-out entry point today.

This record settles the question for the two entry points #2504's acceptance names.
`stack-services.sh --skip-libvirt`, `apply-migrations.sh` and `onboard.sh` are libvirt-free by the
same argument and are left unconverted, with no owner assigned; each needs only the declaration.

Because resolution stays at source time, issue #2509's guard — reporting a preset
`KDIVE_LIBVIRT_URI` that contradicts the published contract — belongs in `resolve_libvirt_uri`, in
the `[[ -n "${KDIVE_LIBVIRT_URI:-}" ]]` branch, the one point holding both values; lazy resolution
would have forced it to be replicated per consumer.

## Considered & rejected

- **Resolve lazily at first libvirt use (#2504 direction 1).** verified against the pre-change
  tree, whose line numbers this bullet alone keeps: `rg -n 'KDIVE_LIBVIRT_URI' scripts/
  examples/` returns 37 lines, of which eleven are top-level reads
  outside any function — `stack-services.sh:176,184,189,214,221`, `stack-status.sh:55`,
  `stack-down.sh:83,84`, `examples/local-libvirt/demo-up.sh:42,46,123` — each needing its own
  guard, and `lib.sh:300,304` forks the server and reconciler with the inherited environment, so
  the export must precede that fork rather than wait for a later first use. The blast radius is
  every consumer, not the resolver.
- **Move `resolve_libvirt_uri` out of `lib.sh:68` and `env.sh:15` into each entry point's first
  statement, with no flag at all.** verified: `worker-lifecycle.sh:188` needs the endpoint for
  `start`, so it would resolve in its own first statement and `stack-down.sh:79` would still spawn
  a child that aborts — the process boundary in *Context* survives this variant, which would then
  need per-subcommand laziness inside `worker-lifecycle.sh` as well.
- **Degrade to `KDIVE_LIBVIRT_URI=''` instead of leaving it unset.** verified: `virsh -c '' list`
  exits 0 against the probed default connection (virsh 12.0.0, Fedora 44 host) — the empty value
  reads as "no URI given" to libvirt, so a wrong-daemon query would succeed silently.
- **Degrade to `qemu:///system`.** verified: this is the exact fallback #2480 removed; the
  resolver's own comment at `libvirt-uri.sh:81-119` records that it puts the server on a daemon
  holding no kdive domains while the worker uses the published session URI.
- **Do nothing and let operators export `KDIVE_LIBVIRT_URI` by hand.** judgment: the override
  already exists and the abort message already names it, yet the recovery tools stay unusable
  until an operator knows which of two allowlisted values their host publishes — the fact the
  broken contract just made unreadable.
- **Invert the default: libvirt-free everywhere, opt *in* to resolution.** judgment: migration
  cost. Every entry point, plus `examples/local-libvirt/`, would need the declaration at once, to
  reach a default that is wrong for most of them.
