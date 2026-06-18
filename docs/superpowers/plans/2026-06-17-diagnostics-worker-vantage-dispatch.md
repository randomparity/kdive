# Diagnostics worker-vantage dispatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire worker-job dispatch so `ops.diagnostics`' `provider_tls` and `gdbstub_acl` checks run for real on the worker instead of returning `not_implemented`.

**Architecture:** A new `diagnostics_worker_check` durable job runs the two worker-vantage checks on the worker and returns their `CheckResult`s inline in `result_ref`. `ops.diagnostics` (server) enqueues the job behind a `WorkerCheckDispatcher` port and bounded-waits within a reserved worker-phase budget, keeping the single coherent verdict (ADR-0091 §1). Worker-down → `WORKER_UNAVAILABLE` (ADR-0139).

**Tech Stack:** Python 3.13, `uv`, `ruff`, `ty`, `pytest`. Postgres durable job queue (`jobs/queue.py`), libvirt, Python `ssl`/`socket`.

**Spec:** `docs/superpowers/specs/2026-06-17-diagnostics-worker-vantage-dispatch.md`
**ADR:** `docs/adr/0163-diagnostics-worker-vantage-dispatch.md`

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree (`just type`).
- Functions ≤100 lines, cyclomatic complexity ≤8, ≤5 positional params, absolute imports only.
- Every guardrail green before each commit: `just lint`, `just type`, the touched tests. Run `just ci` before the first push.
- Conventional-commit subjects ≤72 chars, imperative; end each commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Doc-style guard: plain factual prose; avoid the project's banned promotional adjectives (see CLAUDE.md); use "Milestone" not the s-word for an iteration.
- Pick the most specific existing `ErrorCategory`; never invent strings.
- The diagnostics result carries **no secret material** — probes put only operator config (`gdb_addr`, port range, CA label) in `detail`/`fix`.
- Constants: `WORKER_DISPATCH_BUDGET = 15.0` (worker-phase budget). Server phase keeps the existing `_DEFAULT_OVERALL_TIMEOUT = 30.0`. `_REMOTE_PROVIDER = "remote-libvirt"`. Worker-vantage check ids: `provider_tls`, `gdbstub_acl`.

## File Structure

- Create `src/kdive/db/schema/0040_diagnostics_worker_check_job_kind.sql` — widen `jobs_kind_check`.
- Modify `src/kdive/domain/models.py` — add `JobKind.DIAGNOSTICS_WORKER_CHECK`.
- Modify `src/kdive/jobs/payloads.py` — add `DiagnosticsWorkerCheckPayload` + map/union entries.
- Create `src/kdive/diagnostics/result_codec.py` — `serialize_results` / `deserialize_results` (inline JSON ↔ `CheckResult`s, validated).
- Create `src/kdive/diagnostics/provider_tls.py` — `provider_tls_probe()` (direct `ssl` handshake).
- Create `src/kdive/diagnostics/gdbstub_acl.py` — `gdbstub_acl_probe()` (TCP-connect heuristic).
- Create `src/kdive/jobs/handlers/diagnostics.py` — worker handler + `register_handlers`.
- Create `src/kdive/diagnostics/worker_dispatch.py` — `WorkerCheckDispatcher` protocol + `JobWorkerCheckDispatcher`.
- Modify `src/kdive/diagnostics/service.py` — `worker_dispatcher` seam in `DiagnosticsService`; `default_service_factory` wiring; refine `WORKER_UNAVAILABLE_DETAIL`.
- Modify `src/kdive/mcp/app.py` — bind the pool into the diagnostics factory; register the handler in `_HANDLER_REGISTRARS`.
- Tests mirror under `tests/diagnostics/` and `tests/jobs/handlers/`.

---

### Task 1: New job kind, migration, and payload

**Files:**
- Modify: `src/kdive/domain/models.py:78-88` (JobKind enum)
- Create: `src/kdive/db/schema/0040_diagnostics_worker_check_job_kind.sql`
- Modify: `src/kdive/jobs/payloads.py` (add payload + map/union)
- Test: `tests/db/test_migrate.py` (already asserts the `jobs_kind_check`↔`JobKind` tie), `tests/jobs/test_payloads.py`

**Interfaces:**
- Produces: `JobKind.DIAGNOSTICS_WORKER_CHECK = "diagnostics_worker_check"`; `DiagnosticsWorkerCheckPayload(provider: str)`.

- [ ] **Step 1: Write the failing payload test**

In `tests/jobs/test_payloads.py` add:

```python
def test_diagnostics_worker_check_payload_roundtrips():
    from kdive.domain.models import JobKind
    from kdive.jobs.payloads import DiagnosticsWorkerCheckPayload, dump_payload

    payload = DiagnosticsWorkerCheckPayload(provider="remote-libvirt")
    dumped = dump_payload(JobKind.DIAGNOSTICS_WORKER_CHECK, payload)
    assert dumped == {"provider": "remote-libvirt"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/jobs/test_payloads.py::test_diagnostics_worker_check_payload_roundtrips -q`
Expected: FAIL (`AttributeError: DIAGNOSTICS_WORKER_CHECK` / import error).

- [ ] **Step 3: Add the enum value**

In `src/kdive/domain/models.py`, after `IMAGE_BUILD = "image_build"`:

```python
    DIAGNOSTICS_WORKER_CHECK = "diagnostics_worker_check"
```

- [ ] **Step 4: Add the payload + map/union**

In `src/kdive/jobs/payloads.py`, add the model (after `ImageBuildPayload`):

```python
class DiagnosticsWorkerCheckPayload(_PayloadBase):
    """The inputs a ``DIAGNOSTICS_WORKER_CHECK`` job carries.

    Only the concrete provider id (``remote-libvirt``); the handler re-resolves the host
    config from the inventory at probe time, so no host identity or secret rides on the queue.
    """

    provider: str
```

Add `DiagnosticsWorkerCheckPayload` to both the `_PayloadModel` and `PayloadModel` unions, and add to `_PAYLOAD_MODELS`:

```python
    JobKind.DIAGNOSTICS_WORKER_CHECK: DiagnosticsWorkerCheckPayload,
```

- [ ] **Step 5: Add the migration**

