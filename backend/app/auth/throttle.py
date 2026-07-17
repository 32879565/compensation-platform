"""登录失败限速（防在线爆破）。

按 (ip, username) 维度做滑动窗口失败计数，超阈值锁定一段时间。
说明：进程内实现，仅适用于单进程/单 worker 部署；多 worker/多实例需换
Redis 等共享存储（TODO S17）。设计成可注入以便将来替换。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta


class LoginThrottle:
    def __init__(self, max_failures: int, lockout_minutes: int) -> None:
        self._max = max_failures
        self._lockout = timedelta(minutes=lockout_minutes)
        self._lock = threading.Lock()
        # key -> (失败次数, 窗口起点/锁定起点)
        self._failures: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _prune(self, key: tuple[str, str], now: datetime) -> None:
        window_start = now - self._lockout
        self._failures[key] = [t for t in self._failures[key] if t >= window_start]

    def is_locked(self, ip: str, username: str) -> bool:
        key = (ip, username)
        with self._lock:
            now = self._now()
            self._prune(key, now)
            return len(self._failures[key]) >= self._max

    def record_failure(self, ip: str, username: str) -> None:
        key = (ip, username)
        with self._lock:
            now = self._now()
            self._prune(key, now)
            self._failures[key].append(now)

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            self._failures.pop((ip, username), None)
