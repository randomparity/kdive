# Plan: `faultinject.set` and `faultinject.reset` (#3684)

Spec: [design](../specs/2026-11-19-faultinject-set-reset-3684-design.md). ADR-0826.

1. Port: add `FAULT_PARAMETER_BOUNDS`, `validate_set_request`, and the `set_knob`/`reset` methods to
   `providers/ports/fault_injection.py`.
2. Provider: give `LocalLibvirtFaultInjection` the two methods; run the helper with argv; map exits;
   parse a one- or two-knob reply. Tests for argv, validation, mapping, bounds-drift.
3. Tools: share the System/SSH/key resolution in `mcp/tools/debug/faultinject.py`; add `set` and
   `reset`; change inspect next actions. Tool tests.
4. Registry: exposure, tool index, docs mirrors, generated verbs, obligations, contract refusals,
   count assertions, test_tool_docs map.
5. Live: system-tool cells; real-hardware proof; record in the live-testing runbook.
