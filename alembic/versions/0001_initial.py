"""Initial Dialog Spy schema.

Revision ID: 0001
Revises:
"""
from alembic import op
from app.db.base import Base
import app.db.models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
