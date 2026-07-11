# Build-host source-kind discovery at the MCP boundary (#536)

- **Status:** Draft
- **Date:** 2026-06-17
- **ADR:** [0160](../adr/0160-buildhost-source-kind-discovery.md)
- **Issue:** #536
- **Builds on (does not supersede):** [ADR-0157](../adr/0157-create-time-build-host-source-check.md)
  (the shared `check_source_kind_compatibility` helper this reuses), [ADR-0099](../adr/0099-remote-build-host-targets.md)
  (§5 fail-closed matrix; the `build_hosts` inventory), [ADR-0124](../adr/0124-provisioning-profile-discoverability.md)
  (`systems.profile_examples`, the sibling discovery pattern this mirrors).

## Problem

The `kernel_source_ref` conventions are learnable only by trial and error. A build
profile's `kernel_source_ref` is interpreted per build-host kind (ADR-0099 §5):

- a `local` host (including the default `worker-local`) accepts a **warm-tree string**
  only — a bare `"linux-6.9"`;
- an `ssh` / `ephemeral_libvirt` host accepts a **git object** only —
  `{"git": {"remote": ..., "ref": ...}}`.

ADR-0157 now rejects an incompatible pairing at `runs.create` (no longer only at
`runs.build`). But the rule is still exposed **nowhere a caller reads before
building**. An agent discovers it by submitting a profile and reading the rejection.

Two read surfaces should advertise the existing rule:

1. `build_hosts.list` (`mcp/tools/ops/build_hosts/lifecycle.py`) returns each host's
   `kind` but no source-kind. A caller sees `kind="ssh"` with no signal that it
   requires a git ref.
2. There is no build-profile examples tool. `systems.profile_examples`
   (`mcp/tools/lifecycle/systems/profile_examples.py`) covers **provisioning**
   profiles only; nothing emits a ready-to-edit **build** profile, and nothing shows
   the warm-tree string form at all.

## Constraint: the advertised rule must be the same rule the validator enforces

The host-kind → accepted-source-kind matrix has exactly one definition today:
`check_source_kind_compatibility` in `services/runs/build_host_selection.py`
(ADR-0157). If `build_hosts.list` re-derived "ssh ⇒ git" inline, the advertisement
and the validator could drift: a future host kind, or a change to which kinds a host
accepts, would update one and not the other, and the tool would advertise a lane the
validator rejects (or vice versa). That is worse than no advertisement — it actively
misleads.

So the advertiser and the validator MUST consume one shared definition of "which
source kinds does this host kind accept." This spec factors that mapping into a pure
function and has both `check_source_kind_compatibility` and the new read surfaces
consume it.

## Design

### The shared source of truth (`services/runs/build_host_selection.py`)

Introduce a closed token set and a pure mapping function, beside
`check_source_kind_compatibility`:

```python
class SourceKind(StrEnum):
    """The two kernel_source_ref provenances a build host can accept (ADR-0099 §5)."""

    WARM_TREE = "warm-tree"
    GIT = "git"


def accepted_source_kinds(host_kind: BuildHostKind) -> tuple[SourceKind, ...]:
    """Return the kernel_source_ref kinds a build host of this kind accepts.

    LOCAL accepts a warm-tree string only; SSH/EPHEMERAL_LIBVIRT accept a git ref
    only. Single source of truth for the ADR-0099 §5 matrix: both the create/build
    compatibility check and the build_hosts.list / runs.profile_examples discovery
    surfaces derive from this one function so the advertised lane can never drift
    from the enforced one.
    """
    if host_kind is BuildHostKind.LOCAL:
        return (SourceKind.WARM_TREE,)
    return (SourceKind.GIT,)
```

`check_source_kind_compatibility` is rewritten to **consume** this function rather
than restate the matrix, preserving its existing error strings, category, and
details byte-for-byte (ADR-0157's create-time and build-time rejections must stay
identical):

```python
def check_source_kind_compatibility(*, host_kind, is_git, build_host) -> None:
    accepted = accepted_source_kinds(host_kind)
    submitted = SourceKind.GIT if is_git else SourceKind.WARM_TREE
    if submitted in accepted:
        return
    if host_kind is BuildHostKind.LOCAL:
        raise CategorizedError(
            "a local build host requires a warm-tree kernel_source_ref, not a git ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": build_host, "host_kind": host_kind.value},
        )
    raise CategorizedError(
        "a remote build host requires a git kernel_source_ref",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"build_host": build_host, "host_kind": host_kind.value},
    )
```

The two branches stay distinct because the two error strings are
host-kind-specific; the matrix (which kind accepts which token) is the part now
single-sourced. A property test pins that for **every** `BuildHostKind`,
`check_source_kind_compatibility` raises iff the submitted kind is absent from
`accepted_source_kinds(host_kind)` — i.e. the validator and the advertiser cannot
disagree.

### Surface 1 — `build_hosts.list` advertises `supported_source_kinds`

`list_build_hosts` already maps each row to a `ToolResponse.success(...)` item. Add
one derived field to each item's `data`:

```python
"supported_source_kinds": [k.value for k in accepted_source_kinds(BuildHostKind(row["kind"]))],
```

- It is a `list[str]`: `["warm-tree"]` for `local`, `["git"]` for `ssh` /
  `ephemeral_libvirt`. (`data` values are JSON-validated; a list of strings is
  allowed — other list-valued fields already appear across the surface.)
- It is derived purely from `kind`, reads no new column, and needs no migration or
  schema change. No new authorization: the field is on the same
  `platform_auditor`-gated read, carries no secret, and exposes only the public rule.
- The field is **always present** (never omitted), so a caller can rely on it for
  every row including the seeded `worker-local`.

