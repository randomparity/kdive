"""Fault-injection mock provider (ADR-0072).

A second :class:`~kdive.providers.core.runtime.ProviderRuntime` that satisfies every typed
provider port with synthetic-but-plausible outputs. The runtime can run as an inert
happy-path provider or with seeded faults and secret-handling probes. It is opt-in and
absent from the default production composition (ADR-0071).
"""
