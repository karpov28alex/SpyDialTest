"""Backfill missing user settings.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO user_settings (
            user_id,
            notifications_enabled,
            save_protected_media,
            notify_edits,
            notify_deletions,
            notify_protected_media,
            notify_connection,
            hide_preview,
            notify_emoji,
            theme,
            language,
            timezone,
            created_at,
            updated_at
        )
        SELECT
            u.id,
            TRUE,
            TRUE,
            TRUE,
            TRUE,
            TRUE,
            TRUE,
            FALSE,
            TRUE,
            'dark',
            COALESCE(NULLIF(u.language_code, ''), 'ru'),
            'UTC',
            NOW(),
            NOW()
        FROM users AS u
        LEFT JOIN user_settings AS s ON s.user_id = u.id
        WHERE s.user_id IS NULL
        ON CONFLICT (user_id) DO NOTHING
        """
    )


def downgrade() -> None:
    pass
