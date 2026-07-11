# Spec — kdive advises the required external-build artifact format (#769)

- **Issue:** [#769](https://github.com/randomparity/kdive/issues/769)
- **Epic:** [#771](https://github.com/randomparity/kdive/issues/771) — external upload as the default build path.
- **Decision of record:** [ADR-0234 §5](../../adr/0234-external-build-default-and-contributor-role.md).
  This spec tightens the *implementation* of an already-ratified decision; it introduces no new
  decision and reopens nothing from ADR-0234's "Considered & rejected".
- **Depends on:** #766 (unified combined kernel+modules tar) — merged.

## Problem

After #766 there is one provider-neutral build-artifact format, but its byte contract is only
discoverable two ways: by reading source (`build_artifacts/validation.py`) or by failing an upload
and reading the rejection. `artifacts.expected_uploads` advertises accepted *names* plus a one-line
prose `descriptions` map, not the structured format/magic/layout contract. The promoted external
loop (`runs.create(source="external") → artifacts.expected_uploads → upload → runs.complete_build`)
is therefore not fully self-describing over MCP.

## Goal (acceptance)

1. An agent can learn, **from MCP responses alone**, exactly what bytes to produce per artifact name
   and in what format — without reading source or triggering a rejection.
2. The advisory matches the unified format: one combined `kernel` tar (`boot/vmlinuz` bzImage +
   `lib/modules/<release>/`); optional `vmlinux` (requires a matching `build_id`); optional `initrd`;
   conditional `effective_config`. The contract is stated as provider-neutral.
3. The advertised **byte contract** (magic bytes, layout member paths, size caps) cannot drift from
   the validator/admission constants. The required-vs-optional **semantics** are a hand-maintained
   mapping, backed by a behavioral test that exercises the validator (see Test strategy), not a
   constant comparison.

## Design

### A. A single-source-of-truth contract description (`build_artifacts`)

The magic bytes, layout members, and the effective-config size cap already live as constants in
`build_artifacts/validation.py`. Add a public, data-only contract description **in that module**,
built from those same constants, so the validator and the advisory share one source. Shape:

- `ArtifactContract` (frozen dataclass): `name`, `requirement` (`"required" | "optional" |
  "conditional"`), `summary`, `format` (a `FormatContract`), optional `layout`
  (ordered `LayoutMember`s), and optional `notes` (e.g. the `vmlinux → build_id` dependency, the
  `effective_config` config-source pointer). `to_json()` returns a JSON-safe mapping.
- `FormatContract`: `container` (e.g. `"gzip tar"`, `"ELF (uncompressed)"`, `"kernel .config (text)"`)
  and `magic` — a list of `{offset, hex}` taken verbatim from the validator constants
  (`_GZIP_MAGIC`, `_BZIMAGE_MAGIC`/`_BZIMAGE_MAGIC_OFFSET`, `_ELF_MAGIC`). `max_bytes` where a cap
  applies. For `effective_config` the cap is the **upload-admission** cap
  `_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES` (`uploads.py`) — the first gate an upload hits — not the
  later `validation.py` cap; a unit assertion pins the two equal so the choice cannot silently drift.
- `LayoutMember`: `path`, `required`, `note`, and an optional nested `FormatContract`. The member
  `path` is the **drift-checked** value derived from the validator constant — `boot/vmlinuz`
  (`_KERNEL_BOOT_MEMBER`) and `lib/modules/` (`_MODULES_MEMBER_PREFIX`). The human `<release>/`
  subdir hint lives in the member's `note` ("one or more `lib/modules/<release>/` trees"), never in
  the structured `path`, so the drift guard can assert `path` equality against the constants. The
  `boot/vmlinuz` member carries the bzImage `HdrS` magic as its nested `FormatContract`.

`EXTERNAL_BUILD_CONTRACTS: Mapping[str, ArtifactContract]` covers the run build artifacts
(`kernel`, `vmlinux`, `initrd`, `effective_config`), keyed by name. The `requirement` values encode
ADR-0234 §5: `kernel` required; `vmlinux`/`initrd` optional; `effective_config` conditional (on the
Run's `profile_requirements`). The `vmlinux` note states the `build_id` requirement; the
`effective_config` note points at the Kconfig source (the profile's `profile_requirements` symbol
check), satisfying the issue's "point the agent at the right Kconfig source".

`build_id` itself is **not** an uploadable artifact (it is a `runs.complete_build` argument), so it is
surfaced as a `vmlinux` note, not a separate contract entry.

### B. `artifacts.expected_uploads` projects the contracts

Each owner-kind item's `data` gains:

- `contracts`: `{name → ArtifactContract.to_json()}` for that owner kind's accepted names.
- `provider_neutral: true` (run) — the format is one shape across providers (ADR-0234 §2).
- `doc`: `resource://kdive/docs/operating/external-build-upload.md` (the human recipe).

The flat `descriptions` map is **replaced** by the richer `contracts` map (each contract carries a
`summary`), per "replace, don't deprecate". `accepted_names` and `create_tool` are unchanged. The
`system`/`rootfs` owner kind gets a minimal contract (required; `summary`; `format.container =
"filesystem image"`; no magic/layout — rootfs validation is not the combined-tar validator and
enforces no magic at this seam).

### C. Wire `suggested_next_actions` so the loop is self-describing

- `runs.create` with `source="external"` returns
  `["runs.get", "artifacts.expected_uploads", "artifacts.create_run_upload"]` instead of the
  warm-tree `["runs.get", "runs.build"]`. `RunCreateResult` gains an `is_external: bool`, set from
  the already-parsed build profile at construction (no re-parse); `_created_response` branches on it.
  The `server` lane is unchanged.
- `runs.complete_build`'s artifact-validation failure (a format/shape rejection) attaches
  `suggested_next_actions=["artifacts.expected_uploads", "artifacts.create_run_upload"]`, so a
  rejection points the agent back at the contract it must satisfy. Non-validation failures
  (e.g. wrong run state) are unchanged.

## Non-goals

- No change to validation behavior or accepted bytes — advisory only.
- No change to the `rootfs`/system upload contract beyond surfacing it in the new structure.
- No new ADR (ADR-0234 §5 is the decision of record).

## Test strategy

- **Byte-contract drift guard (unit):** assert each projected `FormatContract.magic` hex/offset
  equals the validator's own constants; the `kernel` layout member `path`s equal `_KERNEL_BOOT_MEMBER`
  and `_MODULES_MEMBER_PREFIX` (literal equality — the `<release>` hint is in `note`, not `path`); the
  `effective_config` `max_bytes` equals `_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES`; and
  `_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES == _MAX_EFFECTIVE_CONFIG_BYTES` (the two caps stay in lockstep).
- **Requirement-semantics drift guard (unit):** because `requirement` is hand-encoded, exercise the
  real `validate_external_artifacts` to prove each value: a manifest missing `kernel` is rejected
  (`kernel` is `required`); a manifest with `vmlinux` but no `build_id` is rejected (the `build_id`
  dependency note is real); a manifest with only `kernel` is accepted (`vmlinux`/`initrd` are
  `optional`). This backs the requirement mapping against validator behavior, not a constant.
- **Advisory shape (unit):** `expected_uploads` returns `contracts` for both owner kinds; `kernel`
  is `required` with a gzip magic and the two layout members; `vmlinux`/`initrd` optional;
  `effective_config` conditional with the config-source note and size cap; `provider_neutral` and
  `doc` present on the run item.
- **runs.create wiring (unit):** an external-source create returns the expected-uploads next actions;
  a server-source create still returns `runs.build`.
- **complete_build wiring (unit):** a validation failure carries the expected-uploads next actions; a
  success/non-validation path does not gain them.
- Edge: an accepted name with no contract entry must be impossible — the advisory builds contracts
  for exactly `accepted_names`, asserted by a set-equality test (mirrors the existing
  names-can't-drift test).
