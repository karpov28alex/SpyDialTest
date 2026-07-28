"""Add global monetization settings.

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
    op.create_table(
        "app_monetization_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("free_trial_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("show_trial_in_profile", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_tariffs", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("referral_bonus_days", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("entry_price_rub", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("weekly_price_rub", sa.Integer(), nullable=False, server_default="125"),
        sa.Column("fallback_three_day_price_rub", sa.Integer(), nullable=False, server_default="70"),
        sa.Column("payment_placeholder_url", sa.String(length=1024), nullable=False, server_default="https://game.hidenow.su/app?screen=subscription&demo=1"),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("updated_at_override", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("""
        INSERT INTO app_monetization_settings
        (id, free_trial_enabled, show_trial_in_profile, show_tariffs, trial_days,
         referral_bonus_days, entry_price_rub, weekly_price_rub,
         fallback_three_day_price_rub, payment_placeholder_url)
        VALUES (1, true, false, true, 3, 3, 20, 125, 70,
                'https://game.hidenow.su/app?screen=subscription&demo=1')
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table("app_monetization_settings")
