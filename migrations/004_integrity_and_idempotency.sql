-- Cross-row integrity that cannot be expressed by ordinary single-column FKs.
-- All upgrade checks fail closed before constraints are installed.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM person_profiles p JOIN person_profiles old ON old.id = p.supersedes
    WHERE old.user_id <> p.user_id
  ) OR EXISTS (
    SELECT 1 FROM relationship_states s JOIN relationship_states old ON old.id = s.supersedes
    WHERE old.relationship_id <> s.relationship_id
  ) OR EXISTS (
    SELECT 1 FROM strategy_records s JOIN preference_models p ON p.id = s.preference_model_id
    WHERE p.relationship_id <> s.relationship_id
  ) OR EXISTS (
    SELECT 1 FROM preference_evidence e JOIN interactions i ON i.id = e.source_interaction_id
    WHERE i.relationship_id <> e.relationship_id
  ) OR EXISTS (
    SELECT 1 FROM preference_evidence e JOIN strategy_outcomes o ON o.id = e.source_strategy_outcome_id
    WHERE o.relationship_id <> e.relationship_id
  ) THEN
    RAISE EXCEPTION '004 integrity preflight found cross-owner scalar references'
      USING ERRCODE = '23514';
  END IF;
END
$$;

-- Referenced composite keys. Current fresh schemas already have these under
-- their canonical names; old schemas receive the `_upgrade` variants. Name
-- checks also make this migration safe to replay.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'voice_profiles'::regclass AND conname IN ('voice_profiles_id_person_unique', 'voice_profiles_id_person_integrity_unique')) THEN
    ALTER TABLE voice_profiles ADD CONSTRAINT voice_profiles_id_person_integrity_unique UNIQUE (id, person_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'messages'::regclass AND conname IN ('messages_id_interaction_unique', 'messages_id_interaction_integrity_unique')) THEN
    ALTER TABLE messages ADD CONSTRAINT messages_id_interaction_integrity_unique UNIQUE (id, interaction_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'signals'::regclass AND conname IN ('signals_id_relationship_unique', 'signals_id_relationship_integrity_unique')) THEN
    ALTER TABLE signals ADD CONSTRAINT signals_id_relationship_integrity_unique UNIQUE (id, relationship_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'preference_models'::regclass AND conname IN ('preference_models_id_relationship_unique', 'preference_models_id_relationship_integrity_unique')) THEN
    ALTER TABLE preference_models ADD CONSTRAINT preference_models_id_relationship_integrity_unique UNIQUE (id, relationship_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'preference_models'::regclass AND conname IN ('preference_models_id_person_unique', 'preference_models_id_person_integrity_unique')) THEN
    ALTER TABLE preference_models ADD CONSTRAINT preference_models_id_person_integrity_unique UNIQUE (id, person_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'strategy_outcomes'::regclass AND conname IN ('strategy_outcomes_id_relationship_unique', 'strategy_outcomes_id_relationship_integrity_unique')) THEN
    ALTER TABLE strategy_outcomes ADD CONSTRAINT strategy_outcomes_id_relationship_integrity_unique UNIQUE (id, relationship_id);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'person_profiles'::regclass AND conname IN ('person_profiles_supersedes_same_user', 'person_profiles_supersedes_same_user_upgrade')) THEN
    ALTER TABLE person_profiles ADD CONSTRAINT person_profiles_supersedes_same_user_upgrade FOREIGN KEY (supersedes, user_id) REFERENCES person_profiles(id, user_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'person_profiles'::regclass AND conname IN ('person_profiles_preference_model_same_person', 'person_profiles_preference_model_same_person_upgrade')) THEN
    ALTER TABLE person_profiles ADD CONSTRAINT person_profiles_preference_model_same_person_upgrade FOREIGN KEY (preference_model_id, id) REFERENCES preference_models(id, person_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'person_profiles'::regclass AND conname IN ('person_profiles_voice_profile_same_person', 'person_profiles_voice_profile_same_person_upgrade')) THEN
    ALTER TABLE person_profiles ADD CONSTRAINT person_profiles_voice_profile_same_person_upgrade FOREIGN KEY (voice_profile_id, id) REFERENCES voice_profiles(id, person_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'relationship_states'::regclass AND conname IN ('relationship_states_supersedes_same_relationship', 'relationship_states_supersedes_integrity')) THEN
    ALTER TABLE relationship_states ADD CONSTRAINT relationship_states_supersedes_integrity FOREIGN KEY (supersedes, relationship_id) REFERENCES relationship_states(id, relationship_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'preference_evidence'::regclass AND conname IN ('preference_evidence_interaction_same_relationship', 'preference_evidence_interaction_same_relationship_upgrade')) THEN
    ALTER TABLE preference_evidence ADD CONSTRAINT preference_evidence_interaction_same_relationship_upgrade FOREIGN KEY (source_interaction_id, relationship_id) REFERENCES interactions(id, relationship_id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'preference_evidence'::regclass AND conname IN ('preference_evidence_outcome_same_relationship', 'preference_evidence_outcome_same_relationship_upgrade')) THEN
    ALTER TABLE preference_evidence ADD CONSTRAINT preference_evidence_outcome_same_relationship_upgrade FOREIGN KEY (source_strategy_outcome_id, relationship_id) REFERENCES strategy_outcomes(id, relationship_id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'strategy_records'::regclass AND conname IN ('strategy_records_preference_same_relationship', 'strategy_records_preference_same_relationship_upgrade')) THEN
    ALTER TABLE strategy_records ADD CONSTRAINT strategy_records_preference_same_relationship_upgrade FOREIGN KEY (preference_model_id, relationship_id) REFERENCES preference_models(id, relationship_id) NOT VALID;
  END IF;
