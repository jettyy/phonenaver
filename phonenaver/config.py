from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 프로그램 폴더 (어디서 실행하든 이 폴더 기준으로 .env 와 data/ 를 사용)
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(part) for part in raw.replace(" ", "").split(",") if part}


@dataclass
class Config:
    # Claude: 구독 계정(Pro/Max)으로 Claude Code CLI 를 실행
    claude_bin: str = "claude"
    claude_oauth_token: str = ""  # `claude setup-token` 으로 받은 토큰 (서버용, 비우면 로그인 정보 사용)
    claude_model: str = ""  # 비우면 계정 기본 모델. opus / sonnet 등
    claude_max_turns: int = 30
    claude_timeout: int = 900
    max_searches: int = 5

    telegram_bot_token: str = ""
    allowed_chat_ids: set[int] = field(default_factory=set)

    naver_blog_id: str = ""
    # 네이버 아이디/비밀번호: 로그인이 풀리면 자동으로 다시 로그인
    naver_id: str = ""
    naver_pw: str = ""
    headless: bool = True
    browser_profile_dir: Path = DATA / "browser"
    browser_executable: str | None = None
    screenshot_dir: Path = DATA / "screens"

    include_sources: bool = False
    append_hashtags: bool = True

    image_count: int = 3
    thumbnail_card: bool = True
    pexels_api_key: str = ""
    font_path: str | None = None
    image_dir: Path = DATA / "images"

    auto_category: bool = True
    # 카테고리 목록을 직접 지정할 때 (쉼표 구분). 비우면 블로그에서 자동으로 불러옴
    naver_categories: list[str] = field(default_factory=list)

    # SLR클럽 장터 새 글 알림 (키워드는 텔레그램 /watch 로 등록 → data/slr_watch.json)
    slr_watch: bool = True
    slr_board_url: str = ""  # 비우면 회원장터 > 팝니다
    slr_interval: int = 60  # 확인 간격(초)
    slr_id: str = ""  # 목록이 로그인해야 보일 때만
    slr_pw: str = ""
    slr_watch_file: Path = DATA / "slr_watch.json"

    @classmethod
    def load(cls, env_file: str | None = None) -> "Config":
        load_dotenv(env_file or ROOT / ".env", override=True)
        return cls(
            claude_bin=os.getenv("CLAUDE_BIN", "").strip() or "claude",
            claude_oauth_token=os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
            claude_model=os.getenv("CLAUDE_MODEL", "").strip(),
            claude_max_turns=_int("CLAUDE_MAX_TURNS", 30),
            claude_timeout=_int("CLAUDE_TIMEOUT", 900),
            max_searches=_int("MAX_SEARCHES", 5),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_chat_ids=_ids("ALLOWED_CHAT_IDS"),
            naver_blog_id=os.getenv("NAVER_BLOG_ID", "").strip(),
            naver_id=os.getenv("NAVER_ID", "").strip(),
            naver_pw=os.getenv("NAVER_PW", ""),
            headless=_bool("HEADLESS", True),
            browser_profile_dir=_path("BROWSER_PROFILE_DIR", DATA / "browser"),
            browser_executable=os.getenv("BROWSER_EXECUTABLE", "").strip() or None,
            include_sources=_bool("INCLUDE_SOURCES", False),
            append_hashtags=_bool("APPEND_HASHTAGS", True),
            image_count=_int("IMAGE_COUNT", 3),
            thumbnail_card=_bool("THUMBNAIL_CARD", True),
            pexels_api_key=os.getenv("PEXELS_API_KEY", "").strip(),
            font_path=os.getenv("FONT_PATH", "").strip() or None,
            auto_category=_bool("AUTO_CATEGORY", True),
            naver_categories=[c.strip() for c in os.getenv("NAVER_CATEGORIES", "").split(",") if c.strip()],
            slr_watch=_bool("SLR_WATCH", True),
            slr_board_url=os.getenv("SLR_BOARD_URL", "").strip(),
            slr_interval=max(30, _int("SLR_INTERVAL", 60)),
            slr_id=os.getenv("SLR_ID", "").strip(),
            slr_pw=os.getenv("SLR_PW", ""),
        )