### Surface 2 — `runs.profile_examples` (new tool)

A read-only, auth-only discovery tool (modeled on `systems.profile_examples`,
ADR-0124/0117: a valid token gates the transport as defence-in-depth, but there is
no platform/project gate and no audit). Unlike `systems.profile_examples` (which
projects the file-based `systems.toml` inventory), the build-host inventory is in
Postgres, so this tool is **pool-backed**: it reads the `build_hosts` rows the
operator has registered and emits one ready-to-edit build profile per host.

Handler `build_host_profile_examples(hosts: list[BuildHost]) -> ToolResponse` (pure
over a host list, for direct unit testing) returns a `ToolResponse.collection`. Each
item:

- `object_id`: the host name.
- `data.build_host`: the host name.
- `data.host_kind`: the host's `kind` value.
- `data.supported_source_kinds`: `accepted_source_kinds(kind)` rendered as strings —
  the **same** field name and derivation as Surface 1.
- `data.profile`: a `ServerBuildProfile`-parseable document whose
  `kernel_source_ref` matches the host's accepted kind:
  - LOCAL → a warm-tree **string** placeholder, e.g. `"REPLACE_ME-warm-tree-name"`.
  - SSH / EPHEMERAL_LIBVIRT → a git **object** placeholder,
    `{"git": {"remote": "REPLACE_ME-git-remote", "ref": "REPLACE_ME-git-ref"}}`.
  - `build_host` is set to the host name; `source: "server"`, `schema_version: 1`.
- `data.note`: replace every `REPLACE_ME` placeholder before building.

The collection's `suggested_next_actions` point at the build lane: `runs.create`
then `runs.build` (the cold-agent path: edit an example → `runs.create` → `runs.build`).

The pool-bound registrar wrapper resolves the configured pool, lists hosts via a new
`list_all_hosts` repository read (`db/build_hosts.py`, ordered by name — mirrors
`list_probeable_ssh_hosts`). The name is deliberately distinct from the existing
`list_build_hosts` *handler* in `mcp/tools/ops/build_hosts/lifecycle.py` (a tool
handler, not a row reader); the new read follows the repository's verb-noun
convention (`get_by_name`, `list_probeable_ssh_hosts`) so the two names cannot be
confused or imported in place of each other. The wrapper then calls the pure handler.
When **no** operator hosts are registered the repository read still returns at least
the seeded `worker-local` row (`kind='local'`, so its advertised
`supported_source_kinds` is `["warm-tree"]`; it is never deleted — `build_hosts.remove`
rejects it), so the collection is never empty in a migrated database; a defensive
empty-list path still returns a valid empty collection.

The emitted example is **schema-valid as emitted**: each `data.profile` parses via
`BuildProfile.parse` and is compatible with its host (`check_source_kind_compatibility`
does not raise for the host's kind + the example's source kind). That is the
anti-rot guarantee, mirroring `systems.profile_examples`' parse-and-validate test.

### Registration

`runs.profile_examples` is added to the `runs.*` plane registrar
(`mcp/tools/lifecycle/runs/registrar.py`) alongside the existing `runs.*` tools,
`read_only()` annotation, `meta={"maturity": "implemented"}`, bound to the same
pool the other `runs.*` tools use. No new plane, no `app.py` registrar-tuple change.

## Acceptance

- **`build_hosts.list` advertises the lane:** a `local` host's item carries
  `supported_source_kinds == ["warm-tree"]`; an `ssh` host's and an
  `ephemeral_libvirt` host's each carry `["git"]`. The field is present on every
  row, including the seeded `worker-local`.
- **`runs.profile_examples` returns one valid example per registered host:** with a
  `local`, an `ssh`, and an `ephemeral_libvirt` host registered, the collection has
  one item per host (plus `worker-local`); each item's `data.profile` parses via
  `BuildProfile.parse`, and its `kernel_source_ref` is a **string** for the local
  host and a `{"git": {...}}` **object** for the remote hosts.
- **Example source form matches the advertised kind:** for every item,
  `is_git_source(BuildProfile.parse(item.data.profile))` is `True` iff
  `"git" in item.data.supported_source_kinds`. The example never advertises a lane it
  does not itself use.
- **Examples are compatible with their host:** for every item,
  `check_source_kind_compatibility(host_kind=<item kind>, is_git=<example is git>,
  build_host=<name>)` does **not** raise — the emitted example would survive
  `runs.create`/`runs.build`.
- **Advertiser and validator share one source of truth:** a parameterized test over
  **every** `BuildHostKind` asserts `check_source_kind_compatibility` raises iff the
  submitted source kind is absent from `accepted_source_kinds(host_kind)`. They
  cannot drift.
- **`runs.profile_examples` is read-only and auth-only:** it requires a token
  (`current_context()` is invoked) but no platform/project role; it writes no audit
  row and performs no mutation. Its registrar annotation is `read_only`.
- **No regression at the compatibility boundary:** ADR-0157's create-time and
  build-time rejections are byte-identical after the refactor (same message,
  category, details) for every host kind / source kind pairing.
- **Empty/degenerate inputs:** the pure handler over an empty host list returns a
  valid empty collection (`count == "0"`, no items), not an error.

## Out of scope

- Multiple selectable source repos (#530) — a capability change. This advertises the
  existing single-repo convention; #530 would later extend the per-host
  advertisement, not replace it.
- Any change to `build_hosts` schema, columns, or migrations — `supported_source_kinds`
  is derived from `kind`, not stored.
- Changing `kernel_source_ref` interpretation or the ADR-0099 §5 matrix itself — this
  surfaces the rule unchanged.
- A `kdivectl` CLI surface for either tool — the MCP tools are the deliverable.
