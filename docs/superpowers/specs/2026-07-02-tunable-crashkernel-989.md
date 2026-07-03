# Tunable kdump crashkernel reservation per install (#989)

- **Issue:** #989 (`OPUS_REVIEW.md` §5, item I-9, Tier 3)
- **ADR:** [ADR-0300](../../adr/0300-tunable-crashkernel-reservation.md)
- **Status:** Draft

## Problem

The kdump crashkernel reservation is hard-coded. `system_required_cmdline`
(`services/runs/steps.py:24,344`) emits the constant `_KDUMP_CRASHKERNEL = "crashkernel=256M"` as
the trailing platform token whenever the resolved capture method is `KDUMP`. KASAN kernels and
larger guests can need a bigger capture reservation, and there is no way to request one.

The provisioning profile already carries a `crashkernel` field, but per #116 its **value is dead** —
nothing reads it at install/boot. It is used only as a boolean signal
(`profile_policy.capture_method` returns `KDUMP` iff `crashkernel is not None`); the string on the
cmdline is always the hard-coded `256M`. A profile declaring `crashkernel: "512M"` still boots
`crashkernel=256M`.

`crashkernel=` is a **platform-owned token** (`_PLATFORM_OWNED_CMDLINE_TOKENS`): the free-form
`runs.install` cmdline override (ADR-0299) rejects any value containing it. So the reservation-size
lever must be a **structured field**, not free-form cmdline text.

## Goal / acceptance

Let an agent set the kdump crashkernel reservation for an already-built kernel through
`runs.install`, boot it, and confirm the applied value — without a rebuild.

Acceptance (from the issue): **a Run can request `crashkernel=512M` and the value reaches the
domain `<cmdline>`.** Concretely, on a kdump-provisioned local System:

```
runs.install(run, crashkernel="512M") ; runs.boot(run)
```

boots a domain whose `<cmdline>` contains `crashkernel=512M` (not `256M`), and
`runs.get(run).data.installed_crashkernel == "512M"`.

## Design decisions (from the issue owner)

Two forks were confirmed with the issue owner before design:

1. **Granularity: a new per-Run parameter** (not honoring the existing per-System profile field's
   value). The profile `crashkernel` keeps its role as the kdump *signal*; the new parameter is the
   *size*.
2. **Validation: opaque token**, not a size range. Non-empty and injection-safe only; the booted
   kernel is the arbiter of the grammar (ranges like `1G-2G:128M,2G-:256M` are admissible).

## Agent-facing contract

`runs.install(run_id, cmdline=None, crashkernel=None, idempotency_key=None)` — `crashkernel` is new.

- **Semantics.** When `crashkernel` is supplied, the platform emits `crashkernel=<value>` instead of
  the default `crashkernel=256M`. Omitting it uses 256M. The value is the argument after
  `crashkernel=` (e.g. `512M`) — the agent passes the size, never the whole token. This is
  orthogonal to `cmdline` (ADR-0299 extra args): both may be set in one install.
- **Method-gated.** The reservation is applied only when the System's resolved capture method is
  `KDUMP`. Supplying `crashkernel` for a non-kdump System is **rejected** (see failure contract) —
  never silently ignored.
- **The `Field` description** states: sets the kdump `crashkernel=` reservation size; default 256M;
  applies only to kdump-capture Systems; iterate without a rebuild. **Each `runs.install` fully
  specifies both `cmdline` and `crashkernel`**: omitting either reverts *that* parameter to its
  default anchor (`cmdline` → the build-baked extra; `crashkernel` → 256M), so on an
  already-installed Run, omitting a previously-applied value re-stages it back to the default rather
  than preserving it. When iterating one dimension, restate the other to keep it, and confirm both
  via `runs.get`.
- **Read-back.** `runs.get` surfaces `data.installed_crashkernel` — the reservation recorded on the
  last install (`null` when the default 256M is in force), beside `data.installed_cmdline`.
- **Sweep note:** to sweep reservations, omit `idempotency_key` or vary it per variant (standard
  replay-idempotency, mirroring ADR-0299).

## Validation (injection-safe, not range)

`crashkernel` is stripped and:

- must be **non-empty** after stripping (blank → rejected, a caller mistake);
- must contain **no internal whitespace** — the token is space-joined into the cmdline rendered
  into the domain `<cmdline>`, so a space would inject an arbitrary extra kernel token; legitimate
  crashkernel grammar (including multi-range) has no spaces;
