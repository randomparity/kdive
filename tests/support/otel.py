"""The one place a test builds an OTel ``TracerProvider``, with sampling pinned off.

``TracerProvider()``'s default sampler is ``ParentBased(ALWAYS_ON)``, which honors the
sampled flag of whatever context happens to be ambient when a span opens. Under
``-n auto --dist worksteal`` a worker can still have another test's non-sampled parent
context attached when it picks up the next test, and against that default
``start_as_current_span`` records nothing: the SDK returns a ``NonRecordingSpan``, so
``SimpleSpanProcessor`` never sees a span to export and the assertion downstream sees a
span *missing* rather than a sampling decision (#1683, generalized in #1693).

Pinning the sampler to ``ALWAYS_ON`` makes the sampling decision independent of the
ambient context, so a missing span in an assertion means the span was never emitted.
It only ever *keeps* a span the default would have discarded — it cannot invent one, so
it cannot mask a span the code under test failed to open.

Production sampling is deliberately different: ``kdive.observability.facade`` builds
``ParentBased(TraceIdRatioBased(0.1))`` and is not affected by this module. This is a
test-determinism seam only.

``tests/guards/test_pinned_otel_sampler.py`` enforces that no other module under
``tests/`` constructs a ``TracerProvider`` itself, which is what keeps the pin from
drifting back out one file at a time.
"""

from __future__ import annotations

from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context


def tracer_provider() -> TracerProvider:
    """A ``TracerProvider`` whose sampling decision ignores the ambient context.

    Returns:
        A provider with no span processors attached. Callers that assert on exported
        spans add their own, typically
        ``provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))``.
    """
    return TracerProvider(sampler=ALWAYS_ON)


def non_sampled_ambient_context() -> Context:
    """A context carrying a non-recording parent span with the sampled flag unset.

    Stands in for the ambient context a ``--dist worksteal`` worker can still have
    attached when it picks up a test, left behind by whichever test ran on that worker
    beforehand without clearing it. ``trace_flags=TraceFlags.DEFAULT`` is the "not
    sampled" bit, and a ``ParentBased`` sampler propagates that decision to any span
    opened while this context is current — the exact leak #1683 traced.

    Returns:
        A context to ``opentelemetry.context.attach``, for tests that reproduce the
        leak deterministically rather than by re-running the suite and hoping for the
        right interleaving.
    """
    span_context = SpanContext(
        trace_id=0x1,
        span_id=0x1,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),
    )
    return set_span_in_context(NonRecordingSpan(span_context))
