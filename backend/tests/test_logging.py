import json
import logging

from app.core.logging import JsonFormatter


def _record(msg: str, **extra):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_basic_fields():
    out = JsonFormatter().format(_record("hello"))
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "t"
    assert parsed["msg"] == "hello"
    assert "ts" in parsed


def test_json_formatter_merges_context():
    out = JsonFormatter().format(_record("e", context={"user_id": 5, "action": "login"}))
    parsed = json.loads(out)
    assert parsed["user_id"] == 5
    assert parsed["action"] == "login"
