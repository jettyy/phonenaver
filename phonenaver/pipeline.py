"""메시지 한 건 → (조사/링크 분석) → 글 작성 → 네이버 임시저장."""
from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import logging
from pathlib import Path

from . import html_utils, images
from .ai import BlogPost, PhotoAnalysis, Research, Writer
from .command import Command, parse
from .config import Config
from .fetcher import Page, fetch
from .naver import Category, DraftResult, NaverBlog, match_category

log = logging.getLogger(__name__)

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
    images: list[images.PreparedImage] = field(default_factory=list)
    category: Category | None = None
    warnings: list[str] = field(default_factory=list)
    photo_analysis: PhotoAnalysis | None = None
    photos_received: int = 0
    photos_attached: int = 0
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

    async def _categories(self, cmd: Command, warnings: list[str]) -> list[Category]:
        if self.naver is None or not (self.cfg.auto_category or cmd.category):
            return []
        try:
            return await self.naver.categories()
        except Exception as exc:
            log.warning("카테고리 목록 불러오기 실패: %s", exc)
            warnings.append("카테고리 목록을 불러오지 못해 기본 카테고리에 저장")
            return []

    async def generate(self, cmd: Command, photos: list[Path] | None = None, progress: Progress = _silent) -> Result:
        photos = list(photos or [])
        warnings: list[str] = []
        # 보낸 사진: '분석만' 이거나 '이미지 없이' 면 첨부하지 않음
        attach = photos if (cmd.photo_mode == "attach" and not cmd.no_images) else []
        if cmd.raw:
            lines = cmd.text.splitlines()
            title = lines[0].strip() if lines else "제목 없음"
            count = len(attach)  # 그대로 모드는 보낸 사진만 넣는다
            body = html_utils.text_to_html("\n".join(lines[1:]) or cmd.text)
            body = images.ensure_markers(body, count) if count else body
            post = BlogPost(title=title, body_html=body, tags=[], images=[], category=cmd.category or "")
            cats = await self._categories(cmd, warnings) if cmd.category else []
            category = match_category(cmd.category, cats) or (Category(name=cmd.category) if cmd.category else None)
            prepared = await asyncio.to_thread(images.prepare, self.cfg, title, [], attach, count)
            return Result(
                command=cmd, post=post, body_html=body, images=prepared, category=category, warnings=warnings,
                photos_received=len(photos), photos_attached=len(attach),
            )

        image_count = 0 if cmd.no_images else max(self.cfg.image_count, len(attach))

        photo_analysis = None
        if photos:
            await progress(f"📷 보낸 사진 {len(photos)}장 분석 중...")
            photo_analysis = await asyncio.to_thread(self._writer().analyze_photos, photos, cmd.instruction)

        pages: list[Page] = []
        if cmd.urls:
            await progress(f"🔗 링크 {len(cmd.urls)}개 읽는 중...")
            pages = list(await asyncio.gather(*(asyncio.to_thread(fetch, u) for u in cmd.urls)))
            failed = [p for p in pages if not p.ok]
            if failed:
                await progress("⚠️ 읽지 못한 링크: " + ", ".join(p.url for p in failed))

        cats = await self._categories(cmd, warnings)

        research = None
        topic = cmd.instruction
        if photo_analysis and photo_analysis.search_topic:
            topic = f"{topic}\n(사진 분석으로 파악한 주제: {photo_analysis.search_topic})".strip()
        if cmd.search and topic:
            await progress("🔎 최신 정보 검색 중...")
            research = await asyncio.to_thread(self._writer().research, topic)

        await progress("✍️ 글 작성 중...")
        post = await asyncio.to_thread(
            self._writer().write,
            cmd.instruction + (f"\n(카테고리는 '{cmd.category}' 로 지정)" if cmd.category else ""),
            research,
            pages,
            cmd.insert_urls,
            image_count,
            [c.label for c in cats],
            len(attach),
            photos,
            photo_analysis,
        )

        body = html_utils.sanitize(post.body_html)
        body = html_utils.append_links_section(body, cmd.insert_urls)  # 빠뜨린 링크 보정
        if self.cfg.include_sources and research and research.sources:
            items = "".join(
                f'<li><a href="{html.escape(u)}">{html.escape(t)}</a></li>' for t, u in research.sources[:8]
            )
            body += f"\n<h3>참고 자료</h3>\n<ul>{items}</ul>"
        if self.cfg.append_hashtags and post.tags:
            body += "\n<p>" + " ".join("#" + t.replace(" ", "").lstrip("#") for t in post.tags) + "</p>"
        if image_count:
            body = images.ensure_markers(body, image_count)

        wanted = cmd.category or post.category
        category = match_category(wanted, cats)
        if category is None and cmd.category:
            category = Category(name=cmd.category)
        if wanted and category is None:
            warnings.append(f"카테고리 '{wanted}' 를 찾지 못해 기본 카테고리에 저장")

        prepared: list[images.PreparedImage] = []
        if image_count:
            await progress(f"🖼 이미지 {image_count}장 준비 중...")
            plans = [(p.query, p.card_text) for p in post.images]
            prepared = await asyncio.to_thread(images.prepare, self.cfg, post.title, plans, attach, image_count)
        return Result(
            command=cmd, post=post, body_html=body, pages=pages, research=research,
            images=prepared, category=category, warnings=warnings,
            photo_analysis=photo_analysis, photos_received=len(photos), photos_attached=len(attach),
        )

    async def run(self, text: str, progress: Progress = _silent, photos: list[Path] | None = None) -> Result:
        cmd = parse(text)
        result = await self.generate(cmd, photos, progress)
        if cmd.dry_run:
            return result
        if self.naver is None:
            raise RuntimeError("NAVER_BLOG_ID 가 설정되지 않아 임시저장할 수 없습니다.")
        await progress("💾 네이버 블로그에 임시저장 중...")
        result.draft = await self.naver.save_draft(
            result.post.title, result.body_html, [img.path for img in result.images], result.category
        )
        result.warnings += result.draft.warnings
        return result
