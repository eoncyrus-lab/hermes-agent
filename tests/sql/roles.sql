-- Test-only passwords supplied by docker-compose.test.yml. They are generated
-- afresh by scripts/check.sh and never stored in the repository.
--
-- The migrations create these roles WITHOUT a password on purpose, so nothing
-- in the deployed tree is a published credential. Real deployments set them
-- from the environment (scripts/db-roles.sh); the throwaway container this
-- file runs in sets them here so the suite can connect as each role and check
-- that the boundaries actually hold.
\getenv app_password SONDER_TEST_APP_DB_PASSWORD
\getenv maint_password SONDER_TEST_MAINT_DB_PASSWORD
\getenv identity_password SONDER_TEST_IDENTITY_DB_PASSWORD
\getenv erase_password SONDER_TEST_ERASURE_DB_PASSWORD
\getenv worker_password SONDER_TEST_WORKER_DB_PASSWORD
\getenv bot_password SONDER_TEST_BOT_DB_PASSWORD

ALTER ROLE sonder_app      LOGIN PASSWORD :'app_password';
ALTER ROLE sonder_maint    LOGIN PASSWORD :'maint_password';
ALTER ROLE sonder_identity LOGIN PASSWORD :'identity_password';
ALTER ROLE sonder_erase    LOGIN PASSWORD :'erase_password';
ALTER ROLE sonder_worker   LOGIN PASSWORD :'worker_password';
ALTER ROLE sonder_bot      LOGIN PASSWORD :'bot_password';
