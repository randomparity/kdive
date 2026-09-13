# ADR-0284 Phase 3 operator guides design

## Purpose

Finish ADR-0284's unshipped role-gated operator documentation phase without changing tool
authorization. A caller with any platform role already passes the existing doc-exposure gate;
this change gives that gated surface an index and the missing purpose guides.

## Scope and constraints

The frozen #2299/P3 charter authorizes canonical operator docs, their fixed registrations and
snapshots, focused registration/exposure/completeness coverage, and changing ADR-0284 to
Accepted once those artifacts are complete. It excludes auth middleware changes, non-doc
features, and edits to Phase 2 guides except required index integration.

The role gate remains `DocResource.audience="operator"` and the existing middleware remains
the enforcement point. Every new Phase 3 resource uses that audience. Documents link to other
served documents only with their `resource://kdive/docs/...` URI.

## Coverage decision

The live registry's reviewed exposure map is the source of truth. The Phase 3 set is the
operator/admin namespace set not already covered by an investigation guide:

| Namespace | Live elevated surface that requires the guide |
| --- | --- |
| `accounting` | project-admin budget and quota configuration |
| `audit` | project-admin or platform-auditor audit queries |
| `inventory` | platform-auditor inventory reads and platform-admin override clearing |
| `ops` | platform operator, auditor, and admin operations |
| `reports` | platform-auditor all-project reporting branch |
| `secrets` | platform-operator secret-reference listing |
| `shapes` | platform-operator shape and cost-class configuration |

`build_hosts` in the original planning example is not a live namespace and is intentionally
absent. Existing Phase 2 `control`, `images`, `jobs`, `resources`, and `systems` guides already
name their elevated operations as part of their investigation namespaces, so duplicating them
would violate the one-purpose-doc-per-namespace design and the frozen Phase 2 exclusion.
Public `fixtures`, `projects`, `session`, and `tools` have no operator/admin purpose guide.

## Design

Add `agent-index-operator.md` as the role-gated entry point. It introduces the platform-role
boundary, maps the seven derived namespaces to their guides, says that tool schemas remain the
parameter authority, and links only to role-gated operator resources.

Add one canonical purpose guide per derived namespace. Each guide names every live tool in its
namespace, explains its operational purpose and role boundary, and directs the reader to the
tool schema for parameters. `DOC_RESOURCES` registers the index and seven guides from fixed
literal paths with `audience="operator"`; the generator copies them into package snapshots.

Tests assert the complete derived operator resource set, their operator audience and
registration/read-back, middleware exposure to platform-role callers and denial to callers with
no platform role, and that each guide names every live tool in its namespace. Once these
contracts and snapshots are present, ADR-0284's status accurately becomes Accepted.

## Error and security handling

No request-derived path, URI, or authorization decision is introduced. Registration keeps the
existing literal allowlist and packaged-snapshot read path. The existing middleware still fails
closed for `audience="operator"` when authentication cannot establish a platform role. The new
guides disclose only tool names and documented role requirements; they contain no secrets,
operational endpoints, or executable content.

## Threat model

### Boundary inventory

The changed boundary is MCP resource listing and reads: a caller-controlled resource request
reaches the existing `DocExposureMiddleware`, which looks up the literal resource URI's
`audience` in the fixed registry. The change adds eight entries on the already-gated side of
that boundary. Canonical Markdown crosses the build-time snapshot generator into package data;
it is repository-authored, not request supplied.

### Actor model

An authenticated project-only caller and an unauthenticated or malformed-auth caller must not
learn the operator index or its guide URIs. A caller holding any platform role may read the
operator set, consistent with ADR-0284. A repository contributor can author canonical docs but
cannot select runtime paths beyond the reviewed literal allowlist.

### Controls

`DocResource.audience="operator"` is the registration marker for every new entry; the existing
middleware filters list responses and rejects direct reads when `ctx.platform_roles` is empty or
unavailable. The registrar reads only a fixed `content_file` below its package content directory,
and the snapshot and citation guards reject missing content or a link to an unregistered served
resource. Exposure tests cover both list and direct-read denial, so a registration omission or
audience typo is observable before merge.

### Explicitly out of scope

This work does not change token verification, platform-role semantics, tool execution RBAC,
provider gating, or authorization-denial responses. Those controls already govern the boundary;
changing them would exceed the frozen Phase 3 documentation scope.

## Verification

Run the focused doc-resource and exposure tests, regenerate and check snapshots, then run the
repository local gate. Review the diff for literal allowlist entries, role audience values,
link reachability, and the absence of Phase 2 or middleware behavior changes.
