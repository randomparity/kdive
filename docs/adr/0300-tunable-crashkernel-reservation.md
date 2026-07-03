# ADR 0300 — Tune the kdump crashkernel reservation per install without a rebuild

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** kdive maintainers

## Context

The kdump crashkernel reservation is hard-coded. `system_required_cmdline`
(`services/runs/steps.py`) emits a constant `_KDUMP_CRASHKERNEL = "crashkernel=256M"` as the
trailing platform token whenever the resolved capture method is `KDUMP`. A KASAN kernel or a
large guest can need a bigger capture reservation than 256M for kdump to succeed, and there is no
way to ask for one through the tool surface.

The provisioning profile already carries a `crashkernel` field
(`LibvirtProfile.crashkernel` / `RemoteLibvirtProfile.crashkernel`, a `NonEmptyStr | None`). But
per #116 that field's **value is dead**: nothing reads it at install/boot. It is used only as a
boolean *signal* — `profile_policy.capture_method` returns `KDUMP` iff `crashkernel is not None` —
and the string actually placed on the cmdline is the hard-coded `256M`. So a profile that declares
`crashkernel: "512M"` still boots `crashkernel=256M`: a phantom the field's own docstring
("an optional opaque non-empty token — the booted kernel is the arbiter of its grammar") implies
is honored but is not.

`crashkernel=` is also a **platform-owned token** (`_PLATFORM_OWNED_CMDLINE_TOKENS`): the free-form
`runs.install` cmdline override (ADR-0299) rejects any value containing it. So an agent cannot tune
the reservation through that override either — by deliberate design, the platform owns the token.
The one sanctioned lever for the reservation size therefore has to be a **structured field**, not
free-form cmdline text.

See `docs/superpowers/specs/2026-07-02-tunable-crashkernel-989.md`.

## Decision

Add an optional, structured **`crashkernel` parameter to `runs.install`** that sets the size of the
platform-injected `crashkernel=` reservation for that install. It is the sibling of the ADR-0299
`cmdline` override — the same install-time, no-rebuild, ledger-recycling iteration path — for the
one boot token the free-form override may not touch.

- **`runs.install` gains an optional `crashkernel`.** When supplied, the platform emits
  `crashkernel=<value>` instead of the default `crashkernel=256M`. Omitting it uses the default
  256M. The value is the argument that follows `crashkernel=` (e.g. `512M`, or a full kernel range
  `1G-2G:128M,2G-:256M`) — the profile field's existing grammar. Per the issue's decision it is an
  **opaque token**: validated for non-emptiness and for injection-safety only (see below), not for
  a size range — the booted kernel remains the arbiter of the grammar.

- **The reservation is method-gated, and a mismatch is rejected synchronously.** The
  `crashkernel=` token is emitted only when the System's resolved capture method is `KDUMP`
  (unchanged: `console`/`gdbstub`/`host_dump` boots carry no crashkernel). A `runs.install` that
  supplies `crashkernel` for a System whose method is **not** `KDUMP` is rejected at the tool
  boundary — `CONFIGURATION_ERROR`, `data.reason = "crashkernel_requires_kdump"`,
  `data.method = "<resolved>"` — rather than silently dropped. Accepting the field while not
  emitting it would recreate exactly the phantom this ADR removes. The boundary resolves the method
  via `resolver.binding_for_system` + `install_method_for`, which is a cheap `(kind, name)` lookup
  plus in-process runtime construction (no libvirt round-trip), so the check is synchronous. This
  resolution runs **only when `crashkernel` is supplied**: the no-crashkernel install path fetches
  no System, makes no binding call, and gains no new failure surface; a resolution failure on the
  crashkernel path maps to `CONFIGURATION_ERROR`, not an unhandled 500.

- **Injection-safe validation, not range validation.** The value is stripped and must be non-empty;
  it must contain **no internal whitespace** (the token is space-joined into the cmdline that is
  rendered into the domain `<cmdline>`, so a space would inject an arbitrary extra kernel token —
  and legitimate crashkernel grammar, including ranges, contains no spaces); and it must not itself
  begin with `crashkernel=` (the agent passes the argument, not the whole token). These are safety
  guards, not the range enforcement the issue deliberately declined. Both the tool boundary and the
  `InstallPayload` validator enforce them, so a hand-crafted job payload cannot bypass the boundary.