Create `src/kdive/db/schema/0040_diagnostics_worker_check_job_kind.sql`:

```sql
-- 0040_diagnostics_worker_check_job_kind.sql — diagnostics worker-vantage dispatch (ADR-0163, #514).
-- Additive to 0003/0024 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `diagnostics_worker_check` op (ops.diagnostics enqueues it to run provider_tls/gdbstub_acl on
-- the worker); mirrors JobKind in domain/models.py. Drop-and-recreate keeps the constraint name
-- stable for the SQL<->enum tie (tested in test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check'));
```

- [ ] **Step 6: Run payload test + migration tie test**

Run: `uv run python -m pytest tests/jobs/test_payloads.py -q tests/db/test_migrate.py -q`
Expected: PASS (the migrate test needs Docker; if Docker is absent it SKIPs — run it in CI).

- [ ] **Step 7: Lint/type + commit**

Run: `just lint && just type`
```bash
git add src/kdive/domain/models.py src/kdive/jobs/payloads.py src/kdive/db/schema/0040_diagnostics_worker_check_job_kind.sql tests/jobs/test_payloads.py
git commit -m "feat(diagnostics): add diagnostics_worker_check job kind + payload"
```

---

### Task 2: Inline result codec

**Files:**
- Create: `src/kdive/diagnostics/result_codec.py`
- Test: `tests/diagnostics/test_result_codec.py`

**Interfaces:**
- Consumes: `CheckResult`, `CheckStatus`, `PROVIDER_TLS_ID`, `GDBSTUB_ACL_ID` from `kdive.diagnostics.checks`.
- Produces: `serialize_results(results: list[CheckResult]) -> str`; `deserialize_results(raw: str | None) -> list[CheckResult]` — accepts only the two worker-vantage `check_id`s, reconstructs through `CheckResult` (re-running its invariants); on malformed/empty/unexpected input raises `ResultCodecError`.

- [ ] **Step 1: Write the failing tests**

`tests/diagnostics/test_result_codec.py`:

```python
import pytest

from kdive.diagnostics.checks import CheckResult, CheckStatus, GDBSTUB_ACL_ID, PROVIDER_TLS_ID
from kdive.diagnostics.result_codec import (
    ResultCodecError,
    deserialize_results,
    serialize_results,
)


def test_roundtrip_preserves_three_state_and_fields():
    src = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(
            GDBSTUB_ACL_ID, CheckStatus.FAIL, "blocked", fix="open the ACL",
            provider="remote-libvirt", failure_category="configuration_error",
        ),
    ]
    out = deserialize_results(serialize_results(src))
    assert [(r.check_id, r.status, r.fix, r.failure_category) for r in out] == [
        (PROVIDER_TLS_ID, CheckStatus.PASS, None, None),
        (GDBSTUB_ACL_ID, CheckStatus.FAIL, "open the ACL", "configuration_error"),
    ]


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '{"results": 3}'])
def test_malformed_raises(raw):
    with pytest.raises(ResultCodecError):
        deserialize_results(raw)


def test_unexpected_check_id_raises():
    payload = '{"results": [{"check_id": "secret_ref", "status": "pass", "detail": "x"}]}'
    with pytest.raises(ResultCodecError):
        deserialize_results(payload)


def test_invariant_violation_raises():
    # fail without a fix violates CheckResult.__post_init__
    payload = '{"results": [{"check_id": "provider_tls", "status": "fail", "detail": "x"}]}'
    with pytest.raises(ResultCodecError):
        deserialize_results(payload)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_result_codec.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the codec**

`src/kdive/diagnostics/result_codec.py`:

```python
"""Inline (de)serialization of worker-vantage CheckResults carried in a job's result_ref (ADR-0163).

The diagnostics worker job returns its two CheckResults as a compact JSON string inline in
`result_ref` (the verdict is small, non-secret, and read only by the dispatcher). The dispatcher
reconstructs each result through `CheckResult`, re-running its invariants, and accepts only the
two worker-vantage check ids — a malformed, empty, or unexpected payload becomes a `ResultCodecError`
that the dispatcher maps to an `error` verdict rather than injecting a surprising result verbatim.
"""

from __future__ import annotations

import json
from typing import Any

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckResult,
    CheckStatus,
)

_ALLOWED_IDS = frozenset({PROVIDER_TLS_ID, GDBSTUB_ACL_ID})


class ResultCodecError(ValueError):
    """The inline worker result is malformed, empty, or carries an unexpected check id."""


def serialize_results(results: list[CheckResult]) -> str:
    """Serialize worker-vantage CheckResults to a compact JSON string for inline transport."""
    return json.dumps(
        {
            "results": [
                {
                    "check_id": r.check_id,
                    "status": r.status.value,
                    "detail": r.detail,
                    "fix": r.fix,
                    "provider": r.provider,
                    "failure_category": r.failure_category,
                }
                for r in results
            ]
        },
        separators=(",", ":"),
    )


def deserialize_results(raw: str | None) -> list[CheckResult]:
    """Parse + validate inline worker results; raise ResultCodecError on anything unexpected."""
    if not raw:
        raise ResultCodecError("empty diagnostics result")
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ResultCodecError(f"diagnostics result is not valid JSON: {exc}") from exc
    items = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ResultCodecError("diagnostics result has no 'results' list")
    return [_reconstruct(item) for item in items]


def _reconstruct(item: Any) -> CheckResult:
    if not isinstance(item, dict):
        raise ResultCodecError("diagnostics result item is not an object")
    check_id = item.get("check_id")
    if check_id not in _ALLOWED_IDS:
        raise ResultCodecError(f"unexpected worker-vantage check id {check_id!r}")
    try:
        return CheckResult(
            check_id=check_id,
            status=CheckStatus(item["status"]),
            detail=item["detail"],
            fix=item.get("fix"),
            provider=item.get("provider"),
            failure_category=item.get("failure_category"),
        )
    except (KeyError, ValueError) as exc:  # missing field, bad enum, or invariant violation
        raise ResultCodecError(f"invalid diagnostics result item: {exc}") from exc
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/diagnostics/test_result_codec.py -q`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/diagnostics/result_codec.py tests/diagnostics/test_result_codec.py
git commit -m "feat(diagnostics): inline worker-result codec with validation"
```

