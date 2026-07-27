import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from app.core.security import validate_telegram_init_data


def make_init_data(token: str) -> str:
    values = {"auth_date": str(int(time.time())), "query_id": "q", "user": json.dumps({"id": 123, "first_name": "Test"}, separators=(",", ":"))}
    data_check = "\n".join(f"{k}={values[k]}" for k in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_telegram_init_data_signature() -> None:
    token = "123456:TEST_TOKEN"
    user = validate_telegram_init_data(make_init_data(token), token, 600)
    assert user["id"] == 123
