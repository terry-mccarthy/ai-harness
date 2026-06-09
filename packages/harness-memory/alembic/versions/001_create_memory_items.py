"""create memory_items table

Revision ID: 001
Revises:
Create Date: 2025-01-01
"""
import os
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = int(os.environ.get("EMBED_DIM", "5120"))


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS memory_items (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            namespace    TEXT NOT NULL,
            key          TEXT NOT NULL,
            memory_type  TEXT NOT NULL DEFAULT 'episodic',
            value        JSONB NOT NULL,
            source_ids   UUID[],
            embedding    vector({EMBED_DIM}),
            confidence   FLOAT DEFAULT 1.0,
            consolidated BOOL DEFAULT FALSE,
            created_at   TIMESTAMPTZ DEFAULT now(),
            expires_at   TIMESTAMPTZ,
            UNIQUE (namespace, key)
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS memory_items")
