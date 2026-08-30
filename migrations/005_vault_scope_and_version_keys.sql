-- One constraint that was the right shape for one relationship and the wrong
-- shape for two, and one leftover privilege.
--
-- Idempotent, and safe on a fresh database.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper
  ) THEN
    RAISE EXCEPTION
      '005_vault_scope_and_version_keys.sql must run as a PostgreSQL superuser';
  END IF;
END
$$;

-- --------------------------------------------------- preference versioning
--
-- `UNIQUE (person_id, version)` assumed one relationship per person. Nothing
-- enforces that: the same profile can be the target of two of a user's
-- relationships, and each numbers its preference models from 1. The second
-- one then collides on a row its own row-level policy hides from it -- an
-- integrity error citing a record the session cannot select.
--
-- The code already versions per relationship: `PreferenceRepo.current_model`
-- filters by relationship_id and `rebuild` increments from that. So this moves
-- the constraint onto the key the application was already using.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM preference_models a
      JOIN preference_models b
        ON b.relationship_id = a.relationship_id
       AND b.version = a.version
       AND b.id <> a.id
  ) THEN
    RAISE EXCEPTION
      'preference_models has duplicate (relationship_id, version) rows'
      USING ERRCODE = '23505',
            HINT = 'Inspect them before rerunning; they should not exist.';
  END IF;

  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'preference_models'::regclass
       AND conname = 'preference_models_person_id_version_key'
  ) THEN
    ALTER TABLE preference_models
      DROP CONSTRAINT preference_models_person_id_version_key;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'preference_models'::regclass
       AND conname = 'preference_models_relationship_version_unique'
  ) THEN
    ALTER TABLE preference_models
      ADD CONSTRAINT preference_models_relationship_version_unique
      UNIQUE (relationship_id, version);
  END IF;
END
$$;

-- ------------------------------------------------------------- the vault
--
-- `vault_records` is already out of reach of the request role: 002 and 003
-- revoke every privilege on it from `sonder_app`, which is a stronger answer
-- than scoping its policy would have been. Erasure keeps it, because erasing
-- an account has to reach every row in it.
--
-- `sonder_worker` was the one left holding a privilege it has no policy for.
-- Today that reads nothing -- row security denies by default, and the worker
-- has no policy on this table -- but a privilege with no policy is a brace
-- with no belt: it becomes reachable the moment someone adds a policy for the
-- pipeline. The pipeline has no business in the vault, so take the privilege
-- away and leave the question closed.
REVOKE ALL ON vault_records FROM sonder_worker;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_policies
     WHERE tablename = 'vault_records'
       AND policyname = 'vault_records_dyad_isolation'
  ) THEN
    -- Created by an earlier revision of this file, before it was clear the
    -- privilege had already been revoked. A policy for a role that cannot
    -- reach the table reads as though it can.
    DROP POLICY vault_records_dyad_isolation ON vault_records;
  END IF;
END
$$;
