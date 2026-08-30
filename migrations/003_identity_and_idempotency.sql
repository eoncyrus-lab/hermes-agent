-- Three things this migration fixes, all of them holes you cannot see from
-- the application code:
--
--   1. Setting `app.user_id` proved nothing. Row-level security covered the
--      dyad tables only, so every user-scoped table -- profiles, the vault,
--      tokens, cross-relationship priors -- was protected by application
--      WHERE clauses alone, and the dyad policies themselves never asked
--      WHOSE relationship the session had pinned.
--
--   2. Every request path held a BYPASSRLS credential, because that is what
--      authentication used. A bug anywhere in a handler could read the whole
--      database. Authentication now happens on a table-blind role over one
--      RPC, and erasure -- the only other cross-relationship request path --
--      moves to a role that is still confined to one user.
--
--   3. `relationship_states` and `preference_models` carried an
--      `input_fingerprint` in the ORM and not in the schema, so recomputation
--      could not be made idempotent. The column, its uniqueness, and the
--      composite ownership constraints the ORM already declared are added
--      here.
--
-- Idempotent throughout, and safe on a fresh database.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper
  ) THEN
    RAISE EXCEPTION '003_identity_and_idempotency.sql must run as a PostgreSQL superuser';
  END IF;
END
$$;

-- ------------------------------------------------------------------- roles
--
--   sonder_identity -- bearer-token authentication only. No table privileges
--                      at all; it can execute one RPC and nothing else. This
--                      is what the API holds in place of the old BYPASSRLS
--                      credential.
--   sonder_bot      -- the same, for Telegram. A separate role because a
--                      Telegram user id is guessable and a token digest is
--                      not; see auth_telegram below.
--   sonder_erase    -- erasure and inspection, which legitimately span every
--                      relationship a user has. NOT BYPASSRLS: it reads and
--                      deletes through user-scoped policies, so it can no
--                      more reach another account than sonder_app can.
--   sonder_worker   -- the ingest/analysis pipeline. A queued job has no
--                      bearer token to prove identity with, so its policies
--                      key on the pinned relationship alone and the ownership
--                      half is checked in `worker.tasks`. NOT BYPASSRLS: a
--                      job that forgets to pin still reads nothing.
--   sonder_app      -- every ordinary request path (unchanged).
--   sonder_maint    -- BYPASSRLS. Scheduled cross-user jobs only (outcome
--                      scoring, artifact purging), and only in the worker.
--
-- Roles are created without a password: LOGIN with no password cannot
-- authenticate under md5/scram, so a role is unusable until the operator sets
-- one. Passwords are never written into this file -- see scripts/db-roles.sh.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_identity') THEN
    CREATE ROLE sonder_identity LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_erase') THEN
    CREATE ROLE sonder_erase LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_worker') THEN
    CREATE ROLE sonder_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_bot') THEN
    CREATE ROLE sonder_bot LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
END
$$;

ALTER ROLE sonder_identity NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE sonder_erase NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE sonder_worker NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE sonder_bot NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;

DO $$
BEGIN
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO sonder_identity, sonder_erase, sonder_worker, sonder_bot',
    current_database()
  );
END
$$;

GRANT USAGE ON SCHEMA public TO sonder_identity, sonder_erase, sonder_worker,
  sonder_bot;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO sonder_erase, sonder_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sonder_erase, sonder_worker;

-- Neither authentication role gets table privileges, now or by default.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sonder_identity, sonder_bot;

-- ------------------------------------------------------------- identity fn
--
-- The whole point of this function: `app.user_id` on its own is a claim, not
-- a credential. A handler that sets it to someone else's id gets nothing back
-- unless the session also carries `app.identity_claim` -- the SHA-256 digest
-- of a live bearer token for that same user, which the caller can only have
-- if it was given the token.
--
-- SECURITY DEFINER because no service role may read api_tokens directly;
-- being able to verify a digest you already hold is a strictly smaller
-- capability than being able to list them.
CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT t.user_id
    FROM api_tokens t
   WHERE t.token_sha256 = NULLIF(current_setting('app.identity_claim', true), '')
     AND t.revoked_at IS NULL
     AND t.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
   LIMIT 1
$$;

REVOKE ALL ON FUNCTION app_current_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_user_id()
  TO sonder_app, sonder_erase, sonder_maint;

