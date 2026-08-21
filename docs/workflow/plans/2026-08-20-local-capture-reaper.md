# Plan: local-libvirt orphaned-capture reaper (#1948)

**Goal**: register a concrete `CaptureReaper` for the `local-libvirt` kind so the ADR-0556
sweep reclaims orphaned local captures, and close local's prepare-time stale-pcap gap.

**Architecture**: `LocalLibvirtCaptureReaper` joins `LibvirtInfraReaper` in
`src/kdive/providers/local_libvirt/reaping.py`, mirroring the remote reaper's structure
(`RemoteLibvirtCaptureReaper`, `src/kdive/providers/remote_libvirt/reaping/capture.py`):
narrow protocols, a blocking core offloaded with `asyncio.to_thread`, only-`True` returns.
`LocalLibvirtTrafficCapture.prepare` gains the job-keyed pre-delete. Composition flips
`build_capture_reaper` from `NullCaptureReaper` to the concrete class.

**Tech stack**: Python 3.14 (uv), pydantic-free plain classes, libvirt Python binding,
pytest. Spec: `docs/workflow/specs/2026-08-20-local-capture-reaper-design.md`. Decision:
`docs/adr/0567-local-libvirt-capture-reaper-colocated-host-path.md`.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict, whole tree. Run `just lint`
  and `just type` after every task; `just ci` at the end.
- Detach **before** unlink, always. A genuine (non-absence) filter error must abort before
  the unlink.
- Tolerate: `VIR_ERR_NO_DOMAIN` on lookup (typed code), QMP message text containing
  `"not found"` or `"devicenotfound"` (lowercased) on `object-del`, missing pcap file
  (`missing_ok=True`). Everything else raises `CategorizedError`:
  `CONTROL_FAILURE` for connect/lookup/monitor, `INFRASTRUCTURE_FAILURE` for unlink OSError.
- `reclaim_capture` returns `True` on every path that reaches the unlink step; it never
  returns `False`.
- The QOM id is `capture_qom_id(job_id)` (`providers/ports/traffic.py`); the pcap path is
  `pcap_path(system_id, job_id)` (`providers/shared/runtime_paths.py`). Never re-derive
  either.
- Doc-style guard: plain factual prose; no "critical/robust/comprehensive/elegant"; no
  "Sprint".
- No schema/migration, no MCP surface change, no remote-libvirt behavior change.
- libvirt binding method names are camelCase — mark each with
  `# noqa: N802 - libvirt binding name` where ruff's N rules see it (existing files already
  carry these).

## Task 1 — `LocalLibvirtCaptureReaper` and its unit tests

**Files**: modify `src/kdive/providers/local_libvirt/reaping.py`;
modify `tests/providers/local_libvirt/test_reaping.py`.

**Interfaces**: consumes `OrphanedCapture` and `CaptureReaper` from
`kdive.providers.infra.reaping`; `capture_qom_id` from `kdive.providers.ports.traffic`;
`pcap_path` from `kdive.providers.shared.runtime_paths`; `LIBVIRT_URI` from
`kdive.providers.local_libvirt.settings`; `config.require` from `kdive.config`.
Task 3's composition consumes `LocalLibvirtCaptureReaper.from_env()`.

**Where it fits**: the port implementation; nothing else in this plan works without it.

**Steps**:

