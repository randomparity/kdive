# Explain partial MCP tool maturity and provider support

- **Status:** Accepted
- **Date:** 2026-06-18
- **ADR:** [ADR-0175](../../adr/0175-partial-tool-maturity-reason.md)
- **Issue:** [#570](https://github.com/randomparity/kdive/issues/570)

## Problem

Every registered MCP tool carries a `maturity` marker in its `meta` dict —
`implemented`, `partial`, or `planned` (ADR-0047). The generated reference
(`scripts/gen_tool_reference.py`) renders the marker as a badge, and
`tests/mcp/core/test_tool_docs.py` only checks that the value is one of the three.

26 tools are marked `partial` (artifacts, control, debug, introspection, postmortem,
runs, systems, vmcore). A black-box agent reading `tools/list` or the reference sees
`partial` but not *why*: whether the limitation is provider support, a live
dependency, an unproven worker path, an operator gate, or degraded/stubbed behavior.
The agent cannot tell a tool that is fully wired but only exercised under the gated
`live_vm`/`live_stack` markers from one whose provider seam is a stub. It plans
against an unexplained marker and either over-trusts or avoids the tool.

## Goals

- Every `partial` tool carries a short machine-readable reason an agent can read from
  `meta` (same channel as `maturity`).
- Provider-dependent partial tools state which providers (local-libvirt,
  remote-libvirt, fault-inject) the path is wired for.
- The generated reference renders the reason and any provider note.
- Tests fail when a `partial` tool lacks the reason metadata.
- Promotion `partial` → `implemented` has a documented, machine-readable bar.

## Non-goals

- No per-provider × per-plane support *matrix* in tool metadata. That state already
  lives in the provider compositions (`supported_capture_methods`, the port
  protocols a provider implements) and drifts if duplicated per tool. The
  provider note is a short free-text pointer, not a parallel source of truth.
- No change to the three maturity values, to the `ToolResponse` envelope, or to any
  runtime behavior. This is metadata + documentation only.
- No migration; no DB change.

## Design

### Metadata shape

Extend the per-tool `meta` dict (today `{"maturity": ...}`) with a nested
`maturity_detail` object, populated **iff** `maturity == "partial"`:

```python
meta = maturity_meta(
    "partial",
    reason=MaturityReason.LIVE_DEPENDENCY,
    detail="Boots the installed kernel through the provider; the install→boot path "
           "is exercised only under the gated live_vm/live_stack markers.",
    promotion="Boot verified by a non-gated test or a recorded live_stack run that "
              "asserts the booted kernel identity.",
    providers="local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.",
)
```

`maturity_meta` is the single constructor in `mcp/tools/_docmeta.py`. It returns the
`meta` dict and enforces the invariants at registration time:

- `maturity == "partial"` requires `reason`, `detail`, and `promotion`; `providers`
  is optional (present only for provider-dependent tools).
- `maturity in {"implemented", "planned"}` rejects any of those kwargs (a non-partial
  tool with a partial reason is a coding error).

`reason` is a closed enum (`MaturityReason`, a `StrEnum`) so the category is
machine-comparable and the vocabulary cannot drift into free text:

| value | meaning |
|---|---|
| `provider_support` | Wired for some providers, not all; see `providers`. |
| `live_dependency` | Real path, but exercised only under gated `live_vm`/`live_stack`. |
| `unproven_worker_path` | Worker job path not yet proven end-to-end. |
| `operator_gate` | Functional but behind an operator/destructive gate. |
| `degraded_stub` | Backed by a stub or placeholder; not production output. |

`detail` and `promotion` are short single-line strings (no `|` or newline — same
table-safety rule the generator already enforces on parameter descriptions).

### Generation

`scripts/gen_tool_reference.py` reads `maturity_detail` off `meta`, carries it on
`ToolDoc`, and renders a dedicated **Maturity** block under the badge for `partial`
tools only:

```
`partial`

**Maturity:** live_dependency — Boots the installed kernel ...
**Promotion:** Boot verified by ...
**Provider support:** local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.
```

The render lives in one function (`_maturity_block`) so the gen-script diff is
localized and a sibling PR touching nested-schema rendering rebases cleanly.

### Enforcement

`tests/mcp/core/test_tool_docs.py` gains:

- `test_partial_tools_carry_a_maturity_reason`: every `partial` tool has a
  `maturity_detail` with a valid `reason`, a non-empty `detail`, and a non-empty
  `promotion`. Fails the build when a new partial tool omits them.
- `test_non_partial_tools_have_no_maturity_detail`: `implemented`/`planned` tools
  carry no `maturity_detail` (catches a stale reason left behind after promotion).
- The generator's own `tool_docs` raises `ValueError` on the same conditions, so
  `just docs-check` (a CI gate) fails independently of the test.

`maturity_meta` raising at registration is the third, earliest layer: `build_app`
runs at import for both the generator and the test, so a malformed registration
fails before either reads the registry.

## Edge cases

- **New partial tool, no reason** → `maturity_meta("partial")` raises `ValueError` at
  registration; the test and generator both fail too.
- **Reason left on a promoted tool** → `maturity_meta("implemented", reason=...)`
  raises; `test_non_partial_tools_have_no_maturity_detail` backstops a hand-built dict.
- **Table-breaking char in `detail`/`promotion`** → generator raises, same rule as
  parameter descriptions.
- **Provider note drift** → accepted risk, bounded by keeping `providers` a short
  pointer, not a matrix; the authoritative provider state stays in the compositions.

## Rollout

One PR: the `_docmeta.py` constructor + enum, the 26 partial registrations migrated to
`maturity_meta`, the generator render, the regenerated reference pages, and the tests.
No migration, no runtime change, reversible by reverting the PR.