END
$$;

DO $$
DECLARE constraint_name text; table_name text;
BEGIN
  FOR table_name, constraint_name IN VALUES
    ('person_profiles', 'person_profiles_supersedes_same_user'),
    ('person_profiles', 'person_profiles_supersedes_same_user_upgrade'),
    ('person_profiles', 'person_profiles_preference_model_same_person'),
    ('person_profiles', 'person_profiles_preference_model_same_person_upgrade'),
    ('person_profiles', 'person_profiles_voice_profile_same_person'),
    ('person_profiles', 'person_profiles_voice_profile_same_person_upgrade'),
    ('relationship_states', 'relationship_states_supersedes_same_relationship'),
    ('relationship_states', 'relationship_states_supersedes_integrity'),
    ('preference_evidence', 'preference_evidence_interaction_same_relationship'),
    ('preference_evidence', 'preference_evidence_interaction_same_relationship_upgrade'),
    ('preference_evidence', 'preference_evidence_outcome_same_relationship'),
    ('preference_evidence', 'preference_evidence_outcome_same_relationship_upgrade'),
    ('strategy_records', 'strategy_records_preference_same_relationship'),
    ('strategy_records', 'strategy_records_preference_same_relationship_upgrade')
  LOOP
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = table_name::regclass AND conname = constraint_name AND NOT convalidated) THEN
      EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', table_name, constraint_name);
    END IF;
  END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION enforce_scoped_array_references() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE ref uuid; source_valid boolean;
