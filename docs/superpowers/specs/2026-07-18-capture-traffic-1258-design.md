# Spec: Host-side network traffic capture (#1258)

- Issue: #1258 "Add Network Traffic Capture Tool"
- ADR: [ADR-0384](../../adr/0384-host-side-traffic-capture.md)
- Status: Design accepted

## Problem

For network-stack kernel bugs, in-guest `tcpdump` perturbs the stack under test and dies with
the guest on a panic — the packets around the failure are the ones lost. Capturing on the host
side of the guest's virtual NIC observes without touching guest state and survives the panic.
The issue asks for a `control.capture_traffic` tool that produces a host-side pcap and slots it
into the existing artifact model.

The default provider (local-libvirt) has **no host tap device**: its only guest NIC is a QEMU
SLIRP user-mode netdev (`-netdev user,id=kdivessh,...`), so `tcpdump -i <iface>` has nothing to
attach to. QEMU's built-in `filter-dump` netfilter object dumps a netdev's packets to a libpcap
file regardless of netdev type and is host-side, so it captures the SLIRP traffic no host tap
exposes.

## Requirement (restated)

Add a single fixed-duration MCP tool that captures host-side packets from a running
local-libvirt guest into a pcap, stored and egressed through the existing artifact model,
without perturbing the guest and surviving a guest panic. The agent controls the capture window,
a size cap, the per-packet snaplen (default 128 bytes), and an optional BPF capture filter.

## Tool surface

One contributor-gated MCP tool, Run-addressed (mirroring `vmcore.fetch`, ADR-0244):

`control.capture_traffic(run_id, duration_s=30, max_bytes=67108864, snaplen=128,
capture_filter=None, idempotency_key=None)`

| param | meaning | bound | default |
|-------|---------|-------|---------|
| `run_id` | investigation Run the pcap is evidence for | — | required |
| `duration_s` | capture window (seconds) | 1–300 | 30 |
| `max_bytes` | file-size cap that stops the capture early | 1 MiB – 512 MiB | 64 MiB |
| `snaplen` | per-packet bytes captured (`filter-dump maxlen`) | 1–262144 | 128 |
| `capture_filter` | optional pcap-filter(7)/tcpdump BPF expression | ≤ 1024 chars, printable | none (capture all) |
| `idempotency_key` | shared `keyed_mutation` idempotency | — | none |

All numeric bounds in `Field`/docstring text are f-string-interpolated from the enforcing
constants (the `test_tool_docs` numeric-bounds guard). The wrapper docstring and `Field`
descriptions carry no `ADR-NNNN` references (the `test_no_adr_leak` guard).

Returns the standard job envelope `{object_id: run_id, status: running, refs:{}}` with
`suggested_next_actions` steering to `jobs.wait`; on completion `refs.result` is the pcap
artifact id and the steer points at `artifacts.fetch_raw`.

## Behavior contract

- **Preconditions.** The Run exists and is in the caller's projects; caller has `contributor`
  on the Run's project; the Run is bound to a System; the System is `READY` and local-libvirt;
  the bound provider advertises `supports_traffic_capture`. Failing each precondition returns a
  typed envelope and creates no job:
  - unknown/foreign Run → `config_error` / `not_found`.
  - unbound Run → `config_error` `{reason: run_unbound}`.
  - System not `READY` → `config_error` `{current_status: <state>}`.
  - non-local provider → `capability_unsupported` (`capability="traffic_capture"`).
  - malformed `run_id` → `invalid_uuid`.
  - `capture_filter` failing the admission hygiene check (too long / non-printable) →
    `config_error` `{reason: invalid_filter}`.
