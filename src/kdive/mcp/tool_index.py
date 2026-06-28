"""Curated keyword index for ``tools.search`` and server instructions (ADR-0268, #866).

``TOOL_KEYWORDS`` maps a registered tool name to extra terms that improve lexical
ranking when the tool's name or description alone matches poorly.  Ranking tokenises
name + description + these keywords and counts matches against the caller's query.

Every key **must** be a live registered tool name — the completeness guard in
``tests/mcp/test_tool_index.py`` asserts this so stale entries trip CI.

``NAMESPACE_TOC`` maps each live tool namespace (the prefix before the first ``.``)
to a one-line description.  ``build_instructions()`` renders these into the server
``instructions`` string that an agent receives at session start.
"""

from __future__ import annotations

# One-line description for every live tool namespace.  The completeness guard in
# ``tests/mcp/test_tool_index.py`` (test_instructions_cover_every_live_namespace)
# asserts that every namespace present in the live registry appears here.
NAMESPACE_TOC: dict[str, str] = {
    "accounting": "Budget, quota, usage estimates, and cost reporting",
    "allocations": "System capacity reservation, leasing, and release",
    "artifacts": "Run artifact access, uploads, and raw binary retrieval",
    "audit": "Audit log queries across operations",
    "build_envs": "Available kernel build environment listing",
    "build_hosts": "Build host registration, listing, and removal",
    "buildconfig": "Kernel build configuration management",
    "control": "In-guest power cycling and crash injection (NMI / panic)",
    "debug": "Live GDB-based kernel debugging sessions (breakpoints, registers, memory)",
    "fixtures": "Test fixture profile listing and validation",
    "images": "Kernel and rootfs image lifecycle (build, publish, expire)",
    "introspect": "drgn kernel introspection — live attach and vmcore offline analysis",
    "inventory": "Resource inventory listing and override management",
    "investigations": "Investigation lifecycle tracking (open, link, close)",
    "jobs": "Background job status polling and cancellation",
    "ops": "Platform operator tools (reconciliation, diagnostics, cost classes)",
    "postmortem": "Crash analysis and triage from vmcore or console evidence",
    "projects": "Project listing",
    "reports": "Generated usage and accounting report retrieval",
    "resources": "Physical resource registration, availability, and cordon/drain",
    "runs": "Kernel test run lifecycle (build, install, boot, cancel, bind)",
    "secrets": "Secret listing",  # pragma: allowlist secret
    "session": "Session identity (whoami)",
    "shapes": "System shape and cost-class configuration",
    "systems": "Target system provisioning, reprovision, teardown, and profiling",
    "tools": "Tool discovery gateway — search capabilities and invoke any registered tool",
    "vmcore": "Crash dump listing and download",
}


def build_instructions() -> str:
    """Return the server instructions string shown to agents at session start.

    The string has two parts:
    1. A gateway usage pattern paragraph explaining that only ``tools.search`` and
       ``tools.invoke`` are exposed by default and how to use them.
    2. A namespace table of contents so agents can orient themselves without calling
       ``tools.search`` for every operation.
    """
    toc_lines = "\n".join(f"  {ns}: {desc}" for ns, desc in sorted(NAMESPACE_TOC.items()))
    return f"""\
This server uses a tool gateway. Only a small set of core tools are listed directly
(tools.search and tools.invoke plus a few essentials). All other capabilities are
discoverable via tools.search — pass a short description of what you want to do and
it returns the best matching tool names and descriptions. Once you have a name, call
tools.invoke(name, arguments) to execute it.

Namespace table of contents (prefix before the first dot):
{toc_lines}
"""


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
