"""Cover the shared MCP-middleware helpers."""

from __future__ import annotations

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.shared import ToolOutcome, request_context, result_error_category
from kdive.mcp.responses import ToolResponse


def test_tool_outcome_values() -> None:
    assert ToolOutcome.OK.value == "ok"
    assert ToolOutcome.ERROR.value == "error"
    assert ToolOutcome.DENIED.value == "denied"


def test_result_error_category_reads_tool_response_failure() -> None:
    resp = ToolResponse.failure("runs.create", ErrorCategory.CONFIGURATION_ERROR)
    assert result_error_category(resp) == ErrorCategory.CONFIGURATION_ERROR.value


def test_result_error_category_none_for_successful_tool_response() -> None:
    resp = ToolResponse.success("runs.create", "created")
    assert result_error_category(resp) is None


class _Structured:
    def __init__(self, structured_content: object) -> None:
        self.structured_content = structured_content


def test_result_error_category_reads_structured_content_string() -> None:
    result = _Structured({"error_category": "configuration_error"})
    assert result_error_category(result) == "configuration_error"


def test_result_error_category_none_when_structured_category_absent() -> None:
    assert result_error_category(_Structured({"object_id": "x"})) is None


def test_result_error_category_none_when_structured_category_not_a_string() -> None:
    # a non-str category must not leak through as a category
    assert result_error_category(_Structured({"error_category": 500})) is None


def test_result_error_category_none_when_structured_content_not_a_dict() -> None:
    assert result_error_category(_Structured("not-a-dict")) is None


def test_result_error_category_none_for_plain_object() -> None:
    assert result_error_category(object()) is None


def test_request_context_resolves_through_shared_patch_point(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr("kdive.mcp.middleware.shared.current_context", lambda: sentinel)
    assert request_context() is sentinel
