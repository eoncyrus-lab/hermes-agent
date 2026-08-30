-- Core schema, v1. Mirrors domain/schemas/*.py, which is the source of truth.
--
-- Three storage rules are enforced here rather than in application code,
-- because application code is the thing most likely to be rewritten:
--   1. observed tables are append-only (no UPDATE, no DELETE)
--   2. derived state is versioned, never overwritten
--   3. vault-tier rows live in their own table with their own grants

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ---------------------------------------------------------------- guards
CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only (attempted %)', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TYPE provenance AS ENUM
  ('user_stated','target_stated','observed','model_inference','imported');
CREATE TYPE sensitivity AS ENUM ('standard','sensitive','vault');
CREATE TYPE data_subject AS ENUM ('self','third_party');
CREATE TYPE speaker AS ENUM ('self','target','other','unknown');
CREATE TYPE direction AS ENUM ('positive','neutral','negative');
CREATE TYPE consent_status AS ENUM ('confirmed','declined','hard_limit','unknown');
CREATE TYPE outcome_label AS ENUM ('positive','neutral','negative','indeterminate');

-- ---------------------------------------------------------------- identity
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- 1/5 PersonProfile (versioned; one row per snapshot)
CREATE TABLE person_profiles (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject              text NOT NULL CHECK (subject IN ('self','target')),
  data_subject         data_subject NOT NULL,
  display_name         text NOT NULL,
  is_pseudonym         boolean NOT NULL DEFAULT true,
  version              integer NOT NULL CHECK (version >= 1),
  supersedes           uuid REFERENCES person_profiles(id) ON DELETE SET NULL,
  attributes           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- key -> Assessment
  traits               jsonb NOT NULL DEFAULT '{}'::jsonb,
  preference_model_id  uuid,
  voice_profile_id     uuid,
  first_seen_at        timestamptz,
  last_interaction_at  timestamptz,
  sensitivity          sensitivity NOT NULL DEFAULT 'standard',
  retention_until      timestamptz,
  deletion_requested_at timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT target_is_third_party
    CHECK (subject <> 'target' OR data_subject = 'third_party'),
  -- a third-party profile must always have an expiry
  CONSTRAINT third_party_expires
    CHECK (data_subject <> 'third_party' OR retention_until IS NOT NULL),
  CONSTRAINT person_profiles_id_user_unique UNIQUE (id, user_id)
);
CREATE UNIQUE INDEX person_profiles_current
  ON person_profiles (user_id, display_name, version DESC);

-- User-scoped: your own register does not change per person, and relearning
-- it for each relationship would be both wasteful and worse.
CREATE TABLE voice_profiles (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id             uuid NOT NULL REFERENCES person_profiles(id) ON DELETE CASCADE,
  version               integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  n_samples             integer NOT NULL DEFAULT 0,
  features              jsonb NOT NULL DEFAULT '{}'::jsonb,
  recurring_phrases     text[] NOT NULL DEFAULT '{}',
  avoided_constructions text[] NOT NULL DEFAULT '{}',
  exemplar_message_ids  uuid[] NOT NULL DEFAULT '{}',
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (person_id, version),
  CONSTRAINT voice_profiles_id_person_unique UNIQUE (id, person_id)
);

CREATE TABLE relationships (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  target_person_id  uuid NOT NULL,
  started_at        timestamptz NOT NULL DEFAULT now(),
  archived_at       timestamptz,
  CONSTRAINT relationship_target_same_user
    FOREIGN KEY (target_person_id, user_id)
    REFERENCES person_profiles(id, user_id) ON DELETE CASCADE,
  CONSTRAINT relationships_id_target_unique UNIQUE (id, target_person_id),
  CONSTRAINT relationships_id_user_unique UNIQUE (id, user_id)
);

-- ---------------------------------------------------------------- observed
-- 3/5 Interaction + Message + Signal. Append-only.
CREATE TABLE interactions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relationship_id        uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  channel                text NOT NULL,
  source                 text NOT NULL,
  started_at             timestamptz NOT NULL,
  ended_at               timestamptz,
  message_count_self     integer NOT NULL DEFAULT 0,
  message_count_target   integer NOT NULL DEFAULT 0,
  initiated_by           speaker NOT NULL DEFAULT 'unknown',
  ended_by               speaker NOT NULL DEFAULT 'unknown',
  median_latency_self_s  double precision,
  median_latency_target_s double precision,
  source_artifact_id     uuid,
  source_artifact_deleted boolean NOT NULL DEFAULT false,
  sensitivity            sensitivity NOT NULL DEFAULT 'standard',
  extractor_version      text NOT NULL,
  ingested_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (ended_at IS NULL OR ended_at >= started_at),
  CONSTRAINT interactions_id_relationship_unique UNIQUE (id, relationship_id)
);
CREATE INDEX ON interactions (relationship_id, started_at DESC);

CREATE TABLE messages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interaction_id    uuid NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
  sender            speaker NOT NULL,
  sent_at           timestamptz NOT NULL,
  seq               integer NOT NULL,
  body              text,             -- NULL when sensitivity = 'vault'
  text_sha256       text NOT NULL,
  reply_latency_s   double precision,
  features          jsonb,
  sensitivity       sensitivity NOT NULL DEFAULT 'standard',
  redacted          boolean NOT NULL DEFAULT false,
  UNIQUE (interaction_id, seq),
  CONSTRAINT messages_id_interaction_unique UNIQUE (id, interaction_id),
  CONSTRAINT vault_text_not_inline
    CHECK (sensitivity <> 'vault' OR body IS NULL)
);
CREATE INDEX ON messages (interaction_id, seq);

CREATE TABLE signals (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relationship_id      uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  interaction_id       uuid NOT NULL,
  type                 text NOT NULL,
  occurred_at          timestamptz NOT NULL,
  magnitude            double precision NOT NULL CHECK (magnitude BETWEEN -1 AND 1),
  confidence           double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  by_speaker           speaker NOT NULL,
  evidence_message_ids uuid[] NOT NULL CHECK (array_length(evidence_message_ids,1) >= 1),
  baseline_note        text,
  extractor_version    text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT signals_interaction_same_relationship
    FOREIGN KEY (interaction_id, relationship_id)
    REFERENCES interactions(id, relationship_id) ON DELETE CASCADE,
  CONSTRAINT signals_id_relationship_unique UNIQUE (id, relationship_id)
);
CREATE INDEX ON signals (relationship_id, occurred_at DESC);

-- ---------------------------------------------------------------- derived
-- 2/5 RelationshipState. Versioned snapshots; the trend IS the table.
CREATE TABLE relationship_states (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relationship_id  uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  version          integer NOT NULL CHECK (version >= 1),
  supersedes       uuid REFERENCES relationship_states(id) ON DELETE SET NULL,
  as_of            timestamptz NOT NULL,
  window_start     timestamptz,
  window_end       timestamptz,
  attraction       jsonb NOT NULL DEFAULT '{}'::jsonb,
  intimacy         jsonb NOT NULL DEFAULT '{}'::jsonb,
  bond             jsonb NOT NULL DEFAULT '{}'::jsonb,
  dynamics         jsonb NOT NULL DEFAULT '{}'::jsonb,
  health           jsonb NOT NULL DEFAULT '{}'::jsonb,
  compatibility    jsonb NOT NULL DEFAULT '{}'::jsonb,
  stage            text NOT NULL,
  computed_from    uuid[] NOT NULL DEFAULT '{}',
  model_version    text NOT NULL,
  input_fingerprint varchar(64) NOT NULL,
  computed_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (relationship_id, version),
  CONSTRAINT relationship_states_id_relationship_unique
    UNIQUE (id, relationship_id),
  CONSTRAINT relationship_states_relationship_fingerprint_unique
    UNIQUE (relationship_id, input_fingerprint),
  CONSTRAINT relationship_states_supersedes_same_relationship
    FOREIGN KEY (supersedes, relationship_id)
    REFERENCES relationship_states(id, relationship_id)
    DEFERRABLE INITIALLY DEFERRED,
  CONSTRAINT versions_chain CHECK (version = 1 OR supersedes IS NOT NULL)
);
CREATE INDEX ON relationship_states (relationship_id, as_of DESC);

-- 4/5 PreferenceEvidence (append-only) + its aggregate (recomputable)
CREATE TABLE preference_evidence (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id                 uuid NOT NULL,
  relationship_id           uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  domain                    text NOT NULL,
  key                       text NOT NULL,
  direction                 direction NOT NULL,
  strength                  double precision NOT NULL DEFAULT 0.5,
  provenance                provenance NOT NULL,
  observed_at               timestamptz NOT NULL,
  source_interaction_id     uuid REFERENCES interactions(id),
  source_signal_ids         uuid[] NOT NULL DEFAULT '{}',
  source_strategy_outcome_id uuid,
  stage_at_time             text,
  channel                   text,
  in_group_setting          boolean NOT NULL DEFAULT false,
  known_confounders         text[] NOT NULL DEFAULT '{}',
  excluded_from_learning    boolean NOT NULL DEFAULT false,
  sensitivity               sensitivity NOT NULL DEFAULT 'standard',
  recorded_at               timestamptz NOT NULL DEFAULT now(),
  -- the Preference Hallucination guard, at the storage layer
  CONSTRAINT protected_domains_need_statement CHECK (
    domain NOT IN ('physical','sexual','kink')
    OR (provenance IN ('user_stated','target_stated') AND sensitivity = 'vault')
  ),
  CONSTRAINT must_cite_source CHECK (
    source_interaction_id IS NOT NULL
    OR array_length(source_signal_ids,1) >= 1
    OR source_strategy_outcome_id IS NOT NULL
    OR provenance IN ('user_stated','target_stated')
  ),
  CONSTRAINT preference_evidence_person_same_relationship
    FOREIGN KEY (relationship_id, person_id)
    REFERENCES relationships(id, target_person_id) ON DELETE CASCADE
);
CREATE INDEX ON preference_evidence (person_id, key, observed_at DESC);

CREATE TABLE preference_models (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL,
  relationship_id  uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  version          integer NOT NULL CHECK (version >= 1),
  entries          jsonb NOT NULL DEFAULT '{}'::jsonb,
  scorer_version   text NOT NULL,
  input_fingerprint varchar(64) NOT NULL,
  computed_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (person_id, version),
  CONSTRAINT preference_models_relationship_fingerprint_unique
    UNIQUE (relationship_id, input_fingerprint),
  CONSTRAINT preference_models_id_relationship_unique UNIQUE (id, relationship_id),
  CONSTRAINT preference_models_id_person_unique UNIQUE (id, person_id),
  CONSTRAINT preference_models_person_same_relationship
    FOREIGN KEY (relationship_id, person_id)
    REFERENCES relationships(id, target_person_id) ON DELETE CASCADE
);

-- 5/5 StrategyRecord (prediction) and StrategyOutcome (observation)
CREATE TABLE strategy_records (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relationship_id        uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  user_id                uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  objective              text NOT NULL,
  tactic_keys            text[] NOT NULL CHECK (array_length(tactic_keys,1) >= 1),
  avoided_keys           text[] NOT NULL DEFAULT '{}',
  relationship_state_id  uuid NOT NULL,
  preference_model_id    uuid REFERENCES preference_models(id),
  skills_invoked         text[] NOT NULL DEFAULT '{}',
  rendered_text_sha256   text NOT NULL,
  rendered_text          text,
  n_candidates           integer NOT NULL DEFAULT 1,
  critic                 jsonb,
  predicted_positive     double precision NOT NULL
                           CHECK (predicted_positive BETWEEN 0 AND 1),
  predicted_detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
  prediction_basis       text NOT NULL CHECK (length(btrim(prediction_basis)) > 0),
  observation_window_s   integer NOT NULL DEFAULT 259200,
  user_action            text NOT NULL DEFAULT 'not_yet',
  shown_at               timestamptz NOT NULL DEFAULT now(),
  sent_at                timestamptz,
  model_version          text NOT NULL,
  CONSTRAINT strategy_records_id_relationship_unique
    UNIQUE (id, relationship_id),
  CONSTRAINT strategy_records_user_same_relationship
    FOREIGN KEY (relationship_id, user_id)
    REFERENCES relationships(id, user_id) ON DELETE CASCADE,
  CONSTRAINT strategy_records_state_same_relationship
    FOREIGN KEY (relationship_state_id, relationship_id)
    REFERENCES relationship_states(id, relationship_id),
  CONSTRAINT strategy_records_preference_same_relationship
    FOREIGN KEY (preference_model_id, relationship_id)
    REFERENCES preference_models(id, relationship_id)
);
CREATE INDEX ON strategy_records (relationship_id, shown_at DESC);

CREATE TABLE strategy_outcomes (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id              uuid NOT NULL UNIQUE,
  relationship_id          uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  observed                 jsonb NOT NULL,
  label                    outcome_label NOT NULL,
  evidence_interaction_ids uuid[] NOT NULL DEFAULT '{}',
  window_start             timestamptz NOT NULL,
  window_end               timestamptz NOT NULL,
  confounders              text[] NOT NULL DEFAULT '{}',
  valid_for_learning       boolean NOT NULL DEFAULT true,
  scorer_version           text NOT NULL,
  recorded_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (window_end >= window_start),
  CONSTRAINT boundary_forces_negative CHECK (
    NOT COALESCE((observed->>'discomfort_or_boundary')::boolean, false)
    OR label = 'negative'
  ),
  CONSTRAINT indeterminate_never_trains CHECK (
    label <> 'indeterminate' OR valid_for_learning = false
  ),
  CONSTRAINT strategy_outcomes_strategy_same_relationship
    FOREIGN KEY (strategy_id, relationship_id)
    REFERENCES strategy_records(id, relationship_id),
  CONSTRAINT strategy_outcomes_id_relationship_unique UNIQUE (id, relationship_id)
);

-- ---------------------------------------------------------------- vault
-- doc s45. Separate table, separate grants, separate key. Nothing here is
-- ever inferred: every row cites a statement someone actually made.
CREATE TABLE vault_records (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  relationship_id uuid REFERENCES relationships(id) ON DELETE CASCADE,
  kind            text NOT NULL,
  ciphertext      bytea NOT NULL,
  key_version     text NOT NULL,
  provenance      provenance NOT NULL
                    CHECK (provenance IN ('user_stated','target_stated')),
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE consent_records (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relationship_id uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  activity        text NOT NULL,
  status          consent_status NOT NULL,
  provenance      provenance NOT NULL
                    CHECK (provenance IN ('user_stated','target_stated')),
  stated_at       timestamptz NOT NULL,
  stated_by       text NOT NULL CHECK (stated_by IN ('self','target')),
  verbatim_ref    uuid,
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT known_consent_cites_statement
    CHECK (status = 'unknown' OR verbatim_ref IS NOT NULL)
);
CREATE INDEX ON consent_records (relationship_id, activity, stated_at DESC);

-- ---------------------------------------------------------------- append-only
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'interactions','messages','signals','relationship_states',
    'preference_evidence','strategy_records','strategy_outcomes',
    'consent_records'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I_append_only BEFORE UPDATE ON %I
         FOR EACH ROW EXECUTE FUNCTION reject_mutation()', t, t);
  END LOOP;
END $$;

-- Exception: strategy_records.user_action and sent_at are set once, after
-- the row is written, when the user acts on the suggestion. Everything else
-- on that row -- above all predicted_positive -- stays frozen.
DROP TRIGGER strategy_records_append_only ON strategy_records;
CREATE OR REPLACE FUNCTION strategy_records_guard() RETURNS trigger AS $$
BEGIN
  IF NEW.predicted_positive IS DISTINCT FROM OLD.predicted_positive
     OR NEW.predicted_detail IS DISTINCT FROM OLD.predicted_detail
     OR NEW.prediction_basis IS DISTINCT FROM OLD.prediction_basis
     OR NEW.rendered_text_sha256 IS DISTINCT FROM OLD.rendered_text_sha256
     OR NEW.relationship_state_id IS DISTINCT FROM OLD.relationship_state_id THEN
    RAISE EXCEPTION 'prediction fields are immutable once shown';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER strategy_records_guard BEFORE UPDATE ON strategy_records
  FOR EACH ROW EXECUTE FUNCTION strategy_records_guard();

-- Semantic memory. Pointers only: the structured state above stays in SQL.
--
-- Deliberately NOT append-only, unlike every other derived table here.
-- A memory chunk is a regenerable summary over source_ids, so re-summarising
-- or re-embedding is a legitimate update rather than a rewrite of history --
-- the history itself lives in the append-only rows it points at.
CREATE TABLE memory_chunks (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- NOT NULL is load-bearing: a nullable scope means "global memory", and a
  -- vector search that forgets the filter will happily surface person A's
  -- inside joke as context for person B.
  relationship_id uuid NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
  kind            text NOT NULL,
  summary         text NOT NULL,
  embedding       vector(1536),
  source_ids      uuid[] NOT NULL DEFAULT '{}',
  source_kind     text,
  sensitivity     sensitivity NOT NULL DEFAULT 'standard',
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT memory_sources_are_typed
    CHECK (cardinality(source_ids) = 0 OR source_kind IS NOT NULL)
);
CREATE INDEX ON memory_chunks USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX ON memory_chunks (relationship_id, kind, created_at DESC);

-- ------------------------------------------------------ reference integrity
-- Composite scalar references cannot cross an account/dyad. Array references
-- are checked at commit so a batch may insert sources and derived rows in any
-- order without weakening validation.
ALTER TABLE person_profiles
  ADD CONSTRAINT person_profiles_supersedes_same_user
  FOREIGN KEY (supersedes, user_id) REFERENCES person_profiles(id, user_id)
  DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE person_profiles
  ADD CONSTRAINT person_profiles_preference_model_same_person
  FOREIGN KEY (preference_model_id, id) REFERENCES preference_models(id, person_id)
  DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE person_profiles
  ADD CONSTRAINT person_profiles_voice_profile_same_person
  FOREIGN KEY (voice_profile_id, id) REFERENCES voice_profiles(id, person_id)
  DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE preference_evidence
  ADD CONSTRAINT preference_evidence_interaction_same_relationship
  FOREIGN KEY (source_interaction_id, relationship_id)
  REFERENCES interactions(id, relationship_id);
ALTER TABLE preference_evidence
  ADD CONSTRAINT preference_evidence_outcome_same_relationship
  FOREIGN KEY (source_strategy_outcome_id, relationship_id)
  REFERENCES strategy_outcomes(id, relationship_id);

CREATE OR REPLACE FUNCTION enforce_scoped_array_references() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE ref uuid; source_valid boolean;
BEGIN
  IF TG_TABLE_NAME = 'signals' THEN
    FOREACH ref IN ARRAY NEW.evidence_message_ids LOOP
      IF NOT EXISTS (SELECT 1 FROM messages m WHERE m.id = ref AND m.interaction_id = NEW.interaction_id) THEN
        RAISE EXCEPTION 'signal evidence message is outside its interaction' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'relationship_states' THEN
    FOREACH ref IN ARRAY NEW.computed_from LOOP
      IF NOT EXISTS (SELECT 1 FROM interactions i WHERE i.id = ref AND i.relationship_id = NEW.relationship_id) THEN
        RAISE EXCEPTION 'state input is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'preference_evidence' THEN
    FOREACH ref IN ARRAY NEW.source_signal_ids LOOP
      IF NOT EXISTS (SELECT 1 FROM signals s WHERE s.id = ref AND s.relationship_id = NEW.relationship_id) THEN
        RAISE EXCEPTION 'preference signal is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'strategy_outcomes' THEN
    FOREACH ref IN ARRAY NEW.evidence_interaction_ids LOOP
      IF NOT EXISTS (SELECT 1 FROM interactions i WHERE i.id = ref AND i.relationship_id = NEW.relationship_id) THEN
        RAISE EXCEPTION 'outcome evidence is outside its relationship' USING ERRCODE = '23503';
      END IF;
    END LOOP;
  ELSIF TG_TABLE_NAME = 'voice_profiles' THEN
    FOREACH ref IN ARRAY NEW.exemplar_message_ids LOOP
      IF NOT EXISTS (
        SELECT 1 FROM messages m JOIN interactions i ON i.id = m.interaction_id
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
        RAISE EXCEPTION 'memory source is missing, untyped, or outside its relationship' USING ERRCODE = '23503';
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
    EXECUTE format(
      'CREATE CONSTRAINT TRIGGER %I_scoped_references AFTER INSERT OR UPDATE ON %I
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
       EXECUTE FUNCTION enforce_scoped_array_references()', t, t
    );
  END LOOP;
END
$$;


-- ------------------------------------------------------- cross-dyad priors
-- The ONLY sanctioned channel between relationships. Carries Beta parameters
-- for a tactic key and nothing else: no person, no text, no context. It feeds
-- BetaEstimate.prior_alpha/prior_beta so a new relationship starts with the
-- user's own base rate instead of a flat prior -- and gets overwhelmed by
-- person-level evidence as n grows.
--
-- k >= 3 distinct relationships is required before a prior exists at all,
-- otherwise this degenerates into "what worked on my ex, applied to her".
CREATE TABLE user_tactic_priors (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tactic_key       text NOT NULL,
  domain           text NOT NULL,
  prior_alpha      double precision NOT NULL CHECK (prior_alpha > 0),
  prior_beta       double precision NOT NULL CHECK (prior_beta > 0),
  n_relationships  integer NOT NULL CHECK (n_relationships >= 3),
  computed_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, tactic_key),
  -- protected domains never form cross-person priors, at any k
  CONSTRAINT no_protected_priors
    CHECK (domain NOT IN ('physical','sexual','kink'))
);

-- ------------------------------------------------------- row-level security
-- Application-layer scoping WILL eventually be forgotten in one query. This
-- is the backstop: with app.relationship_id set, a missing WHERE clause
-- returns nothing instead of returning someone else.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'interactions','signals','relationship_states','preference_evidence',
    'preference_models','strategy_records','strategy_outcomes',
    'consent_records','memory_chunks'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I_dyad_isolation ON %I USING (
         relationship_id = NULLIF(current_setting(''app.relationship_id'', true), '''')::uuid
       )', t, t);
  END LOOP;
END $$;

-- messages are scoped through their interaction
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
CREATE POLICY messages_dyad_isolation ON messages USING (
  interaction_id IN (
    SELECT id FROM interactions
    WHERE relationship_id
        = NULLIF(current_setting('app.relationship_id', true), '')::uuid
  )
);

-- Deliberately NOT under dyad RLS, because they are user-scoped by design:
--   person_profiles (self), user_tactic_priors, voice profiles.
-- The user's own voice is shared across every relationship on purpose;
-- relearning it per person would be both wasteful and worse.

-- ------------------------------------------------------------------ access
-- Multi-user from the start. Tokens are stored hashed: a database dump of a
-- system holding this kind of data should not also hand over live sessions.
CREATE TABLE api_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_sha256 text NOT NULL UNIQUE,
  label        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at   timestamptz
);
CREATE INDEX ON api_tokens (token_sha256) WHERE revoked_at IS NULL;

-- Telegram identity -> account. The bot must never infer who is talking to it
-- from message content; an unmapped chat gets nothing.
CREATE TABLE telegram_accounts (
  telegram_user_id bigint PRIMARY KEY,
  user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  linked_at        timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------- roles
-- Two roles, and the split is the point.
--
--   sonder_app   -- every request path. NOBYPASSRLS, so a forgotten WHERE
--                   clause returns nothing instead of returning someone else.
--   sonder_maint -- erasure, outcome scoring, artifact purging. BYPASSRLS,
--                   because these legitimately span relationships. Nothing on
--                   a request path may use it without its own ownership check.
--
-- Run once per deployment, with real passwords. Kept here rather than in a
-- runbook because an app running as the owner or a superuser silently
-- disables every policy above.
--
--   CREATE ROLE sonder_app   LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '...';
--   CREATE ROLE sonder_maint LOGIN NOSUPERUSER BYPASSRLS   PASSWORD '...';
--   GRANT USAGE ON SCHEMA public TO sonder_app, sonder_maint;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
--     TO sonder_app, sonder_maint;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sonder_app, sonder_maint;