1. Write the failing tests first — append to `tests/providers/local_libvirt/test_reaping.py`
   (the file currently tests only `LibvirtInfraReaper`; keep those tests untouched). New
   imports at the top of the file:

   ```python
   import libvirt
   import pytest

   from kdive.domain.errors import CategorizedError, ErrorCategory
   from kdive.providers.infra.reaping import CaptureReaper, OrphanedCapture
   from kdive.providers.local_libvirt.reaping import (
       LibvirtInfraReaper,
       LocalLibvirtCaptureReaper,
   )
   from kdive.providers.ports.traffic import capture_qom_id
   from kdive.providers.shared.runtime_paths import pcap_path
   ```

   Test doubles and helpers (place above the new tests):

   ```python
   _SID = UUID("00000000-0000-0000-0000-0000000000cc")
   _JID = UUID("00000000-0000-0000-0000-0000000000dd")
   _DOMAIN = "kdive-x"


   def _capture() -> OrphanedCapture:
       return OrphanedCapture(
           provider_kind="local-libvirt",
           resource_id=_SID,
           resource_name="local",
           system_id=_SID,
           domain_name=_DOMAIN,
           job_id=_JID,
       )


   class _ReaperConn:
       """Records lookup calls; raises the configured lookup error, if any."""

       def __init__(
           self,
           *,
           domain: object | None = object(),
           lookup_error: libvirt.libvirtError | None = None,
       ) -> None:
           self.lookups: list[str] = []
           self.closed = False
           self._domain = domain
           self._lookup_error = lookup_error

       def lookupByName(self, name: str) -> object:  # noqa: N802 - libvirt binding name
           self.lookups.append(name)
           if self._lookup_error is not None:
               raise self._lookup_error
           return self._domain

       def close(self) -> int:
           self.closed = True
           return 0


   def _no_domain() -> libvirt.libvirtError:
       err = libvirt.libvirtError("synthetic")
       err.err = (libvirt.VIR_ERR_NO_DOMAIN, 0, "synthetic", 0, "", None, None, 0, 0)
       return err


   def _reaper(conn: _ReaperConn, monitor) -> LocalLibvirtCaptureReaper:
       return LocalLibvirtCaptureReaper(connect=lambda: conn, monitor=monitor)
   ```

   The tests (each names its spec requirement):


   ```python
   def test_reaper_satisfies_the_capture_reaper_port() -> None:
       assert isinstance(LocalLibvirtCaptureReaper.from_env(), CaptureReaper)


   def test_detach_happens_before_the_unlink(monkeypatch) -> None:
       """The ordering is the whole control (ADR-0556): unlink-first orphans a live inode."""
       order: list[str] = []
       conn = _ReaperConn()

       def monitor(domain, cmd, flags):
           order.append("object-del")
           return "{}"

       reaper = _reaper(conn, monitor)

       def _record_unlink(capture: OrphanedCapture) -> None:
           order.append("unlink")

       monkeypatch.setattr(reaper, "_unlink_pcap", _record_unlink)
       assert asyncio.run(reaper.reclaim_capture(_capture())) is True
       assert order == ["object-del", "unlink"]


   def test_a_missing_domain_is_tolerated_and_the_pcap_still_unlinks(tmp_path) -> None:
       """The domain stopped (or was reaped); there is no filter to detach (spec R2)."""
       pcap = tmp_path / f"{_JID}.pcap"
       pcap.write_bytes(b"stale")
       qmp_calls: list[str] = []
       conn = _ReaperConn(domain=None, lookup_error=_no_domain())

       def monitor(domain, cmd, flags):  # pragma: no cover - must never run
           qmp_calls.append("object-del")
           return "{}"

       assert asyncio.run(_reaper(conn, monitor).reclaim_capture(_capture())) is True
       assert conn.lookups == [_DOMAIN]
       assert qmp_calls == []  # no domain to address: no QMP call attempted
       assert not pcap.exists()


   def test_a_missing_domain_and_missing_pcap_reclaim_cleanly(tmp_path) -> None:
       """Concurrent absences: no QMP call, unlink attempted, True (spec R2, test 4b)."""
       conn = _ReaperConn(domain=None, lookup_error=_no_domain())

       def monitor(domain, cmd, flags):  # pragma: no cover - must never run
           raise AssertionError("no QMP call without a domain")

       assert asyncio.run(_reaper(conn, monitor).reclaim_capture(_capture())) is True


   def test_a_missing_filter_is_tolerated_and_the_pcap_still_unlinks(tmp_path) -> None:
       """A QMP object-del on an absent id must not fail the reclaim (spec R2)."""
       pcap = tmp_path / f"{_JID}.pcap"
       pcap.write_bytes(b"stale")
       conn = _ReaperConn()

       def monitor(domain, cmd, flags):
           raise libvirt.libvirtError(f"Device '{capture_qom_id(_JID)}' not found")

       assert asyncio.run(_reaper(conn, monitor).reclaim_capture(_capture())) is True
       assert not pcap.exists()


   def test_a_missing_pcap_is_tolerated(tmp_path) -> None:
       """The in-job reclaim already removed it; the sweep still marks completion."""
       conn = _ReaperConn()

       def monitor(domain, cmd, flags):
           return "{}"

       assert asyncio.run(_reaper(conn, monitor).reclaim_capture(_capture())) is True


   def test_a_non_not_found_monitor_error_aborts_before_the_unlink(tmp_path) -> None:
       """A genuine monitor failure is CONTROL_FAILURE and never deletes on unknown state."""
       pcap = tmp_path / f"{_JID}.pcap"
       pcap.write_bytes(b"stale")
       conn = _ReaperConn()

       def monitor(domain, cmd, flags):
           raise libvirt.libvirtError("monitor locked")

       with pytest.raises(CategorizedError) as excinfo:
           asyncio.run(_reaper(conn, monitor).reclaim_capture(_capture()))
       assert excinfo.value.category is ErrorCategory.CONTROL_FAILURE
       assert excinfo.value.details == {"domain": _DOMAIN}
       assert pcap.exists()  # abort-before-unlink
       assert conn.closed  # the connection is closed on the raise path too


   def test_a_non_absence_unlink_failure_is_infrastructure_failure(monkeypatch) -> None:
       """A swallowed unlink failure would mark the row complete with the file on disk."""
       conn = _ReaperConn()

       def monitor(domain, cmd, flags):
           return "{}"

       reaper = _reaper(conn, monitor)

       def _refuse(capture: OrphanedCapture) -> None:
           raise PermissionError(1, "operation not permitted")

       monkeypatch.setattr(reaper, "_unlink_pcap", _refuse)
       with pytest.raises(CategorizedError) as excinfo:
           asyncio.run(reaper.reclaim_capture(_capture()))
       assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


   def test_from_env_builds_without_connecting() -> None:
       """Lazy construction: no libvirt connection is opened until reclaim."""
       reaper = LocalLibvirtCaptureReaper.from_env()
       assert isinstance(reaper, LocalLibvirtCaptureReaper)
   ```

   The two monkeypatched tests patch `_unlink_pcap` on the *instance* via the `monkeypatch`
   fixture — the same pattern the reconciler wiring tests use for `loop._reap_orphaned_captures`.

