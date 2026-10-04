from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 프로그램 폴더 (어디서 실행하든 이 폴더 기준으로 .env 와 data/ 를 사용)
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
# 사진 분석·최신 정보 조사·글 작성 모두 이 모델로만 돌린다 (Claude Code CLI 의 모델 별칭)
CLAUDE_MODEL = "sonnet"


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
    # 글쓰기 모델은 sonnet 으로 고정 (.env 로 바꿀 수 없음)
    claude_model: str = CLAUDE_MODEL
    claude_max_turns: int = 30
    claude_timeout: int = 900
    max_searches: int = 5

    telegram_bot_token: str = ""
    allowed_chat_ids: set[int] = field(default_factory=set)

    naver_blog_id: str = ""
    headless: bool = True
    browser_profile_dir: Path = DATA / "browser"
    browser_executable: str | None = None
    screenshot_dir: Path = DATA / "screens"

    include_sources: bool = False
    append_hashtags: bool = True

    image_count: int = 3
    # 문단(소제목) 사이마다 사진 넣기: 도입부 뒤 + 소제목마다. 끄면 image_count 장만
    image_per_section: bool = True
    max_images: int = 8
    # 문단 사이 빈 줄 수
    paragraph_gap: int = 2
    # 글 품질: 최소 분량(공백 제외), 모든 글에 순위표, 링크·유튜브 글도 최신 정보 검색
    min_chars: int = 1500
    always_ranking: bool = True
    ranking_min: int = 10
    always_research: bool = True
    # 글을 연달아 저장할 때 글 사이에 쉬는 시간(초)
    post_delay_min: int = 30
    post_delay_max: int = 90
    thumbnail_card: bool = True
    pexels_api_key: str = ""
    font_path: str | None = None
    image_dir: Path = DATA / "images"

    auto_category: bool = True
    # 카테고리 목록을 직접 지정할 때 (쉼표 구분). 비우면 블로그에서 자동으로 불러옴
    naver_categories: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, env_file: str | None = None) -> "Config":
        load_dotenv(env_file or ENV_FILE, override=True)
        return cls(
            claude_bin=os.getenv("CLAUDE_BIN", "").strip() or "claude",
            claude_oauth_token=os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
            claude_max_turns=_int("CLAUDE_MAX_TURNS", 30),
            claude_timeout=_int("CLAUDE_TIMEOUT", 900),
            max_searches=_int("MAX_SEARCHES", 5),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_chat_ids=_ids("ALLOWED_CHAT_IDS"),
            naver_blog_id=os.getenv("NAVER_BLOG_ID", "").strip(),
            headless=_bool("HEADLESS", True),
            browser_profile_dir=_path("BROWSER_PROFILE_DIR", DATA / "browser"),
            browser_executable=os.getenv("BROWSER_EXECUTABLE", "").strip() or None,
            include_sources=_bool("INCLUDE_SOURCES", False),
            append_hashtags=_bool("APPEND_HASHTAGS", True),
            image_count=_int("IMAGE_COUNT", 3),
            image_per_section=_bool("IMAGE_PER_SECTION", True),
            max_images=_int("MAX_IMAGES", 8),
            paragraph_gap=_int("PARAGRAPH_GAP", 2),
            min_chars=_int("MIN_CHARS", 1500),
            always_ranking=_bool("ALWAYS_RANKING", True),
            ranking_min=_int("RANKING_MIN", 10),
            always_research=_bool("ALWAYS_RESEARCH", True),
            post_delay_min=_int("POST_DELAY_MIN", 30),
            post_delay_max=_int("POST_DELAY_MAX", 90),
            thumbnail_card=_bool("THUMBNAIL_CARD", True),
            pexels_api_key=os.getenv("PEXELS_API_KEY", "").strip(),
            font_path=os.getenv("FONT_PATH", "").strip() or None,
            auto_category=_bool("AUTO_CATEGORY", True),
            naver_categories=[c.strip() for c in os.getenv("NAVER_CATEGORIES", "").split(",") if c.strip()],
        )


ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def ensure_env_file() -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


def set_env(key: str, value: str, env_file: Path | None = None) -> None:
    """.env 의 해당 줄만 바꾼다 (주석·다른 값은 유지)."""
    path = env_file or ENV_FILE
    if path == ENV_FILE:
        ensure_env_file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    value = str(value).replace("\n", " ").strip()
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
