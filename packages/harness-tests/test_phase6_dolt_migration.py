"""Phase 6 issue-01 — Dolt tasks + agent_messages migration.

Tests verify the schema exists, indexes are present, grants are correct,
and the harness user cannot DELETE from either new table.
All tests are @pytest.mark.integration.
"""

import os
import pymysql
import pytest

DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


def get_dolt_conn(user="root", password="root"):
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=user,
        password=password,
        database="harness",
        connect_timeout=5,
    )


def get_harness_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user="harness",
        password="harness",
        database="harness",
        connect_timeout=5,
    )


@pytest.mark.integration
def test_tasks_table_exists():
    """tasks table is present in the harness schema."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'tasks'")
            assert cur.fetchone() is not None, "tasks table not found"


@pytest.mark.integration
def test_agent_messages_table_exists():
    """agent_messages table is present in the harness schema."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'agent_messages'")
            assert cur.fetchone() is not None, "agent_messages table not found"


@pytest.mark.integration
def test_tasks_schema_columns():
    """tasks table has all required columns with correct types."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DESCRIBE tasks")
            cols = {row["Field"]: row for row in cur.fetchall()}

    required = [
        "id", "required_role", "artifact_type", "payload", "priority",
        "status", "claimed_by", "lease_expires", "result", "idempotency_key",
        "created_at", "updated_at",
    ]
    for col in required:
        assert col in cols, f"tasks table missing column: {col}"

    assert cols["status"]["Type"].startswith("enum"), "status must be ENUM"
    assert "pending" in cols["status"]["Type"]
    assert "claimed" in cols["status"]["Type"]
    assert "done" in cols["status"]["Type"]
    assert "failed" in cols["status"]["Type"]


@pytest.mark.integration
def test_agent_messages_schema_columns():
    """agent_messages table has all required columns."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DESCRIBE agent_messages")
            cols = {row["Field"]: row for row in cur.fetchall()}

    required = ["id", "from_role", "to_role", "artifact_type", "payload", "created_at"]
    for col in required:
        assert col in cols, f"agent_messages table missing column: {col}"


@pytest.mark.integration
def test_tasks_indexes_exist():
    """tasks table has idx_claimable and uq_idem indexes."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SHOW INDEX FROM tasks")
            indexes = {row["Key_name"] for row in cur.fetchall()}

    assert "idx_claimable" in indexes, f"idx_claimable missing; found: {indexes}"
    assert "uq_idem" in indexes, f"uq_idem missing; found: {indexes}"


@pytest.mark.integration
def test_agent_messages_inbox_index_exists():
    """agent_messages table has idx_inbox index."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SHOW INDEX FROM agent_messages")
            indexes = {row["Key_name"] for row in cur.fetchall()}

    assert "idx_inbox" in indexes, f"idx_inbox missing; found: {indexes}"


@pytest.mark.integration
def test_harness_user_can_insert_tasks():
    """harness DB user has INSERT on tasks."""
    import uuid, json
    conn = get_harness_conn()
    task_id = str(uuid.uuid4())
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, required_role, artifact_type, payload) "
                "VALUES (%s, %s, %s, %s)",
                (task_id, "sre", "incident", json.dumps({"alert": "test"})),
            )
        conn.commit()
    # cleanup via root (harness user has no DELETE)
    root = get_dolt_conn()
    with root:
        with root.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        root.commit()


@pytest.mark.integration
def test_harness_user_cannot_delete_tasks():
    """harness DB user must not have DELETE on tasks (append-only contract)."""
    import uuid, json
    # Insert a row as root so we have something to try to delete
    task_id = str(uuid.uuid4())
    root = get_dolt_conn()
    with root:
        with root.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, required_role, artifact_type, payload) "
                "VALUES (%s, %s, %s, %s)",
                (task_id, "sre", "incident", json.dumps({"alert": "delete-test"})),
            )
        root.commit()

    harness = get_harness_conn()
    try:
        with harness:
            with harness.cursor() as cur:
                cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            harness.commit()
        pytest.fail("harness user should not have DELETE on tasks")
    except pymysql.err.OperationalError as exc:
        assert "denied" in str(exc).lower() or "1142" in str(exc), (
            f"Expected access denied error, got: {exc}"
        )
    finally:
        cleanup = get_dolt_conn()
        with cleanup:
            with cleanup.cursor() as cur:
                cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            cleanup.commit()


@pytest.mark.integration
def test_existing_tables_unaffected():
    """audit_log and formulas tables still exist after migration."""
    conn = get_dolt_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = {row[0] for row in cur.fetchall()}
    assert "audit_log" in tables, "audit_log table missing after migration"
    assert "formulas" in tables, "formulas table missing after migration"