-- The one RPC sonder_identity may call. Returns the caller's user id and the
-- claim to present for the rest of the request, or no rows for a token that
-- does not exist or has been revoked -- the caller cannot tell which.
CREATE OR REPLACE FUNCTION auth_bearer(digest text)
RETURNS TABLE (user_id uuid, identity_claim text)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  UPDATE api_tokens
     SET last_used_at = now()
   WHERE token_sha256 = digest
     AND revoked_at IS NULL
  RETURNING api_tokens.user_id, api_tokens.token_sha256
$$;

REVOKE ALL ON FUNCTION auth_bearer(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_bearer(text) TO sonder_identity;

-- Telegram identity resolution is the bot's equivalent of a bearer token: it
-- must map a chat to an account without being able to read the mapping table.
--
-- Granted to `sonder_bot` and NOT to `sonder_identity`, because the two are
-- not equally hard to guess. `auth_bearer` needs a 256-bit digest. This takes
-- a Telegram user id -- ten digits, frequently public -- so anything holding
-- it can walk the id space and trade a hit for a working claim. Only the bot
-- has any reason to call it, and the bot is the one process where a Telegram
-- id is already a trusted input. The API must never hold this role.
CREATE OR REPLACE FUNCTION auth_telegram(chat_user_id bigint)
RETURNS TABLE (user_id uuid, identity_claim text)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT t.user_id, k.token_sha256
    FROM telegram_accounts t
    JOIN api_tokens k ON k.user_id = t.user_id AND k.revoked_at IS NULL
   WHERE t.telegram_user_id = chat_user_id
   ORDER BY k.created_at
   LIMIT 1
$$;

REVOKE ALL ON FUNCTION auth_telegram(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_telegram(bigint) FROM sonder_identity;
GRANT EXECUTE ON FUNCTION auth_telegram(bigint) TO sonder_bot;

-- No service role reads the token table itself.
REVOKE ALL ON api_tokens FROM sonder_app, sonder_erase, sonder_worker, sonder_bot;
GRANT SELECT, INSERT, UPDATE, DELETE ON api_tokens TO sonder_maint;

-- ------------------------------------------------- user-scoped row security
--
-- ENABLE without FORCE on api_tokens. FORCE only ever adds the table owner to
-- the policy's reach, and the owner here is the migration superuser, who
-- bypasses row security either way -- so FORCE would change nothing. The
-- policy is defence in depth behind the REVOKE above, which is what actually
-- keeps every service role out of this table.
ALTER TABLE api_tokens ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS api_tokens_user_isolation ON api_tokens;
CREATE POLICY api_tokens_user_isolation ON api_tokens
  FOR ALL TO sonder_app, sonder_erase
  USING (user_id = app_current_user_id())
  WITH CHECK (user_id = app_current_user_id());

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'person_profiles','relationships','vault_records','user_tactic_priors',
    'telegram_accounts'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I_user_isolation ON %I', t, t);
    EXECUTE format(
      'CREATE POLICY %I_user_isolation ON %I
         FOR ALL TO sonder_app, sonder_erase
         USING (user_id = app_current_user_id())
         WITH CHECK (user_id = app_current_user_id())', t, t);
  END LOOP;
END $$;

-- The pipeline reads the pinned relationship to learn its target person and
-- to check that the queued job's user id really owns it. Nothing wider.
DROP POLICY IF EXISTS relationships_worker_isolation ON relationships;
CREATE POLICY relationships_worker_isolation ON relationships
  FOR ALL TO sonder_worker
  USING (id = NULLIF(current_setting('app.relationship_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.relationship_id', true), '')::uuid);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS users_self_isolation ON users;
CREATE POLICY users_self_isolation ON users
  FOR ALL TO sonder_app, sonder_erase
  USING (id = app_current_user_id())
  WITH CHECK (id = app_current_user_id());

-- The user's own voice, scoped through the profile it belongs to. The inner
-- SELECT is itself under person_profiles' policy, so it can only ever return
-- this user's profiles.
ALTER TABLE voice_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_profiles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS voice_profiles_user_isolation ON voice_profiles;
CREATE POLICY voice_profiles_user_isolation ON voice_profiles
  FOR ALL TO sonder_app, sonder_erase
  USING (person_id IN (SELECT id FROM person_profiles))
  WITH CHECK (person_id IN (SELECT id FROM person_profiles));

-- ------------------------------------------------------- dyad row security
--
-- 001 pinned these to `app.relationship_id` and stopped there, which meant
-- that knowing a relationship id was enough to read it. The ownership half
-- was enforced only by `auth.owned_relationship` in the application. Both
-- halves now live in the database.
--
-- The ownership check is written against the GUC rather than the row column
-- so the planner can evaluate it once per statement instead of once per row.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'interactions','signals','relationship_states','preference_evidence',
    'preference_models','strategy_records','strategy_outcomes',
    'consent_records','memory_chunks'
  ] LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I_dyad_isolation ON %I', t, t);
    EXECUTE format(
      'CREATE POLICY %I_dyad_isolation ON %I
         FOR ALL TO sonder_app
         USING (
           relationship_id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid
           AND EXISTS (
             SELECT 1 FROM relationships r
              WHERE r.id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid
                AND r.user_id = app_current_user_id()
           )
         )
         WITH CHECK (
           relationship_id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid
           AND EXISTS (
             SELECT 1 FROM relationships r
              WHERE r.id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid
                AND r.user_id = app_current_user_id()
           )
         )', t, t);
    -- A queued job carries no bearer token, so its policy can only key on
    -- the pinned relationship. `worker.tasks` supplies the ownership half.
    EXECUTE format('DROP POLICY IF EXISTS %I_worker_isolation ON %I', t, t);
    EXECUTE format(
      'CREATE POLICY %I_worker_isolation ON %I
         FOR ALL TO sonder_worker
         USING (relationship_id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid)
         WITH CHECK (relationship_id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid)',
      t, t);
    -- Erasure spans every relationship the user has, and nothing beyond it.
    EXECUTE format('DROP POLICY IF EXISTS %I_user_erasure ON %I', t, t);
    EXECUTE format(
      'CREATE POLICY %I_user_erasure ON %I
         FOR ALL TO sonder_erase
         USING (relationship_id IN (SELECT id FROM relationships))
         WITH CHECK (relationship_id IN (SELECT id FROM relationships))', t, t);
  END LOOP;
END $$;

DROP POLICY IF EXISTS messages_dyad_isolation ON messages;
CREATE POLICY messages_dyad_isolation ON messages
  FOR ALL TO sonder_app
  USING (interaction_id IN (SELECT id FROM interactions))
  WITH CHECK (interaction_id IN (SELECT id FROM interactions));

DROP POLICY IF EXISTS messages_worker_isolation ON messages;
CREATE POLICY messages_worker_isolation ON messages
  FOR ALL TO sonder_worker
  USING (interaction_id IN (SELECT id FROM interactions))
  WITH CHECK (interaction_id IN (SELECT id FROM interactions));

DROP POLICY IF EXISTS messages_user_erasure ON messages;
CREATE POLICY messages_user_erasure ON messages
  FOR ALL TO sonder_erase
  USING (interaction_id IN (SELECT id FROM interactions))
  WITH CHECK (interaction_id IN (SELECT id FROM interactions));

-- --------------------------------------------------------- artifact purging
--
-- `interactions` carries the blanket append-only trigger from 001, which also
-- blocked the one UPDATE the retention promise depends on: `purge_artifacts`
-- clearing `source_artifact_id` after the TTL. So screenshots were never
-- purged, and the bot's "the image itself is deleted within a day" was not
-- true. Narrow the trigger the way `strategy_records` already does -- those
-- two columns may move, in one direction, and nothing else may change.
CREATE OR REPLACE FUNCTION interactions_guard() RETURNS trigger AS $fn$
BEGIN
  -- The artifact may only ever be dropped, never restored or replaced.
  IF NEW.source_artifact_id IS NOT NULL
     OR NEW.source_artifact_deleted IS NOT TRUE THEN
    RAISE EXCEPTION
      'interactions is append-only except for clearing a source artifact';
  END IF;
  -- Everything else must be untouched. Compared by column NAME rather than by
  -- position, so a column added to this table later is guarded the moment it
  -- exists instead of silently falling outside the check.
  IF (to_jsonb(NEW) - 'source_artifact_id' - 'source_artifact_deleted')
     IS DISTINCT FROM
     (to_jsonb(OLD) - 'source_artifact_id' - 'source_artifact_deleted') THEN
    RAISE EXCEPTION
      'interactions is append-only except for clearing a source artifact';
  END IF;
  RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS interactions_append_only ON interactions;
DROP TRIGGER IF EXISTS interactions_guard ON interactions;
CREATE TRIGGER interactions_guard BEFORE UPDATE ON interactions
  FOR EACH ROW EXECUTE FUNCTION interactions_guard();

-- ------------------------------------------------------ idempotent recompute
--
-- Recomputation is triggered by retries and duplicate queue deliveries, and
-- the same inputs must converge on the same row rather than pile up a new
-- version per delivery. The fingerprint is over the INPUTS -- the signals and
-- interactions the snapshot was computed from, and the model version -- not
-- over the output, so a scorer change produces a new row and a replay does
-- not.
ALTER TABLE relationship_states
  ADD COLUMN IF NOT EXISTS input_fingerprint varchar(64) NOT NULL DEFAULT '';
ALTER TABLE preference_models
  ADD COLUMN IF NOT EXISTS input_fingerprint varchar(64) NOT NULL DEFAULT '';

-- Rows written before the column existed have no recoverable input digest.
-- Seed them from the primary key so they stay distinct under the uniqueness
-- constraint below: the worst case is one redundant recomputation each, which
-- is exactly what the fingerprint is there to avoid going forward.
UPDATE relationship_states SET input_fingerprint = 'legacy:' || id
 WHERE input_fingerprint = '';
UPDATE preference_models SET input_fingerprint = 'legacy:' || id
 WHERE input_fingerprint = '';

ALTER TABLE relationship_states ALTER COLUMN input_fingerprint DROP DEFAULT;
ALTER TABLE preference_models ALTER COLUMN input_fingerprint DROP DEFAULT;

-- `memory_chunks.source_ids` is a list of ids into one of several tables, so
-- the rows it points at are only resolvable alongside the table they came
-- from. The ORM declared the discriminator and the schema never had it, which
-- made every write with sources fail against a real database.
ALTER TABLE memory_chunks
  ADD COLUMN IF NOT EXISTS source_kind varchar(32);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'memory_chunks'::regclass
       AND conname = 'memory_sources_are_typed'
  ) THEN
    ALTER TABLE memory_chunks
      ADD CONSTRAINT memory_sources_are_typed
      CHECK (cardinality(source_ids) = 0 OR source_kind IS NOT NULL);
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'relationship_states'::regclass
       AND conname = 'relationship_states_relationship_fingerprint_unique'
  ) THEN
    ALTER TABLE relationship_states
      ADD CONSTRAINT relationship_states_relationship_fingerprint_unique
      UNIQUE (relationship_id, input_fingerprint);
  END IF;

  -- Scoped by relationship, not by person. One person can be the target of
  -- two of the same user's relationships, and a per-person constraint is then
  -- violated by a row the writing session cannot see: its RLS policy pins one
  -- relationship, the lookup misses, and the insert dies on a conflict with
  -- an invisible row.
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'preference_models'::regclass
       AND conname = 'preference_models_person_fingerprint_unique'
  ) THEN
    ALTER TABLE preference_models
      DROP CONSTRAINT preference_models_person_fingerprint_unique;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'preference_models'::regclass
       AND conname = 'preference_models_relationship_fingerprint_unique'
  ) THEN
    ALTER TABLE preference_models
      ADD CONSTRAINT preference_models_relationship_fingerprint_unique
      UNIQUE (relationship_id, input_fingerprint);
  END IF;

  -- Composite ownership constraints the ORM declares and 001/002 did not.
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'preference_models'::regclass
       AND conname = 'preference_models_id_relationship_unique'
  ) THEN
    ALTER TABLE preference_models
      ADD CONSTRAINT preference_models_id_relationship_unique
      UNIQUE (id, relationship_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'preference_models'::regclass
       AND conname = 'preference_models_id_person_unique'
  ) THEN
    ALTER TABLE preference_models
      ADD CONSTRAINT preference_models_id_person_unique
      UNIQUE (id, person_id);
  END IF;

  -- A superseded snapshot must belong to the same relationship. Without the
  -- composite key this was a plain reference to any state row anywhere.
  IF EXISTS (
    SELECT 1 FROM relationship_states a
      JOIN relationship_states b ON b.id = a.supersedes
     WHERE a.relationship_id <> b.relationship_id
  ) THEN
    RAISE EXCEPTION
      'relationship_states supersedes a snapshot from another relationship'
      USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'relationship_states'::regclass
       AND conname = 'relationship_states_supersedes_same_relationship'
  ) THEN
    ALTER TABLE relationship_states
      ADD CONSTRAINT relationship_states_supersedes_same_relationship
      FOREIGN KEY (supersedes, relationship_id)
      REFERENCES relationship_states(id, relationship_id) NOT VALID;
  END IF;
END
$$;

ALTER TABLE relationship_states
  VALIDATE CONSTRAINT relationship_states_supersedes_same_relationship;
