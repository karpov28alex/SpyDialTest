from datetime import UTC, datetime
from types import SimpleNamespace

from app.business.events import (
    format_delete_notification,
    format_edit_notification,
    is_protected_message,
    protected_reply_is_allowed,
)


def prefs(**overrides):
    values = {"notify_emoji": True, "hide_preview": False}
    values.update(overrides)
    return SimpleNamespace(**values)


def dialog():
    return SimpleNamespace(peer_name="Иван <Иванов>", peer_username=None, telegram_chat_id=42)


def message():
    return SimpleNamespace(
        text="новое & важное",
        caption=None,
        edited_at=datetime(2026, 7, 28, 12, 30, tzinfo=UTC),
    )


def versions():
    return [
        SimpleNamespace(
            version_number=1,
            text="старое <b>",
            caption=None,
            created_at=datetime(2026, 7, 28, 12, 20, tzinfo=UTC),
        )
    ]


def test_edit_notification_contains_escaped_history_and_current_content() -> None:
    text = format_edit_notification(
        dialog=dialog(), settings=prefs(), message=message(), versions=versions()
    )
    assert "Версия 1" in text
    assert "Текущая версия" in text
    assert "старое &lt;b&gt;" in text
    assert "новое &amp; важное" in text
    assert "Все версии сохранены" in text


def test_hidden_preview_never_leaks_message_text() -> None:
    text = format_edit_notification(
        dialog=dialog(), settings=prefs(hide_preview=True), message=message(), versions=versions()
    )
    assert "старое" not in text
    assert "новое" not in text


def test_delete_notification_keeps_saved_content() -> None:
    deleted = SimpleNamespace(
        text="удалённый текст",
        caption=None,
        sent_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        deleted_at=datetime(2026, 7, 28, 12, 5, tzinfo=UTC),
    )
    text = format_delete_notification(dialog=dialog(), settings=prefs(), message=deleted)
    assert "удалённый текст" in text
    assert "Отправлено:" in text
    assert "Удалено:" in text


class FakeTelegramMessage:
    def __init__(self, protected: bool, raw: dict | None = None):
        self.has_protected_content = protected
        self._raw = raw or {}

    def model_dump(self, **_: object) -> dict:
        return dict(self._raw)


def test_protected_media_requires_explicit_telegram_signal() -> None:
    assert is_protected_message(FakeTelegramMessage(True)).allowed is True
    assert is_protected_message(FakeTelegramMessage(False, {"ttl_seconds": 10})).allowed is True
    assert is_protected_message(FakeTelegramMessage(False, {"is_view_once": True})).allowed is True
    assert is_protected_message(FakeTelegramMessage(False)).allowed is False


def test_reply_to_ordinary_media_is_blocked() -> None:
    media = SimpleNamespace(is_protected=False)
    reply = SimpleNamespace(reply_to_message_id=100)
    decision = protected_reply_is_allowed(media=media, reply_message=reply)
    assert decision.allowed is False
    assert decision.reason == "stored_media_not_protected"


def test_reply_to_stored_protected_media_is_allowed() -> None:
    media = SimpleNamespace(is_protected=True)
    reply = SimpleNamespace(reply_to_message_id=100)
    assert protected_reply_is_allowed(media=media, reply_message=reply).allowed is True
