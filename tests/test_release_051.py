from pathlib import Path

from app.main import app


def test_legacy_and_canonical_webhook_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/telegram/webhook/{secret}" in paths
    assert "/api/telegram/webhook/{secret}" in paths


def test_miniapp_profile_requests_do_not_abort_each_other() -> None:
    source = Path("app/static/miniapp/app.js").read_text(encoding="utf-8")
    assert "Promise.all([api('/api/me'),api('/api/settings')])" in source
    assert "controller?.abort()" not in source
    assert "state.screen==='profile'?await profile()" in source


def test_edit_history_is_only_rendered_for_actually_edited_messages() -> None:
    source = Path("app/static/miniapp/app.js").read_text(encoding="utf-8")
    assert "if(!m.edited_at||!m.versions?.length)return ''" in source


def test_admin_is_mobile_responsive_and_russian_localized() -> None:
    source = Path("app/static/admin/index.html").read_text(encoding="utf-8")
    assert "@media(max-width:760px)" in source
    assert "Защищённые медиа" in source
    assert "Реально изменённые сообщения" in source
    assert "Действия за последние 24 часа" in source