- **A differing crashkernel recycles the `install`/`boot` ledger, exactly like a differing
  cmdline.** The ADR-0299 re-stage state machine is extended so the requested effective reservation
  participates in the equality check: `runs.install` re-stages when **either** the requested extra
  cmdline **or** the requested crashkernel differs from what the `install` step recorded. Requested
  crashkernel = `normalize(value)` when supplied, else the default (recorded as absent) — mirroring
  ADR-0299's "omit → build-baked": each install fully specifies its variant, so omitting reverts to
  the platform default 256M. `running` steps still reject `step_in_progress`; only settled rows are
  ever deleted. The install handler records the applied reservation on the `install` step result
  under `crashkernel` (`null` = default), alongside the ADR-0299 `cmdline`.

- **`InstallPayload` carries `crashkernel: str | None`** with the injection-safe validator, beside
  its `cmdline` field. `cmdline_for` and `system_required_cmdline` gain a `crashkernel` keyword that
  threads to the token: `crashkernel=<value or 256M>`. `runs.boot` keeps the bare `RunPayload`. The
  composite `runs.build_install_boot` path does not gain a `crashkernel` and always boots the default
  256M — documented, not silent: its tool doc points a KASAN/large-guest caller at the granular
  `runs.build → runs.install(crashkernel=…) → runs.boot` path. (Threading crashkernel through the
  composite would reopen ADR-0299's contract that the composite install phase reads no install-time
  override; the granular path fully satisfies the acceptance.) Remote-libvirt install rides along for
  free — the cmdline is composed upstream by `cmdline_for` and threaded via `InstallRequest.cmdline`.

- **`runs.get` surfaces the applied reservation** as `data.installed_crashkernel` (the value
  recorded on the `install` step; `null` when the default is in force), beside the ADR-0299
  `data.installed_cmdline`, so a sweep can confirm which reservation is live rather than being
  write-only.

## Consequences

- An agent tunes the capture reservation against an already-built kernel with
  `runs.install(crashkernel="512M") → runs.boot`, no rebuild, and confirms it via
  `runs.get data.installed_crashkernel` and the booted `<cmdline>` — the acceptance criterion.
- No schema change and no migration: the reservation rides the existing `InstallPayload` jsonb and
  the `install` step-result jsonb. ADR-0300 is a pure behavior/contract addition.
- The profile `crashkernel` field keeps its role as the **kdump signal** (its presence resolves the
  method); its *value* remains unread by the cmdline path. The per-Run parameter is the sanctioned
  reservation-size lever. The field's value staying a pure signal is a known limitation, not a
  second source of truth for the size (see Considered & rejected — "Honor the profile field
  value").
- `crashkernel` on a non-kdump System fails fast at the tool boundary rather than booting a
  surprising reservation or silently ignoring the request.
- Per-variant reservation history is out of scope, exactly as for the ADR-0299 cmdline: the ledger
  holds only the current install; `runs.get` shows the live value; each `runs.install` is audited
  with the reservation folded into its one-way `args_digest`.

## Considered & rejected

- **Honor the existing profile `crashkernel` field's value (per-System).** The field already
  exists and is documented as an opaque token, so wiring its value into the cmdline is the smallest
  change and removes the phantom directly. Rejected per the issue's decision: the reservation is
  wanted **per Run** (an agent iterating a KASAN kernel against a System provisioned once), and
  reusing the field's value as both the kdump signal and the size conflates two concerns on one
  field. The per-Run parameter keeps the signal (presence) and the size (the parameter) separate.
  The field's value remaining a pure signal is the accepted cost.
- **A new per-Run parameter on `runs.create` / the build profile.** The reservation is a boot-time
  property with no bearing on the built artifact; putting it on build would force a rebuild to
  change it — the opposite of the goal — and split a boot concern into the build plane. `runs.install`
  is the boot-parameter iteration surface (ADR-0299).
- **Fold the reservation into the free-form `runs.install` cmdline override.** `crashkernel=` is a
  platform-owned token the override rejects by design (ADR-0299); relaxing that would let an agent
  set `root=`/`console=` too. A dedicated structured field keeps the platform's ownership of the
  token intact while exposing only the size.
- **Validate a size range (e.g. 64M–2G, `<N>{M|G}`).** Rejected per the issue's decision to keep
  the token opaque: the kernel supports a richer grammar (multi-range reservations) than a single
  size, and the booted kernel is the real arbiter. Only injection-safety is enforced.
- **Gate the non-kdump rejection in the install handler (fail the job) instead of the boundary.**
  Avoids threading the resolver into `install_run`, but surfaces a plain input error as a *failed
  install job* the agent must poll to discover, rather than a synchronous tool rejection. The
  boundary check is cheap (no libvirt round-trip) and gives the cleaner agent contract; the
  `InstallPayload` validator still backstops a hand-crafted payload.
- **Silently ignore `crashkernel` on a non-kdump System (emit nothing).** Recreates the phantom
  this ADR removes — an accepted-but-unhonored field. Rejected in favor of a loud rejection.
