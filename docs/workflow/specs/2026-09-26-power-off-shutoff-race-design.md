# Accept a raced SHUTOFF after local-libvirt destroy

## Problem

The local-libvirt power-off helper can observe an active domain, then call `destroy()` after the guest has already reached `SHUTOFF`. Libvirt reports `VIR_ERR_OPERATION_INVALID` for that stale operation, causing a successful stop to fail. The external-boot session has the same gap around its direct destroy call.

## Scope

Use one helper in `lifecycle/power.py` for destroy operations owned by this issue. It calls `destroy()` once. On `VIR_ERR_OPERATION_INVALID`, it reads `state()` once and accepts only `VIR_DOMAIN_SHUTOFF`; all other errors and states propagate. The helper is used by the clean power-off timeout, the immediate non-honouring-state and refused-shutdown paths, and `stop_and_require_inactive`'s direct destroy path. Its existing final inactive check remains.

The existing wait bound and re-send behavior remain governed by ADR-0679. Operator OFF behavior belongs to #2801. Other lifecycle destroy sites keep their owners.

### Failure model

- An operation-invalid error with `SHUTOFF` is accepted as the achieved goal.
- An operation-invalid error with another state is re-raised.
- Any other destroy error is re-raised.
- A failed state re-read propagates, because the achieved state cannot be verified.

## Success

- Timeout fallback and immediate destroy accept a guest that reaches `SHUTOFF` in the destroy window.
- Direct external-boot destroy accepts the same race and still requires inactivity.
- Non-`SHUTOFF` states and other destroy failures retain failure behavior.

## Validation

- `focused-test`: fake domains raise `VIR_ERR_OPERATION_INVALID` after transitioning to `SHUTOFF` at timeout and immediate destroy; `tests/providers/local_libvirt/lifecycle/test_power.py` fails before the fix and passes with `uv run python -m pytest tests/providers/local_libvirt/lifecycle/test_power.py -q`.
- `focused-test`: the session's fake domain raises at direct destroy after transitioning to `SHUTOFF`; `tests/providers/local_libvirt/lifecycle/boot/test_session.py` fails before the fix and passes with its focused pytest command.
- `focused-test`: both paths re-raise when the domain is still active or a different libvirt error occurs, using the same focused test files.
