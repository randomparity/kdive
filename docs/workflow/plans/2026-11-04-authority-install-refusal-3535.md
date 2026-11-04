# Authority install refusal implementation plan (#3535)

**Goal:** a guest free-space refusal on the local authority path ends the job on that attempt with
`install_failure` and the three sizes in `failure_context.install_refusal`.

**Architecture:** one typed provider exception maps to one closed numeric error frame. The sender
turns it into a terminal `CategorizedError`, the runner copies the sizes into the closed failure
context, and migration 0193 admits the field. See
[the spec](../specs/2026-11-04-authority-install-refusal-3535-design.md) and
[ADR-0814](../../adr/0814-a-guest-free-space-refusal-reaches-the-job-from-the-authority.md).

**Tech stack:** Python 3.14, pydantic, PostgreSQL plpgsql, pytest.

Expected implementation size: 150–230 changed lines (M) — source and SQL in the four tasks below, excluding tests and these design artifacts.

## Global Constraints

- Assigned numbers are exact: `docs/adr/0814-*.md`=0814, `src/kdive/db/schema/0193_*.sql`=0193.
- No free-form text in the frame. No new dependency.
- Guardrails: `just lint`, `just type`, `just test-changed`, `prek run`; full gate
  `just ci > <file> 2>&1 < /dev/null`, run in bash after `git fetch origin main`.
- Other provider exceptions keep their mapping. `install-failure` stays out of `_PEER_REASONS`.

## File map

| File | Owns after this change |
|------|------------------------|
| `src/kdive/providers/local_libvirt/lifecycle/boot/guest_free_space.py` | `GuestFreeSpaceRefusal` raised on a shortfall |
| `src/kdive/providers/external_boot_authority/protocol.py` | `AuthorityInstallRefusalV1` |
| `src/kdive/providers/external_boot_authority/service.py` | the category, its pairing rule, the refusal gate |
| `src/kdive/providers/external_boot_authority/transport.py` | `_service_error` |
| `src/kdive/providers/local_libvirt/external_boot_authority.py` | the `_apply` mapping |
| `src/kdive/jobs/authority_sender.py` | frame decode, `_install_failure` |
| `src/kdive/jobs/models.py` | `failure_context.install_refusal` |
| `src/kdive/jobs/handlers/external_boot/runner.py` | sizes into the failure context |
| `src/kdive/db/schema/0193_external_boot_install_refusal.sql` | field list and helper |

No caller migration is needed, no obsolete path is removed, and no compatibility path is retained.

## Task 1: closed wire model and typed refusal

**Interfaces:** provides `AuthorityInstallRefusalV1(vmlinux_bytes, free_bytes, headroom_bytes)` and
`GuestFreeSpaceRefusal`, whose `details` keep the existing five keys.

**Verification**

- Wire model bounds. Mode: focused-test. `tests/providers/external_boot_authority/test_install_refusal.py`;
  red: `ImportError` on the model; green: `uv run python -m pytest tests/providers/external_boot_authority/test_install_refusal.py -q`.
- Refusal type. Mode: focused-test. `tests/providers/local_libvirt/lifecycle/boot/test_stage_vmlinux_free_space.py`;
  red: the shortfall is not a `GuestFreeSpaceRefusal`; green: run that file. The implausible raise is
  asserted not to be the subclass.

Steps:

1. Write the tests: accepted triple; refused bool, float, string, negative, 2^63, extra key, missing
   key, free equal to the sum.
2. Confirm red, then add to `protocol.py`, beside `_ClosedValue`:

```python
type _RefusalBytes = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class AuthorityInstallRefusalV1(_ClosedValue):
    """A proven shortfall: the only provider facts an error frame carries (ADR-0814)."""

    vmlinux_bytes: _RefusalBytes
    free_bytes: _RefusalBytes
    headroom_bytes: _RefusalBytes

    @model_validator(mode="after")
    def _is_a_shortfall(self) -> AuthorityInstallRefusalV1:
        if self.free_bytes >= self.vmlinux_bytes + self.headroom_bytes:
            raise ValueError("an install refusal must prove a shortfall")
        return self
```

3. In `guest_free_space.py` add the subclass and raise it for the shortfall:

```python
class GuestFreeSpaceRefusal(CategorizedError):
    """``install_failure`` whose ``details`` hold the sizes that prove the shortfall (ADR-0814)."""
```

4. Confirm green and commit.

## Task 2: authority side

**Interfaces:** consumes Task 1. Provides `AuthorityServiceError(category, install_refusal=...)` and
the frame `{"category":"install-failure","install_refusal":{...},"status":"error"}`.

**Verification**

- Pairing rule. Mode: focused-test, `test_install_refusal.py`; red: no `ValueError`.
- `_apply` mapping. Mode: focused-test, `tests/providers/local_libvirt/test_external_boot_authority.py`;
  red: `provider_conflict`; cases: shortfall, size-less `INSTALL_FAILURE`, sizes outside the model.
- Refusal gate. Mode: focused-test, service test; red: `observe` not called and a `conflict`
  observation not answered.
- Frame. Mode: focused-test, `tests/providers/external_boot_authority/test_transport.py`; red: bytes
  differ. Exact expected bytes:
  `b'{"category":"install-failure","install_refusal":{"free_bytes":1,"headroom_bytes":2,"vmlinux_bytes":3},"status":"error"}'`.

Steps:

1. Write the tests and confirm red.
2. `service.py`: add `"install_failure"` to the `Literal`, add
   `install_refusal: AuthorityInstallRefusalV1 | None = None`, store it, and refuse a mismatch:

