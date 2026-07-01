-- 0056_system_bootstrap_keys.sql — per-System SSH bootstrap keypair (ADR-0289, #963).
-- Additive (forward-only, ADR-0015). Each System gets a unique throwaway ed25519 keypair
-- generated at provision and injected into its overlay; the private half lives here and is
-- reclaimed by the teardown handler (explicit DELETE), with ON DELETE CASCADE as the backstop
-- for a hard systems-row delete. No standing credential is baked into catalog images (ADR-0289
-- supersedes ADR-0052).
CREATE TABLE system_bootstrap_keys (
    system_id   uuid PRIMARY KEY REFERENCES systems (id) ON DELETE CASCADE,
    private_key text NOT NULL,
    public_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
