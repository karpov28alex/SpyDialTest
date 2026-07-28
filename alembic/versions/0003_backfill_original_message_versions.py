"""Backfill original message snapshots.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    messages = sa.table(
        "messages",
        sa.column("id", sa.Integer()),
        sa.column("text", sa.Text()),
        sa.column("caption", sa.Text()),
        sa.column("sent_at", sa.DateTime(timezone=True)),
    )
    versions = sa.table(
        "message_versions",
        sa.column("message_id", sa.Integer()),
        sa.column("version_number", sa.Integer()),
        sa.column("text", sa.Text()),
        sa.column("caption", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    existing = sa.select(versions.c.message_id)
    statement = versions.insert().from_select(
        ["message_id", "version_number", "text", "caption", "created_at"],
        sa.select(
            messages.c.id,
            sa.literal(1),
            messages.c.text,
            messages.c.caption,
            messages.c.sent_at,
        ).where(messages.c.id.not_in(existing)),
    )
    op.execute(statement)


def downgrade() -> None:
    # Backfilled rows are legitimate history and must not be destructively removed.
    pass
