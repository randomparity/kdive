from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import CRASH_CAPTURE, DEBUGINFO, feature_requirement
from kdive.kernel_config.support import (
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)

_CRASH = feature_requirement(CRASH_CAPTURE)
_DEBUGINFO = feature_requirement(DEBUGINFO)
_FULL = frozenset(
    {
        "KEXEC_CORE",
        "KEXEC",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "VMCORE_INFO",
        "FW_CFG_SYSFS",
        "RELOCATABLE",
    }
)


def test_kaslr_off_full_gate_set_is_supported():
    # RANDOMIZE_BASE absent but every gate_required clause met -> no unmet clauses (supported).
    assert unmet_clauses(KernelConfig(_FULL), _CRASH) == ()


def test_kexec_or_group_satisfied_by_either_syscall():
    only_file = (_FULL - {"KEXEC"}) | {"KEXEC_FILE"}
    assert unmet_clauses(KernelConfig(frozenset(only_file)), _CRASH) == ()


def test_missing_one_clause_is_unsupported_and_named():
    cfg = KernelConfig(_FULL - {"PROC_VMCORE"})
    unmet = unmet_clauses(cfg, _CRASH)
    assert unmet != ()
    assert missing_symbols(unmet) == ["PROC_VMCORE"]


def test_missing_both_kexec_syscalls_names_both():
    cfg = KernelConfig(_FULL - {"KEXEC"})  # neither KEXEC nor KEXEC_FILE
    unmet = unmet_clauses(cfg, _CRASH)
    assert missing_symbols(unmet) == ["KEXEC", "KEXEC_FILE"]


# --- unmet_advertised_clauses (ADR-0322): the warn-only check over `advertised` ------------------


def test_advertised_debuginfo_fully_satisfied_by_btf():
    # DEBUG_INFO + a DWARF/BTF member + DEBUG_KERNEL satisfies every advertised debuginfo clause.
    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_INFO_BTF", "DEBUG_KERNEL"}))
    assert unmet_advertised_clauses(cfg, _DEBUGINFO) == ()


def test_advertised_debuginfo_missing_dwarf_btf_clause_is_named():
    # DEBUG_INFO + DEBUG_KERNEL present but no DWARF/BTF: only the middle clause is unmet.
    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_KERNEL"}))
    unmet = unmet_advertised_clauses(cfg, _DEBUGINFO)
    assert missing_symbols(unmet) == ["DEBUG_INFO_BTF", "DEBUG_INFO_DWARF4", "DEBUG_INFO_DWARF5"]


def test_advertised_debuginfo_empty_config_names_every_clause():
    unmet = unmet_advertised_clauses(KernelConfig(frozenset()), _DEBUGINFO)
    assert missing_symbols(unmet) == [
        "DEBUG_INFO",
        "DEBUG_INFO_BTF",
        "DEBUG_INFO_DWARF4",
        "DEBUG_INFO_DWARF5",
        "DEBUG_KERNEL",
    ]


def test_advertised_ignores_gate_required_only_semantics():
    # debuginfo has an empty gate_required, so unmet_clauses (gate semantics) never reports it;
    # the advertised check does. This is the distinction the warn path relies on.
    empty = KernelConfig(frozenset())
    assert unmet_clauses(empty, _DEBUGINFO) == ()
    assert unmet_advertised_clauses(empty, _DEBUGINFO) != ()
