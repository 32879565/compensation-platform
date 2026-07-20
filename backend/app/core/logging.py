"""结构化 JSON 日志（替代旧系统的『日志全禁』）。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 允许通过 extra={"context": {...}} 附加结构化字段
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(debug: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
