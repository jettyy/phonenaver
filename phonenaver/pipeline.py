"""메시지 한 건 → (조사/링크 분석) → 글 작성 → 네이버 임시저장."""
from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import html_utils
from .ai import BlogPost, Research, Writer
from .command import Command, parse
from .config import Config
from .fetcher import Page, fetch
from .naver import DraftResult, NaverBlog

Progress = Callable[[str], Awaitable[None]]


async def _silent(_: str) -> None:
    return None


@dataclass
class Result:
    command: Command
    post: BlogPost
    body_html: str
    pages: list[Page] = field(default_factory=list)
    research: Research | None = None
    draft: DraftResult | None = None


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.writer: Writer | None = None
        self.naver = NaverBlog(cfg) if cfg.naver_blog_id else None

    def _writer(self) -> Writer:
        if self.writer is None:
            self.writer = Writer(self.cfg)
        return self.writer

    async def generate(self, cmd: Command, progress: Progress = _silent) -> Result:
        if cmd.raw:
            lines = cmd.text.splitlines()
            title = lines[0].strip() if lines else "제목 없음"
            body = html_utils.text_to_html("\n".join(lines[1:]) or cmd.text)
            return Result(command=cmd, post=BlogPost(title=title, body_html=body, tags=[]), body_html=body)

        pages: list[Page] = []
        if cmd.urls:
            await progress(f"🔗 링크 {len(cmd.urls)}개 읽는 중...")
            pages = list(await asyncio.gather(*(asyncio.to_thread(fetch, u) for u in cmd.urls)))
            failed = [p for p in pages if not p.ok]
            if failed:
                await progress("⚠️ 읽지 못한 링크: " + ", ".join(p.url for p in failed))

        research = None
        if cmd.search and cmd.instruction:
            await progress("🔎 최신 정보 검색 중...")
            research = await asyncio.to_thread(self._writer().research, cmd.instruction)

        await progress("✍️ 글 작성 중...")
        post = await asyncio.to_thread(self._writer().write, cmd.instruction, research, pages, cmd.insert_urls)

        body = html_utils.sanitize(post.body_html)
        body = html_utils.append_links_section(body, cmd.insert_urls)  # 빠뜨린 링크 보정
        if self.cfg.include_sources and research and research.sources:
            items = "".join(
                f'<li><a href="{html.escape(u)}">{html.escape(t)}</a></li>' for t, u in research.sources[:8]
            )
            body += f"\n<h3>참고 자료</h3>\n<ul>{items}</ul>"
        if self.cfg.append_hashtags and post.tags:
            body += "\n<p>" + " ".join("#" + t.replace(" ", "").lstrip("#") for t in post.tags) + "</p>"
        return Result(command=cmd, post=post, body_html=body, pages=pages, research=research)

    async def run(self, text: str, progress: Progress = _silent) -> Result:
        cmd = parse(text)
        result = await self.generate(cmd, progress)
        if cmd.dry_run:
            return result
        if self.naver is None:
            raise RuntimeError("NAVER_BLOG_ID 가 설정되지 않아 임시저장할 수 없습니다.")
        await progress("💾 네이버 블로그에 임시저장 중...")
        result.draft = await self.naver.save_draft(result.post.title, result.body_html)
        return result
