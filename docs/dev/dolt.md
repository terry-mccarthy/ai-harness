# Dolt — init and gotchas

Dolt is a git-versioned MySQL-compatible database. The init approach in `services/dolt/init.sh` is three-phase:

1. **Local SQL mode** (`dolt sql`): DDL (`CREATE TABLE`) and initial commit (`CALL DOLT_COMMIT`). No server needed.
2. **Start server**: `dolt sql-server --host 0.0.0.0 --port 3306` (no `--user`/`--password` flags — removed in Dolt v1.x; root starts with no password).
3. **User management via `mysql` client**: `CREATE USER`, `GRANT` — these require server mode.

Key gotchas:
- `dolt init` requires author identity: set `user.email` and `user.name` via `dolt config --global` before running it.
- `dolt sql-server` v1.x: no `--user`/`--password` flags. Root has no password by default.
- `dolt sql` is a **local** command — it does not connect to a running server. Use a real MySQL client (`mysql`) to interact with a running Dolt server.
- `dolt_log` and `dolt_diff_audit_log` are system tables — they require explicit `GRANT SELECT` to non-root users.
- Governance commits after every audit INSERT: `CALL DOLT_COMMIT('-Am', 'audit: <tool_name>')`. The `-A` flag stages all changes.
- Commit hash retrieved via `SELECT commit_hash FROM dolt_log LIMIT 1` — `@@dolt_repo_head` does not exist in Dolt v1.x.
