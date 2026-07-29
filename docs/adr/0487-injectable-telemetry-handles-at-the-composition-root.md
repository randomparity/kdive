# ADR 0487 — `build_app` takes its tracer and meter as arguments, defaulting to the process globals

- **Status:** Accepted
- **Date:** 2026-07-29
- **Issue:** #1640

## Context

`TelemetryMiddleware` (`mcp/middleware/telemetry.py`) emits one `mcp.tool/<name>` span and
three RED instruments per tool call, and skips `REENTRANT_TOOLS` so the `tools.invoke`
gateway's outer chain does not record the dispatcher alongside the inner tool it re-enters
([ADR-0485](0485-purpose-keyed-middleware-skip-sets.md) §2, [ADR-0268](0268-tool-gateway-dispatcher.md) §6). It takes both handles by constructor argument, but
`mcp/assembly/app.py` supplied them from the process globals and nothing else:

```python
TelemetryMiddleware(tracer=trace.get_tracer("kdive.mcp"), meter=metrics.get_meter("kdive.mcp"))
```

So the skip had no end-to-end pin, and ADR-0485 §3 recorded that limit and named #1640 as
the issue that would close it. Deleting the skip left the whole suite green: the two
existing real-gateway tests
(`tests/mcp/tools/test_gateway_usage_recording_e2e.py`) assert `tool_invocation` and
`audit_log` rows, and a telemetry double-count lands in neither — it appears as a spurious
`mcp.tool/tools.invoke` span and a second point on each of `kdive.mcp.requests` and
`kdive.mcp.request.duration`. The unit tests in
`tests/mcp/middleware/test_gateway_skip.py` do redden on that deletion, but they construct
`TelemetryMiddleware` themselves over a fake tracer and meter and drive `on_call_tool`
directly, so they cannot observe how `build_app` wires the middleware — which is exactly the
composition-level failure at stake, since the gateway's re-entry only exists inside an
assembled app.

The obvious test-only route is to install in-memory SDK providers globally and drive a real
`build_app`. It does not work here:

- `trace.set_tracer_provider` / `metrics.set_meter_provider` are **set-once per process**.
  The second call logs a warning and keeps the first provider, so a test that installs one
  after any other code has (including `init_telemetry`) silently observes nothing.
- `just test` runs `-n auto --dist worksteal`. Each xdist worker is one process running many
  test modules, so a global install is not scoped to the test that made it: every span any
  other test in that worker emits lands in the same exporter, and worksteal makes which
  tests those are non-deterministic between runs.

Monkeypatching `opentelemetry.trace.get_tracer` for the duration of one test does work —
tests within a worker are serial, and `monkeypatch` undoes it — but it reaches past the
composition root into a vendor module and pins the test to the exact call `app.py` happens to
make, rather than to the fact that the app's telemetry is configurable at all.

## Decision

`build_app` gains two keyword-only, optional parameters that default to the process globals:

```python
def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_composition: ProviderComposition | None = None,
    secret_registry: SecretRegistry,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
) -> FastMCP: ...
```

with `tracer or trace.get_tracer("kdive.mcp")` and `meter or metrics.get_meter("kdive.mcp")`
at the one construction site. Every production caller — `processes/server.py` and the
script/test callers that build an app to introspect its tool surface — is unchanged and keeps
reading the globals the observability facade ([ADR-0090](0090-opentelemetry-adoption-service-health.md)) installs.

This is the same seam `verifier` already is, for the same reason. `verifier` is optional so a
test can build an app around a keypair it minted and have token verification really run,
rather than patching auth state the whole process shares; `tracer` and `meter` make an app's
telemetry observable on identical terms. Three of `build_app`'s five arguments are now
"the real thing by default, supplied by the caller when the caller needs to see it", which is
a consistent shape for a composition root rather than a new one.

**Two handles, not a provider pair.** `TelemetryMiddleware` takes a tracer and a meter; a
`TracerProvider`/`MeterProvider` pair would be a wider type that `build_app` would only ever
call `get_tracer`/`get_meter` on, and would additionally fix the instrumentation-scope name
inside `build_app` where a caller cannot see it. The narrow handles match what is consumed.

**Injection, not construction control.** The parameters do not let a caller replace
`TelemetryMiddleware`, reorder the chain, or opt out of telemetry. The middleware is still
constructed here, still added at the same position, and a caller that passes nothing gets
byte-identical behaviour to before.

### What this pins

`tests/mcp/tools/test_gateway_usage_recording_e2e.py` now scopes an `InMemorySpanExporter`
and an `InMemoryMetricReader` to one `build_app` call through the seam, and asserts the exact
span-name list and the exact set of metric points for a real `tools.invoke` dispatch on both
its success and its denial arm. Deleting `telemetry.py`'s skip reddens both. The same tests
drive a real `tools.search`, which closes ADR-0485 §3's other stated limit: that the assembled
app wires the middleware such that a real search call is traced and metered was true but
unproven end to end.

The local `TracerProvider` those tests build uses the SDK's default sampler rather than the
facade's `ParentBased(TraceIdRatioBased(0.1))`, so an absent span is a regression rather than
a 1-in-10 sampling outcome. That is a property of the test's provider, not of this decision.

## Consequences

- **No behaviour change in any process.** Both defaults are the previous expressions, so a
  caller that passes neither argument gets the app it got before. No config setting, no
  environment variable, no schema, migration, MCP tool, or RBAC surface change.
- **`build_app`'s signature is public API within the repo.** It gains two optional keyword
  arguments; nothing is removed or renamed, so every existing call site compiles unchanged.
- **The telemetry skip is now falsifiable end to end,** on the one recording plane where a
  regression never touches Postgres and therefore never touched an assertion.
- **A future middleware that needs its own OTel handle does not get a third parameter by
  default.** If a second consumer appears, the right move is to reconsider passing the
  providers (or a small telemetry bundle) once, not to keep appending handles. Recording that
  here so the next such change is a decision rather than a drift.

## Rejected alternatives

- **Install in-memory providers globally in a fixture.** The set-once semantics and the
  shared xdist worker process make it both unreliable (a silent no-op after the first
  installer wins) and leaky (every other test's spans pool into the assertion). A
  session-scoped global provider plus per-test exporter swapping would work only if no other
  test in the process ever emitted a span, which is not a property the suite can hold.
- **Monkeypatch `opentelemetry.trace.get_tracer` / `metrics.get_meter`.** Needs no
  production change, and is safe under worksteal because a worker runs its tests serially.
  Rejected because it asserts against a vendor module's function rather than against the
  app's own composition, so it silently stops applying the moment `app.py` obtains its
  handles any other way — and because "this app's telemetry is configurable" is a property
  worth having in the signature rather than reconstructing at each test.
- **Accept a `TelemetryMiddleware` instance.** One parameter instead of two, but it hands the
  caller the whole middleware — its position in the chain is the only thing `build_app` would
  still own — and it moves the default construction into a `or TelemetryMiddleware(...)`
  expression that reads as though the middleware were optional. It is not.
- **A module-level factory hook** (`app._tracer_factory`, patched by tests). Same
  observability with none of the visibility: the seam exists but is invisible at the call
  site, which is the property that makes it decay.
- **Leave the skip unpinned and rely on the unit tests.** The status quo ADR-0485 §3 recorded
  as a limit. The unit tests prove the middleware's guard; they cannot prove the assembled
  app routes a gateway re-entry through exactly one instance of it, and the re-entry is the
  entire mechanism the skip exists for.
