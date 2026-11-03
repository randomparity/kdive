# 0762 Reuse worker installation for remote TLS refs

## Status

Accepted (2026-10-08)

## Context

Issue #3088 requests provisioning for the layout already established by
[ADR-0574](0574-systemd-supervises-host-worker-incarnations.md): fixed secrets root,
remote-libvirt child root/provider-group 0750, and TLS files 0440.
Both runner and standalone paths already reuse worker installation directory tasks.

## Decision

Keep TLS delivery in `local_worker_host`, imported by the existing shared installation
entrypoint. Three optional controller source inputs select installation; partial
input fails. Use the existing authority TLS source validation/no-log pattern and the
fixed remote filenames. The standalone TLS task entrypoint creates the secrets
parent for installer-only hosts after their provider group exists.
No runtime inventory, secret resolver, lifecycle setting or permission policy changes.

## Consequences

Local-only defaults remain empty and do not remove existing credentials. An operator
with remote instances explicitly supplies the sources. Copies are individually atomic,
not a three-file transaction; retry a failed installation with the validated input set.
The existing shared client identity remains shared by operator and fixed worker peers.

## Considered & rejected

- Keep manual installation. verified: #3088 and the remote live-stack runbook at
  `2e46da4b55ac6959ca391adabf00c632231b9144` leave the documented layout unprovisioned.
- Add another installer or duplicate runner tasks. judgment: the existing shared
  entrypoint already serves both callers and can serve installer-only hosts directly.
- Parse runtime inventory to infer controller certificate paths. judgment: inventory
  contains refs, not controller sources; this would add an unrelated discovery interface.
