# ADR 0432 — Remote-libvirt TrafficCapturer with pcap fetch-back (the deferred ADR-0385 follow-up)

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers

## Context

ADR-0385 (#1258) added host-side network traffic capture as a provider-advertised capability and
wired it for local-libvirt only, recording the deferral verbatim and naming the three obstacles a
remote realization must solve: remote's data-plane traffic "rides a libvirt-managed NIC whose
netdev id libvirt auto-generates (not `kdivessh`, which carries only its SSH forward), needs runtime
netdev/tap discovery, and the pcap is written on the remote host and must be streamed back to the
worker." This ADR is that opt-in (#1434, part of the remote-libvirt parity epic #1423).

Several forces are already resolved by the ADR-0385 design and need no re-litigation:

- The capture surface keys on capability, not provider identity. `control.capture_traffic` gates on
  `ProviderSupport.supports_traffic_capture` (`mcp/tools/lifecycle/control/registrar.py`) and
  returns `capability_unsupported` on a miss; the handler carries a defence-in-depth
  `runtime.traffic_capturer is None` backstop (ADR-0427 removed the old provider-identity gate). So
  wiring the port and setting the flag makes the tool reachable with no change outside the provider
  package and the handler.
- The worker handler owns the bounded size-poll and the cooperative cancel-check, off the
  synchronous libvirt thread. That division survives unchanged; only the file-side primitives the
  loop drives become provider-dispatched.
- BPF-filter validation, the packet count, the zero-packet-is-success rule, the artifact store path,
  and the audit transition are all provider-agnostic and stay in the handler.

What does **not** survive is the assumption that the pcap `dest_path` is worker-readable. The local
handler prepared a local directory, stat-polled a local file, read the local file whole, and
reclaimed a local file. A remote capture writes on the remote host. ADR-0385 kept the netdev a
provider-internal detail already, so netdev discovery belongs inside the provider; the path
assumption is what this change generalizes.

## Decision

We will add `RemoteLibvirtTrafficCapture`, a `TrafficCapturer` realized against the remote host over
the existing `qemu+tls://` transport, advertise `supports_traffic_capture=True` together with the
wired port in `remote_libvirt/composition.py`, and generalize the `TrafficCapturer` port and the
worker handler so the file-side of the capture (prepare, size, fetch, reclaim) is provider-dispatched
rather than assumed worker-local.

### Capture mechanism: QEMU `filter-dump` over the qemu+tls monitor

We keep local's mechanism — a `filter-dump` netfilter object attached via the libvirt QMP
passthrough (`qemuMonitorCommand`) — rather than ADR-0385's alternative of `tcpdump -i vnetN` on the
remote tap. The remote provider has no host-side shell transport (it connects purely over
`qemu+tls://`; the guest agent runs commands *inside* the guest, not on the host), so `tcpdump`
would require a new SSH channel. `filter-dump` reuses the monitor channel `RemoteLibvirtTransportResetter`
already uses, and writes a libpcap file on the remote host with no new transport.

### Netdev discovery at runtime

The netdev id is discovered from the running domain's XML (`domain.XMLDesc`): the first
`<interface>` with an assigned `<alias name='netN'/>` yields the QEMU netdev id `hostnetN` (libvirt's
`qemuAliasHostnetFromDevice` prepends `host` to the device alias). A domain with no aliased
interface is a `CONFIGURATION_ERROR` (nothing to capture). Capturing the first interface is a
documented limitation: a multi-NIC guest captures only its first data-plane interface.

### pcap fetch-back over the storage-volume download stream

The `filter-dump` `file=` writes into the operator's `storage_pool` directory (already
QEMU-writable and libvirt-managed — it holds the domain's disk images), under the deterministic name
`kdive-pcap-<system_id>-<job_id>.pcap`. After the capture window the file is fetched back exactly as
remote host_dump fetches a core: `pool.refresh` discovers it as a volume, then
`volume.download` + `stream.recvAll` streams it to worker memory, bounded by `max_bytes` (a
mid-stream overrun aborts and raises, as host_dump's spooler does). No SSH, no shared code with
local — the same `remote_connection` materialize→connect→cleanup lifecycle every remote port uses.

### Reclaim guarantee

The remote-side pcap volume is reclaimed on every handler exit path — cancel, failure, and success —
in the handler's `finally` via `reclaim`, mirroring local's unlink. Two additional guarantees cover
a non-graceful exit: `attach`'s `object-del`-first is idempotent (a leaked filter-dump re-attaches
cleanly), and `prepare` pre-deletes **this job's own** deterministic `kdive-pcap-<system_id>-<job_id>.pcap`
volume before a new capture, so an at-least-once retry of a job that died mid-capture starts from a
clean volume. The pre-delete is deliberately keyed on the job's own volume, not a whole-System
sweep: a sweep would delete a *concurrent* capture on the same System (each job writes its own
volume, so per-job pre-delete is both sufficient for retry and concurrency-safe). A pcap orphaned by
a job that exhausts its retries leaks until reclaimed; extending the reconciler's `DumpVolumeReaper`
sweep to pcap volumes is a noted follow-up, not part of this change.

### Port and handler generalization

`TrafficCapturer` gains four thin primitives beside `attach`/`detach`:

- `prepare(system_id, job_id) -> str` — prepare the destination and return the provider dest token
  (local: prepare the pcap dir, return the local path; remote: sweep stale volumes, return the
  remote pool path). The token is passed to `attach`/`detach` as `dest_path` and to the three below.
- `captured_size(dest_path) -> int` — current bytes (local: `stat`; remote: pool refresh + volume
  info), for the handler's poll loop.
- `fetch(dest_path, *, max_bytes) -> bytes` — read the pcap to worker memory (local:
  `read_pcap_bytes`, preserving the ADR-0223 root-readback wall; remote: bounded download stream). An
  absent capture is empty `bytes`, so the handler's existing "< 24-byte header ⇒ hypervisor could
  not write it" check fires identically for both providers.
- `reclaim(dest_path)` — delete the pcap (local: unlink; remote: delete the volume).

The port also exposes `write_remediation`, the provider-appropriate operator guidance the handler
attaches to a `pcap_not_written` config error (local: the qemu:///system pcap-dir remedy; remote:
the storage-pool remedy). The handler no longer touches the filesystem directly; local behavior is
byte-for-byte unchanged because `LocalLibvirtTrafficCapture` implements the four primitives over the
same `runtime_paths` helpers the handler used before.

No migration: the port, the flag, and the storage layout are provider-composition state, not schema.
No new host tool: `filter-dump` is a QEMU built-in over the monitor, and the BPF trim/validate run
`tcpdump` on the *worker* (already declared), not the remote host — no provisioning-parity change.

## Consequences

- A remote System gains `control.capture_traffic`: `systems.get.data.supports_traffic_capture`
  reports `true`, and the tool returns a non-empty pcap artifact fetched back through the existing
  artifact path — no agent-surface change (per ADR-0385).
- `#1428` (the capability-parity guard) will enforce that `supports_traffic_capture` and the
  `traffic_capturer` port agree; this change sets both, satisfying that pairing.
- The remote capture reconnects `qemu+tls://` per poll of the file size. The window is bounded and
  the op is a diagnostic, so the reconnect cost is accepted rather than holding one connection across
  the handler's threaded poll loop (which would cross the libvirt connection over worker threads).
- Live proof is gated on the remote `live_vm` tier (#1424/ADR-0425); the provider mechanics and the
  handler generalization are unit-tested with fakes here, and the live end-to-end proof is deferred
  to that tier, exactly as ADR-0385 proved local against real KVM.
