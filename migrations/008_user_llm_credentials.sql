-- Model credentials a user brings themselves.
--
-- The operator's keys live in the environment and serve everyone. These are
-- per-user, so somebody who wants a different vendor -- or a model they host
-- themselves, which is the only honest answer to a hosted model hedging on
-- frank adult conversation -- can point their own requests at it.
--
-- Two things this table deliberately does not do:
--
--   * It never carries an EXTRACT or CRITIC route. A credential the operator
--     did not provision is `permissive` by construction (see
--     `llm/router.py`), because those two tasks are the operator's guarantee
--     to the people being modelled, and a user can point a custom endpoint at
--     anything at all.
--   * It never appears on the worker's DSN. Extraction runs on the operator's
--     trusted endpoint or not at all.
--
-- Idempotent, and safe on a fresh database.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper
  ) THEN
    RAISE EXCEPTION
      '008_user_llm_credentials.sql must run as a PostgreSQL superuser';
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS user_llm_credentials (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- The name this provider is addressed by in a route chain.
  provider     text NOT NULL,
  -- Which entry of `llm.configuration.PROFILES` supplies the protocol and
  -- the default endpoint. Never a safety-trust grant: trust is code-owned.
  profile      text NOT NULL,
  base_url     text,
  model        text,
  -- 'general' extends the user's available providers. 'intimate' is the
  -- endpoint their intimate track is sent to, which is how a self-hosted
  -- model gets used without anybody jailbreaking a hosted one.
  purpose      text NOT NULL DEFAULT 'general',
  ciphertext   bytea NOT NULL,
  key_version  text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT user_llm_credentials_purpose
    CHECK (purpose IN ('general', 'intimate')),
  CONSTRAINT user_llm_credentials_unique UNIQUE (user_id, provider)
);

CREATE INDEX IF NOT EXISTS user_llm_credentials_user_id_idx
  ON user_llm_credentials (user_id);

-- ------------------------------------------------------------ row security
--
-- Same shape as `vault_records`: one user's rows, and only while the session
-- carries that user's verified identity claim.
ALTER TABLE user_llm_credentials ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_llm_credentials_user_isolation
  ON user_llm_credentials;
CREATE POLICY user_llm_credentials_user_isolation ON user_llm_credentials
  FOR ALL
  USING (user_id = app_current_user_id())
  WITH CHECK (user_id = app_current_user_id());

-- --------------------------------------------------------------- privileges
--
-- `sonder_app` holds them, because the API is what builds a router for the
-- user making the request. The ciphertext is useless without the Fernet key,
-- which lives in the environment rather than in this database.
--
-- The worker is left out on purpose. It runs EXTRACT, which may not follow a
-- user's endpoint, so giving it these rows would only be a way to get that
-- wrong later.
REVOKE ALL ON user_llm_credentials
  FROM sonder_identity, sonder_bot, sonder_wechat, sonder_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_llm_credentials TO sonder_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_llm_credentials TO sonder_erase;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_llm_credentials TO sonder_maint;
