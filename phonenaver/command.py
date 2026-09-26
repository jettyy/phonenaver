"""휴대폰에서 온 메시지를 해석해 '무엇을 쓸지'를 정한다.

- 링크가 없으면: 키워드/문장 주제로 최신 정보를 검색해서 쓴다.
- 링크만 주면: 링크 내용을 분석해서 쓴다.
- "넣어/삽입/첨부/걸어" 같은 말과 함께 준 링크는 본문에 하이퍼링크로 넣는다.
- "그대로:" 로 시작하면 AI 없이 보낸 글을 그대로 임시저장한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

URL_RE = re.compile(r"https?://[^\s<>\"'\]\)）」』]+")
# 링크를 '본문에 넣어 달라'는 뜻으로 보는 표현
INSERT_WORDS = ("넣어", "넣고", "삽입", "첨부", "걸어", "걸고", "달아", "포함", "링크추가", "링크 추가")
# 링크가 있어도 최신 정보를 추가로 검색하라는 표현
SEARCH_WORDS = ("검색", "최신", "찾아", "조사", "요즘", "최근")
RAW_PREFIXES = ("그대로:", "그대로 :", "원문:", "/raw")
DRY_PREFIXES = ("/test", "/dry", "미리보기:")


@dataclass
class Command:
    text: str
    instruction: str
    urls: list[str] = field(default_factory=list)
    insert_urls: list[str] = field(default_factory=list)
    search: bool = True
    raw: bool = False
    dry_run: bool = False

    @property
    def analyze_urls(self) -> list[str]:
        """본문 삽입용이 아닌, 분석 대상으로만 준 링크."""
        return [u for u in self.urls if u not in self.insert_urls]


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:!?…~")


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    compact = text.replace(" ", "")
    return any(w.replace(" ", "") in compact for w in words)


def parse(text: str) -> Command:
    text = text.strip()
    dry_run = False
    for prefix in DRY_PREFIXES:
        if text.lower().startswith(prefix):
            dry_run = True
            text = text[len(prefix):].strip()
            break

    for prefix in RAW_PREFIXES:
        if text.lower().startswith(prefix):
            body = text[len(prefix):].strip()
            return Command(text=body, instruction=body, raw=True, search=False, dry_run=dry_run)

    urls: list[str] = []
    insert_urls: list[str] = []
    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    # 여러 줄 중 '링크 + 넣어' 가 같은 줄에 있으면 그 줄의 링크만 삽입용으로 본다.
    # 그렇지 않으면 메시지 어딘가에 '넣어' 가 있을 때 모든 링크를 삽입용으로 본다.
    per_line = len(lines) > 1 and any(
        URL_RE.search(ln) and _has_any(URL_RE.sub(" ", ln), INSERT_WORDS) for ln in lines
    )
    message_insert = _has_any(URL_RE.sub(" ", text), INSERT_WORDS)

    for line in lines:
        line_insert = _has_any(URL_RE.sub(" ", line), INSERT_WORDS) if per_line else message_insert
        for url in (_clean_url(u) for u in URL_RE.findall(line)):
            if url in urls:
                continue
            urls.append(url)
            if line_insert:
                insert_urls.append(url)

    instruction = re.sub(r"\s+", " ", URL_RE.sub(" ", text)).strip()
    analyze_only = [u for u in urls if u not in insert_urls]
    # 분석할 링크를 줬으면 그 링크가 주 재료. 검색하라는 말이 있을 때만 추가 검색.
    search = not analyze_only or _has_any(instruction, SEARCH_WORDS)
    return Command(
        text=text,
        instruction=instruction,
        urls=urls,
        insert_urls=insert_urls,
        search=search,
        dry_run=dry_run,
    )
