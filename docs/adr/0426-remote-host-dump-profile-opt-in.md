# ADR 0426 — remote-libvirt `host_dump` profile opt-in

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0094](0094-remote-host-dump-via-coredump-volume.md) (remote host_dump
  via a core-dump-to-volume + presigned-PUT stream download — the capture this opt-in gates),
  [ADR-0049](0049-crash-capture-tiers.md) (the crash-capture tiers and the per-System debug
  provisioning flags on local-libvirt, of which `preserve_on_crash` is the local host_dump
  opt-in this mirrors)
- **Issue:** #1425 (part of #1423)

## Context

The advertise-vs-provision split for crash capture is deliberate and shared across
providers. `ProviderSupport.capture_methods` answers "what mechanisms can this provider
ever perform"; `ProfilePolicy.host_dump_provisioned` answers "did *this profile* opt into a
host-side dump". Local-libvirt honours both levels: it advertises `HOST_DUMP` in its
support set and reads a per-profile opt-in flag,
`profile.provider.local_libvirt.debug.preserve_on_crash` (default `False`,
`profiles/provisioning.py`), from `LocalLibvirtProfilePolicy.host_dump_provisioned`.

Remote-libvirt implements the host_dump capture (ADR-0094: a `virsh dump` of guest memory
to an operator-pool coredump volume, streamed to the object store, with the reconciler's
orphaned-volume reaper already wired) and advertises `HOST_DUMP` in its support set
(`providers/remote_libvirt/composition.py`). But its profile section
(`RemoteLibvirtProfile`) carried only `base_image_volume`, `crashkernel`, and
`destructive_ops` — no host_dump opt-in — so `RemoteLibvirtProfilePolicy.host_dump_provisioned`
hardcoded `return False`.

The agent-visible consequence: `available_capture` / `inert_capture`
(`jobs/handlers/runs/boot_evidence.py`) append `host_dump` only when
`host_dump_provisioned(profile)` is true, so a halted remote System was never told its
follow-up capture options include `host_dump`, and an expected-crash's inert-capture set
under-reported. A remote operator therefore could not enable a capture the provider already
knows how to perform.

Unlike local's `preserve_on_crash`, the remote opt-in is not a pvpanic/`<on_crash>preserve</on_crash>`
provisioning knob: remote host_dump is taken on demand from a halted guest via `virsh dump`,
so the flag is a pure profile-level authorization of that retrieve path, not a domain-XML
change. It shares the *policy* semantics (deny-by-default per-profile opt-in) but not the
libvirt mechanism, so it does not belong in a copy of local's `LibvirtDebugOptions` block.

## Decision

**Add a flat `host_dump: bool = False` opt-in field to `RemoteLibvirtProfile` and read it
from `RemoteLibvirtProfilePolicy.host_dump_provisioned`.**

1. `RemoteLibvirtProfile` (`profiles/provisioning.py`) gains `host_dump: bool = False`,
   deny-by-default, matching local's `preserve_on_crash` default. The section docstring
   documents it as the host_dump opt-in and notes its mechanism differs from local
   (on-demand `virsh dump`, not a pvpanic provisioning device).
2. `RemoteLibvirtProfilePolicy.host_dump_provisioned` returns
   `profile.provider.remote_libvirt.host_dump` instead of the `False` constant. Every other
   remote policy predicate is unchanged.
3. With the flag set on a halted remote System, `available_capture` includes `host_dump` and
   `inert_capture` reports it, through the existing `host_dump_provisioned` call sites — no
   change to `boot_evidence.py`. With the flag unset, behaviour is identical to today.

The field is a flat boolean on the section, not a nested `debug` sub-block, because remote
carries exactly one such flag and its mechanism is unrelated to local's provisioning-time
debug devices — a `RemoteLibvirtDebugOptions` mirror would be premature abstraction over a
single field with different semantics.

No SQL migration: the profile is a pydantic/TOML document persisted as JSON on the System
row, not a DB column. The regenerated agent-facing profile-schema reference
(`docs/guide/reference/systems.md`, `docs/guide/reference/config.md`) picks up the new field
through the existing config/tool-reference generators.

## Consequences

- A remote operator can now opt a System into host_dump by setting
  `provider.remote-libvirt.host_dump = true`; a halted System's `available_capture` and an
  expected crash's `inert_capture` then advertise `host_dump`, matching what the provider can
  actually deliver.
- Default behaviour is unchanged (deny-by-default): an existing remote profile with no
  `host_dump` key still resolves `host_dump_provisioned() == False`.
- The two-level advertise-vs-provision model is preserved, not flattened: `HOST_DUMP` stays
  in the support set independent of the per-profile opt-in.
- `host_dump_provisioned` is now honest across both providers; the remote `return False`
  constant that masked the capability is removed.

## Alternatives considered

- **A nested `debug` block on remote mirroring `LibvirtDebugOptions`.** Rejected: remote has
  one opt-in and its mechanism (`virsh dump` on demand) is unrelated to local's
  provisioning-time pvpanic/gdbstub/fadump devices, so a shared shape would misleadingly imply
  parity and add a container for a single field (premature abstraction).
- **Reuse the name `preserve_on_crash` on remote.** Rejected: `preserve_on_crash` names local's
  pvpanic + `<on_crash>preserve</on_crash>` provisioning behaviour, which remote does not do;
  `host_dump` names the capture method the flag authorizes and matches `CaptureMethod.HOST_DUMP`.
- **Drive `host_dump_provisioned` off the support set instead of a profile flag.** Rejected: it
  would collapse the deliberate advertise-vs-provision split — every remote System would report
  `host_dump` provisioned whether or not the operator opted in, the exact conflation ADR-0094 and
  the local model keep separate.
