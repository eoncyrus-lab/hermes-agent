-- Security hardening for deployments created before the application roles and
-- relationship/profile ownership constraint were part of the core schema.
-- This migration is intentionally idempotent so it is also safe on a fresh DB.

-- Role creation/hardening and ownership cleanup require a superuser. Run and
-- rerun this file as the same cluster-admin migration identity, never as a
-- service role.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper
  ) THEN
    RAISE EXCEPTION '002_security_boundaries.sql must run as a PostgreSQL superuser';
  END IF;
END
$$;

-- Refuse to bless inconsistent existing data. An operator must investigate it
-- rather than silently reassigning either side of a relationship.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM relationships r
      JOIN person_profiles p ON p.id = r.target_person_id
     WHERE r.user_id <> p.user_id
  ) THEN
    RAISE EXCEPTION
      'cannot add relationship ownership constraint: cross-user rows exist'
      USING ERRCODE = '23514',
            HINT = 'Inspect relationships joined to person_profiles and correct ownership before rerunning this migration.';
  END IF;
END
$$;

-- Every duplicated relationship/person/user key below is a security boundary,
-- not denormalisation. Abort on legacy mismatches before adding constraints.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM preference_evidence pe
    JOIN relationships r ON r.id = pe.relationship_id
    WHERE pe.person_id <> r.target_person_id
  ) THEN
    RAISE EXCEPTION 'preference_evidence contains a person from another relationship'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM preference_models pm
    JOIN relationships r ON r.id = pm.relationship_id
    WHERE pm.person_id <> r.target_person_id
  ) THEN
    RAISE EXCEPTION 'preference_models contains a person from another relationship'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM signals s
    JOIN interactions i ON i.id = s.interaction_id
    WHERE s.relationship_id <> i.relationship_id
  ) THEN
    RAISE EXCEPTION 'signals contains an interaction from another relationship'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM strategy_records sr
    JOIN relationships r ON r.id = sr.relationship_id
    WHERE sr.user_id <> r.user_id
  ) THEN
    RAISE EXCEPTION 'strategy_records contains a user from another relationship'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM strategy_records sr
    JOIN relationship_states rs ON rs.id = sr.relationship_state_id
    WHERE sr.relationship_id <> rs.relationship_id
  ) THEN
    RAISE EXCEPTION 'strategy_records contains a state from another relationship'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM strategy_outcomes so
    JOIN strategy_records sr ON sr.id = so.strategy_id
    WHERE so.relationship_id <> sr.relationship_id
  ) THEN
    RAISE EXCEPTION 'strategy_outcomes contains a strategy from another relationship'
      USING ERRCODE = '23514';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'person_profiles'::regclass
       AND conname = 'person_profiles_id_user_unique'
  ) THEN
    ALTER TABLE person_profiles
      ADD CONSTRAINT person_profiles_id_user_unique UNIQUE (id, user_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'relationships'::regclass
       AND conname = 'relationship_target_same_user'
  ) THEN
    ALTER TABLE relationships
      ADD CONSTRAINT relationship_target_same_user
      FOREIGN KEY (target_person_id, user_id)
      REFERENCES person_profiles(id, user_id) ON DELETE CASCADE
      NOT VALID;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relationships_id_target_unique' AND conrelid = 'relationships'::regclass) THEN
    ALTER TABLE relationships ADD CONSTRAINT relationships_id_target_unique UNIQUE (id, target_person_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relationships_id_user_unique' AND conrelid = 'relationships'::regclass) THEN
    ALTER TABLE relationships ADD CONSTRAINT relationships_id_user_unique UNIQUE (id, user_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interactions_id_relationship_unique' AND conrelid = 'interactions'::regclass) THEN
    ALTER TABLE interactions ADD CONSTRAINT interactions_id_relationship_unique UNIQUE (id, relationship_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relationship_states_id_relationship_unique' AND conrelid = 'relationship_states'::regclass) THEN
    ALTER TABLE relationship_states ADD CONSTRAINT relationship_states_id_relationship_unique UNIQUE (id, relationship_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'strategy_records_id_relationship_unique' AND conrelid = 'strategy_records'::regclass) THEN
    ALTER TABLE strategy_records ADD CONSTRAINT strategy_records_id_relationship_unique UNIQUE (id, relationship_id);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'signals_interaction_same_relationship' AND conrelid = 'signals'::regclass) THEN
    ALTER TABLE signals ADD CONSTRAINT signals_interaction_same_relationship FOREIGN KEY (interaction_id, relationship_id) REFERENCES interactions(id, relationship_id) ON DELETE CASCADE NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'preference_evidence_person_same_relationship' AND conrelid = 'preference_evidence'::regclass) THEN
    ALTER TABLE preference_evidence ADD CONSTRAINT preference_evidence_person_same_relationship FOREIGN KEY (relationship_id, person_id) REFERENCES relationships(id, target_person_id) ON DELETE CASCADE NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'preference_models_person_same_relationship' AND conrelid = 'preference_models'::regclass) THEN
    ALTER TABLE preference_models ADD CONSTRAINT preference_models_person_same_relationship FOREIGN KEY (relationship_id, person_id) REFERENCES relationships(id, target_person_id) ON DELETE CASCADE NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'strategy_records_user_same_relationship' AND conrelid = 'strategy_records'::regclass) THEN
    ALTER TABLE strategy_records ADD CONSTRAINT strategy_records_user_same_relationship FOREIGN KEY (relationship_id, user_id) REFERENCES relationships(id, user_id) ON DELETE CASCADE NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'strategy_records_state_same_relationship' AND conrelid = 'strategy_records'::regclass) THEN
    ALTER TABLE strategy_records ADD CONSTRAINT strategy_records_state_same_relationship FOREIGN KEY (relationship_state_id, relationship_id) REFERENCES relationship_states(id, relationship_id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'strategy_outcomes_strategy_same_relationship' AND conrelid = 'strategy_outcomes'::regclass) THEN
    ALTER TABLE strategy_outcomes ADD CONSTRAINT strategy_outcomes_strategy_same_relationship FOREIGN KEY (strategy_id, relationship_id) REFERENCES strategy_records(id, relationship_id) NOT VALID;
  END IF;
