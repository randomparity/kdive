# ADR-0385: Host-side network traffic capture on local-libvirt (#1258)

- Status: Accepted
- Date: 2026-07-18

## Context

Network-stack kernel bugs are hard to observe from inside the guest. In-guest `tcpdump`
perturbs the very stack under test and dies with the guest on a panic, so the packets around
the failure are exactly the ones lost. Capturing on the host side of the guest's virtual NIC
observes without touching guest state and survives the panic. Issue #1258 asks for a
`control.capture_traffic` tool that produces such a host-side pcap and slots it into the
existing artifact model.

The literal request — "host-side pcap on the tap device" — assumes a host tap interface. The
default provider has none: local-libvirt's only guest NIC is a QEMU **SLIRP user-mode**
netdev (`-netdev user,id=kdivessh,...`, `providers/local_libvirt/lifecycle/xml.py`), an
in-process userspace NAT with no `vnet`/tap device on the host to run `tcpdump -i` against.
Remote-libvirt does have a real tap (a libvirt-managed `<interface type="network">`), but it
is an operator opt-in provider and needs runtime netdev/tap discovery and a remote→worker
file transport that do not exist yet.

QEMU's built-in `filter-dump` netfilter object dumps a netdev's packets to a libpcap file
regardless of netdev type, so it captures SLIRP traffic that no host tap exposes. It is
host-side (the QEMU process, not the guest), survives a guest panic, and its `maxlen` option
is a per-packet snaplen. It has no BPF filter and no other tcpdump flags — it captures every
packet on the netdev. It is added and removed at runtime through libvirt's QMP passthrough
(`libvirt_qemu.qemuMonitorCommand`, already used in-tree by
`providers/remote_libvirt/connection/transport_reset.py` and the remote guest agent).

The existing artifact model already handles a large, unredactable binary captured from a
guest: the raw `vmcore` (ADR-0031/0244). It is written `SENSITIVE`, is never inline-served,
is owned by the crashing Run (`owner_kind='runs'`), and is egressed only through
`artifacts.fetch_raw` — a presigned-URL tool gated on `contributor` over the Run's project,
whose `RawAsset` enum is a closed egress allow-list (`vmcore`, `vmlinux`). A pcap is the same
shape: a raw binary whose packet payloads no regex redactor can safely scrub.

## Decision

Add a single fixed-duration, contributor-gated MCP tool, backed by a new provider capability
port, that captures host-side traffic on a running local-libvirt guest into a Run-owned pcap
egressed through the existing raw-fetch path.

**Tool — `control.capture_traffic` (Run-addressed, contributor).** Mirrors `vmcore.fetch`
(ADR-0244): resolves the Run, derives its bound System, and requires the System `READY` and
local-libvirt (a running guest). Parameters:

- `run_id` — the investigation Run the capture is evidence for.
- `duration_s` — capture window (default 30, bounded 1–300).
- `max_bytes` — hard file-size cap that stops the capture early (default 64 MiB, bounded
  1 MiB–512 MiB).
- `snaplen` — per-packet bytes captured (default 128 — header-focused, small files, less
  payload exposure; bounded 1–262144).
- `capture_filter` — optional pcap-filter(7)/tcpdump BPF expression (e.g.
  `tcp port 80 and host 10.0.0.5`), applied after capture; omitted means capture all.
- `idempotency_key` — optional, via the shared `keyed_mutation`.

The tool enqueues a durable `CAPTURE_TRAFFIC` job (`JobKind`), returns
`{job_id, status: running}`, and is contributor-cancelable like `watch_for_crash`. No agent
inline packet bytes are ever returned.

