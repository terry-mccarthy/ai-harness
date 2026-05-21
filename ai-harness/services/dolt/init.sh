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

# Create audit_log table and initial commit using local SQL mode (no server needed)
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
CALL DOLT_ADD('.');
CALL DOLT_COMMIT('-m', 'init: audit_log schema');
SQL

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
SQL

echo "Dolt init complete."
wait "$SERVER_PID"
