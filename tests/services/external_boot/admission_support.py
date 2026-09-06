"""The decided-tool tables for the external-boot admission matrix (ADR-0583, #2117).

Kept out of the test modules that read them because two of them do: `test_admission.py`
walks the live registry against `GUARDED_TOOLS` to prove the matrix is closed, and
`test_reverse_admission.py` derives its unkeyed-replay gate from the same table so a newly
guarded tool cannot ship without one. A collected test module may not import another
(`tests/test_test_module_dependencies.py`), so the shared table lives here.
"""

from __future__ import annotations

from kdive.services.external_boot import ExternalBootOperation

_OP = ExternalBootOperation

# Every registered mutating tool, mapped to the operation its handler guards with. A tool here
# is enforced by `tests/services/external_boot/test_reverse_admission.py`, except the two
# external-boot recovery contracts, whose guard is enforced by
# `tests/services/external_boot/test_recovery_requests.py` (they are the reverse operations, so
# they have no reverse case of their own).
GUARDED_TOOLS: dict[str, ExternalBootOperation] = {
    "control.capture_traffic": _OP.CAPTURE_TRAFFIC,
    "control.diagnostic_sysrq": _OP.SYSTEM_SYSRQ,
    "control.force_crash": _OP.FORCE_CRASH,
    "control.power": _OP.SYSTEM_POWER,
    "control.watch_for_crash": _OP.SYSTEM_WATCH_CRASH,
    "allocations.release": _OP.ALLOCATION_RELEASE,
    "debug.end_session": _OP.DEBUG_DETACH,
    "debug.start_session": _OP.DEBUG_ATTACH,
    "runs.bind": _OP.RUN_BIND,
    "runs.boot": _OP.RUN_BOOT,
    "runs.cancel": _OP.RUN_CANCEL,
    "runs.create": _OP.RUN_CREATE,
    "runs.install": _OP.RUN_INSTALL,
    "runs.release_external_boot": _OP.EXTERNAL_BOOT_RELEASE,
    "systems.authorize_ssh_key": _OP.SYSTEM_AUTHORIZE_SSH_KEY,
    "systems.delete_snapshot": _OP.SYSTEM_SNAPSHOT,
    "systems.reprovision": _OP.SYSTEM_REPROVISION,
    "systems.resolve_external_boot_conflict": _OP.EXTERNAL_BOOT_RESOLVE_CONFLICT,
    "systems.restore": _OP.SYSTEM_SNAPSHOT,
    "systems.snapshot": _OP.SYSTEM_SNAPSHOT,
    "systems.teardown": _OP.SYSTEM_TEARDOWN,
    "vmcore.fetch": _OP.CAPTURE_VMCORE,
}

# The reviewed exemptions, each with the reason it decides nothing about a System's external
# boot. A new mutating tool belongs in one map or the other before it can ship.
UNGUARDED_TOOLS: dict[str, str] = {
    "accounting.set_budget": "accounting state; touches no System",
    "accounting.set_quota": "accounting state; touches no System",
    "allocations.renew": "extends a lease; changes nothing about the guest",
    "allocations.request": "grants capacity before any System exists",
    "artifacts.create_investigation_upload": "mints an upload slot; touches no System",
    "artifacts.create_run_upload": "mints an upload slot; touches no System",
    "debug.advance": "in-session debugger control on an already-admitted attach",
    "debug.clear_breakpoint": "in-session debugger control on an already-admitted attach",
    "debug.clear_watchpoint": "in-session debugger control on an already-admitted attach",
    "debug.continue": "in-session debugger control on an already-admitted attach",
    "debug.interrupt": "in-session debugger control on an already-admitted attach",
    "debug.load_module_symbols": "in-session debugger control on an already-admitted attach",
    "debug.set_breakpoint": "in-session debugger control on an already-admitted attach",
    "debug.set_watchpoint": "in-session debugger control on an already-admitted attach",
    "images.delete": "image catalog administration; touches no System",
    "images.extend": "image catalog administration; touches no System",
    "images.prune_expired": "image catalog administration; touches no System",
    "images.publish": "image catalog administration; touches no System",
    "images.upload": "image catalog administration; touches no System",
    "introspect.script": "read-only guest introspection over an already-admitted transport",
    "inventory.clear_override": "operator inventory bookkeeping on a Resource, not a System",
    "investigations.close": "Investigation bookkeeping; closes through systems.teardown",
    "investigations.complete_rootfs_upload": "finishes an upload; touches no System",
    "investigations.link": "Investigation bookkeeping; touches no System",
    "investigations.open": "Investigation bookkeeping; touches no System",
    "investigations.set": "Investigation bookkeeping; touches no System",
    "investigations.unlink": "Investigation bookkeeping; touches no System",
    "jobs.cancel": (
        "cancelling is de-escalation: it starts no guest work and frees the System-held job "
        "that blocks a release, which is why it is the escape hatch that refusal names"
    ),
    "ops.diagnostics": "operator read-out; enqueues no System work",
    "ops.export_systems_toml": "operator read-out; enqueues no System work",
    "ops.force_release": "operator break-glass, the escape hatch a stuck activation needs",
    "ops.force_teardown": "operator break-glass, the escape hatch a stuck activation needs",
    "ops.reconcile_now": "operator break-glass reconcile; must run against a stuck activation",
    "ops.reconcile_systems": "operator break-glass reconcile; must run against a stuck activation",
    "ops.recover_build_use": "build-ledger repair; touches no System",
    "ops.resolve_recovery_orphan": (
        "repairs quarantined recovery objects, which are not the activation the matrix keys on"
    ),
    "ops.set_cost_class_coeff": "accounting configuration; touches no System",
    "ops.set_host_capacity": "capacity configuration on a Resource, not a System",
    "ops.set_queue_paused": "worker-lane configuration; touches no System",
    "resources.deregister": "Resource administration below the System layer",
    "resources.drain": "Resource administration below the System layer",
    "resources.register": "Resource administration below the System layer",
    "resources.renew": "Resource administration below the System layer",
    "resources.set_scheduling": "Resource administration below the System layer",
    "resources.set_status": "Resource administration below the System layer",
    "runs.complete_build": "records a build result; the guest is untouched",
    "runs.set": "Run metadata; the guest is untouched",
    "shapes.delete": "shape catalog administration; touches no System",
    "shapes.set": "shape catalog administration; touches no System",
    "systems.check_ssh_reachable": (
        "read-only liveness probe (read_only, VIEWER); ADR-0583 does not reject System "
        "observation in any restricted state"
    ),
    "systems.provision": "creates the System; no activation can restrict it yet",
    "tools.invoke": "gateway dispatcher; the re-entered inner tool carries its own guard",
}
