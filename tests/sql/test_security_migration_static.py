"""Static assertions about the security migrations.

These run without a database, so they hold in CI on a laptop with no Postgres.
The behavioural proof lives in tests/test_identity_boundary.py; this file
guards the properties that are easiest to lose in an edit and hardest to
notice: a credential written into the tree, a constraint added without being
validated, a role handed a privilege it should never have.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).parents[2] / "migrations"
SECURITY = MIGRATIONS / "002_security_boundaries.sql"
IDENTITY = MIGRATIONS / "003_identity_and_idempotency.sql"


def test_security_migration_requires_admin_and_removes_cross_dyad_oracle():
    sql = SECURITY.read_text()
    assert "must run as a PostgreSQL superuser" in sql
    assert "rolname = session_user AND rolsuper" in sql
    assert sql.count("IF NOT EXISTS (SELECT FROM pg_roles") == 2
    assert "DROP FUNCTION IF EXISTS other_dyad_phrases_for_current_relationship" in sql
    assert "GRANT EXECUTE ON FUNCTION other_dyad" not in sql


def test_upgrade_covers_all_composite_ownership_constraints():
    sql = SECURITY.read_text()
    for constraint in (
        "signals_interaction_same_relationship",
        "preference_evidence_person_same_relationship",
        "preference_models_person_same_relationship",
        "strategy_records_user_same_relationship",
        "strategy_records_state_same_relationship",
        "strategy_outcomes_strategy_same_relationship",
    ):
        assert f"ADD CONSTRAINT {constraint}" in sql
        assert f"VALIDATE CONSTRAINT {constraint}" in sql


def test_no_migration_writes_a_role_password():
    """A password in a migration is a published credential everywhere it runs.

    Roles are created with no password at all, which under md5/scram means
    they cannot authenticate until an operator sets one out of band.
    """
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue  # the runbook comment in 001 shows the shape, not a value
            assert "PASSWORD" not in stripped.upper(), f"{path.name}: {stripped}"


def test_identity_migration_requires_admin():
    sql = IDENTITY.read_text()
    assert "must run as a PostgreSQL superuser" in sql
    assert "rolname = session_user AND rolsuper" in sql


def _statements(*paths: Path) -> list[str]:
    """Statement-ish fragments with comments stripped, for keyword checks."""
    out: list[str] = []
    for path in paths:
        body = "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.strip().startswith("--")
        )
        out.extend(fragment.strip() for fragment in body.split(";") if fragment.strip())
    return out


def test_only_the_maintenance_role_may_bypass_row_security():
    """One BYPASSRLS role, reserved for scheduled cross-user jobs."""
    granted = [
        fragment
        for fragment in _statements(IDENTITY, SECURITY)
        if "BYPASSRLS" in fragment.replace("NOBYPASSRLS", "")
    ]
    assert granted, "expected the maintenance role to be declared"
    assert all("sonder_maint" in fragment for fragment in granted), granted


def test_every_user_scoped_table_gets_a_policy():
    sql = IDENTITY.read_text()
    for table in (
        "person_profiles",
        "relationships",
        "vault_records",
        "user_tactic_priors",
        "telegram_accounts",
        "api_tokens",
        "users",
        "voice_profiles",
    ):
        assert table in sql, f"{table} has no user-scoped policy"
    assert "app_current_user_id()" in sql


def test_the_dyad_policy_checks_ownership_not_just_the_pinned_id():
    """Knowing a relationship id must not be enough to read it."""
    sql = IDENTITY.read_text()
    assert "r.user_id = app_current_user_id()" in sql
    assert "FOR ALL TO sonder_app" in sql


def test_the_identity_role_is_granted_no_table_privileges():
    sql = IDENTITY.read_text()
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sonder_identity" in sql
    grants = [
        line
        for line in sql.splitlines()
        if line.strip().startswith("GRANT")
        and "sonder_identity" in line
        and "ON FUNCTION" not in line
    ]
    allowed = ("GRANT CONNECT", "GRANT USAGE ON SCHEMA")
    for line in grants:
        assert any(token in line for token in allowed), line


def test_the_identity_claim_is_verified_against_a_live_token():
    """Setting app.user_id must prove nothing on its own."""
    sql = IDENTITY.read_text()
    assert "FROM api_tokens t" in sql
    assert "t.token_sha256 = NULLIF(current_setting('app.identity_claim'" in sql
    assert "t.revoked_at IS NULL" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = public, pg_temp" in sql


def test_recompute_idempotency_is_enforced_by_the_schema():
    sql = IDENTITY.read_text()
    assert "input_fingerprint" in sql
    assert "relationship_states_relationship_fingerprint_unique" in sql
    assert "preference_models_relationship_fingerprint_unique" in sql
    assert "VALIDATE CONSTRAINT relationship_states_supersedes_same_relationship" in sql
