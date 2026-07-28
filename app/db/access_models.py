from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppMonetizationSettings(Base):
    """Singleton configuration for access, trial and test payment presentation."""

    __tablename__ = "app_monetization_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    free_trial_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_trial_in_profile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    show_tariffs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    referral_bonus_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    entry_price_rub: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    weekly_price_rub: Mapped[int] = mapped_column(Integer, nullable=False, default=125)
    fallback_three_day_price_rub: Mapped[int] = mapped_column(Integer, nullable=False, default=70)
    payment_placeholder_url: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="https://game.hidenow.su/app?screen=subscription&demo=1"
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at_override: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
