from kdive.kernel_config.parse import parse_kernel_config

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