- must **not begin with `crashkernel=`** (case-insensitive) — the agent passes the argument, not the
  whole token; a full token would double-prefix.

Enforced at **both** the tool boundary (synchronous `CONFIGURATION_ERROR`) and the `InstallPayload`
validator (so a hand-crafted job payload cannot bypass the boundary). These are safety guards, not
the range enforcement the issue declined.

## Method resolution & re-stage state machine

`runs.install(crashkernel=Y)` resolves the System's capture method at the boundary **only when `Y`
is supplied** — via `SYSTEMS.get` (the `install_run` boundary fetches only the Run today, so it
must also load the System) then `resolver.binding_for_system` + `install_method_for` (a cheap
`(kind, name)` DB lookup plus in-process runtime construction — no libvirt round-trip). If the
method is not `KDUMP`, reject before any ledger work. When `Y` is omitted the boundary does **no**
new System fetch, binding call, or method resolution, so the no-crashkernel install path is
byte-unchanged and gains no new failure surface. A boundary resolution failure (e.g. a missing
provider-kind row for the System) maps to `CONFIGURATION_ERROR`, not an unhandled 500. The handler
backstop (below) still catches an accept-then-reprovision skew, where the method changes between
the boundary check and job execution.

The requested **effective reservation** is `normalize(Y)` when supplied, else the default (recorded
as absent / `null`) — mirroring ADR-0299's "omit → build-baked": each install fully specifies its
variant, so omitting reverts to 256M. `normalize` is a single leading/trailing whitespace strip
(internal whitespace is already rejected).

The ADR-0299 re-stage decision is extended so **both** the requested extra cmdline and the requested
crashkernel participate in the equality check, under the per-Run advisory lock:

| Current `install` step | cmdline **or** crashkernel differs from recorded | Action |
|---|---|---|
| absent (`pending`) | — | first install: enqueue `INSTALL` carrying `cmdline` + `crashkernel` |
| `succeeded`, both recorded **==** requested | equal | idempotent no-op (replay existing envelope) |
| `succeeded`, either recorded **!=** requested | differ | **re-stage**: delete `install` + `boot` ledger rows, enqueue a fresh `INSTALL` carrying the new payload |
| `install` or `boot` step `running` | — | reject `CONFIGURATION_ERROR`, `data.reason = "step_in_progress"` |

Recording: the install handler records the applied reservation on the `install` step result under
`crashkernel` (the value, or `null` for default), beside the ADR-0299 `cmdline` and the existing
`system_id`. The recorded value is what re-stage compares against, and what `runs.get` surfaces.

## Plumbing

- **`InstallPayload(RunPayload)`** gains `crashkernel: str | None = None` with the injection-safe
  validator, beside its `cmdline` field. `runs.boot` keeps `RunPayload`.
- **`system_required_cmdline(method, root_cmdline, *, crashkernel=None)`** — for `KDUMP`, emits
  `f"crashkernel={crashkernel or _DEFAULT_CRASHKERNEL}"` where `_DEFAULT_CRASHKERNEL = "256M"`
  (renamed from `_KDUMP_CRASHKERNEL`, which was the full `crashkernel=256M` token). Non-kdump paths
  unchanged.
- **`cmdline_for(conn, run, method, *, root_cmdline, override=None, crashkernel=None)`** — threads
  `crashkernel` to `system_required_cmdline`. The `override` (ADR-0299) is unchanged and orthogonal.
- **Install handler** (`jobs/handlers/runs/install.py`) reads `install_payload.crashkernel`, passes
  it to `cmdline_for`, and records it (already-normalized) in the `install` step result under
  `crashkernel`. It resolves `method` as it does today; when `crashkernel` is set and `method` is
  not `KDUMP`, it raises `CONFIGURATION_ERROR` (`reason=crashkernel_requires_kdump`) as a backstop —
  the boundary already rejects this, so the handler path is defensive.
- **`install_run` / `_restage_and_enqueue_install`** (`mcp/tools/lifecycle/runs/steps.py`) accept
  `crashkernel`, resolve+gate the method (reject non-kdump), thread it into `InstallPayload`, extend
  the re-stage equality check, and fold it into the audit args (one-way `args_digest`). `install_run`
  gains a `resolver` parameter; `_register_runs_install` is threaded the resolver it does not take
  today.