BEGIN
  IF TG_TABLE_NAME = 'signals' THEN
    FOREACH ref IN ARRAY NEW.evidence_message_ids LOOP
      IF NOT EXISTS (
        SELECT 1 FROM messages m
        WHERE m.id = ref AND m.interaction_id = NEW.interaction_id
      ) THEN
        RAISE EXCEPTION 'signal evidence message is outside its interaction'
          USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'relationship_states' THEN
    FOREACH ref IN ARRAY NEW.computed_from LOOP
      IF NOT EXISTS (
        SELECT 1 FROM interactions i
        WHERE i.id = ref AND i.relationship_id = NEW.relationship_id
      ) THEN
        RAISE EXCEPTION 'state input is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'preference_evidence' THEN
    FOREACH ref IN ARRAY NEW.source_signal_ids LOOP
      IF NOT EXISTS (
        SELECT 1 FROM signals s
        WHERE s.id = ref AND s.relationship_id = NEW.relationship_id
      ) THEN
        RAISE EXCEPTION 'preference signal is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'strategy_outcomes' THEN
    FOREACH ref IN ARRAY NEW.evidence_interaction_ids LOOP
      IF NOT EXISTS (
        SELECT 1 FROM interactions i
        WHERE i.id = ref AND i.relationship_id = NEW.relationship_id
      ) THEN
        RAISE EXCEPTION 'outcome evidence is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'voice_profiles' THEN
    FOREACH ref IN ARRAY NEW.exemplar_message_ids LOOP
      IF NOT EXISTS (
        SELECT 1 FROM messages m
        JOIN interactions i ON i.id = m.interaction_id
        JOIN relationships r ON r.id = i.relationship_id
        JOIN person_profiles p ON p.id = NEW.person_id
        WHERE m.id = ref AND r.user_id = p.user_id
      ) THEN
        RAISE EXCEPTION 'voice exemplar is outside its user' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'consent_records' THEN
    IF NEW.verbatim_ref IS NOT NULL THEN
      IF NOT EXISTS (
        SELECT 1 FROM messages m JOIN interactions i ON i.id = m.interaction_id
        WHERE m.id = NEW.verbatim_ref AND i.relationship_id = NEW.relationship_id
      ) THEN
        RAISE EXCEPTION 'consent statement is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END IF;
  ELSIF TG_TABLE_NAME = 'memory_chunks' THEN
    FOREACH ref IN ARRAY NEW.source_ids LOOP
      source_valid := CASE NEW.source_kind
        WHEN 'interaction' THEN (SELECT EXISTS (SELECT 1 FROM interactions x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        WHEN 'message' THEN (SELECT EXISTS (SELECT 1 FROM messages m JOIN interactions i ON i.id = m.interaction_id WHERE m.id = ref AND i.relationship_id = NEW.relationship_id))
        WHEN 'signal' THEN (SELECT EXISTS (SELECT 1 FROM signals x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        WHEN 'state' THEN (SELECT EXISTS (SELECT 1 FROM relationship_states x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        WHEN 'preference_evidence' THEN (SELECT EXISTS (SELECT 1 FROM preference_evidence x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        WHEN 'strategy' THEN (SELECT EXISTS (SELECT 1 FROM strategy_records x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        WHEN 'outcome' THEN (SELECT EXISTS (SELECT 1 FROM strategy_outcomes x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        WHEN 'consent' THEN (SELECT EXISTS (SELECT 1 FROM consent_records x WHERE x.id = ref AND x.relationship_id = NEW.relationship_id))
        ELSE false END;
      IF NOT source_valid THEN
        RAISE EXCEPTION 'memory source is missing, untyped, or outside its relationship'
          USING ERRCODE = '23503';
      END IF;
    END LOOP;
  END IF;
  RETURN NEW;
END
$fn$;

REVOKE ALL ON FUNCTION enforce_scoped_array_references() FROM PUBLIC;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'signals','relationship_states','preference_evidence','strategy_outcomes',
    'voice_profiles','consent_records','memory_chunks'
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgrelid = t::regclass
        AND tgname IN (t || '_scoped_references', t || '_scoped_references_upgrade')
        AND NOT tgisinternal
    ) THEN
      EXECUTE format(
        'CREATE CONSTRAINT TRIGGER %I_scoped_references_upgrade
         AFTER INSERT OR UPDATE ON %I DEFERRABLE INITIALLY DEFERRED
         FOR EACH ROW EXECUTE FUNCTION enforce_scoped_array_references()', t, t
      );
    END IF;
  END LOOP;
END
$$;

-- Existing arrays are checked explicitly because constraint triggers do not
-- retroactively inspect old rows.
DO $$
DECLARE row_record record;
BEGIN
  FOR row_record IN
    SELECT s.id FROM signals s, unnest(s.evidence_message_ids) ref
    WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.id = ref AND m.interaction_id = s.interaction_id)
    UNION ALL
    SELECT s.id FROM relationship_states s, unnest(s.computed_from) ref
    WHERE NOT EXISTS (SELECT 1 FROM interactions i WHERE i.id = ref AND i.relationship_id = s.relationship_id)
    UNION ALL
    SELECT e.id FROM preference_evidence e, unnest(e.source_signal_ids) ref
    WHERE NOT EXISTS (SELECT 1 FROM signals s WHERE s.id = ref AND s.relationship_id = e.relationship_id)
    UNION ALL
    SELECT o.id FROM strategy_outcomes o, unnest(o.evidence_interaction_ids) ref
    WHERE NOT EXISTS (SELECT 1 FROM interactions i WHERE i.id = ref AND i.relationship_id = o.relationship_id)
  LOOP
    RAISE EXCEPTION '004 integrity preflight found invalid array reference in row %', row_record.id
      USING ERRCODE = '23514';
  END LOOP;

  IF EXISTS (SELECT 1 FROM memory_chunks WHERE cardinality(source_ids) > 0 AND source_kind IS NULL) THEN
    RAISE EXCEPTION '004 cannot infer memory source_kind for legacy source_ids'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1 FROM voice_profiles v, unnest(v.exemplar_message_ids) ref
    WHERE NOT EXISTS (
      SELECT 1 FROM messages m JOIN interactions i ON i.id = m.interaction_id
      JOIN relationships r ON r.id = i.relationship_id
      JOIN person_profiles p ON p.id = v.person_id
      WHERE m.id = ref AND r.user_id = p.user_id
    )
  ) OR EXISTS (
    SELECT 1 FROM consent_records c
    WHERE c.verbatim_ref IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM messages m JOIN interactions i ON i.id = m.interaction_id
      WHERE m.id = c.verbatim_ref AND i.relationship_id = c.relationship_id
    )
  ) THEN
    RAISE EXCEPTION '004 integrity preflight found invalid voice or consent reference'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1 FROM memory_chunks m, unnest(m.source_ids) ref
    WHERE NOT CASE m.source_kind
      WHEN 'interaction' THEN (SELECT EXISTS (SELECT 1 FROM interactions x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      WHEN 'message' THEN (SELECT EXISTS (SELECT 1 FROM messages x JOIN interactions i ON i.id = x.interaction_id WHERE x.id = ref AND i.relationship_id = m.relationship_id))
      WHEN 'signal' THEN (SELECT EXISTS (SELECT 1 FROM signals x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      WHEN 'state' THEN (SELECT EXISTS (SELECT 1 FROM relationship_states x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      WHEN 'preference_evidence' THEN (SELECT EXISTS (SELECT 1 FROM preference_evidence x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      WHEN 'strategy' THEN (SELECT EXISTS (SELECT 1 FROM strategy_records x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      WHEN 'outcome' THEN (SELECT EXISTS (SELECT 1 FROM strategy_outcomes x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      WHEN 'consent' THEN (SELECT EXISTS (SELECT 1 FROM consent_records x WHERE x.id = ref AND x.relationship_id = m.relationship_id))
      ELSE false END
  ) THEN
    RAISE EXCEPTION '004 integrity preflight found invalid memory source reference'
      USING ERRCODE = '23514';
  END IF;
END
$$;
