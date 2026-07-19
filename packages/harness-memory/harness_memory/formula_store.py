"""Dolt-backed formula store with commit-per-change versioning."""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import pymysql
import pymysql.cursors

from .models import Formula


_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "to", "for", "of", "by",
    "is", "be", "are", "was", "were", "it", "this", "that", "with", "as",
    "at", "from", "not", "no", "if", "its", "than", "then", "so",
})


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if w not in _STOP_WORDS]


def _count_hits(q_words: set[str], d_counter: Counter) -> int:
    total = 0
    for w in q_words:
        if w in d_counter:
            total += d_counter[w]
    return total


def _tfidf_score(query: str, doc: str) -> float:
    """Keyword overlap score — no ML needed for formula matching."""
    q_words = set(_tokenize(query))
    d_words = _tokenize(doc)
    if not q_words or not d_words:
        return 0.0
    d_counter = Counter(d_words)
    hits = _count_hits(q_words, d_counter)
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
        """Insert or add a new version of a skill; returns Dolt commit hash."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO skills
                        (id, name, agent_role, version, status, description,
                         input_schema, steps, output_contract,
                         promoted_by, source_candidate_id, expires_at,
                         revoked_reason, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        formula.id, formula.name, formula.agent_role,
                        formula.version, formula.status, formula.description,
                        json.dumps(formula.input_schema),
                        json.dumps(formula.steps),
                        json.dumps(formula.output_contract),
                        formula.promoted_by, formula.source_candidate_id,
                        formula.expires_at, formula.revoked_reason, now,
                    ),
                )
                cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"skill: {formula.id} v{formula.version}",),
                )
                cur.execute("SELECT commit_hash FROM dolt_log LIMIT 1")
                row = cur.fetchone()
        return row["commit_hash"] if row else ""

    def get(self, formula_id: str, version: int | None = None) -> Formula | None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                if version is None:
                    cur.execute(
                        "SELECT * FROM skills WHERE id = %s ORDER BY version DESC LIMIT 1",
                        (formula_id,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM skills WHERE id = %s AND version = %s",
                        (formula_id, version),
                    )
                row = cur.fetchone()
        return self._row_to_formula(row) if row else None

    def list_active(self, agent_role: str) -> list[Formula]:
        """Return the latest active version of each skill for the given role."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.*
                    FROM skills f
                    INNER JOIN (
                        SELECT id, MAX(version) AS max_ver
                        FROM skills
                        WHERE agent_role = %s AND status = 'active'
                        GROUP BY id
                    ) latest ON f.id = latest.id AND f.version = latest.max_ver
                    WHERE f.status = 'active'
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
                    "UPDATE skills SET status = 'deprecated' WHERE id = %s",
                    (formula_id,),
                )
                cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"skill: {formula_id} deprecated",),
                )

    def update_quality(self, formula_id: str, quality_score: float, status: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE skills
                    SET status = %s
                    WHERE id = %s
                      AND status = 'active'
                    """,
                    (status, formula_id),
                )
                if cur.rowcount > 0:
                    cur.execute(
                        "CALL DOLT_COMMIT('-Am', %s)",
                        (f"skill: {formula_id} status={status}",),
                    )

    def get_all_formula_ids(self) -> list[str]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT id FROM skills WHERE status = 'active'")
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

    def _get_drafts_by_role(self, agent_role: str) -> list:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM skills WHERE agent_role = %s AND status = 'draft'",
                    (agent_role,),
                )
                rows = cur.fetchall()
        return [self._row_to_formula(r) for r in rows]

    def _delete_where_id_like(self, pattern: str) -> None:
        """Delete test skills by id pattern (e.g. 'test:%')."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM skills WHERE id LIKE %s", (pattern,))
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
    def _parse_json_field(row: dict, field: str) -> dict | list:
        val = row[field]
        return json.loads(val) if isinstance(val, str) else val

    def _row_to_formula(self, row: dict) -> Formula:
        return Formula(
            id=row["id"],
            name=row["name"],
            agent_role=row["agent_role"],
            version=row["version"],
            status=row["status"],
            description=row.get("description") or "",
            input_schema=DoltFormulaStore._parse_json_field(row, "input_schema"),
            steps=DoltFormulaStore._parse_json_field(row, "steps"),
            output_contract=DoltFormulaStore._parse_json_field(row, "output_contract"),
            promoted_by=row.get("promoted_by") or "",
            source_candidate_id=row.get("source_candidate_id"),
            expires_at=row.get("expires_at"),
            revoked_reason=row.get("revoked_reason"),
        )
