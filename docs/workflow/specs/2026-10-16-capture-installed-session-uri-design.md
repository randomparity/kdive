# Installed local capture session URIs (#3257)

## Problem

`LocalCaptureConfiguration` rejects both fixed Unix session endpoints published by the
live-worker installer, preventing supervised local traffic capture configuration staging.

## Scope

Extend only the existing exact allowlist in `src/kdive/providers/ports/traffic.py` with:

- `qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock`
- `qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock`

Keep `qemu:///system` and `qemu:///session`. Keep the existing validator and canonical
serializer as owners; no caller migration, new parser, configurable endpoint, dependency,
ADR, or schema change. Extend `tests/providers/test_capture_operation_ports.py` only.
Authority: issue #3257 and operator-approved bounded scope/design, frozen in
`WORK:SCOPE` token `q3257-3b160d82`. Expected implementation size: 40–60 changed lines.

### Failure model

- Actors and deployments: local capture supervisor and gated child on supported installed
  local-libvirt deployments; DTO parsing in worker assembly.
- Invariants and assets at stake: destination allowlist remains exact; canonical bytes
  preserve the chosen URI and Resource identity.
- Accepted failure classes: unavailable libvirt sockets still fail at existing provider
  connection boundaries; validating an allowed URI does not assert endpoint availability.
- Covered elsewhere: setgid and positively verified launch-abort repair remain #3225;
  generic after-launch finalization has no established owner and is excluded. No live
  execution is authorized by this design.

## Success

The four named URIs construct and round-trip canonically without normalization. Network
transports, arbitrary socket paths, altered socket spellings, extra query parameters,
and leading/trailing whitespace remain rejected at construction and deserialization.

## Validation

- focused-test: parameterize the existing local DTO test over the four accepted values;
  assert Resource identity, round-trip equality and canonical byte stability; expected red:
  both installed URI cases raise ValidationError before the validator fix.
- focused-test: parameterize rejected URI variants, checking both construction and JSON
  deserialization; run red before the source change, then green.
- focused-test: existing executor dispatch Resource binding test and capture provider-child
  tests exercise shared reconstruction. Green command for these entries:
  `just test-verbose tests/providers/test_capture_operation_ports.py` and
  `just test-verbose tests/jobs/capture_operations/test_provider_child.py`.
- task-test-not-applicable: no serializer algorithm or caller behavior changes; retain its
  existing implementation and use DTO plus existing reconstruction checks.
- Run `just lint`, whole-tree `just type`, hooks, then one managed pre-push `just ci`.
  Live-provider proof, if required, needs a separate exact candidate-bound operation grant.