- **Admission.** Enqueues `JobKind.CAPTURE_TRAFFIC` with `CaptureTrafficPayload(run_id,
  duration_s, max_bytes, snaplen, capture_filter)` under `keyed_mutation`; contributor-cancelable
  (in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`). Admission does **not** run a subprocess — the server
  stays non-blocking; authoritative filter validation happens in the worker.
- **Worker capture.** Under a per-System advisory lock, re-verify `READY`+local and resolve the
  `TrafficCapturer` port. Lock-free: `object-add` a `filter-dump` on the `kdivessh` netdev
  writing to `/var/lib/kdive/pcap/<system_id>/<job_id>.pcap` with `maxlen=snaplen`; poll the
  file size every `POLL_INTERVAL` and stop at `duration_s` or when the file reaches `max_bytes`;
  `object-del` the filter (best-effort on the teardown path so a filter is never left attached).
- **Worker filter + store.** Read the raw pcap off host disk (a `PermissionError` under
  qemu:///system → `CONFIGURATION_ERROR` with `WORKER_READABILITY_REMEDIATION`, ADR-0223). If
  `capture_filter` is set: validate with `tcpdump -d <expr>` (compile-only; failure →
  `CONFIGURATION_ERROR` `{reason: invalid_filter}` carrying tcpdump's stderr), then
  `tcpdump -r <raw> -w <out> <expr>` (single argv, no shell). Stream the resulting pcap to the
  object store via `put_stream` as `SENSITIVE`, `retention_class="pcap"`, `owner_kind="runs"`,
  `owner_id=run_id`. Insert the artifact row insert-if-absent on the object key (at-least-once
  safe), audit (`tool="control.capture_traffic"`, `transition="capture_traffic"`), delete the
  host files, return the artifact id.
- **Egress.** The agent fetches the binary via `artifacts.fetch_raw(run_id, asset="pcap")` — a
  presigned URL gated on `contributor` over the Run's project. `artifacts.get`/`find` return
  `not_found` for the `SENSITIVE` pcap (unchanged `REDACTED`-only gate).
- **Cancellation / early exit.** A canceled job stops the poll, `object-del`s the filter, and
  deletes any partial host file; no artifact row is written.

## Provider seam

- New port `TrafficCapturer` (`providers/ports/`): `capture(domain_name, *, duration_s,
  max_bytes, snaplen, dest_path) -> TrafficCaptureResult` (result carries `bytes_captured` and a
  `truncated` flag for the `max_bytes` early-stop). Keyed on the provider domain name, DB-free —
  the handler drives the state machine, exactly like `Controller`.
- `ProviderRuntime.traffic_capturer: TrafficCapturer | None = None` and a static
  `ProviderSupport.supports_traffic_capture: bool = False` (ADR-0378 `supports_snapshots`
  pattern). Local-libvirt sets both; remote-libvirt leaves them fail-closed.
- Local impl `LocalLibvirtTrafficCapture` (`providers/local_libvirt/lifecycle/`): narrow
  `_LibvirtConn`/`_LibvirtDomain` Protocols, `qemuMonitorCommand` object-add/del of `filter-dump`,
  bounded size-poll. Unit-tested with a fake connection; the real `libvirt_qemu` adapter is
  `live_vm`-only.

## Cross-cutting integration

- `JobKind.CAPTURE_TRAFFIC` appended (`domain/operations/jobs.py`; Postgres enum member — the
  `jobs_kind_check` constraint widened by a migration) and added to
  `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.
- `CaptureTrafficPayload(SystemPayload-shaped over run_id)` in `jobs/payloads.py`, registered in
  `_ACTIVE_PAYLOAD_MODELS`.
- Handler `capture_traffic_handler` + `register_handlers` in `jobs/handlers/control/`, wired via
  a `_capture_traffic_handler_registrar` appended to `jobs/assembly.py`
  `build_handler_registrars`.
- Tool wrapper + admission handler added inside the existing `control.register`
  (`mcp/tools/lifecycle/control/registrar.py`) — no new tool-registration tuple entry.
- `mcp/exposure.py` `_TOOL_SCOPES`: `"control.capture_traffic": _CONTRIBUTOR`.
- `RawAsset.PCAP` + `_resolve_key` branch (`mcp/tools/catalog/artifacts/raw_fetch.py`) and
  `raw_pcap_key(conn, run_id)` (`artifacts/read_model.py`, mirroring `raw_vmcore_key`).
- `_BEHAVIOR_TESTS_BY_TOOL["control.capture_traffic"]` → the new behavior test file
  (`tests/mcp/core/test_tool_docs.py`).
- Migration: one numbered migration widening `jobs_kind_check` for `capture_traffic` (no new
  table — the pcap is a plain `artifacts` row).
- Regenerate: `just rbac-matrix`, `just docs`, `just resources-docs` (and their `-check`
  variants gate CI).

## Test strategy

- **Provider unit** — fake libvirt connection asserts the exact `object-add`/`object-del` QMP
  JSON (qom-type `filter-dump`, `netdev=kdivessh`, `maxlen=snaplen`, `file=dest_path`), the
  size-poll early-stop at `max_bytes`, and `object-del` on both the success and error paths.
- **Capture-loop unit** — a pure poll function (injected size-reader/sleeper, no libvirt),
  covering: stop-at-duration, stop-at-max_bytes (`truncated=True`), and cancellation.
- **Filter validation unit** — `tcpdump -d` accept/reject and argv-not-shell construction; a
  filter containing shell metacharacters is passed literally and rejected by `tcpdump -d` (never
  interpreted).
- **Worker handler** — READY+local snapshot; store-as-SENSITIVE `owner_kind='runs'`
  `retention_class='pcap'`; insert-if-absent idempotency on retry; `PermissionError` →
  `CONFIGURATION_ERROR` with the readability remediation; host-file cleanup on success, filter
  failure, and cancellation.
- **Admission (behavior test, `test_control_tools`-adjacent)** — each precondition rejection
  creates no job; happy path enqueues `CAPTURE_TRAFFIC` and returns the running envelope;
  `capture_filter` hygiene rejection.
- **Egress** — `fetch_raw(run_id, asset="pcap")` presigns for a `contributor` and is
  `not_found` via `artifacts.get`.
- **Registry guards** — flat-top-level-params, description/maturity, no-ADR-leak,
  destructive-set (tool is **not** destructive), rbac-matrix drift.
- **Live proof (`live_vm`)** — on a READY local-libvirt guest, capture ~5s of loopback ping
  traffic, fetch the pcap, and assert it is a valid libpcap file with packets. Validates the
  SELinux/label + qemu:///system readback path end to end.

## Non-goals (this change)

- Remote-libvirt implementation (fail-closed `capability_unsupported`; documented follow-up:
  auto-generated netdev id discovery, remote→worker pcap transport, optional real
  `tcpdump -i vnetN` on the `vnet` tap).
- A `REDACTED` text/flow-digest sibling (pcap is fetched whole as a binary, like `vmlinux`).
- A TTL GC sweep for `retention_class="pcap"` (matches `vmcore`; broader retention concern).
- A free-form tcpdump command line (the agent controls `snaplen` + BPF `capture_filter` only).
- On-the-wire filtering (impossible with `filter-dump`; the BPF filter is a post-capture trim).
- A start/stop capture pair (the operator selected the fixed-duration job).

## Risks

- **SELinux** — QEMU (`svirt_t`) writing the pcap under `/var/lib/kdive/pcap/` may need a label,
  the same class of host-config issue as the staged-rootfs path and console log. Surfaced by the
  live proof; a denial is an operator-remediation `CONFIGURATION_ERROR`, not a silent failure.
- **qemu:///system cross-uid readback** — the raw pcap is QEMU-owned; a non-root worker cannot
  read it. Reuses the ADR-0223 `WORKER_READABILITY_REMEDIATION` contract unchanged.
- **`qemuMonitorCommand` is libvirt "unsupported"** — a QEMU/libvirt version whose `filter-dump`
  QOM schema changes breaks object-add; the op fails `CONTROL_FAILURE`, visible in per-kind job
  telemetry. Precedent in-tree: `remote_libvirt/connection/transport_reset.py`.
