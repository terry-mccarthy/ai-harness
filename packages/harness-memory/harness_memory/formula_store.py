"""Dolt-backed formula store with commit-per-change versioning."""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import pymysql
import pymysql.cursors

from .models import Formula


def _tfidf_score(query: str, doc: str) -> float:
    """Keyword overlap score — no ML needed for formula matching."""
    q_words = set(re.findall(r"\w+", query.lower()))
    d_words = re.findall(r"\w+", doc.lower())
    d_counter = Counter(d_words)
    hits = sum(d_counter[w] for w in q_words if w in d_counter)
    denom = len(q_words) + len(set(d_words))
    return hits / denom if denom else 0.0


class DoltFormulaStore:
    def __init__(self, host: str, port: int, user: str, password: str, database: str) -> None:
        self._conn_kwargs = dict(
            host=host, port=port, user=user, password=password,
            database=database, autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _conn(self) -> pymysql.Connection:
        return pymysql.connect(**self._conn_kwargs)

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def propose(self, formula: Formula) -> str:
        """Insert or add a new version of a formula; returns Dolt commit hash."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO formulas
                        (id, name, agent_role, version, status, description,
                         input_schema, steps, output_contract, quality_score,
                         created_at, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        formula.id, formula.name, formula.agent_role,
                        formula.version, formula.status, formula.description,
                        json.dumps(formula.input_schema),
                        json.dumps(formula.steps),
                        json.dumps(formula.output_contract),
                        formula.quality_score, now, formula.created_by,
                    ),
                )
                cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"formula: {formula.id} v{formula.version}",),
                )
                cur.execute("SELECT commit_hash FROM dolt_log LIMIT 1")
                row = cur.fetchone()
        return row["commit_hash"] if row else ""

    def get(self, formula_id: str, version: int | None = None) -> Formula | None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                if version is None:
                    cur.execute(
                        "SELECT * FROM formulas WHERE id = %s ORDER BY version DESC LIMIT 1",
                        (formula_id,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM formulas WHERE id = %s AND version = %s",
                        (formula_id, version),
                    )
                row = cur.fetchone()
        return self._row_to_formula(row) if row else None

    def list_active(self, agent_role: str) -> list[Formula]:
        """Return the latest active version of each formula for the given role."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.*
                    FROM formulas f
                    INNER JOIN (
                        SELECT id, MAX(version) AS max_ver
                        FROM formulas
                        WHERE agent_role = %s AND status NOT IN ('deprecated')
                        GROUP BY id
                    ) latest ON f.id = latest.id AND f.version = latest.max_ver
                    WHERE f.status NOT IN ('deprecated')
                    """,
                    (agent_role,),
                )
                rows = cur.fetchall()
        return [self._row_to_formula(r) for r in rows]

    def lookup(self, agent_role: str, task: str) -> Formula | None:
        """Return best-matching active formula for the task using keyword similarity."""
        candidates = self.list_active(agent_role)
        if not candidates:
            return None
        scored = [
            (f, _tfidf_score(task, f"{f.name} {f.description}"))
            for f in candidates
        ]
        best_formula, best_score = max(scored, key=lambda x: x[1])
        return best_formula if best_score > 0.05 else None

    # ------------------------------------------------------------------
    # Quality / lifecycle
    # ------------------------------------------------------------------

    def deprecate(self, formula_id: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE formulas SET status = 'deprecated' WHERE id = %s",
                    (formula_id,),
                )
                cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"formula: {formula_id} deprecated",),
                )

    def update_quality(self, formula_id: str, quality_score: float, status: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE formulas
                    SET quality_score = %s, status = %s
                    WHERE id = %s
                      AND status NOT IN ('deprecated')
                    """,
                    (quality_score, status, formula_id),
                )
                cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"formula: {formula_id} quality={quality_score:.2f} status={status}",),
                )

    def get_pour_stats(self, formula_id: str) -> dict:
        """Return {total, successes} pour counts."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS total, SUM(success) AS successes FROM formula_pours WHERE formula_id = %s",
                    (formula_id,),
                )
                row = cur.fetchone()
        total = row["total"] or 0
        successes = int(row["successes"] or 0)
        return {"total": total, "successes": successes}

    def get_all_formula_ids(self) -> list[str]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT id FROM formulas WHERE status != 'deprecated'")
                rows = cur.fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------
    # Helpers for tests
    # ------------------------------------------------------------------

    def recent_commits(self, n: int = 10) -> list[dict]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT commit_hash, message FROM dolt_log LIMIT %s",
                    (n,),
                )
                return cur.fetchall()

    def _record_pours(self, formula_id: str, successes: int, failures: int) -> None:
        """Seed formula_pours for quality tests."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                for _ in range(successes):
                    cur.execute(
                        "INSERT INTO formula_pours (formula_id, success) VALUES (%s, TRUE)",
                        (formula_id,),
                    )
                for _ in range(failures):
                    cur.execute(
                        "INSERT INTO formula_pours (formula_id, success) VALUES (%s, FALSE)",
                        (formula_id,),
                    )

    def _delete_where_id_like(self, pattern: str) -> None:
        """Delete test formulas by id pattern (e.g. 'test:%')."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM formula_pours WHERE formula_id LIKE %s", (pattern,))
                cur.execute("DELETE FROM formulas WHERE id LIKE %s", (pattern,))
                try:
                    cur.execute(
                        "CALL DOLT_COMMIT('-Am', %s)",
                        ("test: cleanup",),
                    )
                except Exception:
                    pass  # nothing to commit if tables were clean

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_formula(row: dict) -> Formula:
        return Formula(
            id=row["id"],
            name=row["name"],
            agent_role=row["agent_role"],
            version=row["version"],
            status=row["status"],
            description=row.get("description") or "",
            input_schema=json.loads(row["input_schema"]) if isinstance(row["input_schema"], str) else row["input_schema"],
            steps=json.loads(row["steps"]) if isinstance(row["steps"], str) else row["steps"],
            output_contract=json.loads(row["output_contract"]) if isinstance(row["output_contract"], str) else row["output_contract"],
            quality_score=float(row.get("quality_score") or 0.0),
            created_by=row.get("created_by") or "",
        )
