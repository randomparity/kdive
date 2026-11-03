"""ADR-0014: an authority rejection's category and reason reach the JSON log (#3391)."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from kdive import log as klog
from tests.providers.external_boot_authority.service_support import _service


def test_a_rejection_warning_carries_its_category_and_reason(tmp_path: Path) -> None:
    service, *_rest = _service(tmp_path)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(klog.JsonFormatter())
    logger = logging.getLogger("kdive.providers.external_boot_authority.service")
    logger.addHandler(handler)
    try:
        service._reject("journal_conflict", reason="lane_failed_lifecycle")
    finally:
        logger.removeHandler(handler)
    payload = json.loads(stream.getvalue().splitlines()[-1])
    assert payload["msg"] == "authority request rejected"
    assert payload["category"] == "journal_conflict"
    assert payload["reason"] == "lane_failed_lifecycle"
