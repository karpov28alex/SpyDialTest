"""Add notification emoji and theme preferences.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("notify_emoji", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("user_settings", sa.Column("theme", sa.String(length=16), nullable=False, server_default="dark"))


def downgrade() -> None:
    op.drop_column("user_settings", "theme")
    op.drop_column("user_settings", "notify_emoji")