2. Run `uv run python -m pytest tests/providers/local_libvirt/test_reaping.py -q` and confirm
   the new tests **fail** with `ImportError`/`AttributeError` (`LocalLibvirtCaptureReaper`
   does not exist yet). Expected: collection error naming the missing import.

3. Implement in `src/kdive/providers/local_libvirt/reaping.py`. Extend the module docstring's
   first line to `"""Local-libvirt reconciler reapers (ADR-0111 infra, ADR-0556/0567 capture)."`
   and add the imports the file lacks:

   ```python
   import json
   import logging

   import libvirt

   from kdive.config import config
   from kdive.domain.errors import CategorizedError, ErrorCategory
   from kdive.providers.infra.reaping import OrphanedCapture, OwnedDomain
   from kdive.providers.local_libvirt.settings import LIBVIRT_URI
   from kdive.providers.ports.traffic import capture_qom_id
   from kdive.providers.shared.runtime_paths import pcap_path
   ```

   (`asyncio`, `Callable`, `Protocol` are already imported; add `Callable` from
   `collections.abc` if absent.) Append at the end of the module:

   ```python
   _log = logging.getLogger(__name__)


   class _CaptureConn(Protocol):
       def lookupByName(self, name: str) -> object: ...  # noqa: N802 - libvirt binding name
       def close(self) -> int: ...  # noqa: N802 - libvirt binding name


   type CaptureConnect = Callable[[], _CaptureConn]
   type CaptureMonitor = Callable[[object, str, int], str]


   def _capture_is_not_found(exc: libvirt.libvirtError) -> bool:
       """A QMP ``object-del`` on a missing id yields "object 'X' not found" / DeviceNotFound.

       Deliberate duplicate of the live capturer's matcher: QMP passthrough errors carry no
       distinct ``VIR_ERR_*`` code, so absence is matched on lowercased message text.
       """
       message = str(exc).lower()
       return "not found" in message or "devicenotfound" in message


   def _close_capture_conn(conn: _CaptureConn) -> None:
       """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
       try:
           conn.close()
       except libvirt.libvirtError:
           _log.warning("libvirt connection close failed; continuing", exc_info=True)


   class LocalLibvirtCaptureReaper:
       """Detach an orphaned capture's filter-dump, then unlink its worker-local pcap.

       Reconciler-side by ADR-0567: the pcap is a fixed absolute path on the kdive host
       (``pcap_path``), the provider host *is* the kdive host, and the root reconciler already
       drives local-host reapers over the same connection. Idempotent per the ``CaptureReaper``
       port: an already-missing domain, filter, or file is tolerated; any other failure raises
       so the sweep defers the row and retries.
       """

       def __init__(self, *, connect: CaptureConnect, monitor: CaptureMonitor) -> None:
           self._connect = connect
           self._monitor = monitor

       @classmethod
       def from_env(cls) -> LocalLibvirtCaptureReaper:
           """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
           # Lazy import keeps the QEMU-specific binding off the module import path (mirrors
           # lifecycle/traffic_capture.py), so unit tests inject a fake ``monitor`` instead.
           import libvirt_qemu

           host_uri = config.require(LIBVIRT_URI)
           return cls(
               connect=lambda: libvirt.open(host_uri),
               monitor=libvirt_qemu.qemuMonitorCommand,
           )

       async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
           """Detach the capture's filter, then unlink its pcap; ``True`` when nothing is left.

           Raises:
               CategorizedError: ``CONTROL_FAILURE`` for a connect, domain-lookup, or monitor
                   error other than an absence, ``INFRASTRUCTURE_FAILURE`` for an unlink
                   failure other than absence. The sweep defers the row on both; this
                   implementation only returns ``True`` — every non-success path either
                   tolerates an absence or raises.
           """
           return await asyncio.to_thread(self._reclaim_blocking, capture)

       def _reclaim_blocking(self, capture: OrphanedCapture) -> bool:
           conn = self._open()
           try:
               self._detach_filter(conn, capture)
           finally:
               _close_capture_conn(conn)
           self._unlink_pcap(capture)
           return True

       def _open(self) -> _CaptureConn:
           try:
               return self._connect()
           except libvirt.libvirtError as exc:
               raise self._control_failure("connecting to libvirt for", "capture") from exc

       def _detach_filter(self, conn: _CaptureConn, capture: OrphanedCapture) -> None:
           """``object-del`` the capture's filter on its domain, tolerating either absence."""
           qom_id = capture_qom_id(capture.job_id)
           try:
               domain = conn.lookupByName(capture.domain_name)
           except libvirt.libvirtError as exc:
               if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                   _log.info(
                       "reconciler: capture domain %s is gone; no filter left to detach",
                       capture.domain_name,
                   )
                   return
               raise self._control_failure("looking up", capture.domain_name) from exc
           cmd = {"execute": "object-del", "arguments": {"id": qom_id}}
           try:
               self._monitor(domain, json.dumps(cmd), 0)
           except libvirt.libvirtError as exc:
               if _capture_is_not_found(exc):
                   _log.info(
                       "reconciler: capture filter %s already absent on %s; continuing",
                       qom_id,
                       capture.domain_name,
                   )
                   return
               raise self._control_failure(
                   "removing capture filter on", capture.domain_name
               ) from exc

       def _unlink_pcap(self, capture: OrphanedCapture) -> None:
           """Remove the job's pcap file, tolerating absence; any other OSError raises."""
           path = pcap_path(capture.system_id, capture.job_id)
           try:
               path.unlink(missing_ok=True)
           except OSError as exc:
               raise CategorizedError(
                   f"could not remove orphaned capture pcap {path}",
                   category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                   details={"path": str(path)},
               ) from exc
           _log.info("reconciler: removed orphaned capture pcap %s", path)

       @staticmethod
       def _control_failure(verb: str, domain_name: str) -> CategorizedError:
           return CategorizedError(
               f"libvirt error {verb} domain",
               category=ErrorCategory.CONTROL_FAILURE,
               details={"domain": domain_name},
           )
   ```

   If `kdive.config` exposes `require` differently than `config.require(...)`, match the
   exact call shape used in `lifecycle/traffic_capture.py` (`import kdive.config as config`;
   `config.require(LIBVIRT_URI)`) — copy that file's import and call verbatim.