---

### Task 3: provider_tls probe

**Files:**
- Create: `src/kdive/diagnostics/provider_tls.py`
- Test: `tests/diagnostics/test_provider_tls_probe.py`

**Interfaces:**
- Consumes: `TlsProbe`, `TlsProbeOutcome` from `kdive.diagnostics.checks`; `RemoteLibvirtConfig` from `kdive.providers.remote_libvirt.config`.
- Produces: `provider_tls_probe(config, *, connector=...) -> TlsProbe`, where `connector` is an injectable `Callable[[str, int, ssl.SSLContext], None]` performing the handshake (production: real socket; tests: a fake that raises/returns). The probe derives host/port from `config.uri` (default port 16514).

- [ ] **Step 1: Write the failing tests**

`tests/diagnostics/test_provider_tls_probe.py`:

```python
import ssl

import pytest

from kdive.diagnostics.checks import TlsProbeOutcome
from kdive.diagnostics.provider_tls import provider_tls_probe, tls_endpoint
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs


def _config(uri="qemu+tls://host.example/system"):
    return RemoteLibvirtConfig(
        uri=uri,
        cert_refs=TlsCertRefs("c", "k", "ca"),
        concurrent_allocation_cap=1,
        gdb_addr="host.example",
    )


def test_tls_endpoint_defaults_and_overrides_port():
    assert tls_endpoint("qemu+tls://host.example/system") == ("host.example", 16514)
    assert tls_endpoint("qemu+tls://host.example:17000/system") == ("host.example", 17000)


@pytest.mark.parametrize(
    "raiser, expected",
    [
        (None, TlsProbeOutcome.VALID),
        (ssl.SSLCertVerificationError("bad cert"), TlsProbeOutcome.INVALID),
        (ConnectionRefusedError(), TlsProbeOutcome.UNREACHABLE),
        (TimeoutError(), TlsProbeOutcome.UNREACHABLE),
        (ssl.SSLError("protocol"), TlsProbeOutcome.UNREACHABLE),
    ],
)
async def test_probe_classifies(raiser, expected):
    captured = {}

    def fake_connector(host, port, ctx):
        captured["host"], captured["port"] = host, port
        if raiser is not None:
            raise raiser

    def fake_context(config):
        return ssl.create_default_context()

    probe = provider_tls_probe(_config(), connector=fake_connector, context_factory=fake_context)
    assert await probe("ca-label") == expected
    assert captured == {"host": "host.example", "port": 16514}
```

