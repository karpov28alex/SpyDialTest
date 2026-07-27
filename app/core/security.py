import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from app.core.config import Settings

ALGORITHM = "HS256"


def validate_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int) -> dict:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing Telegram hash")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid Telegram signature")
    auth_date = int(values.get("auth_date", "0"))
    if auth_date <= 0 or time.time() - auth_date > max_age_seconds:
        raise ValueError("Telegram initData expired")
    user = json.loads(values.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("Telegram user missing")
    return user


def create_token(subject: str, token_type: str, ttl: timedelta, settings: Settings) -> str:
    from jose import jwt
    now = datetime.now(UTC)
    payload = {"sub": subject, "typ": token_type, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str, settings: Settings) -> str:
    from jose import JWTError, jwt
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid token") from exc
    if payload.get("typ") != expected_type or not payload.get("sub"):
        raise ValueError("Invalid token type")
    return str(payload["sub"])
