# 0398 — complete_build nudges effective_config upload when it is absent

Status: Accepted

## Context

The `missing_boot_config` advisory (ADR-0318, ADR-0330) warns on `runs.complete_build`
when a Run's uploaded `effective_config` provably lacks the boot-critical `EXT4_FS` /
`VIRTIO_BLK` symbols — a "you built a kernel that cannot mount its rootfs" guardrail. But
`effective_config` is an optional artifact, and `rootfs_mount_warning` fails open (returns
`None`, complete-as-today) whenever no config was uploaded. Nothing in the workflow prompts
the upload, so the default path — skip the optional artifact — silently yields neither the
advisory nor any signal that the check was skipped. A black-box agent skipped
`effective_config`, got a bare `succeeded`, and discovered the non-booting kernel only at
boot (#1342).

The advisory is therefore dead input in the common case: it can only fire on the minority of
Runs that already opted into the upload.

## Decision

**Add a distinct, non-blocking nudge to the `complete_build` success envelope when
`effective_config` is genuinely absent.** A new `missing_effective_config_nudge` gate keys on
artifact *presence* (`effective_config_key is None`), not readability, and returns
`{reason: "no_effective_config_uploaded", remediation}`. The handler spreads it into
`data.missing_effective_config` and sets `suggested_next_actions =
[artifacts.create_run_upload, runs.get]`.

**The nudge and the `missing_boot_config` warning are mutually exclusive.** The warning keys
on a *present* config missing symbols; the nudge on a config *absent* entirely. The handler
computes the nudge only when the warning is silent, so a single completion never carries both
and the absent case costs no second config read beyond the presence lookup.

**Presence, not readability, gates the nudge.** A present-but-unreadable or degenerate config
is treated as *provided* (nudge stays silent) — the agent already made the upload choice; the
signal is only for the agent that skipped it. This matches the "genuinely absent" scope and
avoids nagging on a config the agent did supply.

**Advisory only.** The completion still succeeds; this adds a `data` field and reorders
`suggested_next_actions`, never a refusal. The `missing_boot_config` warning behavior is
unchanged.

## Consequences

- An agent that skips `effective_config` now gets an explicit, actionable signal on every
  successful build completion instead of a bare `succeeded`, closing the chicken-and-egg gap
  where the boot-config advisory could never fire.
- The `complete_build` success envelope gains a conditional `data.missing_effective_config`
  field and a conditional `create_run_upload` entry in `suggested_next_actions`; clients that
  ignore unknown `data` keys are unaffected.
- No migration and no schema change: the nudge reads the existing `artifacts` row via
  `effective_config_key`.
- Scope is the tool-response contract only. The build-doc cross-reference (#1341) and the
  `external-build-upload.md` advisory wording are handled separately.

## Considered & rejected

- **Extend `rootfs_mount_warning` to also cover the absent case.** Muddies a helper whose
  documented fail-open contract is "no config ⇒ `None`, complete as today"; a distinct gate
  keeps each advisory single-purpose and independently testable.
- **Refuse or block on absent `effective_config`.** The upload is optional by design and the
  config is never validated (ADR-0318); a hard gate would break the advisory-only contract
  and reject legitimate builds.
- **Nudge whenever the config is unreadable/degenerate too.** Would nag an agent that did
  upload a config, conflating "you skipped the check" with "your config failed to parse" —
  two different problems with different fixes.
