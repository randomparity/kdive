-- 0049_build_host_toolchain_desc.sql — operator-asserted build-env toolchain description (ADR-0242).
-- Additive, forward-only (ADR-0015). Nullable; existing rows read as NULL ("no description").
ALTER TABLE build_hosts ADD COLUMN toolchain_desc text;
