-- Bind relationship-scoped vault rows to the same owning user and remove
-- duplicate integrity objects created by early revisions of migration 004.
-- Safe to replay. Legacy ownership violations fail closed before DDL changes.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper
  ) THEN
    RAISE EXCEPTION
      '006_vault_ownership_and_cleanup.sql must run as a PostgreSQL superuser';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM vault_records v
    LEFT JOIN relationships r ON r.id = v.relationship_id
    WHERE v.relationship_id IS NOT NULL
      AND (r.id IS NULL OR r.user_id <> v.user_id)
  ) THEN
    RAISE EXCEPTION '006 vault ownership preflight found a cross-owner or orphan relationship'
      USING ERRCODE = '23514',
            HINT = 'Correct vault_records.user_id/relationship_id ownership before rerunning.';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'vault_records'::regclass
      AND conname = 'vault_records_relationship_same_user'
  ) THEN
    ALTER TABLE vault_records
      ADD CONSTRAINT vault_records_relationship_same_user
      FOREIGN KEY (relationship_id, user_id)
      REFERENCES relationships(id, user_id)
      ON DELETE CASCADE NOT VALID;
  END IF;
END
$$;

ALTER TABLE vault_records
  VALIDATE CONSTRAINT vault_records_relationship_same_user;

-- The composite key now provides the same relationship cascade while also
-- checking its owner. Remove the legacy single-column relationship FK.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'vault_records'::regclass
      AND conname = 'vault_records_relationship_id_fkey'
  ) THEN
    ALTER TABLE vault_records
      DROP CONSTRAINT vault_records_relationship_id_fkey;
  END IF;
END
$$;

-- A fresh 001 already supplied the canonical objects. An early 004 installed
-- equivalent `_upgrade`/`_integrity` objects as well; retain the canonical
-- object and remove only its known duplicate.
DO $$
DECLARE item text[];
BEGIN
  FOREACH item SLICE 1 IN ARRAY ARRAY[
    ARRAY['voice_profiles', 'voice_profiles_id_person_unique', 'voice_profiles_id_person_integrity_unique'],
    ARRAY['messages', 'messages_id_interaction_unique', 'messages_id_interaction_integrity_unique'],
    ARRAY['signals', 'signals_id_relationship_unique', 'signals_id_relationship_integrity_unique'],
    ARRAY['preference_models', 'preference_models_id_relationship_unique', 'preference_models_id_relationship_integrity_unique'],
    ARRAY['preference_models', 'preference_models_id_person_unique', 'preference_models_id_person_integrity_unique'],
    ARRAY['strategy_outcomes', 'strategy_outcomes_id_relationship_unique', 'strategy_outcomes_id_relationship_integrity_unique'],
    ARRAY['person_profiles', 'person_profiles_supersedes_same_user', 'person_profiles_supersedes_same_user_upgrade'],
    ARRAY['person_profiles', 'person_profiles_preference_model_same_person', 'person_profiles_preference_model_same_person_upgrade'],
    ARRAY['person_profiles', 'person_profiles_voice_profile_same_person', 'person_profiles_voice_profile_same_person_upgrade'],
    ARRAY['relationship_states', 'relationship_states_supersedes_same_relationship', 'relationship_states_supersedes_integrity'],
    ARRAY['preference_evidence', 'preference_evidence_interaction_same_relationship', 'preference_evidence_interaction_same_relationship_upgrade'],
    ARRAY['preference_evidence', 'preference_evidence_outcome_same_relationship', 'preference_evidence_outcome_same_relationship_upgrade'],
    ARRAY['strategy_records', 'strategy_records_preference_same_relationship', 'strategy_records_preference_same_relationship_upgrade']
  ] LOOP
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = item[1]::regclass AND conname = item[2])
       AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = item[1]::regclass AND conname = item[3]) THEN
      EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', item[1], item[3]);
    END IF;
  END LOOP;
END
$$;

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'signals','relationship_states','preference_evidence','strategy_outcomes',
    'voice_profiles','consent_records','memory_chunks'
  ] LOOP
    IF EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgrelid = table_name::regclass
        AND tgname = table_name || '_scoped_references'
        AND NOT tgisinternal
    ) AND EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgrelid = table_name::regclass
        AND tgname = table_name || '_scoped_references_upgrade'
        AND NOT tgisinternal
    ) THEN
      EXECUTE format(
        'DROP TRIGGER %I_scoped_references_upgrade ON %I',
        table_name, table_name
      );
    END IF;
  END LOOP;
END
$$;
