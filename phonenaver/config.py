from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(part) for part in raw.replace(" ", "").split(",") if part}


@dataclass
class Config:
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5"
    claude_fallback: bool = True
    max_searches: int = 5

    telegram_bot_token: str = ""
    allowed_chat_ids: set[int] = field(default_factory=set)

    naver_blog_id: str = ""
    headless: bool = True
    browser_profile_dir: Path = Path("data/browser")
    browser_executable: str | None = None
    screenshot_dir: Path = Path("data/screens")

    include_sources: bool = False
    append_hashtags: bool = True

    @classmethod
    def load(cls, env_file: str | None = ".env") -> "Config":
        if env_file:
            load_dotenv(env_file)
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5",
            claude_fallback=_bool("CLAUDE_FALLBACK", True),
            max_searches=_int("MAX_SEARCHES", 5),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_chat_ids=_ids("ALLOWED_CHAT_IDS"),
            naver_blog_id=os.getenv("NAVER_BLOG_ID", "").strip(),
            headless=_bool("HEADLESS", True),
            browser_profile_dir=Path(os.getenv("BROWSER_PROFILE_DIR", "").strip() or "data/browser"),
            browser_executable=os.getenv("BROWSER_EXECUTABLE", "").strip() or None,
            include_sources=_bool("INCLUDE_SOURCES", False),
            append_hashtags=_bool("APPEND_HASHTAGS", True),
        )
