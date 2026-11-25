# Remote transfer acquisition and rollback

## Problem and authority

Three transfer owners acquire a libvirt stream before their try block. Upload additionally
omits local source I/O from rollback. The frozen eight-field authority is
`.desloppify/task-89-charter.md`; the original four-case reproduction and two orphan observations
are retained without changing production source. The current queue names these exact boundaries.

## Scope and success

Extend `_download` in remote lifecycle traffic_capture, `_download_to_file` in retrieve
host_dump_capture and `upload_qcow2_volume` in lifecycle/rootfs/volume_upload. Stream acquisition
belongs to each existing transfer try; initialize to None and guard aborts. Preserve download
ceiling/configuration errors, infrastructure messages and volume/System details and causes.
Upload's createXML handler stays separate. After creation, catch libvirtError/OSError from
setup, upload, reopen/read/send/finish. Abort acquired streams best-effort for libvirtError or
OSError, then attempt deletion even when acquisition or expected abort fails. On native libvirt
deletion failure warn with pool/volume, and chain the original transfer error into existing
`_infra("streaming the qcow2",...)`. Do not swallow unexpected programming errors.

No owner moves or compatibility path is required. Pre-create stat/magic validation, existing
volume/require_new decisions, XML generation, TLS, credentials, permissions, size bounds and
public signatures remain. Existing upload fakes gain accurate created/deleted pool state solely
for the same-name retry contract. One coupled Cast unit closes the single queued transfer issue;
source commits remain local and conventional.

## Failure model

- Actors/deployments: configured remote-libvirt workers downloading pcaps/host dumps and local
  operator staging a qcow2 over an existing connection; tests inject the external API/filesystem.
- Invariants/assets: primary exception provenance, private spool ceiling/mode, no partial target
  mistaken for a successful upload, retry after completed rollback, no abort of absent stream.
- Accepted classes: native delete failure may leave a volume but warns and preserves primary;
  unexpected programming/process-control errors retain current propagation, without a universal
  cleanup promise. No remote live/realPOWER proof is inferred from injected-boundary tests.
- Covered elsewhere: TLS/PKI lifetime by remote_connection; pool/own-job capture deletion by
  ADR0847; pre-create validation/attestation by ADR0440 and existing upload owner; final gates by
  enclosing quality campaign.

## Threat model

- Boundaries: existing remote native stream operations enter worker errors; local qcow2 source
  I/O feeds upload; host bytes enter bounded pcap/private spool sinks. None is added or widened.
- Actors: configured host can fail stream operations; source file may become unreadable after
  validation. Tenant authentication/authorization and provider host trust are unchanged.
- Controls: retain defusedxml/ElementTree, TLS and secret lifecycle, pre-create magic validation,
  existing bounded sinks and private spool permissions. Rollback touches only the returned new
  volume. Warn with existing pool/name context, without logging source bytes or cleanup secrets.
- Out of scope: malicious host/admin control and persistent process death; existing guards and
  operators retain those controls. No new automatic deletion of pre-existing volumes.

## Validation

The full plan inventories download acquisition and upload rollback as focused-test contracts.
Run the new failures before code, existing three-module and caller regressions after, controlled
faults/restoration and the original unchanged root. Verify protected AST and198migration hashes,
lint/whole-tree type/docs/records/hooks and actual extracted-wheel proof. One bounded design
review and scope audit precede implementation; fresh code/security reviews precede closure.

## Global Constraints

Python3.14 managed with uv; use just recipes and existing libvirt/defusedxml dependencies.
Host Linuxx86_64; declared x86_64 and ppc64le targets unchanged. Preserve public contracts and
applied migrations; local conventional commits only, no external writes.

## Decision

[ADR0848](../../adr/0848-cover-remote-stream-acquisition-and-rollback.md) retains the existing
provider owners and makes setup part of their existing transfer failure boundaries.