4. Run `uv run python -m pytest tests/providers/local_libvirt/test_reaping.py -q`. Expected:
   all pass (existing `LibvirtInfraReaper` tests plus the new ones). Then `just lint` and
   `just type` — both clean.

5. Commit: `feat(reaping): local-libvirt CaptureReaper detaches filter, then unlinks pcap`.

**Acceptance**: the call-order test proves object-del precedes unlink; each absence test
proves tolerance; the monitor-error test proves abort-before-unlink and connection close;
`just lint`/`just type` clean.

## Task 2 — prepare-time pre-delete

**Files**: modify `src/kdive/providers/local_libvirt/lifecycle/traffic_capture.py`;
modify `tests/providers/local_libvirt/test_traffic_capture.py`.

**Interfaces**: consumes `pcap_path` (already imported in the module). Task 1 is complete
and untouched here. No later task consumes this beyond the existing handler.

**Where it fits**: closes the R4 gap (ADR-0567); independent of Task 1's module.

**Steps**:

1. Write the failing tests — append to `tests/providers/local_libvirt/test_traffic_capture.py`
   (reuse the existing `_capturer` helper and its monkeypatch pattern from
   `test_prepare_prepares_dir_and_returns_worker_pcap_path`):

   ```python
   def test_prepare_pre_deletes_this_jobs_stale_pcap(monkeypatch, tmp_path) -> None:
       """An at-least-once retry starts from a clean file (ADR-0567); job-keyed, not a sweep."""
       system_id, job_id, other_job = uuid4(), uuid4(), uuid4()
       stale = tmp_path / f"{system_id}-{job_id}.pcap"
       concurrent = tmp_path / f"{system_id}-{other_job}.pcap"
       stale.write_bytes(b"stale")
       concurrent.write_bytes(b"live")
       monkeypatch.setattr(traffic_capture_module, "prepare_pcap_dir", lambda _sid: None)
       monkeypatch.setattr(
           traffic_capture_module,
           "pcap_path",
           lambda sid, jid: tmp_path / f"{sid}-{jid}.pcap",
       )

       _capturer(_noop_monitor).prepare(system_id, job_id)

       assert not stale.exists()  # this job's stale file is gone
       assert concurrent.read_bytes() == b"live"  # a different job's file survives


   def test_prepare_tolerates_an_absent_stale_pcap(monkeypatch, tmp_path) -> None:
       system_id, job_id = uuid4(), uuid4()
       monkeypatch.setattr(traffic_capture_module, "prepare_pcap_dir", lambda _sid: None)
       monkeypatch.setattr(
           traffic_capture_module,
           "pcap_path",
           lambda sid, jid: tmp_path / f"{sid}-{jid}.pcap",
       )

       dest = _capturer(_noop_monitor).prepare(system_id, job_id)  # must not raise

       assert dest == str(tmp_path / f"{system_id}-{job_id}.pcap")
   ```

