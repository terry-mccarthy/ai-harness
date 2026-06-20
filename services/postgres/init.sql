CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS server_config (
    id         INTEGER PRIMARY KEY DEFAULT 1,
    config     JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO server_config (id, config)
VALUES (1, '{}')
ON CONFLICT (id) DO NOTHING;
