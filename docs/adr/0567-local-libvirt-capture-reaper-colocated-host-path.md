# 0567 — Local-libvirt capture reaper reclaims over the colocated host path

## Status

Accepted (2026-08-20)

## Context

ADR-0556 built the provider-agnostic orphaned-capture sweep and left each provider kind
disabled until its concrete reaper landed. Remote-libvirt's landed first (#1947, ADR-0556's
volume path). Local-libvirt's destination is different: not a libvirt storage-pool volume but
a worker-local filesystem path — `/var/lib/kdive/pcap/<system_id>/<job_id>.pcap`
(`providers/shared/runtime_paths.py`), created by the root worker, owned to the QEMU runtime
user, and SELinux-labelled `svirt_image_t` so the confined `qemu:///system` hypervisor can
write it (ADR-0385). ADR-0556 explicitly delegated one question to #1948: can the process
performing reconciliation reach that worker-owned path, or must the port contract execute
worker-side?

The deployment facts answer it. Local-libvirt's defining property is that the provider host
*is* the kdive host: the pcap path is a fixed absolute path on that host's filesystem, and the
reconciler for this kind already drives local-host state over the same `qemu:///system`
connection the worker uses — the `InfraReaper` (ADR-0111) destroys local domains and the
dump-volume reaper reclaims local files from the reconciler process today. The reconciler and
worker are colocated host processes by that same standing assumption. The QEMU-user ownership
and `svirt_image_t` label confine the *hypervisor's* write, not a root host process's unlink:
the worker's in-job `reclaim` already unlinks these exact files as a plain host process.

## Decision

Local-libvirt's `CaptureReaper` is a reconciler-side reaper, not a worker-side sweep. Over one
`KDIVE_LIBVIRT_URI` connection (default `qemu:///system`) it detaches
`capture_qom_id(job_id)` from the captured domain by QMP `object-del`, tolerating an
already-missing domain or filter; it then unlinks `pcap_path(system_id, job_id)` directly on
the shared filesystem, tolerating an already-missing file. It does not use the
`remote_libvirt_reaper_connections` reachability seam — that seam bounds a remote TCP connect,
and the local URI has no dial to bound.

Local also closes its prepare-time pre-delete gap. Remote's `prepare` pre-deletes the job's own
stale volume before capture (#1947); local's only prepared the directory. Local `prepare` now
unlinks this job's own `pcap_path` (job-keyed, never a whole-System sweep) before returning it,
suppressing every `OSError` — absence and permission failure alike. Best-effort is sound here
(unlike the reaper's unlink) because the job path has convergent backstops: a file that could
not be removed is truncated by `filter-dump` on attach, and a job that dies anyway leaves the
file to the sweep.

The reaper's unlink is *not* best-effort: an `OSError` other than absence raises
`INFRASTRUCTURE_FAILURE` so the sweep defers the row and retries, instead of marking a row
complete while the file remains.
The sweep's existing per-row contract makes every such failure observable: ADR-0556 logs one
row's failure with ``(system_id, job_id)`` without stopping the pass, and a raise (unlike a
``False`` decline) logs its traceback, so a deferred row's infrastructure cause is readable
from reconciler logs.

## Consequences

- Both capture-capable provider kinds are now dispatchable, so the ADR-0556 sweep covers all
  three leak classes end to end; `NullCaptureReaper` remains only the default for deployments
  that wire nothing.
- A stale pcap is destroyed at retry start even when the retry never reaches `attach`; a
  filter-dump that does attach would truncate the file anyway, but the design no longer
  depends on that QEMU open-mode behavior.
- A non-root reconciler cannot unlink inside a QEMU-owned `0770` directory. This is not a new
  requirement: the same deployment already requires a root reconciler for the ADR-0111 domain
  reaper and the dump-volume reaper to work. The failure mode is a deferred, retried row with
  a logged `INFRASTRUCTURE_FAILURE`, not silent divergence.
- Detach-before-unlink ordering is preserved under the shared port contract: a genuine filter
  error aborts before the unlink, so QEMU never keeps writing an unlinked inode.

## Considered & rejected

- **Worker-side sweep** (worker startup recovery executing the port contract). Splits
  convergence across two processes, duplicates the ownership fence and reap-once marker
  machinery the reconciler sweep already owns, and never converges state on a worker host that
  dies and does not rejoin. ADR-0556 names the reconciler the durable owner of recovery.
- **Record the stale-pcap gap as harmless** (filter-dump truncates on attach). Relies on an
  undocumented QEMU file-open mode and leaves the stale file in place whenever a retry dies
  before attach — exactly the orphan the sweep then has to reap. Closing the gap is one
  best-effort call and matches remote's #1947 parity.
- **Suppress `OSError` in the reaper's unlink**, mirroring the in-job `reclaim`'s
  best-effort suppression. The in-job path suppresses so cleanup never masks the job's real
  result; the reaper has no other result to protect, and a swallowed failure would mark the
  row complete with the file still on disk — false convergence against ADR-0556's reap-once
  marker.
- **Do nothing and rely on worker-side reclaim only.** The in-job `reclaim` already unlinks
  these exact worker-local files, and the pcap is not a shared storage pool. But every leak
  class begins where the owning job never reached that reclaim — a worker killed mid-capture
  orphans both the filter and the file — and a terminal job does not retry, so the existing
  path converges none of the three leak classes. ADR-0556's rejection of "do nothing" for the
  sweep applies per kind; declining local's entry would leave local-libvirt rows permanently
  ineligible while remote rows converge.
- **Reuse `LocalLibvirtTrafficCapture.detach`** instead of a standalone QMP detach. The
  capturer's `_lookup` raises on a missing domain, while the reaper contract requires
  tolerating one; sharing would mean weakening the live-capture path's failure contract to
  suit the reaper. The remote reaper is likewise standalone.