2. Run
   `uv run python -m pytest tests/providers/local_libvirt/test_traffic_capture.py -q -k prepare`.
   Expected: the new pre-delete test **fails** (stale file still exists); the absent-file test
   passes (prepare already succeeds without a stale file).

3. Implement in `LocalLibvirtTrafficCapture.prepare` — replace the body between
   `prepare_pcap_dir(system_id)` and the `return` with the unlink, and extend the docstring:

   ```python
       def prepare(self, system_id: UUID, job_id: UUID) -> str:
           """Prepare the QEMU-writable per-System pcap dir and return the worker pcap path.

           The confined qemu:///system hypervisor writes the filter-dump as the QEMU runtime user,
           so the dir is owned to that user and SELinux-labelled ``svirt_image_t`` (ADR-0385); a
           genuine write failure surfaces loudly at :meth:`fetch` via a short/absent file.

           Pre-deletes this job's own stale pcap first (ADR-0567), so an at-least-once retry of a
           job whose prior attempt died mid-capture starts from a clean file — job-keyed, never a
           whole-System sweep, which would remove a concurrent capture's live file. Best-effort
           (every ``OSError`` suppressed): a file that could not be removed is truncated by
           filter-dump on attach, and a job that dies anyway leaves the file to the reconciler's
           capture reaper.
           """
           prepare_pcap_dir(system_id)
           with contextlib.suppress(OSError):
               pcap_path(system_id, job_id).unlink(missing_ok=True)
           return str(pcap_path(system_id, job_id))
   ```

