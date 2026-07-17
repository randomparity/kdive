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


_GATEWAY_ON_SURFACE = """\
This server uses a tool gateway. Only a small set of core tools are listed directly
(tools.search and tools.invoke plus a few essentials). All other capabilities are
discoverable via tools.search — pass a short description of what you want to do and
it returns the best matching tool names and descriptions. Once you have a name, call
tools.invoke(name, arguments) to execute it."""

_GATEWAY_OFF_SURFACE = """\
This server exposes its full tool catalog directly: every capability is a first-class
tool, surfaced by lazy-loading hosts as mcp__kdive__* deferred tools. Call tools directly
by name when your host lists them. If a capability you need is not a callable tool in your
client — including lazy-loading hosts that materialize only some of the tools — use the
gateway: tools.search takes a short description of what you want to do and returns the best
matching tool names and schemas, and tools.invoke(name, arguments) executes any registered
tool. tools.search and tools.invoke are always available."""


def build_instructions(gateway_enabled: bool) -> str:
    """Return the server instructions string shown to agents at session start.

    The instructions must match the surface the agent actually sees, which depends on
    whether the core-set tool gateway is enabled (ADR-0268, #1034):

    - Gateway off (the default): the full RBAC catalog is listed directly, so the text
      names the direct ``mcp__kdive__*`` tools as the primary surface and the gateway as
      the fallback for any capability that is not a callable tool in the client — including
      lazy-loading hosts that materialize only a subset of the tools.
    - Gateway on: ``list_tools`` is clipped to the core set, so the text describes the
      ``tools.search`` / ``tools.invoke`` discovery pattern first.

    Both variants end with a namespace table of contents so agents can orient themselves
    without calling ``tools.search`` for every operation.

    Args:
        gateway_enabled: Whether the core-set tool gateway is active for this server
            process (from :func:`kdive.mcp.exposure.gateway_enabled`).
    """
    surface = _GATEWAY_ON_SURFACE if gateway_enabled else _GATEWAY_OFF_SURFACE
    toc_lines = "\n".join(f"  {ns}: {desc}" for ns, desc in sorted(NAMESPACE_TOC.items()))
    return f"""\
{surface}

For a workflow-shaped map of the typical session and a per-toolset guide, read the doc
resource resource://kdive/docs/guide/agent-index.md.

Namespace table of contents (prefix before the first dot):
{toc_lines}
"""


TOOL_KEYWORDS: dict[str, frozenset[str]] = {
    # runs plane — verbs that describe what each step does
    "runs.boot": frozenset({"boot", "kernel", "start", "launch", "built", "load", "power"}),
    "runs.install": frozenset({"install", "modules", "kernel", "load"}),
    "runs.create": frozenset({"create", "run", "investigation", "profile"}),
    "runs.get": frozenset({"get", "run", "status", "fetch", "lookup"}),
    "runs.list": frozenset({"list", "runs", "filter", "paginate"}),
    "runs.cancel": frozenset({"cancel", "stop", "abort", "run"}),
    "runs.bind": frozenset({"bind", "attach", "system", "run"}),
    "runs.complete_build": frozenset({"complete", "finish", "external", "build", "upload"}),
    # jobs
    "jobs.get": frozenset({"job", "status", "get", "fetch", "lookup", "result"}),
    "jobs.list": frozenset({"jobs", "list", "filter", "background", "running"}),
    "jobs.wait": frozenset(
        {
            "job",
            "wait",
            "poll",
            "running",
            "queued",
            "retry",
            "complete",
            "terminal",
            "still",
            "call",
            "again",
            "suggested",
            "next",
            "action",
            "actions",
        }
    ),
    "jobs.cancel": frozenset({"job", "cancel", "stop", "abort", "running"}),
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
    "debug.backtrace": frozenset({"backtrace", "stack", "frames", "call", "trace", "unwind"}),
    "debug.read_frame": frozenset({"frame", "stack", "inspect", "select", "backtrace", "debug"}),
    "debug.disassemble": frozenset(
        {"disassemble", "disasm", "instructions", "asm", "opcodes", "code"}
    ),
    "debug.set_watchpoint": frozenset({"watchpoint", "watch", "write", "monitor", "data", "debug"}),
    "debug.list_watchpoints": frozenset({"watchpoints", "list", "watch", "debug"}),
    "debug.clear_watchpoint": frozenset({"watchpoint", "clear", "remove", "delete", "debug"}),
    "debug.list_modules": frozenset({"modules", "module", "list", "lsmod", "loaded", "debug"}),
    "debug.load_module_symbols": frozenset(
        {"module", "symbols", "load", "add-symbol-file", "ko", "debug"}
    ),
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
    "artifacts.get": frozenset({"artifact", "get", "fetch", "download", "file", "console", "log"}),
    "artifacts.find": frozenset({"artifact", "search", "find", "text", "console", "log"}),
    "artifacts.list": frozenset({"artifacts", "list", "files", "uploads"}),
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
