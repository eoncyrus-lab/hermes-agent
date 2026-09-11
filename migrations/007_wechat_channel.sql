-- The WeChat channel's identity mapping, and the one role allowed to read it.
--
-- Pairing Tencent's ClawBot binds a bot account, and every inbound message
-- carries the sender's WeChat id. That id is the whole of this channel's
-- authentication: whoever can see the chat can see the id. So it is handled
-- the way a Telegram id already is -- a mapping table, one SECURITY DEFINER
-- RPC, and a role that holds nothing else.
--
-- A role separate from `sonder_bot` for the same reason `sonder_bot` is
-- separate from `sonder_identity`: two RPCs that are not equally hard to abuse
-- should not share a credential, and a process should hold the narrowest one
-- that does its job. A compromised WeChat bridge must not also hand over
-- `auth_telegram`, which takes a guessable ten-digit id.
--
-- Idempotent, and safe on a fresh database.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper
  ) THEN
    RAISE EXCEPTION
      '007_wechat_channel.sql must run as a PostgreSQL superuser';
  END IF;
END
$$;

-- ---------------------------------------------------------------- the table
CREATE TABLE IF NOT EXISTS wechat_accounts (
  wechat_user_id text PRIMARY KEY,
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  linked_at      timestamptz NOT NULL DEFAULT now()
);

-- The RPC reads by `wechat_user_id`, which the primary key already covers.
-- This index is for the other direction: `ON DELETE CASCADE` from `users` has
-- to find a user's rows when an account is erased, and erasure is a security
-- path -- a sequential scan there is a cost paid at the worst possible moment.
CREATE INDEX IF NOT EXISTS wechat_accounts_user_id_idx
  ON wechat_accounts (user_id);

-- ----------------------------------------------------------------- the role
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sonder_wechat') THEN
    CREATE ROLE sonder_wechat LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
END
$$;

DO $$
BEGIN
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO sonder_wechat', current_database()
  );
END
$$;
GRANT USAGE ON SCHEMA public TO sonder_wechat;

-- ------------------------------------------------------------------- the RPC
--
-- The claim prefix is `w:`, distinct from `auth_bearer`'s and
-- `auth_telegram`'s. A claim is only ever *verified* against the signing key,
-- so the prefix is not what stops a cross-channel replay -- but it does mean a
-- claim found in a log names the channel it was minted for, and it keeps the
-- three claims from being byte-identical for the same user at the same second.
CREATE OR REPLACE FUNCTION auth_wechat(chat_user_id text)
RETURNS TABLE (user_id uuid, identity_claim text)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  WITH matched AS (
    SELECT w.user_id,
           extract(epoch FROM now() + interval '5 minutes')::bigint AS expires_at
      FROM public.wechat_accounts w
     WHERE w.wechat_user_id = chat_user_id
  )
  SELECT m.user_id,
         'w:' || chat_user_id || ':' || m.expires_at::text || ':' || encode(
           hmac(
             convert_to(
               m.user_id::text || ':w:' || chat_user_id || ':' || m.expires_at::text,
               'UTF8'
             ),
             k.secret,
             'sha256'
           ),
           'hex'
         )
    FROM matched m
    CROSS JOIN sonder_private.identity_signing_key k
$$;

REVOKE ALL ON FUNCTION auth_wechat(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_wechat(text) TO sonder_wechat;

-- ------------------------------------------------- and the claim it is worth
--
-- `app_current_user_id()` was taught two claim kinds by 003: `b` for a bearer
-- token digest and `t` for a Telegram id. A `w` claim carrying a valid
-- signature still matched no branch, so every WeChat session resolved to no
-- user and read zero rows -- the failure is silent, which is the worst shape
-- for this particular function to fail in.
--
-- Replaced here rather than amended in 003 because 003 has to keep meaning
-- what it meant when it was applied. Operators are told to run each file once,
-- in order, and someone who ran 003 months ago is not going to run it again.
CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  WITH context AS (
    SELECT NULLIF(current_setting('app.user_id', true), '')::uuid AS user_id,
           split_part(current_setting('app.identity_claim', true), ':', 1) AS source_kind,
           split_part(current_setting('app.identity_claim', true), ':', 2) AS source_id,
           NULLIF(split_part(current_setting('app.identity_claim', true), ':', 3), '') AS expires_text,
           NULLIF(split_part(current_setting('app.identity_claim', true), ':', 4), '') AS signature
  ), parsed AS (
    SELECT user_id, source_kind, source_id,
           CASE WHEN expires_text ~ '^[0-9]+$' THEN expires_text::bigint END AS expires_at,
           signature
      FROM context
  )
  SELECT p.user_id
    FROM parsed p
    CROSS JOIN sonder_private.identity_signing_key k
   WHERE p.expires_at >= extract(epoch FROM now())::bigint
     AND p.expires_at <= extract(epoch FROM now() + interval '10 minutes')::bigint
     AND (
       (p.source_kind = 'b' AND EXISTS (
         SELECT 1 FROM public.api_tokens t
          WHERE t.user_id = p.user_id
            AND t.token_sha256 = p.source_id
            AND t.revoked_at IS NULL
       ))
       OR
       (p.source_kind = 't' AND p.source_id ~ '^[0-9]+$' AND EXISTS (
         SELECT 1 FROM public.telegram_accounts t
          WHERE t.user_id = p.user_id
            AND t.telegram_user_id = CASE
              WHEN p.source_id ~ '^[0-9]+$' THEN p.source_id::bigint
              ELSE NULL
            END
       ))
       OR
       (p.source_kind = 'w' AND p.source_id <> '' AND EXISTS (
         SELECT 1 FROM public.wechat_accounts w
          WHERE w.user_id = p.user_id
            AND w.wechat_user_id = p.source_id
       ))
     )
     AND p.signature = encode(
       hmac(
         convert_to(
           p.user_id::text || ':' || p.source_kind || ':' || p.source_id || ':' || p.expires_at::text,
           'UTF8'
         ),
         k.secret,
         'sha256'
       ),
       'hex'
     )
$$;

-- Exactly the three roles 003 granted, deliberately not widened. The worker is
-- absent on purpose: a queued job carries no user identity to resolve, and
-- handing it this function would suggest otherwise.
REVOKE ALL ON FUNCTION app_current_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_user_id()
  TO sonder_app, sonder_erase, sonder_maint;


-- No service role reads the mapping table itself, the same way none reads
-- `telegram_accounts`. The RPC is SECURITY DEFINER, so it does not need one --
-- and a role that cannot read the mapping cannot enumerate who is reachable.
REVOKE ALL ON wechat_accounts FROM sonder_app, sonder_erase, sonder_worker,
  sonder_identity, sonder_bot, sonder_wechat;

-- Linking and unlinking are operator actions, like `sonder telegram link`, so
-- they run on the maintenance identity and are not reachable from a request.
GRANT SELECT, INSERT, UPDATE, DELETE ON wechat_accounts TO sonder_maint;
