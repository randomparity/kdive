# Python 3.15 lock bumps and zlib-independent size baseline (#3658) — design

## Problem

The Python 3.15 informational lane (ADR-0817, #3600) cannot start the suite, and one test
fails once it does:

- `beartype` 0.22.9 (transitive, via `fastmcp`) imports `typing.no_type_check_decorator`,
  which CPython 3.15 removed, so `tests/conftest.py` fails to import.
- `cffi` 2.0.0, `grpcio` 1.81.0 and `markupsafe` 3.0.3 have no cp315 wheel and build from
  source in the lane.
- `test_external_boot_evidence_matches_pre_stream_guard_baseline`
  (`tests/providers/local_libvirt/test_validate_external_artifacts.py`) hard-codes
  `archive_uncompressed_bytes: 1128` and `vmlinuz_size_bytes: 1121`; on 3.15 the values are
  1130 and 1123.

Cause of the size difference, measured on this host with `uv run --no-project --python <v>`
over the fixture's 221-byte uncompressed kernel ELF:

| Interpreter | `zlib.ZLIB_VERSION` | `len(gzip.compress(kernel))` |
|---|---|---|
| CPython 3.14.8 (uv-managed) | `1.3.1.zlib-ng` | 97 |
| CPython 3.15.2 (uv-managed) | `1.3.2` | 99 |

`gzip.compress` defaults to level 9 and a fixed-width mtime field, so neither `mtime` nor
`compresslevel` makes the output portable: the deflate stream itself differs between zlib-ng
and stock zlib. `vmlinuz_size_bytes` is `0x400 + len(gzip.compress(kernel))` and
`archive_uncompressed_bytes` is that plus the 7-byte module (`b"\x7fELFmod"`) and the empty
`modules.dep`.

## Design

**(a) Test.** Replace the two literals with values derived from the fixture inputs:
`len(_BZIMAGE_BODY)` and `len(_BZIMAGE_BODY) + len(b"\x7fELFmod")` — the same derivation
`test_external_boot_raw_padded_byte_ceiling_includes_tar_framing` already uses. Every other
field of the evidence dict stays a literal, so the test still pins the pre-stream guard
evidence shape and values; the two sizes are pinned to the real member sizes instead of to
one zlib build's output. `decoded_kernel_size_bytes: 221` is uncompressed and stays literal.

**(b) Lock.** `uv lock --upgrade-package <pkg>==<ver>` for exactly:

| Package | From | To | First upload (UTC) | cp315 x86_64 wheel |
|---|---|---|---|---|
| beartype | 0.22.9 | 0.23.1 | 2026-10-11 | `py3-none-any` |
| cffi | 2.0.0 | 2.1.1 | 2026-08-03 | manylinux2014 |
| grpcio | 1.81.0 | 1.85.1 | 2026-10-30 | manylinux2014 |
| markupsafe | 3.0.3 | 3.0.4 | 2026-10-02 | manylinux2014/2_28 |

Each is the newest release on PyPI (queried 2026-11-16) and every file was uploaded more than
seven days before 2026-11-16, inside the lane's `UV_EXCLUDE_NEWER="7 days"` and the
Dependabot cooldown. `pyproject.toml` does not change; all four are within existing
constraints. `pyyaml` has no cp315 wheel at any release and stays source-built in the lane.

## Failure model

1. **Actors and deployments** — developers and CI running the suite on CPython 3.14 (gating)
   and the 3.15 informational lane; release images install from the same `uv.lock`.
2. **Invariants and assets at stake** — the pre-stream guard evidence contract (field set and
   values) stays pinned; the gating 3.14 lane stays green; `uv.lock` resolves the same set of
   packages with no movement beyond the four named.
3. **Accepted failure classes** — other 3.15-only test failures the lane may reveal once the
   suite starts: reported from the lane run, not fixed here (the lane is non-gating).
4. **Covered elsewhere** — direct pins `psycopg`, `pydantic`, `drgn` and the other bumps:
   #3567 (Dependabot). Adopting 3.15 in CI or images: out of scope.

### Threat model

1. **Boundary inventory** — widened: four third-party packages from PyPI enter the locked
   environment at new versions. Added: none.
2. **Actor model** — the upstream package publishers and PyPI; trust stays where `uv.lock`
   already places it (hash-pinned artifacts from the PyPI registry).
3. **Control per boundary** — `uv.lock` sha256 hashes per artifact; seven-day release-age
   cooldown; `uv sync --locked` in every lane; `just ci` and the 3.15 lane exercise the result.
4. **Explicitly out of scope** — auditing upstream source changes in the four releases
   beyond their changelogs: accepted, the same risk Dependabot bumps carry.

## Testing

- Focused: the baseline test passes on 3.14 (`just test-verbose <file>`), and the same file
  passes on 3.15 in the PR's Python 3.15 lane.
- Bites: with the derived expressions, temporarily asserting `len(_BZIMAGE_BODY) + 1` turns
  the test red.
- `tests/test_dependency_policy.py` stays green; full `just ci` via the pre-push hook.

## Alternatives considered

- **Fix `mtime`/`compresslevel`.** verified: the table above — sizes differ at the default
  level with the mtime field fixed-width; the deflate stream differs.
- **Embed a pre-compressed gzip literal in the fixture.** judgment: freezes an opaque blob
  for a test whose subject is the evidence fields, not the compressor.
