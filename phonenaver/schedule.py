"""발행 시각 정하기: 시작 시각 + 글 사이 간격 + 랜덤 추가 대기.

예) 시작 21:00, 간격 60분, 랜덤 0~30분
    1번 글 21:00~21:30 사이, 2번 글 (1번 발행 시각 + 60분) ~ +30분 사이, …
메시지로 '22시에 발행' 처럼 시각을 정하면 그 글은 그 시각(+랜덤)에 발행한다.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Callable

from .config import Config


def next_clock(hhmm: str, now: float | None = None) -> float:
    """오늘 HH:MM (이미 지났으면 내일 HH:MM) 의 시각."""
    now = time.time() if now is None else now
    hour, minute = (int(x) for x in hhmm.split(":"))
    base = datetime.fromtimestamp(now)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


class PublishScheduler:
    def __init__(self, get_cfg: Callable[[], Config], rand: Callable[[float, float], float] = random.uniform):
        self.get_cfg = get_cfg
        self.rand = rand
        self.last = 0.0  # 마지막으로 잡은 발행 시각

    def plan(self, fixed: str | None = None, now: float | None = None) -> float:
        cfg = self.get_cfg()
        now = time.time() if now is None else now
        jitter = self.rand(0, max(0, cfg.publish_random) * 60)
        if fixed:  # 메시지로 시각을 정한 글
            when = next_clock(fixed, now) + jitter
        else:
            when = now
            if cfg.publish_at:
                try:
                    when = max(when, next_clock(cfg.publish_at, now) if self.last < now else when)
                except ValueError:
                    pass
            if self.last:
                when = max(when, self.last + max(0, cfg.publish_interval) * 60)
            when += jitter
        self.last = max(self.last, when)
        return when


def fmt(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    day = "오늘" if d.date() == today else ("내일" if d.date() == today + timedelta(days=1) else f"{d.month}/{d.day}")
    return f"{day} {d:%H:%M}"