4. Run
   `uv run python -m pytest tests/providers/local_libvirt/test_traffic_capture.py -q`. Expected:
   all pass. Then `just lint` and `just type` — both clean.

5. Commit: `feat(capture): pre-delete this job's stale pcap in local prepare`.

**Acceptance**: the stale file is gone after `prepare`; the concurrent job's file survives; an
absent file is a no-op; existing prepare test still passes.

## Task 3 — wiring: enable the local-libvirt kind

**Files**: modify `src/kdive/providers/local_libvirt/composition.py`;
modify `src/kdive/providers/assembly/composition.py` (docstring only);
modify `src/kdive/reconciler/loop.py` (comment only);
modify `tests/reconciler/test_capture_reaping_wiring.py`.

**Interfaces**: consumes `LocalLibvirtCaptureReaper.from_env()` from Task 1. The builders
dict in `providers/assembly/composition.py` (`build_reconciler_capture_reapers`) already
calls `local_composition.build_capture_reaper` — no assembly code change.

**Where it fits**: flips the kind from `NullCaptureReaper` (never dispatched) to concrete
(dispatchable, completion-markable).

**Steps**:

1. Write the failing test change — in `tests/reconciler/test_capture_reaping_wiring.py`,
   rename and invert `test_local_stays_disabled_while_remote_is_wired_concrete` (keep its
   call shape):

   ```python
   def test_both_capture_kinds_are_wired_concrete() -> None:
       """ADR-0556/0567: #1947 registered remote's reaper; #1948 registers local's."""
       composition = ProviderComposition()

       reapers = composition.build_reconciler_capture_reapers(
           enable_local_libvirt=True, enable_remote_libvirt=True, enable_fault_inject=True
       )

       assert set(reapers) == _CAPTURE_KINDS
       assert isinstance(reapers["local-libvirt"], LocalLibvirtCaptureReaper)
       assert isinstance(reapers["remote-libvirt"], RemoteLibvirtCaptureReaper)
       assert dispatchable_capture_kinds(reapers) == _CAPTURE_KINDS
   ```

   Add `LocalLibvirtCaptureReaper` to the file's existing
   `kdive.providers.local_libvirt` import (it currently imports nothing from that module —
   add `from kdive.providers.local_libvirt.reaping import LocalLibvirtCaptureReaper` beside
   the existing remote import) and drop `NullCaptureReaper` from imports only if now unused
   (it is still used by `test_the_sweep_is_wired_with_its_configured_pacing_values` — leave
   it).

