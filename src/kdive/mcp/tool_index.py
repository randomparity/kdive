"""Curated keyword index for ``tools.search`` (ADR-0268, #866).

``TOOL_KEYWORDS`` maps a registered tool name to extra terms that improve lexical
ranking when the tool's name or description alone matches poorly.  Ranking tokenises
name + description + these keywords and counts matches against the caller's query.

Every key **must** be a live registered tool name — the completeness guard in
``tests/mcp/test_tool_index.py`` asserts this so stale entries trip CI.

The namespace TOC (``build_instructions()``) and extra facilities come in a later task.
Only add ``TOOL_KEYWORDS`` here.
"""

from __future__ import annotations

TOOL_KEYWORDS: dict[str, frozenset[str]] = {
    # runs plane — verbs that describe what each step does
    "runs.boot": frozenset({"boot", "kernel", "start", "launch", "built", "load", "power"}),
    "runs.build": frozenset({"build", "compile", "kernel", "modules", "source"}),
    "runs.build_install_boot": frozenset(
        {"build", "install", "boot", "kernel", "composite", "single", "pollable"}
    ),
    "runs.install": frozenset({"install", "modules", "kernel", "load"}),
    "runs.create": frozenset({"create", "run", "investigation", "profile"}),
    "runs.get": frozenset({"get", "run", "status", "fetch", "lookup"}),
    "runs.list": frozenset({"list", "runs", "filter", "paginate"}),
    "runs.cancel": frozenset({"cancel", "stop", "abort", "run"}),
    "runs.bind": frozenset({"bind", "attach", "system", "run"}),
    "runs.complete_build": frozenset({"complete", "finish", "external", "build", "upload"}),
    "runs.validate_profile": frozenset({"validate", "profile", "build", "check", "dry-run"}),
    "runs.profile_examples": frozenset({"profile", "examples", "build", "template", "sample"}),
    # control plane
    "control.force_crash": frozenset({"crash", "force", "panic", "nmi", "kernel", "kdump"}),
    "control.power": frozenset({"power", "on", "off", "reset", "reboot"}),
    # debug plane — introspection vocabulary
    "debug.read_memory": frozenset({"memory", "read", "address", "inspect", "vmem", "peek"}),
    "debug.set_breakpoint": frozenset({"breakpoint", "halt", "pause", "trap", "debug"}),
    "debug.clear_breakpoint": frozenset({"breakpoint", "remove", "delete", "debug"}),
    "debug.list_breakpoints": frozenset({"breakpoints", "list", "debug"}),
    "debug.read_registers": frozenset({"registers", "read", "cpu", "state", "debug"}),
    "debug.resolve_symbol": frozenset({"symbol", "resolve", "address", "lookup", "debug"}),
    "debug.start_session": frozenset({"session", "start", "gdb", "debug", "attach"}),
    "debug.end_session": frozenset({"session", "end", "stop", "gdb", "debug"}),
    "debug.continue": frozenset({"continue", "resume", "run", "debug"}),
    "debug.interrupt": frozenset({"interrupt", "pause", "break", "debug"}),
    "debug.get_session": frozenset({"session", "get", "status", "debug"}),
    "debug.list_sessions": frozenset({"sessions", "list", "debug"}),
    # introspect plane
    "introspect.from_vmcore": frozenset(
        {"vmcore", "crash", "dump", "drgn", "introspect", "postmortem", "offline"}
    ),
    "introspect.run": frozenset({"drgn", "live", "introspect", "kernel", "script", "attach"}),
    "introspect.script": frozenset({"drgn", "script", "live", "introspect", "kernel", "run"}),
    # vmcore plane
    "vmcore.fetch": frozenset({"vmcore", "crash", "dump", "fetch", "download", "retrieve"}),
    "vmcore.list": frozenset({"vmcore", "list", "dumps", "crash"}),
    # postmortem
    "postmortem.crash": frozenset({"postmortem", "crash", "analysis", "vmcore", "triage"}),
    "postmortem.triage": frozenset({"triage", "postmortem", "crash", "analysis"}),
    # allocations
    "allocations.request": frozenset({"allocate", "request", "capacity", "reserve", "system"}),
    "allocations.release": frozenset({"release", "free", "deallocate", "allocation"}),
    "allocations.renew": frozenset({"renew", "extend", "lease", "allocation"}),
    "allocations.wait": frozenset({"wait", "poll", "allocation", "ready"}),
    # artifacts
    "artifacts.get": frozenset({"artifact", "get", "fetch", "download", "file"}),
    "artifacts.list": frozenset({"artifacts", "list", "files", "uploads"}),
    "artifacts.search_text": frozenset({"search", "text", "console", "log", "artifact"}),
    "artifacts.fetch_raw": frozenset({"raw", "fetch", "vmcore", "vmlinux", "download"}),
    "artifacts.create_run_upload": frozenset({"upload", "artifact", "run", "create", "external"}),
    "artifacts.create_system_upload": frozenset({"upload", "artifact", "system", "create"}),
    "artifacts.expected_uploads": frozenset({"expected", "uploads", "contract", "external"}),
    # systems
    "systems.get": frozenset({"system", "get", "status", "fetch"}),
    "systems.list": frozenset({"systems", "list", "filter"}),
    "systems.define": frozenset({"system", "define", "create", "profile"}),
    "systems.provision": frozenset({"system", "provision", "allocate", "create"}),
    "systems.provision_defined": frozenset({"system", "provision", "defined", "named"}),
    "systems.reprovision": frozenset({"system", "reprovision", "rebuild", "refresh"}),
    "systems.teardown": frozenset({"system", "teardown", "destroy", "delete", "remove"}),
    # investigations
    "investigations.open": frozenset({"investigation", "open", "create", "start"}),
    "investigations.close": frozenset({"investigation", "close", "finish", "end"}),
    "investigations.get": frozenset({"investigation", "get", "status", "fetch"}),
    "investigations.list": frozenset({"investigations", "list", "filter"}),
}
