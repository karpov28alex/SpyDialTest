# Contract-level test: ordinary business messages are archived but never enqueue send_text.
# The webhook route only enqueues notifications in connection/edit/delete/protected branches.
def test_ordinary_message_delivery_contract() -> None:
    from pathlib import Path
    source = Path("app/api/routes/webhook.py").read_text()
    ordinary = source.split("elif update.business_message:", 1)[1].split("elif update.edited_business_message:", 1)[0]
    assert 'kind="send_text"' not in ordinary
