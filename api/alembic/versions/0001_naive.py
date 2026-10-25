"""Naive schema (the pre-incident system).

Revision ID: 0001_naive
Revises:
"""

from alembic import op

revision = "0001_naive"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            verified BOOLEAN NOT NULL
        );
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
            UNIQUE (id, workspace_id)
        );
        CREATE TABLE media (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            prompt TEXT,
            frames JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reasons JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_media_workspace_status ON media (workspace_id, status, created_at);
        CREATE TABLE screening_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            media_id UUID NOT NULL REFERENCES media(id),
            workspace_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempt INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_jobs_status_created ON screening_jobs (status, created_at);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE screening_jobs; DROP TABLE media; DROP TABLE users; DROP TABLE workspaces;")
