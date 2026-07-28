"""Add master notification and protected media controls.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "user_settings",
        sa.Column("save_protected_media", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("user_settings", "notifications_enabled", server_default=None)
    op.alter_column("user_settings", "save_protected_media", server_default=None)


def downgrade() -> None:
    op.drop_column("user_settings", "save_protected_media")
    op.drop_column("user_settings", "notifications_enabled")
