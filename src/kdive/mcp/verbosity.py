"""Response-verbosity flag: the opt-in compact-envelope switch (ADR-0314, #1035)."""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import COMPACT_RESPONSES


def compact_responses_enabled() -> bool:
    """Return True when KDIVE_COMPACT_RESPONSES is set to on/1/true (default off, ADR-0314).

    Single source of truth for the compact-envelope toggle: CompactResponseMiddleware reads it
    per call to decide whether to omit null/empty defaulted envelope fields, and build_app reads
    it once at assembly to emit the compaction-enabled startup log.
    """
    return (config.get(COMPACT_RESPONSES) or "").strip().lower() in {"on", "1", "true"}