(Note: `tests/diagnostics/conftest.py` already enables async tests via `anyio`/`asyncio` — confirm the marker style there and match it.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_provider_tls_probe.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the probe**

`src/kdive/diagnostics/provider_tls.py`:

```python
"""Direct-TLS provider_tls probe for the remote-libvirt worker-vantage check (ADR-0163).

A failed libvirt qemu+tls *open* is wrapped opaquely as TRANSPORT_FAILURE by the transport, so a
bad cert is indistinguishable from a down host there — the exact distinction this check exists to
make. The probe instead does a direct TLS handshake (Python `ssl`) to the libvirt TLS endpoint
(host/port parsed from the URI, default 16514) with the materialized client cert/key and the
configured CA, classifying via typed `ssl` exceptions so the verdict is stable across libvirt
versions. Scoped to chain validity, not libvirt's tls_allowed_dn_list authz.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from kdive.diagnostics.checks import TlsProbe, TlsProbeOutcome
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.transport import materialized_pkipath
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_DEFAULT_TLS_PORT = 16514
_CONNECT_TIMEOUT_S = 5.0
_log = logging.getLogger(__name__)

TlsConnector = Callable[[str, int, ssl.SSLContext], None]
ContextFactory = Callable[[RemoteLibvirtConfig], ssl.SSLContext]


def tls_endpoint(uri: str) -> tuple[str, int]:
    """Parse the libvirt TLS (host, port) from the qemu+tls URI; default 16514 when absent."""
    parsed = urlsplit(uri)
    return parsed.hostname or "", parsed.port or _DEFAULT_TLS_PORT


def _default_secret_backend() -> SecretBackend:
    return secret_backend_from_env(registry=SecretRegistry())


def _handshake(host: str, port: int, context: ssl.SSLContext) -> None:
    with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            tls.do_handshake()


def _context_factory(config: RemoteLibvirtConfig) -> ssl.SSLContext:
    backend = _default_secret_backend()
    with materialized_pkipath(backend, config.cert_refs) as pkipath:
        ctx = ssl.create_default_context(cafile=str(Path(pkipath) / "cacert.pem"))
        ctx.load_cert_chain(
            certfile=str(Path(pkipath) / "clientcert.pem"),
            keyfile=str(Path(pkipath) / "clientkey.pem"),
        )
        return ctx


def provider_tls_probe(
    config: RemoteLibvirtConfig,
    *,
    connector: TlsConnector = _handshake,
    context_factory: ContextFactory = _context_factory,
) -> TlsProbe:
    """Build the async provider_tls probe over injectable TLS seams."""
    host, port = tls_endpoint(config.uri)

    async def probe(_ca_path: str) -> TlsProbeOutcome:
        return await asyncio.to_thread(_probe_sync, host, port, config, connector, context_factory)

    return probe


def _probe_sync(
    host: str,
    port: int,
    config: RemoteLibvirtConfig,
    connector: TlsConnector,
    context_factory: ContextFactory,
) -> TlsProbeOutcome:
    try:
        context = context_factory(config)
        connector(host, port, context)
    except ssl.SSLCertVerificationError:
        return TlsProbeOutcome.INVALID
    except (ConnectionRefusedError, TimeoutError, socket.timeout, OSError, ssl.SSLError):
        _log.warning("provider_tls handshake to %s:%s did not validate", host, port, exc_info=True)
        return TlsProbeOutcome.UNREACHABLE
    return TlsProbeOutcome.VALID
```

> **Verify the pkipath filenames** (`cacert.pem`/`clientcert.pem`/`clientkey.pem`) against `materialized_pkipath` in `transport.py` before implementing; use whatever names it writes. If the materialized layout differs, adapt the `_context_factory` paths to match — the test injects `context_factory`, so the production path is exercised only in the live run.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/diagnostics/test_provider_tls_probe.py -q`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/diagnostics/provider_tls.py tests/diagnostics/test_provider_tls_probe.py
git commit -m "feat(diagnostics): direct-TLS provider_tls probe"
```

---

### Task 4: gdbstub_acl probe

**Files:**
- Create: `src/kdive/diagnostics/gdbstub_acl.py`
- Test: `tests/diagnostics/test_gdbstub_acl_probe.py`

**Interfaces:**
- Consumes: `GdbstubAclProbe` from `kdive.diagnostics.checks` (`Callable[[str, str], Awaitable[bool | None]]`).
- Produces: `gdbstub_acl_probe(*, connector=...) -> GdbstubAclProbe`, where `connector` is `Callable[[str, int], None]` (raises to signal blocked/indeterminate). The probe targets the lowest port; `host`/`port_range` come from the check (`f"{min}-{max}"`).

- [ ] **Step 1: Write the failing tests**

`tests/diagnostics/test_gdbstub_acl_probe.py`:

```python
import socket

import pytest

from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe


@pytest.mark.parametrize(
    "raiser, expected",
    [
        (None, True),                       # connect succeeds -> admits
        (ConnectionRefusedError(), True),   # fast refusal -> SYN reached host -> admits
        (TimeoutError(), False),            # DROP -> blocked
        (socket.timeout(), False),          # DROP -> blocked
        (OSError("dns"), None),             # indeterminate
    ],
)
async def test_probe_classifies(raiser, expected):
    captured = {}

    def fake_connector(host, port):
        captured["host"], captured["port"] = host, port
        if raiser is not None:
            raise raiser

    probe = gdbstub_acl_probe(connector=fake_connector)
    assert await probe("host.example", "47000-47099") is expected
    assert captured == {"host": "host.example", "port": 47000}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_gdbstub_acl_probe.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the probe**

`src/kdive/diagnostics/gdbstub_acl.py`:

```python
"""TCP-connect gdbstub_acl probe for the remote-libvirt worker-vantage check (ADR-0163).

A *policy* check with no live listener (ADR-0091 §2): the worker attempts a TCP connect to the
lowest port of the configured gdbstub range. A connect or a fast ECONNREFUSED means the SYN reached
the host's TCP stack (the M2 DROP/blackhole fault is excluded) -> admits; a connect timeout means
the firewall drops it -> blocked; any other error is indeterminate. Known limitation: a fast
ECONNREFUSED cannot distinguish 'no listener' from an iptables -j REJECT rule, so a REJECT-style
block reads as admit (documented in ADR-0163).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable

from kdive.diagnostics.checks import GdbstubAclProbe

_CONNECT_TIMEOUT_S = 3.0
_log = logging.getLogger(__name__)

AclConnector = Callable[[str, int], None]


def _connect(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S):
        pass


def _lowest_port(port_range: str) -> int:
    return int(port_range.split("-", 1)[0])


def gdbstub_acl_probe(*, connector: AclConnector = _connect) -> GdbstubAclProbe:
    """Build the async gdbstub_acl probe over an injectable TCP connector."""

    async def probe(host: str, port_range: str) -> bool | None:
        return await asyncio.to_thread(_probe_sync, host, _lowest_port(port_range), connector)

    return probe


def _probe_sync(host: str, port: int, connector: AclConnector) -> bool | None:
    try:
        connector(host, port)
    except ConnectionRefusedError:
        return True
    except (TimeoutError, socket.timeout):
        return False
    except OSError:
        _log.warning("gdbstub_acl probe to %s:%s was indeterminate", host, port, exc_info=True)
        return None
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/diagnostics/test_gdbstub_acl_probe.py -q`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/diagnostics/gdbstub_acl.py tests/diagnostics/test_gdbstub_acl_probe.py
git commit -m "feat(diagnostics): TCP-connect gdbstub_acl probe"
```

---

### Task 5: Worker handler

**Files:**
- Create: `src/kdive/jobs/handlers/diagnostics.py`
- Test: `tests/jobs/handlers/test_diagnostics_handler.py`

**Interfaces:**
- Consumes: `provider_tls_probe`, `gdbstub_acl_probe`, `serialize_results`, `ProviderTlsCheck`, `GdbstubAclCheck`, `run_check`, `remote_config_from_inventory`.
- Produces: `diagnostics_worker_check_handler(conn, job, *, config_factory=remote_config_from_inventory, build_checks=...) -> str | None` (returns the inline serialized results); `register_handlers(registry)`.

- [ ] **Step 1: Write the failing tests**

`tests/jobs/handlers/test_diagnostics_handler.py`:

```python
import pytest

from kdive.diagnostics.checks import CheckResult, CheckStatus, GDBSTUB_ACL_ID, PROVIDER_TLS_ID
from kdive.diagnostics.result_codec import deserialize_results
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers.diagnostics import diagnostics_worker_check_handler


class _FakeCheck:
    def __init__(self, result):
        self._result = result
        self.id = result.check_id

    async def run(self):
        return self._result


async def test_handler_runs_checks_and_serializes_inline():
    results = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
    ]
    raw = await diagnostics_worker_check_handler(
        conn=None, job=None,
        config_factory=lambda: object(),
        build_checks=lambda _config: [_FakeCheck(r) for r in results],
    )
    assert {r.check_id for r in deserialize_results(raw)} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}


async def test_handler_propagates_config_error():
    def boom():
        raise CategorizedError("bad inventory", category=ErrorCategory.CONFIGURATION_ERROR)

    with pytest.raises(CategorizedError):
        await diagnostics_worker_check_handler(
            conn=None, job=None, config_factory=boom, build_checks=lambda _c: [],
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/jobs/handlers/test_diagnostics_handler.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the handler**

`src/kdive/jobs/handlers/diagnostics.py`:

```python
"""Worker handler for the diagnostics_worker_check job (ADR-0163).

Resolves the remote-libvirt config at probe time, builds the two worker-vantage checks with their
production probes, runs each through `run_check` (per-check timeout -> an unreachable host is an
`error`, never a hang), and returns the serialized CheckResults inline as the job's result_ref.
A config-resolution failure propagates so the job dead-letters and the dispatcher maps it to an
`error` verdict.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from psycopg import AsyncConnection

from kdive.diagnostics.checks import (
    Check,
    GdbstubAclCheck,
    ProviderTlsCheck,
    run_check,
)
from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe
from kdive.diagnostics.provider_tls import provider_tls_probe
from kdive.diagnostics.result_codec import serialize_results
from kdive.domain.models import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    remote_config_from_inventory,
)

