from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class SubscriptionStatus(str, enum.Enum):
    trial = "trial"
    referral = "referral"
    vip = "vip"
    active = "active"
    expired = "expired"
    disabled = "disabled"


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_bot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    vip_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.trial)
    referrer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    referral_bonus_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_access_disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    settings: Mapped[UserSettings] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin")


class UserSettings(Base, TimestampMixin):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    notify_edits: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_deletions: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_protected_media: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_connection: Mapped[bool] = mapped_column(Boolean, default=True)
    hide_preview: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_emoji: Mapped[bool] = mapped_column(Boolean, default=True)
    theme: Mapped[str] = mapped_column(String(16), default="dark")
    language: Mapped[str] = mapped_column(String(16), default="ru")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    user: Mapped[User] = relationship(back_populates="settings")


class BusinessConnection(Base, TimestampMixin):
    __tablename__ = "business_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_connection_id: Mapped[str] = mapped_column(String(128), unique=True)
    business_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    rights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnect_reason: Mapped[str | None] = mapped_column(Text)


class Dialog(Base, TimestampMixin):
    __tablename__ = "dialogs"
    __table_args__ = (UniqueConstraint("business_connection_id", "telegram_chat_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    business_connection_id: Mapped[int] = mapped_column(ForeignKey("business_connections.id"), index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    peer_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    peer_username: Mapped[str | None] = mapped_column(String(64))
    peer_name: Mapped[str | None] = mapped_column(String(256))
    avatar: Mapped[str | None] = mapped_column(Text)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("business_connection_id", "telegram_chat_id", "telegram_message_id"),
        Index("ix_messages_dialog_sent", "dialog_id", "sent_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id", ondelete="CASCADE"), index=True)
    business_connection_id: Mapped[int] = mapped_column(ForeignKey("business_connections.id"), index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MessageVersion(Base):
    __tablename__ = "message_versions"
    __table_args__ = (UniqueConstraint("message_id", "version_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Media(Base, TimestampMixin):
    __tablename__ = "media"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[str] = mapped_column(String(32))
    telegram_file_id: Mapped[str] = mapped_column(Text)
    telegram_unique_file_id: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str | None] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(BigInteger)
    duration: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    storage_key: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(128))
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    download_status: Mapped[str] = mapped_column(String(32), default="pending")
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    update_type: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="processing")


class FailedUpdate(Base, TimestampMixin):
    __tablename__ = "failed_updates"
    id: Mapped[int] = mapped_column(primary_key=True)
    update_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    update_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(Text)
    stack_trace: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)


class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bonus_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32))
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Broadcast(Base, TimestampMixin):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id"))
    audience: Mapped[str] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(32))
    media_reference: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"
    __table_args__ = (UniqueConstraint("broadcast_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BotContent(Base, TimestampMixin):
    __tablename__ = "bot_content"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str | None] = mapped_column(Text)
    media_file_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id"))


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminAudit(Base):
    __tablename__ = "admin_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id"), index=True)
    action: Mapped[str] = mapped_column(String(128))
    object_type: Mapped[str | None] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(128))
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