- **`StepProgress`** gains `installed_crashkernel: str | None`; `step_progress` reads it from the
  `install` result.
- **`runs.get` view** surfaces `data.installed_crashkernel` from `StepProgress`. The
  `data.required_cmdline` advertisement (pre-install, System-level) keeps the default 256M.
- **Composite `runs.build_install_boot`** does not gain a `crashkernel` and always boots the
  default 256M (see the note below — its `cmdline` is baked at build, but `crashkernel` is a
  method-conditional install-time token with no build-baking, and threading it through the composite
  would reopen ADR-0299's "the install phase reads no override" contract). Its tool doc gains a
  discoverable pointer: the one-shot uses the default 256M reservation, and a larger reservation for
  a KASAN/large-guest kernel requires the granular `runs.build → runs.install(crashkernel=…) →
  runs.boot` path.
- **Remote-libvirt** rides along free (`InstallRequest.cmdline` already threaded).

## Failure contract

| Condition | Category | `data` |
|---|---|---|
| `crashkernel` on a non-kdump System | `CONFIGURATION_ERROR` | `reason=crashkernel_requires_kdump`, `method` |
| `crashkernel` blank/whitespace-only | `CONFIGURATION_ERROR` | `reason=crashkernel_blank` |
| `crashkernel` contains internal whitespace | `CONFIGURATION_ERROR` | `reason=crashkernel_malformed` |
| `crashkernel` begins with `crashkernel=` | `CONFIGURATION_ERROR` | `reason=crashkernel_malformed` |
| `install` or `boot` step `running` | `CONFIGURATION_ERROR` | `reason=step_in_progress` (existing) |
| Run not `SUCCEEDED` / unbound / unknown | (existing) | (existing) |

## Testing

- **Unit — `system_required_cmdline` / `cmdline_for`:** `crashkernel` kwarg emits
  `crashkernel=<value>` for `KDUMP`, default 256M when omitted, and nothing for non-kdump methods;
  ordering (console → root → crashkernel) preserved; orthogonal to the ADR-0299 `override`.
- **Unit — `InstallPayload` validator:** accepts a size and a range; rejects blank, internal
  whitespace, and a leading `crashkernel=`.
- **Unit — tool boundary (`tests/mcp/.../runs`):** `runs.install` accepts `crashkernel`; enqueues an
  `InstallPayload` carrying it; rejects a non-kdump System (`crashkernel_requires_kdump`) and each
  malformed/blank value with the right `reason`; rejects `step_in_progress`.
- **Unit — re-stage:** same effective reservation → no-op (ledger survives); differing crashkernel
  (cmdline unchanged) → both ledger rows deleted and a fresh install job enqueued carrying the new
  crashkernel; omit-after-512M → re-stage back to default.
- **Unit — install handler:** passes `payload.crashkernel` to `cmdline_for`; records it in the
  `install` result; raises `crashkernel_requires_kdump` on a non-kdump method (backstop).
- **Unit — `runs.get` read-back:** `data.installed_crashkernel` reflects the last install's applied
  reservation; `null` before the first install and when the default is in force.
- **Unit — XML / provider install:** the domain `<cmdline>` carries `crashkernel=512M` (not 256M)
  when requested — extends `tests/providers/.../install` / `test_provider_xml`.
- **Agent-doc/schema guards:** the `runs.install` `Field` text documents the parameter and the
  default; the regenerated tool reference includes it; existing agent-surface guards (no ADR leak,
  wrapper-docstring contract, completeness) stay green.
- **Live (`live_vm`, gated):** `runs.install(crashkernel="512M") → runs.boot` on a kdump local
  System asserts the booted `<cmdline>` carries `crashkernel=512M`. Runs only on the KVM host; not
  a PR gate.

## Out of scope

- Per-variant reservation history (the ledger holds the current install; `runs.get` shows the live
  value; audit folds the value into a one-way digest).
- Honoring the profile `crashkernel` field's value (the granularity decision keeps it a signal).
- A size range / bounds check (the validation decision keeps the token opaque).
- `crashkernel` on `runs.build_install_boot` (deferred, **not silently**: the one-shot's default-256M
  behavior is documented in its tool doc with a pointer to the granular path; threading it through
  the composite would reopen ADR-0299's install-phase-reads-no-override contract) or on `runs.boot`
  (iteration is an install-plane concern, consistent with ADR-0299).
