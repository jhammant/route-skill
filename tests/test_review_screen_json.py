"""JSON puts a quote between the key and the colon; both secret regexes miss it."""
import pytest

from route.ultra_safety import screen_digest


@pytest.mark.parametrize("text", [
    '{"db_password": "Xk9fP2mQ7rT4zz"}',
    '"api_key": "a1b2c3d4e5f6g7h8"',
])
def test_json_quoted_secret_keys_are_screened(text):
    assert screen_digest(text) is not None