_REMOTE_PROVIDER = "remote-libvirt"
_PER_CHECK_TIMEOUT_S = 6.0

ConfigFactory = Callable[[], RemoteLibvirtConfig]
CheckBuilder = Callable[[RemoteLibvirtConfig], Sequence[Check]]


def _build_checks(config: RemoteLibvirtConfig) -> list[Check]:
    return [
        ProviderTlsCheck(
            provider=_REMOTE_PROVIDER,
            ca_path=config.cert_refs.ca_cert_ref,
            probe=provider_tls_probe(config),
        ),
        GdbstubAclCheck(
            provider=_REMOTE_PROVIDER,
            host=config.gdb_addr or "",
            port_range=f"{config.gdb_port_min}-{config.gdb_port_max}",
            probe=gdbstub_acl_probe(),
        ),
    ]


async def diagnostics_worker_check_handler(
    conn: AsyncConnection | None,
    job: Job | None,
    *,
    config_factory: ConfigFactory = remote_config_from_inventory,
    build_checks: CheckBuilder = _build_checks,
) -> str | None:
    """Run the worker-vantage checks and return their results inline as result_ref."""
    config = config_factory()
    checks = build_checks(config)
    results = [await run_check(check, timeout=_PER_CHECK_TIMEOUT_S) for check in checks]
    return serialize_results(results)


def register_handlers(registry: HandlerRegistry) -> None:
    """Bind the diagnostics_worker_check job handler."""
    registry.register(
        JobKind.DIAGNOSTICS_WORKER_CHECK,
        lambda conn, job: diagnostics_worker_check_handler(conn, job),
    )
```

> **gdb_addr None:** when `config.gdb_addr is None`, `GdbstubAclCheck` runs its probe against host `""`; the probe's `OSError` path returns `None` → `error` ("could not determine the ACL"). That satisfies the spec's "unset gdb_addr → error". A focused test in Task 4 already covers `None` indeterminacy; no extra handler branch is needed, but confirm the empty-host probe yields `error` not a crash when implementing.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/jobs/handlers/test_diagnostics_handler.py -q`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/jobs/handlers/diagnostics.py tests/jobs/handlers/test_diagnostics_handler.py
git commit -m "feat(diagnostics): worker handler for diagnostics_worker_check"
```

---

### Task 6: Bounded-wait dispatcher

**Files:**
- Create: `src/kdive/diagnostics/worker_dispatch.py`
- Test: `tests/diagnostics/test_worker_dispatch.py`

**Interfaces:**
- Consumes: `enqueue`, `get_by_dedup_key` from `kdive.jobs.queue`; `Authorizing`, `DiagnosticsWorkerCheckPayload`; `Job`, `JobKind`; `JobState`; `deserialize_results`, `ResultCodecError`; `CheckResult`, `CheckStatus`, `GDBSTUB_ACL_ID`, `PROVIDER_TLS_ID`; `WORKER_UNAVAILABLE_DETAIL` from `kdive.diagnostics.service`.
- Produces: `WorkerCheckDispatcher` protocol with `async def run_worker_checks(self) -> list[CheckResult]` (no `deadline` arg — the dispatcher owns `WORKER_DISPATCH_BUDGET`); `JobWorkerCheckDispatcher(pool, *, budget=WORKER_DISPATCH_BUDGET, poll_interval=0.25, enqueue_fn=None, get_fn=None, clock=time.monotonic, sleep_fn=asyncio.sleep, dedup_suffix=None)`; module constant `WORKER_DISPATCH_BUDGET = 15.0`. On budget-with-no-pickup returns `WORKER_UNAVAILABLE` substitutions for `provider_tls`/`gdbstub_acl`.

> **Cycle resolution (decided, not deferred):** `WORKER_UNAVAILABLE_DETAIL` has ONE home — `service.py` (its ADR-0139 home; `tests/diagnostics/test_service.py:16` imports it there). `worker_dispatch.py` imports it from `service.py` at module level. The reverse dependency is broken because `service.py` does **not** import `worker_dispatch` at runtime: it references the `WorkerCheckDispatcher` Protocol only under `if TYPE_CHECKING:` (works via `from __future__ import annotations`), and `default_service_factory` imports `JobWorkerCheckDispatcher` **function-locally**. So importing `service` pulls in nothing from `worker_dispatch`, and importing `worker_dispatch` pulls in a fully-loadable `service`. No duplicate constant, no cycle. `WORKER_DISPATCH_BUDGET` lives only in `worker_dispatch` (the dispatcher owns its budget; `service` never sees it, since `run_worker_checks()` takes no deadline).

- [ ] **Step 1: Write the failing tests**

`tests/diagnostics/test_worker_dispatch.py`:

```python
import pytest

from kdive.diagnostics.checks import CheckResult, CheckStatus, GDBSTUB_ACL_ID, PROVIDER_TLS_ID
from kdive.diagnostics.result_codec import serialize_results
from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
from kdive.domain.errors import ErrorCategory
from kdive.domain.state import JobState


class _FakeJob:
    def __init__(self, state, result_ref=None, error_category=None):
        self.state = state
        self.result_ref = result_ref
        self.error_category = error_category


class _FakeQueue:
    """Drives a scripted sequence of job states the dispatcher observes via get_by_dedup_key.

    The fake matches the injected-seam contract exactly:
    `enqueue_fn(dedup_key, payload, authorizing) -> Job` and `get_fn(dedup_key) -> Job | None`.
    """

    def __init__(self, sequence):
        self._sequence = list(sequence)
        self._last = _FakeJob(JobState.QUEUED)
        self.enqueued = None

    async def enqueue(self, dedup_key, payload, authorizing):
        self.enqueued = (dedup_key, payload, authorizing)
        return _FakeJob(JobState.QUEUED)

    async def get_by_dedup_key(self, dedup_key):
        if self._sequence:
            self._last = self._sequence.pop(0)
        return self._last


