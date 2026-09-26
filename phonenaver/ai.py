"""Claude 로 최신 정보 조사 + 네이버 블로그 글 작성."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel, Field

from .config import Config
from .fetcher import Page

KST = ZoneInfo("Asia/Seoul")


class AIError(RuntimeError):
    pass


class BlogPost(BaseModel):
    title: str = Field(description="네이버 블로그 글 제목 (검색 키워드를 앞쪽에, 40자 이내)")
    body_html: str = Field(description="본문 HTML. h2, h3, p, strong, ul, ol, li, a, blockquote, hr, table 만 사용")
    tags: list[str] = Field(description="네이버 태그용 키워드 5~10개, # 없이")


@dataclass
class Research:
    notes: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url)


def today() -> str:
    now = datetime.now(KST)
    return f"{now:%Y년 %m월 %d일} ({'월화수목금토일'[now.weekday()]})"


class Writer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key) if cfg.anthropic_api_key else anthropic.Anthropic()

    def _extra(self) -> dict:
        if not self.cfg.claude_fallback:
            return {}
        # 안전 분류기가 거절하면 서버가 권장 모델로 자동 재시도
        return {
            "extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
            "extra_body": {"fallbacks": "default"},
        }

    @staticmethod
    def _check(response) -> None:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            reason = getattr(details, "explanation", None) or "안전 정책"
            raise AIError(f"Claude가 요청을 거절했습니다: {reason}")

    # ── 1단계: 최신 정보 조사 (웹 검색) ─────────────────────────
    def research(self, topic: str) -> Research:
        prompt = (
            f"오늘은 {today()}입니다.\n"
            "아래 주제로 네이버 블로그 글을 쓰려고 합니다. 웹 검색으로 가장 최신 정보를 조사해서 "
            "글 작성에 필요한 사실, 수치, 날짜, 가격, 일정, 변경 사항 등을 한국어로 정리해 주세요.\n"
            "- 오래된 정보와 최신 정보가 다르면 최신 기준으로, 기준 날짜를 함께 적어 주세요.\n"
            "- 확인되지 않은 내용은 '미확인'으로 표시하세요.\n"
            "- 각 항목 뒤에 근거 출처 URL을 적어 주세요.\n\n"
            f"주제/지시: {topic}"
        )
        tools = [{
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": self.cfg.max_searches,
            "user_location": {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"},
        }]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        response = None
        for _ in range(5):
            response = self.client.messages.create(
                model=self.cfg.claude_model,
                max_tokens=16000,
                tools=tools,
                messages=messages,
                **self._extra(),
            )
            if response.stop_reason != "pause_turn":
                break
            # 서버 측 검색이 길어져 잠시 멈춘 경우 이어서 진행
            messages = [messages[0], {"role": "assistant", "content": response.content}]
        assert response is not None
        self._check(response)

        notes, sources, seen = [], [], set()
        for block in response.content:
            if block.type == "text":
                notes.append(block.text)
            elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                for item in block.content:
                    url = getattr(item, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        sources.append((getattr(item, "title", "") or url, url))
        return Research(notes="\n".join(notes).strip(), sources=sources)

    # ── 2단계: 블로그 글 작성 ─────────────────────────────────
    def write(
        self,
        instruction: str,
        research: Research | None,
        pages: list[Page],
        insert_urls: list[str],
    ) -> BlogPost:
        parts = [f"오늘 날짜: {today()}", f"[사용자 지시]\n{instruction or '(지시 없음 - 링크 내용으로 글 작성)'}"]
        if research and research.notes:
            parts.append(f"[최신 정보 조사 결과]\n{research.notes}")
        for i, page in enumerate(pages, 1):
            if page.ok:
                note = " (길어서 앞부분만)" if page.truncated else ""
                parts.append(
                    f"[참고 링크 {i}{note}] {page.title}\nURL: {page.url}\n---\n{page.text}\n---"
                )
            else:
                parts.append(f"[참고 링크 {i}] {page.url} - 읽지 못함: {page.error}")
        if insert_urls:
            links = "\n".join(f"- {u}" for u in insert_urls)
            parts.append(
                "[본문에 반드시 넣을 링크]\n" + links + "\n"
                "위 링크는 모두 <a href=\"URL 그대로\">자연스러운 안내 문구</a> 형태로 본문의 알맞은 위치에 넣으세요. "
                "URL은 한 글자도 바꾸지 마세요."
            )
        else:
            parts.append("[링크 규칙] 사용자가 넣으라고 한 링크가 없으므로 본문에 외부 링크를 넣지 마세요.")

        response = self.client.messages.parse(
            model=self.cfg.claude_model,
            max_tokens=16000,
            system=WRITER_SYSTEM,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            output_format=BlogPost,
            **self._extra(),
        )
        self._check(response)
        post = response.parsed_output
        if post is None:
            raise AIError(f"글 생성 결과를 읽지 못했습니다 (stop_reason={response.stop_reason})")
        return post


WRITER_SYSTEM = """당신은 네이버 블로그 상위노출 경험이 많은 한국어 블로그 작가입니다.
사용자의 지시와 제공된 자료(최신 조사 결과, 참고 링크 본문)만 근거로 네이버 블로그 글을 씁니다.

글쓰기 원칙
- 친근하고 읽기 쉬운 존댓말(~요, ~습니다 혼용). 광고 티 나는 과장 표현은 피합니다.
- 도입부 2~3문장에서 독자가 얻을 내용을 먼저 알려 줍니다.
- 소제목(h2) 3~6개로 구성하고, 필요하면 h3 를 씁니다. 문단은 2~4문장으로 짧게.
- 핵심 수치·날짜·주의 사항은 <strong> 으로 강조하고, 나열은 ul/ol, 비교는 table 을 씁니다.
- 마지막에 요약 또는 한 줄 정리로 마무리합니다.
- 분량은 사용자가 따로 말하지 않으면 공백 포함 2,000~3,000자.
- 자료에 없는 사실, 수치, 후기를 지어내지 않습니다. 날짜가 중요한 정보는 기준일을 밝힙니다.
- 참고 링크를 분석해 쓸 때는 원문을 그대로 베끼지 말고 내 말로 재구성하고, 필요한 경우 내 의견·정리를 덧붙입니다.
- 사용자가 말투, 분량, 구성, 대상 독자 등을 지정하면 그것을 우선합니다.

형식 규칙
- body_html 에는 제목(h1)을 넣지 않습니다. 허용 태그: h2, h3, p, br, strong, em, u, ul, ol, li, a, blockquote, hr, table, tr, th, td.
- 이미지, 이모지 남용, 마크다운 문법(**, ##)은 쓰지 않습니다.
- tags 는 검색에 쓰일 핵심 키워드 5~10개 (# 없이)."""
