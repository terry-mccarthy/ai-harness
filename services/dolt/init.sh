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
    latency_ms      INT,
    correlation_id  VARCHAR(36)  NULL
);

-- Migrate: remove old formula tables and replace with governed skill tables
DROP TABLE IF EXISTS formula_pours;
DROP TABLE IF EXISTS formulas;

CREATE TABLE IF NOT EXISTS episodes (
    episode_id          CHAR(36)     PRIMARY KEY,
    created_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_principal     TEXT         NOT NULL,
    alert_signature     TEXT,
    service_class       TEXT,
    env_fingerprint     JSON,
    diagnosis           JSON,
    actions             JSON,
    outcome             ENUM('RESOLVED','FAILED','ROLLED_BACK','HUMAN_OVERRIDE','INCONCLUSIVE') NULL,
    outcome_signal      JSON         NULL,
    outcome_labeled_at  TIMESTAMP    NULL,
    human_actor         TEXT         NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id        CHAR(36)     PRIMARY KEY,
    created_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cluster_key         TEXT,
    member_episode_ids  JSON,
    proposed_procedure  JSON,
    support_stats       JSON,
    status              ENUM('PROPOSED','UNDER_REVIEW','PROMOTED','REJECTED') NOT NULL DEFAULT 'PROPOSED'
);

CREATE TABLE IF NOT EXISTS skills (
    id                  VARCHAR(64)  NOT NULL,
    name                TEXT         NOT NULL,
    agent_role          TEXT         NOT NULL,
    description         TEXT,
    version             INTEGER      NOT NULL DEFAULT 1,
    status              TEXT         NOT NULL DEFAULT 'active',
    input_schema        JSON         NOT NULL,
    steps               JSON         NOT NULL,
    output_contract     JSON         NOT NULL,
    promoted_by         TEXT         NOT NULL,
    source_candidate_id VARCHAR(64)  NULL,
    expires_at          DATETIME     NULL,
    revoked_reason      TEXT         NULL,
    created_at          DATETIME     NOT NULL,
    UNIQUE KEY uq_skill_version (id, version)
);

CREATE TABLE IF NOT EXISTS tasks (
    id              CHAR(36)     PRIMARY KEY,
    required_role   VARCHAR(64)  NOT NULL,
    artifact_type   VARCHAR(64)  NOT NULL,
    payload         JSON         NOT NULL,
    priority        INT          NOT NULL DEFAULT 0,
    status          ENUM('pending','claimed','done','failed') NOT NULL DEFAULT 'pending',
    claimed_by      VARCHAR(128) NULL,
    lease_expires   DATETIME     NULL,
    result          JSON         NULL,
    idempotency_key VARCHAR(256) NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_claimable (status, required_role, priority),
    UNIQUE KEY uq_idem (idempotency_key)
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id            CHAR(36)    PRIMARY KEY,
    from_role     VARCHAR(64) NOT NULL,
    to_role       VARCHAR(64) NOT NULL,
    artifact_type VARCHAR(64) NOT NULL,
    payload       JSON        NOT NULL,
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_inbox (to_role, created_at)
);
SQL

# Stage and commit schema — idempotent: skip if nothing changed
dolt add -A && dolt commit -m "init: audit_log + skill-learning schema" || echo "(schema already committed, skipping)"

# Seed skills (migrated from formulas, idempotent INSERT IGNORE)
dolt sql << 'SQL'
INSERT IGNORE INTO skills
    (id, name, agent_role, version, status, description, input_schema, steps, output_contract,
     promoted_by, source_candidate_id, expires_at, created_at)
VALUES
    (
        'sre:triage-incident', 'Triage Incident', 'sre', 1, 'active',
        'Respond to production incidents including database alerts latency spikes and error investigations',
        '{"type":"object","properties":{"alert":{"type":"string"}}}',
        '[{"action":"observability_query"},{"action":"log_search"},{"action":"runbook_read"},{"action":"llm_synthesise"}]',
        '{"type":"object","properties":{"report":{"type":"string"}}}',
        'seed', NULL, DATE_ADD(NOW(), INTERVAL 10 YEAR), NOW()
    ),
    (
        'code_reviewer:review-pr', 'Review Pull Request', 'code_reviewer', 1, 'active',
        'Perform a thorough code review of a pull request checking for bugs security issues and style',
        '{"type":"object","properties":{"pr_number":{"type":"integer"}}}',
        '[{"action":"git_diff"},{"action":"run_linter"},{"action":"review_diff"}]',
        '{"type":"object","properties":{"findings":{"type":"array"}}}',
        'seed', NULL, DATE_ADD(NOW(), INTERVAL 10 YEAR), NOW()
    ),
    (
        'architect:write-adr', 'Write Architecture Decision Record', 'architect', 1, 'active',
        'Research and document an architecture decision record for a significant technical choice',
        '{"type":"object","properties":{"decision":{"type":"string"}}}',
        '[{"action":"codebase_search"},{"action":"adr_read"},{"action":"adr_write"}]',
        '{"type":"object","properties":{"adr_path":{"type":"string"}}}',
        'seed', NULL, DATE_ADD(NOW(), INTERVAL 10 YEAR), NOW()
    );
SQL

dolt add -A && dolt commit -m "seed: three starter skills (migrated from formulas)" || echo "(seed already committed, skipping)"

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
GRANT SELECT, INSERT ON harness.episodes TO 'harness'@'%';
GRANT SELECT, INSERT, UPDATE ON harness.candidates TO 'harness'@'%';
GRANT SELECT, INSERT, UPDATE ON harness.skills TO 'harness'@'%';
GRANT SELECT, INSERT, UPDATE ON harness.tasks TO 'harness'@'%';
GRANT SELECT, INSERT ON harness.agent_messages TO 'harness'@'%';
SQL

echo "Dolt init complete."
wait "$SERVER_PID"