END
$$;

ALTER TABLE relationships
  VALIDATE CONSTRAINT relationship_target_same_user;
ALTER TABLE signals VALIDATE CONSTRAINT signals_interaction_same_relationship;
ALTER TABLE preference_evidence VALIDATE CONSTRAINT preference_evidence_person_same_relationship;
ALTER TABLE preference_models VALIDATE CONSTRAINT preference_models_person_same_relationship;
ALTER TABLE strategy_records VALIDATE CONSTRAINT strategy_records_user_same_relationship;
ALTER TABLE strategy_records VALIDATE CONSTRAINT strategy_records_state_same_relationship;
ALTER TABLE strategy_outcomes VALIDATE CONSTRAINT strategy_outcomes_strategy_same_relationship;

-- The container's POSTGRES_USER owns the schema and must never be used by a
-- running service. These two login roles are the only service identities.
--
-- Created WITHOUT a password on purpose. A LOGIN role with no password cannot
-- authenticate under md5 or scram, so it is inert until an operator sets one;
-- a password written here would be a published credential in every deployment
-- that ran this file. See scripts/db-roles.sh.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_app') THEN
    CREATE ROLE sonder_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_maint') THEN
    CREATE ROLE sonder_maint LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION BYPASSRLS;
  END IF;
END
$$;

ALTER ROLE sonder_app NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE sonder_maint NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION BYPASSRLS;

GRANT USAGE ON SCHEMA public TO sonder_app, sonder_maint;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO sonder_maint;
-- A new table gets no request-path access until its ownership policy and
-- grants are reviewed. Maintenance is the only default grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM sonder_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sonder_maint;

-- Explicit business-table allow-list, and never `ON ALL TABLES` for this role.
--
-- This file is rerunnable and the README tells operators to rerun it, so a
-- blanket grant here silently undoes 003's `REVOKE ALL ON api_tokens FROM
-- sonder_app` every time it is replayed -- handing a request-path role the
-- ability to mint itself a token or clear `revoked_at`. Enumerating the
-- tables means a replay in any order lands in the same place.
DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT unnest(ARRAY[
      'users','person_profiles','voice_profiles','relationships','interactions',
      'messages','signals','relationship_states','preference_evidence',
      'preference_models','strategy_records','strategy_outcomes',
      'consent_records','memory_chunks','user_tactic_priors'
    ])
  LOOP
    EXECUTE format(
      'GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO sonder_app', t
    );
  END LOOP;
END
$$;
REVOKE ALL ON api_tokens, telegram_accounts, vault_records FROM sonder_app;

DO $$
BEGIN
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO sonder_app, sonder_maint',
    current_database()
  );
END
$$;

-- Removed deliberately: a SECURITY DEFINER cross-dyad text function cannot
-- authenticate tenant ownership from a caller-set GUC. The leak critic now
-- degrades without cross-relationship text rather than creating that oracle.
DROP FUNCTION IF EXISTS other_dyad_phrases_for_current_relationship();