```python
if (category == "install_failure") is not (install_refusal is not None):
    raise ValueError("install_failure and install_refusal are a pair")
```

   Add `_OBSERVED_REFUSALS = frozenset({"provider_conflict", "install_failure"})` and change the gate
   in `execute_mutation` to `if not in_provider or error.category not in _OBSERVED_REFUSALS:`.
3. `transport.py`: add `"install_failure": "install-failure"` to `_service_category`, add the
   function below, and use it at the four `except AuthorityServiceError` sites. The
   `AuthoritySystemServiceError` site keeps `_error`.

```python
def _service_error(exc: AuthorityServiceError) -> bytes:
    if exc.install_refusal is None:
        return _error(_service_category(exc.category))
    return _canonical_json(
        {
            "category": _service_category(exc.category),
            "install_refusal": exc.install_refusal.model_dump(mode="json"),
            "status": "error",
        }
    )
```

4. `external_boot_authority.py` (local): before the `except Exception` in `_apply`:

```python
        except GuestFreeSpaceRefusal as error:
            raise _install_refusal(error) from None
```

```python
def _install_refusal(error: GuestFreeSpaceRefusal) -> AuthorityServiceError:
    logger.warning("external-boot install refused: %s", error)
    try:
        sizes = AuthorityInstallRefusalV1.model_validate(
            {key: error.details.get(key) for key in AuthorityInstallRefusalV1.model_fields}
        )
    except ValidationError:
        return AuthorityServiceError("provider_conflict")
    return AuthorityServiceError("install_failure", install_refusal=sizes)
```

5. Confirm green and commit.

## Task 3: sender

**Interfaces:** consumes Task 1. Provides `CategorizedError(category=INSTALL_FAILURE, terminal=True,
details={"install_refusal": {...}})`.

**Verification**

- Frame decode. Mode: focused-test, `tests/jobs/test_authority_sender_install_refusal.py`; red:
  `install-failure` reads as `invalid-response`. Cases: valid frame gives the terminal error with all
  three sizes and the message; bare category, malformed sizes, sizes under `provider-conflict`, and an
  extra key each give `invalid-response`; a `resolve_device_identity` response of `install-failure`
  stays refused.

Steps:

1. Write the tests and confirm red.
2. Add to `authority_sender.py`, and leave `_PEER_REASONS` as it is:

```python
def _install_failure(sizes: AuthorityInstallRefusalV1) -> CategorizedError:
    needed = sizes.vmlinux_bytes + sizes.headroom_bytes
    return CategorizedError(
        f"the guest filesystem has {sizes.free_bytes} bytes free but the "
        f"{sizes.vmlinux_bytes}-byte vmlinux needs {needed} bytes including "
        f"{sizes.headroom_bytes} bytes of headroom; enlarge the System root disk by at least "
        f"{needed - sizes.free_bytes} bytes or build a smaller debuginfo",
        category=ErrorCategory.INSTALL_FAILURE,
        details={"install_refusal": sizes.model_dump(mode="json")},
        terminal=True,
    )
```

   In `_decode_response`, before the generic error branch:

```python
if (
    value.get("status") == "error"
    and value.get("category") == "install-failure"
    and set(value) == {"status", "category", "install_refusal"}
):
    raise _install_failure(AuthorityInstallRefusalV1.model_validate(value["install_refusal"]))
```

3. Confirm green and commit.

## Task 4: job row

**Interfaces:** consumes Tasks 1 and 3. Provides `ExternalBootAuthorityFailureContext.install_refusal`
and `public.external_boot_install_refusal_is_valid(jsonb)`.

**Verification**

- Bound failure. Mode: focused-test, `tests/jobs/handlers/external_boot/test_runner.py`; red: no
  `install_refusal`; the commit leaves the job `failed`, `install_failure`, sizes present, on attempt 1.
- Model rule. Mode: focused-test, same file; red: `install_refusal` accepted on another category.
- Commit refusals. Mode: focused-test, `tests/db/test_migration_0193_external_boot_install_refusal.py`;
  red: migration absent; each malformed context raises SQLSTATE 22023, and a valid one commits.
- Window lists. Mode: focused-test: `tests/db/test_migrate.py`, `test_migration_0115_capture_reap_state.py`,
  `test_migration_0102_build_gc_cursors.py`, `test_migration_0091_system_object_sweep_cursors.py` gain
  the `0193` row, and each `migrations[-K:]` gains one.

Steps:

1. Write the tests and confirm red.
2. `models.py`: import `AuthorityInstallRefusalV1`; add
   `install_refusal: AuthorityInstallRefusalV1 | None = None` to the context; add to `_FailureResult`:

```python
    @model_validator(mode="after")
    def _install_refusal_is_install_failure(self) -> _FailureResult:
        if (
            self.failure_context.install_refusal is not None
            and self.error_category is not ErrorCategory.INSTALL_FAILURE
        ):
            raise ValueError("install refusal requires install failure")
        return self
```

3. `runner.py`, after the `authority_reason` block in the bound failure:

```python
    if category is ErrorCategory.INSTALL_FAILURE and isinstance(exc, CategorizedError):
        if (sizes := exc.details.get("install_refusal")) is not None:
            failure_context["install_refusal"] = sizes
```

4. Write `0193_external_boot_install_refusal.sql`: `external_boot_install_refusal_is_valid(jsonb)`
   (plpgsql, `IMMUTABLE`, `SET search_path = ''`, `REVOKE ALL`), and a `DO` block that rewrites the
   commit function in place with the 0170 pattern: extend the field list, and add a clause beside the
   `authority_reason` one refusing `install_refusal` unless the category is `install_failure` and the
   helper is true. Each old fragment must occur exactly once or the block raises.
5. Update the four window lists, confirm green and commit.

## Rollback

Roll-forward only (ADR-0015). Reverting the Python leaves the SQL field unused.