**Provider port — `TrafficCapturer` (fail-closed).** A new typed port on `ProviderRuntime`
(`traffic_capturer: TrafficCapturer | None = None`) plus a static
`ProviderSupport.supports_traffic_capture` flag, surfaced on `systems.get` for discoverability
exactly like ADR-0378's `supports_snapshots`. A provider that does not implement it yields
`capability_unsupported`. The port is deliberately thin — `attach(domain_name, *, qom_id,
dest_path, snaplen)` and `detach(domain_name, *, qom_id)` primitives — so the
**handler owns the poll loop** (consistent with "the handler drives the state machine, exactly
like `Controller`"). The handler's own poll loop avoids a sync-callback-across-thread-boundary
problem: the size read is `os.stat` (visible cross-uid even where the ADR-0223 content-read wall
blocks) and the cancel check is a direct async read of the job row on the autocommit dispatch
connection (no probe callback threaded into a `to_thread` capture loop). Local-libvirt
implements the primitives via `filter-dump`; remote-libvirt leaves the port `None` (see
follow-up below).

**Local-libvirt implementation.** `attach` runs `qemuMonitorCommand` on the running domain:
`object-del` any stale filter for this job's deterministic QOM id first (idempotent re-attach),
then `object-add` a `filter-dump` on the `kdivessh` netdev (a shared `SYSTEM_SSH_NETDEV_ID`
constant, not a re-hardcoded literal) with QOM id `kdive-dump-<job_id>`, `file=<host path>`, and
`maxlen=snaplen`. The leading `object-del` **tolerates not-found** — the first-ever capture has
no stale filter, so a `DeviceNotFound`/"object not found" QMP error is swallowed as success
(matched on the QMP error class/message text, since `qemuMonitorCommand` surfaces QMP failures
as a generic `libvirtError` string with no distinct `VIR_ERR_*` code, unlike the typed
`_idempotent`/`_delete_if_exists` swallows); any other monitor failure is `CONTROL_FAILURE`. The handler then polls `os.stat(dest_path).st_size` on a bounded interval,
stopping when the window (`duration_s`) elapses, the file reaches `max_bytes` (`truncated=True`),
or a direct async read of the owning job row returns `CANCELED` (a per-interval cooperative
check — a mechanism `watch_for_crash` does not have, added because a stray `filter-dump` fills
host disk); `detach` (`object-del`) runs on every exit path. The result carries `bytes_captured`
and a `packets` count from a small pure-Python pcap record walk (which reads the 4-byte magic to
pick byte order and the µs-vs-ns record format, so it counts correctly regardless of host
endianness); these feed the worker's per-kind job telemetry. A header-only pcap (zero packets —
the common case on the default `restrict=on` NIC, which sees only the SSH forward) is a
distinguishable success: the agent reads it from `artifacts.fetch_raw`'s `data.size_bytes`
(== 24, the bare libpcap header), since a completed job envelope carries only `refs.result`. The
pcap is written under `/var/lib/kdive/pcap/<system_id>/` (the worker creates the per-System directory
mirroring the console-log/host-dump path handling) — the same host-writable location class as
the console log and host-dump core.

**Worker capture, filter, store.** The handler mirrors `diagnostic_sysrq`: a per-System-locked
snapshot verifies `READY`+local and resolves the port; the capture runs lock-free; a second
locked transaction stores and audits. The worker reads the raw pcap off host disk (the
qemu:///system cross-uid readability wall, ADR-0223, applies unchanged — a `PermissionError`
becomes a `CONFIGURATION_ERROR` carrying `WORKER_READABILITY_REMEDIATION`). If `capture_filter`
is set, the worker trims the raw pcap with `tcpdump -r <raw> -w <out> <expr>`; the expression
is validated with `tcpdump -d <expr>` (compile-only, no capture) and passed as a single argv
element (never a shell string). The stored object is named
`pcap-<job_id>` (job-unique so distinct captures never collide, retry-stable so an
at-least-once redelivery dedups), written **`SENSITIVE`**, `retention_class="pcap"`,
`owner_kind="runs"`. Because the pcap is bounded by `max_bytes`, the worker already reads the
whole file for the readback-wall check and the packet count, so it is stored with `put_artifact`
(in-memory) rather than a disk-backed stream. The handler inserts the artifact row insert-if-absent
on the object key (at-least-once
safe), audits the capture, deletes the host files, and returns the artifact id as the job
`result_ref`. A capture whose cancel is observed during the poll stores nothing (it `detach`es
and deletes the partial file), and the final store transaction re-checks `CANCELED` under the
lock and skips the store; the job ends `CANCELED` with no `result_ref`. A cancel that commits
in the narrow window after the store transaction commits but before `queue.complete` runs still
ends the job `CANCELED` with no `result_ref`, but the stored pcap exists and is reachable as the
Run's newest pcap — consistent with the "stored pcaps persist" non-goal.

**Egress.** `RawAsset` gains a `PCAP` member and `artifacts.fetch_raw` gains an optional
`artifact_id` parameter (used only for `asset="pcap"`; ignored for the singleton `vmcore`/
`vmlinux`). Because a Run has **many** pcaps (one per capture), egress is capture-addressable:
the agent passes the pcap id from the completed job's `refs.result` —
`artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<id>)` — and the `_resolve_key` PCAP
branch resolves that exact `artifacts` row, requiring `owner_kind='runs'`, `owner_id=run_id`
(so a cross-Run id is `not_found`), and `retention_class='pcap'`. With `artifact_id` omitted it
resolves the newest pcap for the Run (`ORDER BY created_at DESC, id DESC LIMIT 1`); earlier
captures remain reachable by id, discoverable through the `jobs.list`/`jobs.get` trail. Gated on
`contributor` over the Run's project. `artifacts.get`/`find` continue to serve only `REDACTED`
rows, so the pcap is never inline-served.

## Consequences

- A new contributor-gated tool and a new `JobKind`, `CaptureTrafficPayload`, provider port,
  local implementation, and one `RawAsset` egress member. The agent surface grows by one tool
  and one `fetch_raw` asset value.
- The capability port keeps every non-local provider fail-closed; remote-libvirt drops in
  later with no change to the agent-facing contract.
- The `capture_filter` is applied **after** capture, not on the wire, because `filter-dump`
  cannot filter. The filter therefore shrinks what is stored/returned, not what QEMU pulls off
  the netdev; the raw intermediate is snaplen- and `max_bytes`-bounded and deleted immediately.