async def _noop_sleep(_seconds):
    return None


def _dispatcher(queue, *, clock_ticks):
    ticks = iter(clock_ticks)  # increasing values -> the bounded wait terminates deterministically
    return JobWorkerCheckDispatcher(
        pool=None,
        budget=15.0,
        enqueue_fn=queue.enqueue,
        get_fn=queue.get_by_dedup_key,
        clock=lambda: next(ticks),
        sleep_fn=_noop_sleep,
        dedup_suffix="fixed",
    )


async def test_succeeded_returns_real_results():
    out = serialize_results([
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
    ])
    queue = _FakeQueue([_FakeJob(JobState.SUCCEEDED, result_ref=out)])
    results = await _dispatcher(queue, clock_ticks=[0.0, 0.1]).run_worker_checks()
    assert {r.status for r in results} == {CheckStatus.PASS}


async def test_failed_maps_to_error_with_category():
    queue = _FakeQueue([_FakeJob(JobState.FAILED, error_category=ErrorCategory.CONFIGURATION_ERROR)])
    results = await _dispatcher(queue, clock_ticks=[0.0, 0.1]).run_worker_checks()
    assert all(r.status is CheckStatus.ERROR for r in results)
    assert {r.check_id for r in results} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}


async def test_pending_then_budget_exhausted_returns_worker_unavailable():
    queue = _FakeQueue([_FakeJob(JobState.QUEUED)])
    # start clock=0.0; after one pending read, clock=100.0 exceeds budget -> WORKER_UNAVAILABLE
    results = await _dispatcher(queue, clock_ticks=[0.0, 100.0]).run_worker_checks()
    assert all(r.status is CheckStatus.ERROR for r in results)
    assert all("livez" in r.detail for r in results)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_worker_dispatch.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the dispatcher**

`src/kdive/diagnostics/worker_dispatch.py` (sketch — fill from the interfaces; keep `run_worker_checks` ≤100 lines, complexity ≤8):

```python
"""Server-side bounded-wait dispatcher for the worker-vantage diagnostic checks (ADR-0163)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckResult,
    CheckStatus,
)
from kdive.diagnostics.result_codec import ResultCodecError, deserialize_results
from kdive.diagnostics.service import WORKER_UNAVAILABLE_DETAIL  # single home (ADR-0139)
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue as job_queue
from kdive.jobs.payloads import Authorizing, DiagnosticsWorkerCheckPayload

_REMOTE_PROVIDER = "remote-libvirt"
WORKER_DISPATCH_BUDGET = 15.0
_POLL_INTERVAL_S = 0.25
_TRANSPORT_FAILURE = "transport_failure"  # plain label, mirroring checks.py (no ErrorCategory import)
_INTERNAL_ERROR = "internal_error"
_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED}
_WORKER_CHECK_IDS = (PROVIDER_TLS_ID, GDBSTUB_ACL_ID)
_log = logging.getLogger(__name__)

EnqueueFn = Callable[[str, DiagnosticsWorkerCheckPayload, Authorizing], Awaitable[Job]]
GetFn = Callable[[str], Awaitable[Job | None]]


class WorkerCheckDispatcher(Protocol):
    async def run_worker_checks(self) -> list[CheckResult]: ...


def _unavailable(detail: str, category: str) -> list[CheckResult]:
    return [
        CheckResult(check_id=cid, status=CheckStatus.ERROR, detail=detail,
                    provider=_REMOTE_PROVIDER, failure_category=category)
        for cid in _WORKER_CHECK_IDS
    ]


class JobWorkerCheckDispatcher:
    """Enqueues the diagnostics job and bounded-waits for its inline result."""

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        budget: float = WORKER_DISPATCH_BUDGET,
        poll_interval: float = _POLL_INTERVAL_S,
        enqueue_fn: EnqueueFn | None = None,
        get_fn: GetFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dedup_suffix: str | None = None,
    ) -> None:
        self._pool = pool
        self._budget = budget
        self._poll_interval = poll_interval
        self._enqueue = enqueue_fn or self._pool_enqueue
        self._get = get_fn or self._pool_get
        self._clock = clock
        self._sleep = sleep_fn
        self._dedup_suffix = dedup_suffix

    def _require_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("JobWorkerCheckDispatcher needs a pool or an injected seam")
        return self._pool

    async def _pool_enqueue(
        self, dedup_key: str, payload: DiagnosticsWorkerCheckPayload, authorizing: Authorizing
    ) -> Job:
        async with self._require_pool().connection() as conn:
            return await job_queue.enqueue(
                conn, JobKind.DIAGNOSTICS_WORKER_CHECK, payload, authorizing, dedup_key,
                max_attempts=1,
            )

    async def _pool_get(self, dedup_key: str) -> Job | None:
        async with self._require_pool().connection() as conn:
            return await job_queue.get_by_dedup_key(conn, dedup_key)

    def _dedup_key(self) -> str:
        return f"diagnostics:{_REMOTE_PROVIDER}:{self._dedup_suffix or uuid.uuid4()}"

    async def run_worker_checks(self) -> list[CheckResult]:
        dedup_key = self._dedup_key()
        job = await self._enqueue(
            dedup_key,
            DiagnosticsWorkerCheckPayload(provider=_REMOTE_PROVIDER),
            Authorizing(principal="diagnostics", project=_REMOTE_PROVIDER),
        )
        _log.info("diagnostics worker-check job %s enqueued (dedup_key=%s)", job.id, dedup_key)
        start = self._clock()
        while True:
            current = await self._get(dedup_key)
            if current is not None and current.state in _TERMINAL:
                return self._from_terminal(current)
            if self._clock() - start >= self._budget:
                _log.warning("diagnostics job %s not picked up within %ss", dedup_key, self._budget)
                return _unavailable(WORKER_UNAVAILABLE_DETAIL, _TRANSPORT_FAILURE)
            await self._sleep(self._poll_interval)

    def _from_terminal(self, job: Job) -> list[CheckResult]:
        if job.state is JobState.SUCCEEDED:
            try:
                results = deserialize_results(job.result_ref)
            except ResultCodecError as exc:
                _log.error("diagnostics job %s returned a malformed result: %s", job.id, exc)
                return _unavailable("diagnostics worker returned a malformed result", _INTERNAL_ERROR)
            _log.info("diagnostics job %s succeeded", job.id)
            return results
        category = job.error_category.value if job.error_category else _INTERNAL_ERROR
        _log.warning("diagnostics job %s ended %s (%s)", job.id, job.state.value, category)
        return _unavailable("diagnostics worker job failed", category)
```

