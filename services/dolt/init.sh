#!/bin/bash
set -e

dolt config --global --add user.email "harness@harness.local"
dolt config --global --add user.name "Harness"

DATADIR=/doltdata/harness
mkdir -p "$DATADIR"
cd "$DATADIR"

if [ ! -d .dolt ]; then
    dolt init
fi

# Create tables using local SQL mode (no server needed)
dolt sql << 'SQL'
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_id        VARCHAR(64)  NOT NULL,
    tool_name       VARCHAR(128) NOT NULL,
    server_id       VARCHAR(64),
    request_hash    VARCHAR(64),
    response_hash   VARCHAR(64),
    policy_decision VARCHAR(8)   NOT NULL,
    policy_rule     VARCHAR(128),
    timestamp_ms    BIGINT       NOT NULL,
    latency_ms      INT
);

CREATE TABLE IF NOT EXISTS formulas (
    id               VARCHAR(64)  NOT NULL,
    name             TEXT         NOT NULL,
    agent_role       TEXT         NOT NULL,
    version          INTEGER      NOT NULL DEFAULT 1,
    status           TEXT         NOT NULL DEFAULT 'active',
    description      TEXT,
    input_schema     JSON         NOT NULL,
    steps            JSON         NOT NULL,
    output_contract  JSON         NOT NULL,
    quality_score    FLOAT        NOT NULL DEFAULT 0.0,
    created_at       DATETIME     NOT NULL,
    created_by       TEXT         NOT NULL,
    UNIQUE KEY uq_formula_version (id, version)
);

CREATE TABLE IF NOT EXISTS formula_pours (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    formula_id  VARCHAR(64) NOT NULL,
    success     BOOLEAN     NOT NULL,
    poured_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
SQL

# Stage and commit schema — idempotent: skip if nothing changed
dolt add -A && dolt commit -m "init: audit_log + formulas schema" || echo "(schema already committed, skipping)"

# Seed formulas (local SQL mode, idempotent INSERT IGNORE)
dolt sql << 'SQL'
INSERT IGNORE INTO formulas
    (id, name, agent_role, version, status, description, input_schema, steps, output_contract, quality_score, created_at, created_by)
VALUES
    (
        'sre:triage-incident', 'Triage Incident', 'sre', 1, 'active',
        'Respond to production incidents including database alerts latency spikes and error investigations',
        '{"type":"object","properties":{"alert":{"type":"string"}}}',
        '[{"action":"observability_query"},{"action":"log_search"},{"action":"runbook_read"},{"action":"llm_synthesise"}]',
        '{"type":"object","properties":{"report":{"type":"string"}}}',
        0.0, NOW(), 'seed'
    ),
    (
        'code_reviewer:review-pr', 'Review Pull Request', 'code_reviewer', 1, 'active',
        'Perform a thorough code review of a pull request checking for bugs security issues and style',
        '{"type":"object","properties":{"pr_number":{"type":"integer"}}}',
        '[{"action":"git_diff"},{"action":"run_linter"},{"action":"review_diff"}]',
        '{"type":"object","properties":{"findings":{"type":"array"}}}',
        0.0, NOW(), 'seed'
    ),
    (
        'architect:write-adr', 'Write Architecture Decision Record', 'architect', 1, 'active',
        'Research and document an architecture decision record for a significant technical choice',
        '{"type":"object","properties":{"decision":{"type":"string"}}}',
        '[{"action":"codebase_search"},{"action":"adr_read"},{"action":"adr_write"}]',
        '{"type":"object","properties":{"adr_path":{"type":"string"}}}',
        0.0, NOW(), 'seed'
    );
SQL

dolt add -A && dolt commit -m "seed: three starter formulas" || echo "(seed already committed, skipping)"

# Start SQL server in background — newer Dolt: root has no password by default
dolt sql-server --host 0.0.0.0 --port 3306 &
SERVER_PID=$!

# Wait for server to be ready using mysql client
echo "Waiting for Dolt SQL server to start..."
for i in $(seq 1 30); do
    if mysql -h 127.0.0.1 -P 3306 -u root --connect-timeout=2 -e "SELECT 1" > /dev/null 2>&1; then
        echo "Dolt SQL server is ready."
        break
    fi
    echo "  attempt $i/30 — not ready yet, sleeping 1s"
    sleep 1
done

# User management requires server mode — set root password and create harness user
mysql -h 127.0.0.1 -P 3306 -u root << 'SQL'
CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY 'root';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'harness'@'%' IDENTIFIED BY 'harness';
GRANT SELECT, INSERT ON harness.audit_log TO 'harness'@'%';
GRANT SELECT ON harness.dolt_log TO 'harness'@'%';
GRANT SELECT ON harness.dolt_diff_audit_log TO 'harness'@'%';
GRANT SELECT, INSERT, UPDATE ON harness.formulas TO 'harness'@'%';
GRANT SELECT, INSERT ON harness.formula_pours TO 'harness'@'%';
SQL

echo "Dolt init complete."
wait "$SERVER_PID"
