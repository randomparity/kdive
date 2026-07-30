"""The pinned sampler survives a leaked non-sampled ambient context; the default does not.

Both halves matter. Asserting only that the pinned provider exports the span would pass
just as well against a mis-built leak context that was never non-sampled at all — the
assertion would be green for the wrong reason and would stop guarding anything the day
someone reverted the pin. The paired default-provider assertion is what proves the leak
this module constructs is the real #1683 mechanism, so the pinned half is load-bearing.
"""

from __future__ import annotations

from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.support.otel import non_sampled_ambient_context, tracer_provider


def _span_names_under_the_leak(provider: TracerProvider) -> list[str]:
    """Open one span under a leaked non-sampled parent and return what ``provider`` exported."""
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("kdive.test.otel.support")

    token = otel_context.attach(non_sampled_ambient_context())
    try:
        with tracer.start_as_current_span("probe"):
            pass
    finally:
        otel_context.detach(token)

    return [span.name for span in exporter.get_finished_spans()]


def test_pinned_provider_exports_under_a_leaked_non_sampled_context() -> None:
    assert _span_names_under_the_leak(tracer_provider()) == ["probe"]


def test_unpinned_provider_drops_the_same_span() -> None:
    """The defect the pin removes, proven rather than described.

    Constructs the one ``TracerProvider()`` that ``tests/guards/test_pinned_otel_sampler.py``
    allowlists outside :mod:`tests.support.otel`: showing the default sampler *does* discard
    the span is the only way to know the sibling assertion above is not vacuous.
    """
    assert _span_names_under_the_leak(TracerProvider()) == []


def test_the_leak_is_what_makes_the_default_drop_it() -> None:
    """Control: with no leaked context attached, the default provider exports normally.

    Without this, ``test_unpinned_provider_drops_the_same_span`` would also pass if the
    default provider dropped *every* span for some unrelated reason, which would make the
    pin look necessary when it was not.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("kdive.test.otel.support").start_as_current_span("probe"):
        pass
    assert [span.name for span in exporter.get_finished_spans()] == ["probe"]