Notes for the implementer:
- `WORKER_UNAVAILABLE_DETAIL` is imported from `service.py` (single home); do **not** redefine it here.
- The `failure_category` strings are plain labels mirroring `checks.py` (`transport_failure` already used by the reachability check; `internal_error` mirrors `ErrorCategory.INTERNAL_ERROR`). Confirm `ErrorCategory.INTERNAL_ERROR` exists; if it is named differently, use that value verbatim.
- The production `_pool_enqueue`/`_pool_get` open one short pool connection each; the tests inject `enqueue_fn`/`get_fn` so they need no DB.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/diagnostics/test_worker_dispatch.py -q`
Expected: PASS.

- [ ] **Step 5: DB-backed integration test (enqueue→poll→complete once)**

Add `tests/diagnostics/test_worker_dispatch_db.py` that, against the migrated test DB (testcontainers; SKIPs without Docker), enqueues via the real pool, has a stub "worker" `complete` the job with a serialized result, and asserts the dispatcher returns the real results. Mirror an existing DB-backed queue test for the fixture wiring.

- [ ] **Step 6: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/diagnostics/worker_dispatch.py tests/diagnostics/test_worker_dispatch.py tests/diagnostics/test_worker_dispatch_db.py
git commit -m "feat(diagnostics): bounded-wait worker-check dispatcher"
```

---

### Task 7: Service integration

**Files:**
- Modify: `src/kdive/diagnostics/service.py` (`DiagnosticsService.__init__`/`run`, `WORKER_UNAVAILABLE_DETAIL`)
- Test: `tests/diagnostics/test_service.py`

**Interfaces:**
- Consumes: `WorkerCheckDispatcher` from `kdive.diagnostics.worker_dispatch` (TYPE_CHECKING only).
- Produces: `DiagnosticsService(..., worker_dispatcher: "WorkerCheckDispatcher | None" = None)`. When set, `run()` calls `await worker_dispatcher.run_worker_checks()` (no deadline — the dispatcher owns its budget) and appends; the legacy `unavailable_worker_checks` path runs only when `worker_dispatcher is None`.

- [ ] **Step 1: Write the failing tests (merge + composition)**

Add to `tests/diagnostics/test_service.py`:

```python
async def test_dispatcher_results_replace_substitution():
    from kdive.diagnostics.checks import CheckResult, CheckStatus, PROVIDER_TLS_ID
    from kdive.diagnostics.service import DiagnosticsService

    class _FakeDispatcher:
        async def run_worker_checks(self):
            return [CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt")]

    service = DiagnosticsService(
        checks=[], per_check_timeout=1.0, worker_dispatcher=_FakeDispatcher(),
    )
    report = await service.run()
    assert [r.check_id for r in report.results] == [PROVIDER_TLS_ID]
    assert not report.has_error


async def test_server_and_real_worker_results_compose_into_one_verdict():
    # Composition: a server-vantage check + a JobWorkerCheckDispatcher whose job SUCCEEDS with
    # serialized real results -> one verdict carrying both, no substitution (acceptance criterion 1).
    from kdive.diagnostics.checks import (
        CheckResult, CheckStatus, GDBSTUB_ACL_ID, PROVIDER_TLS_ID, SECRET_REF_ID, Vantage,
    )
    from kdive.diagnostics.result_codec import serialize_results
    from kdive.diagnostics.service import DiagnosticsService
    from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
    from kdive.domain.state import JobState

    class _ServerCheck:
        id = SECRET_REF_ID
        vantage = Vantage.SERVER

        async def run(self):
            return CheckResult(SECRET_REF_ID, CheckStatus.PASS, "all refs resolve")

    class _Job:
        def __init__(self, state, result_ref):
            self.id, self.state, self.result_ref, self.error_category = "j", state, result_ref, None

    serialized = serialize_results([
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.FAIL, "blocked", fix="open the ACL",
                    provider="remote-libvirt", failure_category="configuration_error"),
    ])

    async def _enqueue(dedup_key, payload, authorizing):
        return _Job(JobState.QUEUED, None)

    async def _get(dedup_key):
        return _Job(JobState.SUCCEEDED, serialized)

    dispatcher = JobWorkerCheckDispatcher(
        pool=None, enqueue_fn=_enqueue, get_fn=_get, clock=lambda: 0.0, dedup_suffix="x",
    )
    report = await DiagnosticsService(
        checks=[_ServerCheck()], per_check_timeout=1.0, worker_dispatcher=dispatcher,
    ).run()
    by_id = {r.check_id: r for r in report.results}
    assert set(by_id) == {SECRET_REF_ID, PROVIDER_TLS_ID, GDBSTUB_ACL_ID}
    assert by_id[GDBSTUB_ACL_ID].status is CheckStatus.FAIL  # real result, not a substitution
    assert report.has_failure
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_service.py -k "substitution or compose" -q`
Expected: FAIL (`unexpected keyword argument 'worker_dispatcher'`).

- [ ] **Step 3: Implement**

In `service.py`:
- Refine the `WORKER_UNAVAILABLE_DETAIL` constant (its single home) to the saturation-aware wording, keeping `/livez`/`/readyz`: `"worker did not pick up the diagnostic job in time; check that the worker is up (/livez, /readyz) and not saturated"`.
- Add the dispatcher type under TYPE_CHECKING and the constructor arg:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from kdive.diagnostics.worker_dispatch import WorkerCheckDispatcher
```

```python
    def __init__(self, *, ..., worker_dispatcher: "WorkerCheckDispatcher | None" = None) -> None:
        ...
        self._worker_dispatcher = worker_dispatcher
```

- In `run()`, after the server-vantage `results` are gathered, branch:

```python
        if self._worker_dispatcher is not None:
            results.extend(await self._worker_dispatcher.run_worker_checks())
        else:
            results.extend(
                worker_unavailable_results(self._unavailable_worker_checks, self._substitution_reason)
            )
