"""Skill-learning schema tests — issue 01.

Verifies the Dolt schema for episodes, candidates, and skills tables,
the formulas → skills migration, and harness user grants.
All tests are @pytest.mark.integration.
"""

import json
import os
import uuid

import pymysql
import pymysql.cursors
import pytest

DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


def get_root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5,
    )


def get_harness_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="harness", password="harness",
        database="harness", connect_timeout=5,
    )


# ---------------------------------------------------------------------------
# Tracer bullet: episodes table exists
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_episodes_table_exists():
    """episodes table is present in the harness schema."""
    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'episodes'")
            assert cur.fetchone() is not None, "episodes table not found"


@pytest.mark.integration
def test_episodes_columns():
    """episodes table has all spec-required columns."""
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DESCRIBE episodes")
            cols = {row["Field"] for row in cur.fetchall()}
    required = {
        "episode_id", "created_at", "agent_principal", "alert_signature",
        "service_class", "env_fingerprint", "diagnosis", "actions",
        "outcome", "outcome_signal", "outcome_labeled_at", "human_actor",
    }
    missing = required - cols
    assert not missing, f"episodes table missing columns: {missing}"


@pytest.mark.integration
def test_candidates_table_exists():
    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'candidates'")
            assert cur.fetchone() is not None, "candidates table not found"


@pytest.mark.integration
def test_candidates_columns():
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DESCRIBE candidates")
            cols = {row["Field"] for row in cur.fetchall()}
    required = {
        "candidate_id", "created_at", "cluster_key",
        "member_episode_ids", "proposed_procedure", "support_stats", "status",
    }
    missing = required - cols
    assert not missing, f"candidates table missing columns: {missing}"


@pytest.mark.integration
def test_skills_table_exists():
    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'skills'")
            assert cur.fetchone() is not None, "skills table not found"


@pytest.mark.integration
def test_skills_columns():
    """skills table has all spec columns plus legacy formula fields for lookup."""
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DESCRIBE skills")
            cols = {row["Field"] for row in cur.fetchall()}
    required = {
        "id", "name", "agent_role", "description", "version", "status",
        "input_schema", "steps", "output_contract",
        "promoted_by", "source_candidate_id", "expires_at", "revoked_reason", "created_at",
    }
    missing = required - cols
    assert not missing, f"skills table missing columns: {missing}"
    assert "quality_score" not in cols, "quality_score should have been dropped"


@pytest.mark.integration
def test_seeded_skills_present():
    """Three seeded skills are present in skills after migration."""
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, promoted_by, source_candidate_id, expires_at FROM skills ORDER BY id")
            rows = {r["id"]: r for r in cur.fetchall()}
    expected_ids = {"sre:triage-incident", "code_reviewer:review-pr", "architect:write-adr"}
    assert expected_ids <= rows.keys(), f"missing seeded skills: {expected_ids - rows.keys()}"
    for skill_id in expected_ids:
        row = rows[skill_id]
        assert row["promoted_by"] == "seed", f"{skill_id}: promoted_by should be 'seed'"
        assert row["source_candidate_id"] is None, f"{skill_id}: source_candidate_id should be NULL"
        assert row["expires_at"] is not None, f"{skill_id}: expires_at should be set"


@pytest.mark.integration
def test_formulas_table_gone():
    """formulas and formula_pours tables no longer exist after migration."""
    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = {row[0] for row in cur.fetchall()}
    assert "formulas" not in tables, "formulas table should have been dropped"
    assert "formula_pours" not in tables, "formula_pours table should have been dropped"


# ---------------------------------------------------------------------------
# Grants — harness user
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_harness_user_can_insert_episode():
    """harness DB user has INSERT on episodes."""
    episode_id = str(uuid.uuid4())
    conn = get_harness_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (episode_id, agent_principal) VALUES (%s, %s)",
                (episode_id, "test-agent"),
            )
        conn.commit()
    # cleanup via root (harness has no DELETE)
    root = get_root_conn()
    with root:
        with root.cursor() as cur:
            cur.execute("DELETE FROM episodes WHERE episode_id = %s", (episode_id,))
        root.commit()


@pytest.mark.integration
def test_harness_user_cannot_delete_episodes():
    """harness DB user must not have DELETE on episodes (append-only)."""
    episode_id = str(uuid.uuid4())
    root = get_root_conn()
    with root:
        with root.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (episode_id, agent_principal) VALUES (%s, %s)",
                (episode_id, "delete-test"),
            )
        root.commit()

    harness = get_harness_conn()
    try:
        with harness:
            with harness.cursor() as cur:
                cur.execute("DELETE FROM episodes WHERE episode_id = %s", (episode_id,))
            harness.commit()
        pytest.fail("harness user should not have DELETE on episodes")
    except pymysql.err.OperationalError as exc:
        assert "1142" in str(exc) or "denied" in str(exc).lower()
    finally:
        cleanup = get_root_conn()
        with cleanup:
            with cleanup.cursor() as cur:
                cur.execute("DELETE FROM episodes WHERE episode_id = %s", (episode_id,))
            cleanup.commit()


# ---------------------------------------------------------------------------
# DoltFormulaStore behaviour against the renamed skills table
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_formula_store_list_active_returns_seeded_skills():
    """DoltFormulaStore.list_active reads from skills, returns all three seeded rows."""
    from harness_memory.formula_store import DoltFormulaStore
    store = DoltFormulaStore(
        host=DOLT_HOST, port=DOLT_PORT,
        user="harness", password="harness", database="harness",
    )
    sre_skills = store.list_active("sre")
    assert any(s.id == "sre:triage-incident" for s in sre_skills), \
        "seeded sre skill not returned by list_active"


@pytest.mark.integration
def test_formula_store_lookup_finds_skill_by_keyword():
    """DoltFormulaStore.lookup finds the code_reviewer skill via keyword match."""
    from harness_memory.formula_store import DoltFormulaStore
    store = DoltFormulaStore(
        host=DOLT_HOST, port=DOLT_PORT,
        user="harness", password="harness", database="harness",
    )
    result = store.lookup("code_reviewer", "review a pull request for bugs")
    assert result is not None, "lookup returned None"
    assert result.id == "code_reviewer:review-pr"


@pytest.mark.integration
def test_harness_user_can_insert_skill():
    """harness DB user has INSERT on skills."""
    conn = get_harness_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skills "
                "(id, name, agent_role, version, status, input_schema, steps, output_contract, promoted_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
                ("test:grant-check", "Grant Check", "sre", 99, "active",
                 "{}", "[]", "{}", "harness-test"),
            )
        conn.commit()
    root = get_root_conn()
    with root:
        with root.cursor() as cur:
            cur.execute("DELETE FROM skills WHERE id = %s", ("test:grant-check",))
        root.commit()
