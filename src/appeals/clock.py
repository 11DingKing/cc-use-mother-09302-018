"""时间源抽象，便于在测试中控制期限。"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

Clock = Callable[[], datetime]


def system_clock() -> Clock:
    return datetime.now


class MovableClock:
    """可显式拨快的测试时钟。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 30, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now

    def set(self, value: datetime) -> None:
        self.now = value