```

Do **not** import `worker_dispatch` at module level (TYPE_CHECKING only) — the dispatcher imports `WORKER_UNAVAILABLE_DETAIL` from here, so a runtime import would be a cycle.

- [ ] **Step 4: Run the full diagnostics suite**

Run: `uv run python -m pytest tests/diagnostics/test_service.py -q`
Expected: PASS (existing `WORKER_UNAVAILABLE_DETAIL` assertions still pass — they check substring membership of the constant, which still contains `/livez`/`/readyz`).

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/diagnostics/service.py tests/diagnostics/test_service.py
git commit -m "feat(diagnostics): wire worker_dispatcher seam into the service"
```

---

### Task 8: Factory + app wiring

**Files:**
- Modify: `src/kdive/diagnostics/service.py` (`default_service_factory` — add `pool`, build dispatcher)
- Modify: `src/kdive/mcp/app.py` (`_register_diagnostics_tools` binds pool into factory; add `_register_diagnostics_handlers` to `_HANDLER_REGISTRARS`)
- Test: `tests/diagnostics/test_default_factory.py`, `tests/mcp/` app-wiring test if present

**Interfaces:**
- Consumes: `JobWorkerCheckDispatcher`.
- Produces: `default_service_factory(provider, *, with_egress=False, pool=None)` — when `pool` is not `None` and `is_remote_libvirt_configured()`, builds the service with `worker_dispatcher=JobWorkerCheckDispatcher(pool)` and **no** `unavailable_worker_checks`; otherwise unchanged (substitution).

- [ ] **Step 1: Write the failing test**

In `tests/diagnostics/test_default_factory.py`:

```python
async def test_factory_with_pool_and_remote_uses_dispatcher(monkeypatch, ...):
    # arrange a configured remote-libvirt inventory (reuse the existing fixture in this file)
    from kdive.diagnostics import service as svc
    built = svc.default_service_factory("remote-libvirt", pool=object())
    assert built._worker_dispatcher is not None
    assert built._unavailable_worker_checks == []
```

(Reuse the file's existing remote-libvirt-configured fixture/monkeypatch; assert the `worker_dispatcher` is wired and the static unavailable list is empty. Add a companion asserting `pool=None` keeps the substitution path.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_default_factory.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the factory + app wiring**

`default_service_factory`: add `pool: AsyncConnectionPool | None = None`. When `is_remote_libvirt_configured()` and `pool is not None`, set `worker_dispatcher = JobWorkerCheckDispatcher(pool)` and do **not** populate `unavailable_worker_checks`; drop `worker_available`/`substitution_reason` for that branch (they only matter to the substitution path). Keep the existing `_remote_libvirt_checks()` server-vantage checks.

`app.py`:
- `_register_diagnostics_tools` → bind the pool with an **explicit closure** (not `functools.partial`, which strict `ty` rejects against the parameterized `ServiceFactory` Protocol):

```python
def _register_diagnostics_tools(app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly) -> None:
    def _service_factory(provider: str | None, *, with_egress: bool = False) -> DiagnosticsService:
        return default_service_factory(provider, with_egress=with_egress, pool=pool)

    ops_diagnostics_tools.register(app, pool, _service_factory)
```

(Match the existing `_register_diagnostics_tools` parameter list in `app.py:184`; it already receives `pool`.)
- Add:

```python
def _register_diagnostics_handlers(
    registry: HandlerRegistry,
    _resolver: ProviderResolver,
    _secret_registry: SecretRegistry,
    _transport_factories: BuildHostTransportFactories | None,
) -> None:
    from kdive.jobs.handlers import diagnostics as diagnostics_handler
    diagnostics_handler.register_handlers(registry)
```

and append `_register_diagnostics_handlers` to `_HANDLER_REGISTRARS`.

- [ ] **Step 4: Run factory + app tests**

Run: `uv run python -m pytest tests/diagnostics/test_default_factory.py tests/mcp -q -k "diagnostic or handler or app"`
Expected: PASS. If a handler-registry meta-test enumerates expected kinds, update it to include `DIAGNOSTICS_WORKER_CHECK`.

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/diagnostics/service.py src/kdive/mcp/app.py tests/diagnostics/test_default_factory.py
git commit -m "feat(diagnostics): wire worker dispatch into the default factory + worker"
```

---

### Task 9: Docs + live OPERATOR-TODO + full gate

**Files:**
- Modify: the operator diagnostics runbook (find it: `rg -l "ops.diagnostics" docs/operating`) — add the live-run OPERATOR-TODO for the worker→host probe.

- [ ] **Step 1: Record the live-run TODO**

Add an OPERATOR-TODO note (matching how reachability/base-image-staging record theirs) that the `provider_tls`/`gdbstub_acl` worker probes are verified live only against real hardware: run `ops.diagnostics --provider remote-libvirt` on the HW-validation host and confirm a real three-state result.

- [ ] **Step 2: Run the FULL local gate**

Run: `just ci`
Expected: PASS (lint, type, lint-shell, lint-workflows, check-mermaid, test). Docker-gated DB tests run in CI; if Docker is local, they run here too.

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs(diagnostics): record live-run OPERATOR-TODO for worker probes"
```

---

## Self-Review notes

- **Spec coverage:** job kind+migration (T1), inline codec (T2), provider_tls direct-TLS (T3), gdbstub_acl heuristic (T4), worker handler (T5), bounded-wait dispatcher with budget + WORKER_UNAVAILABLE + logging (T6), service merge (T7), factory/app wiring + substitution-retained-when-None (T8), live TODO (T9). Substitution table: T7/T8 cover all four states.
- **Type consistency:** `run_worker_checks(*, deadline: float)` used identically in T6/T7. `serialize_results`/`deserialize_results` shared T2/T5/T6. `provider_tls_probe(config, *, connector, context_factory)` and `gdbstub_acl_probe(*, connector)` consistent T3/T4/T5.
- **Open implementer checks flagged inline:** pkipath filenames (T3), import-cycle resolution (T6), `ErrorCategory` value names for substitution (T6), handler-registry meta-test (T8).