- A supplied `capture_filter` requires `tcpdump` on the worker host; the snaplen-only path does
  not. A malformed filter fails the job as `CONFIGURATION_ERROR` (from `tcpdump -d`), not at
  admission — the server stays non-blocking (admission does a length/printable-character
  hygiene check only, no subprocess).
- The pcap is `SENSITIVE`. Like a `vmcore`, it is reclaimed by **no** teardown or sweep today
  (System teardown reclaims only `owner_kind='systems'` artifacts; the GC sweeps reclaim only
  run-owned `build`/`kernel-build`) — a stored pcap persists until an object-store lifecycle
  policy or manual cleanup removes it, and unlike a `vmcore` a Run accumulates one pcap **per
  capture**. A busy link at a raised snaplen can produce a large object bounded only by
  `max_bytes` per capture. Wiring pcap into the existing closed-investigation reclaim
  (`gc_investigation_artifacts`) is a named follow-up, not done here (kept consistent with the
  unreclaimed `vmcore`).
- The `filter-dump` stays attached for the whole capture window, so the fixed-duration job
  **does** hold live host state. This is contained without a new reconciler port: (1) the
  handler makes re-attach idempotent (`object-del`-before-`object-add` on the deterministic
  `kdive-dump-<job_id>` QOM id), so a worker crash followed by the at-least-once **retry** —
  the normal recovery — cleans the stranded filter and never double-attaches; (2) System
  teardown removes the per-System `/var/lib/kdive/pcap/<system_id>/` directory (the bespoke
  per-family reclaim pattern already used for console/sysrq artifacts), sweeping any orphaned
  host pcap files; (3) the filter itself dies when the domain stops (teardown, power-off,
  crash). The one residual — a `SIGKILL`/host crash on the *final* attempt with no retry — is a
  documented bounded risk: the filter captures only low-volume SSH-forward traffic on the
  default `restrict=on` NIC and is freed at the next domain stop. A dedicated reconciler reaper
  (a new `qemuMonitorCommand` provider port + loop wiring, modeled on the dump-volume reaper) is
  a named follow-up, not warranted for this residual at priority:low.
- `qemuMonitorCommand` is libvirt's "unsupported" QMP passthrough; a QEMU/libvirt version
  whose `filter-dump` QOM schema changes could break the object-add. The op fails as
  `CONTROL_FAILURE`, observable via per-kind job telemetry.

## Considered & rejected

- **Real `tcpdump -i <tap>` on local-libvirt** — rejected: SLIRP user-mode networking exposes
  no host tap/`vnet` interface to capture on. Physically impossible without reconfiguring the
  netdev.
- **Capturing the loopback `hostfwd` port** — rejected: only sees the SSH forward, not the
  guest's network stack under test.
- **System-owned pcap addressed by `system_id`** (mirroring `diagnostic_sysrq`) — rejected:
  the raw-binary egress path (`artifacts.fetch_raw`) is cleanly Run-keyed, so System ownership
  would require grafting a parallel System-keyed egress onto it or adding a second egress tool.
  Run ownership reuses `fetch_raw` wholesale and matches the `vmcore`/`vmlinux` precedent for a
  large `SENSITIVE` guest binary; a traffic capture is evidence for a specific investigation
  Run.
- **A free-form tcpdump command-line string** — rejected: `filter-dump` is not tcpdump and most
  flags are meaningless here; a raw command string is a command-injection surface with no
  upside. The agent controls the two parts that map onto the mechanism — `snaplen` and the BPF
  `capture_filter` — as typed parameters.
- **A `REDACTED` text sibling** (a scrubbed flow digest, as `vmcore` emits a scrubbed dmesg) —
  rejected for now: a pcap is a binary-analysis artifact fetched whole, like `vmlinux` (which
  has no redacted sibling). No `tshark`/summariser dependency is justified at priority:low.
- **A TTL GC sweep for `retention_class="pcap"`** — rejected: `vmcore` (the same large
  `SENSITIVE` Run-owned binary) has none; adding one only for pcap is inconsistent and
  speculative. Unbounded artifact accumulation is a broader retention concern for `vmcore` and
  pcap together, out of scope here.
- **A start/stop capture pair** — rejected: the fixed-duration job is one tool with bounded
  resource use; the operator selected it. Its live filter is contained by idempotent re-attach +
  teardown cleanup (see Consequences); a start/stop pair would hold the filter open indefinitely,
  turning the bounded residual into an unbounded one and forcing the dedicated reaper this design
  avoids.
- **Remote-libvirt in this change** — rejected for now (documented follow-up): remote's
  data-plane traffic rides a libvirt-managed NIC whose netdev id libvirt auto-generates (not
  `kdivessh`, which carries only its SSH forward), needs runtime netdev/tap discovery, and the
  pcap is written on the remote host and must be streamed back to the worker. Remote could also
  use real `tcpdump -i vnetN` on its `vnet` tap. It drops into the `TrafficCapturer` port later
  with no agent-surface change.
