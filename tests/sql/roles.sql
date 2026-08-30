-- Test-only passwords for the four service roles.
--
-- The migrations create these roles WITHOUT a password on purpose, so nothing
-- in the deployed tree is a published credential. Real deployments set them
-- from the environment (scripts/db-roles.sh); the throwaway container this
-- file runs in sets them here so the suite can connect as each role and check
-- that the boundaries actually hold.
ALTER ROLE sonder_app      LOGIN PASSWORD 'sonder_app';
ALTER ROLE sonder_maint    LOGIN PASSWORD 'sonder_maint';
ALTER ROLE sonder_identity LOGIN PASSWORD 'sonder_identity';
ALTER ROLE sonder_erase    LOGIN PASSWORD 'sonder_erase';
ALTER ROLE sonder_worker   LOGIN PASSWORD 'sonder_worker';
ALTER ROLE sonder_bot      LOGIN PASSWORD 'sonder_bot';