2. Run `uv run python -m pytest tests/reconciler/test_capture_reaping_wiring.py -q`. Expected:
   the renamed test **fails** — `reapers["local-libvirt"]` is `NullCaptureReaper`, so the
   isinstance and dispatchable assertions fail.

3. Implement. In `src/kdive/providers/local_libvirt/composition.py`, replace
   `build_capture_reaper` (currently returns `NullCaptureReaper()`) with:

   ```python
   def build_capture_reaper() -> CaptureReaper:
       """Build local-libvirt's orphaned-capture reaper (ADR-0556, ADR-0567, #1948).

       Detaches the job's QOM filter over the local ``KDIVE_LIBVIRT_URI`` connection and unlinks
       the job's pcap at the shared runtime-path convention. The reconciler for this kind is a
       root process colocated with the worker on the kdive host (ADR-0567's prerequisite), so it
       can reach both the hypervisor and the worker-owned path.
       """
       return LocalLibvirtCaptureReaper.from_env()
   ```

   Update the import block: add `LocalLibvirtCaptureReaper` to the existing
   `kdive.providers.local_libvirt.reaping` import (the file already imports
   `LibvirtInfraReaper`-adjacent names — check the exact import line; it imports
   `CaptureReaper, InfraReaper, NullCaptureReaper` from `kdive.providers.infra.reaping`).
   `NullCaptureReaper` becomes unused in this file — remove it from that import.

4. Update the two stale comments that say local ships disabled:

   - `src/kdive/providers/assembly/composition.py`, in `build_reconciler_capture_reapers`'s
     docstring, replace the sentence "Remote-libvirt registers its concrete reaper (#1947);
     local-libvirt is still ``NullCaptureReaper`` disabled wiring (#1948), which the sweep
     leaves out of selection entirely so it can neither be dispatched nor marked complete."
     with "Both capture-capable kinds register concrete reapers (#1947 remote, #1948 local);
     a kind left on ``NullCaptureReaper`` is excluded from selection entirely so it can
     neither be dispatched nor marked complete."
   - `src/kdive/reconciler/loop.py`, the `capture_reapers` field comment: replace "Both
     capture-capable kinds ship disabled; #1947 and #1948 each register their own concrete
     reaper." with "Both capture-capable kinds ship concrete reapers (#1947 remote, #1948
     local)."

5. Run `uv run python -m pytest tests/reconciler/test_capture_reaping_wiring.py -q`. Expected:
   all pass. Then `just lint` and `just type` — both clean.

6. Commit: `feat(reaping): wire local-libvirt's capture reaper into the sweep`.

**Acceptance**: `dispatchable_capture_kinds` yields both kinds for an
all-providers-enabled composition; the default (nothing wired) still registers no reaper
(`test_the_default_config_registers_no_capture_reaper_at_all` stays green unchanged).

## Task 4 — full guardrail suite

**Files**: none (verification only).

**Steps**:

1. `uv run python -m pytest tests/providers/local_libvirt/ tests/reconciler/ -q` — all green.
2. `just ci` — the full PR gate. Expected: exit 0. This includes `adr-status-check`
   (ADR-0567 is `Accepted` and cited in src/, which is the valid shipped state) and the
   doc/config guards the new ADR and spec must satisfy.
3. No further commit unless the suite surfaced a fix; fix and re-run if so.

**Acceptance**: `just ci` exits 0.
