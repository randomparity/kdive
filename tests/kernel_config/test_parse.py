import pytest

from kdive.kernel_config.parse import KernelConfig, parse_kernel_config

_SAMPLE = b"""# Automatically generated file
CONFIG_KEXEC=y
CONFIG_KEXEC_FILE=y
CONFIG_MAGIC_SYSRQ=m
# CONFIG_RANDOMIZE_BASE is not set
CONFIG_LOCALVERSION="-kdive"
CONFIG_NR_CPUS=8

garbage line that is not a config
CONFIG_KASAN=n
"""


def test_y_and_m_are_enabled():
    cfg = parse_kernel_config(_SAMPLE)
    assert cfg.is_enabled("KEXEC")
    assert cfg.is_enabled("KEXEC_FILE")
    assert cfg.is_enabled("MAGIC_SYSRQ")  # =m counts


def test_not_set_absent_and_n_are_disabled():
    cfg = parse_kernel_config(_SAMPLE)
    assert not cfg.is_enabled("RANDOMIZE_BASE")  # is not set
    assert not cfg.is_enabled("KASAN")  # =n
    assert not cfg.is_enabled("CRASH_DUMP")  # absent


def test_string_and_int_values_are_not_enabled():
    cfg = parse_kernel_config(_SAMPLE)
    assert not cfg.is_enabled("LOCALVERSION")
    assert not cfg.is_enabled("NR_CPUS")


def test_bare_symbol_names_no_config_prefix():
    cfg = parse_kernel_config(_SAMPLE)
    assert "KEXEC" in cfg.enabled
    assert "CONFIG_KEXEC" not in cfg.enabled


def test_empty_and_non_utf8_are_degenerate_not_crash():
    assert parse_kernel_config(b"").is_degenerate
    assert parse_kernel_config(b"\xff\xfe not a config").is_degenerate
    assert not parse_kernel_config(_SAMPLE).is_degenerate


def test_only_y_reaches_builtin_while_m_stays_enabled_only():
    # #1860. `enabled` keeps its present meaning - =y or =m - because =m is the right answer for
    # KASAN, ftrace, kcov and BPF symbols; the y/m distinction the boot clauses need arrives as a
    # second set rather than as a narrower regex.
    cfg = parse_kernel_config(_SAMPLE)
    assert cfg.is_builtin("KEXEC")
    assert cfg.is_builtin("KEXEC_FILE")
    assert cfg.is_enabled("MAGIC_SYSRQ")
    assert not cfg.is_builtin("MAGIC_SYSRQ")  # =m is enabled but not built in
    assert cfg.builtin == frozenset({"KEXEC", "KEXEC_FILE"})
    assert cfg.enabled == frozenset({"KEXEC", "KEXEC_FILE", "MAGIC_SYSRQ"})


def test_a_disabled_or_non_boolean_symbol_reaches_neither_set():
    cfg = parse_kernel_config(_SAMPLE)
    for symbol in ("RANDOMIZE_BASE", "KASAN", "LOCALVERSION", "NR_CPUS", "CRASH_DUMP"):
        assert not cfg.is_enabled(symbol), symbol
        assert not cfg.is_builtin(symbol), symbol


def test_builtin_must_be_a_subset_of_enabled():
    # The invariant that makes `is_builtin` readable as "enabled, and as =y": a construction site
    # that passes the two sets the wrong way round, or invents a built-in symbol the config never
    # enabled, fails at construction rather than silently satisfying a boot clause.
    with pytest.raises(ValueError, match="builtin"):
        KernelConfig(frozenset({"EXT4_FS"}), frozenset({"EXT4_FS", "VIRTIO_BLK"}))


def test_the_two_sets_may_coincide_for_a_wholly_built_in_kernel():
    both = frozenset({"EXT4_FS", "VIRTIO_BLK"})
    cfg = KernelConfig(both, both)
    assert cfg.is_builtin("EXT4_FS")
    assert not cfg.is_degenerate
