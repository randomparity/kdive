"""Search ranking + namespace table-of-contents for the tool gateway (ADR-0267, #866).

Two cheap, pure data maps plus a deterministic lexical ranker, kept here as the single
reviewed source the way ``mcp/exposure.py`` keeps ``_TOOL_SCOPES``:

- :data:`TOOL_KEYWORDS` — curated synonyms per tool, so an intent phrase ("power on the vm")
  finds the tool whose name/description does not literally contain it. A tool absent from the
  map still ranks on its tokenised name + description.
- :data:`NAMESPACE_TOC` — every tool namespace → one-line summary, rendered into the server
  ``instructions`` so an agent knows a capability *exists* and is worth searching for even
  though the flat catalog is hidden (the "ambient map" a small ``list_tools`` would otherwise
  lose).

:func:`rank_tools` is deterministic (lexicographic tie-break) so ``tools.search`` is testable
and adds no latency — no embeddings (ADR-0267 rejected them).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_TOKEN = re.compile(r"[a-z0-9]+")
# Dropped from a query before matching: too common to discriminate, and they inflate scores.
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "to", "for", "on", "in", "and", "or", "my", "me", "is", "it"}
)

#: Curated query synonyms per tool. Keep terms lowercase; multi-word phrases are fine (a query
#: token substring-matches them). Absent tools fall back to tokenised name + description.
TOOL_KEYWORDS: dict[str, frozenset[str]] = {
    "runs.build_install_boot": frozenset(
        {"reproduce", "build install boot", "one shot", "boot a kernel", "crash", "end to end"}
    ),
    "runs.build": frozenset({"compile", "kernel", "tree", "make"}),
    "runs.install": frozenset({"deploy", "kernel", "modules", "lay down"}),
    "runs.boot": frozenset({"boot", "power", "power on", "start", "vm", "console", "kernel"}),
    "runs.create": frozenset({"new run", "start run", "reproduce"}),
    "debug.start_session": frozenset({"debugger", "gdb", "attach", "live"}),
    "debug.read_memory": frozenset({"peek", "dump memory", "inspect"}),
    "control.power": frozenset({"power", "reset", "shutdown", "reboot"}),
    "control.force_crash": frozenset({"panic", "crash", "sysrq"}),
    "artifacts.search_text": frozenset({"grep", "console", "log", "find text"}),
}

#: Every tool namespace → one-line summary. The T5 guard pins this to the live registry.
NAMESPACE_TOC: dict[str, str] = {
    "accounting": "budgets, quotas, usage, and cost estimates",
    "allocations": "request, renew, and release capacity reservations",
    "artifacts": "upload, list, fetch, and search Run/System artifacts",
    "audit": "query the platform audit log",
    "build_envs": "list contributor-visible build environments",
    "build_hosts": "register and manage build hosts (platform admin)",
    "buildconfig": "declarative build configuration",
    "control": "power and crash control of provisioned Systems",
    "debug": "live kernel debugging — breakpoints, memory, registers",
    "fixtures": "list and validate fault-injection fixtures",
    "images": "describe and list base images",
    "introspect": "run drgn/script introspection over live or vmcore targets",
    "inventory": "list the static resource inventory",
    "investigations": "open, link, and manage debugging investigations",
    "jobs": "wait on, get, list, and cancel async jobs",
    "ops": "operator force-release and force-teardown overrides",
    "postmortem": "crash triage and postmortem analysis",
    "projects": "list accessible projects",
    "reports": "generate usage reports",
    "resources": "describe, cordon, drain, and set status of resources",
    "runs": "create, build, install, boot, and read kernel Runs",
    "secrets": "manage stored secrets (operator)",  # pragma: allowlist secret
    "session": "identity and orientation (whoami)",
    "shapes": "list resource shapes",
    "systems": "define, provision, and bind Systems",
    "tools": "search the full tool surface by capability (the gateway)",
    "vmcore": "manage vmcore introspection targets",
}

GATEWAY_INSTRUCTIONS = (
    "Not every tool appears in this list. The default catalog is a small core set; the full "
    "tool surface is reachable on demand. Call `tools.search` with a capability phrase (e.g. "
    '"boot a kernel", "read guest memory") to load the matching tools\' full schemas, then call '
    "the returned tool directly by name. The namespaces below summarise what is searchable."
)


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens with stopwords removed."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def _haystack(name: str, description: str) -> str:
    """The text a query is matched against for one tool: name, description, curated keywords."""
    parts = [name.lower(), description.lower(), *TOOL_KEYWORDS.get(name, frozenset())]
    return " ".join(parts)


def rank_tools(query: str, candidates: Iterable[tuple[str, str]], *, limit: int) -> list[str]:
    """Rank ``(name, description)`` candidates against ``query``; return up to ``limit`` names.

    Deterministic lexical match: a candidate scores by how many distinct query tokens appear in
    its name + description + :data:`TOOL_KEYWORDS`. Zero-score candidates are dropped (the
    search-miss signal). Ties break lexicographically by tool name so truncation is stable.
    An empty/whitespace query matches nothing.
    """
    terms = set(_tokens(query))
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    for name, description in candidates:
        haystack = _haystack(name, description)
        score = sum(1 for term in terms if term in haystack)
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:limit]]


def render_instructions() -> str:
    """Compose the server ``instructions``: gateway preamble + the namespace TOC."""
    lines = [GATEWAY_INSTRUCTIONS, ""]
    for namespace in sorted(NAMESPACE_TOC):
        lines.append(f"- {namespace}.* — {NAMESPACE_TOC[namespace]}")
    return "\n".join(lines)
