"""메시지 한 건 → (조사/링크 분석) → 글 작성 → 네이버 임시저장."""
from __future__ import annotations

import asyncio
import re
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
from .browser import BrowserSession
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
    def __init__(self, cfg: Config, session: BrowserSession | None = None):
        self.cfg = cfg
        self.writer: Writer | None = None
        self.session = session
        self.naver = NaverBlog(cfg, session) if cfg.naver_blog_id else None

    async def close(self) -> None:
        """이 파이프라인이 직접 띄운 브라우저만 닫는다 (공유 브라우저는 앱이 관리)."""
        if self.session is None and self.naver is not None:
            await self.naver.session.close()

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

    async def _youtube_fallback(self, pages: list[Page], progress: Progress) -> list[Page]:
        """유튜브 대사를 빠른 방법으로 못 가져오면 실제 브라우저로 [스크립트 표시] 를 열어 읽는다.
        그래도 못 읽으면 글을 쓰지 않는다 (제목·설명만으로 추측해서 쓰지 않음)."""
        from .browser import BrowserSession
        from .fetcher import youtube_page
        from .youtube import TranscriptUnavailable, transcript_via_browser

        out = []
        for page in pages:
            if page.kind != "youtube" or page.ok or page.video is None or not getattr(page.video, "id", ""):
                out.append(page)
                continue
            video = page.video
            await progress(f"🎬 '{(video.title or video.id)[:40]}' 대사를 브라우저로 다시 읽는 중...")
            session = self.session or (self.naver.session if self.naver else None)
            own = session is None
            session = session or BrowserSession(self.cfg)
            try:
                text, err = await transcript_via_browser(session, video.id)
            finally:
                if own:
                    await session.close()
            if text:
                out.append(youtube_page(page.url, video, text))
                continue
            blocked = bool(getattr(video, "blocked", False)) and "없습니다" not in err
            raise TranscriptUnavailable(
                f"유튜브 대사를 가져오지 못해 글을 쓰지 않았습니다 — {video.error}"
                + (f" / 브라우저: {err}" if err else ""),
                blocked=blocked,
            )
        return out

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
            body = html_utils.add_paragraph_gaps(body, self.cfg.paragraph_gap)
            post = BlogPost(title=title, body_html=body, tags=[], images=[], category=cmd.category or "")
            cats = await self._categories(cmd, warnings) if cmd.category else []
            category = match_category(cmd.category, cats) or (Category(name=cmd.category) if cmd.category else None)
            prepared = await asyncio.to_thread(images.prepare, self.cfg, title, [], attach, count)
            return Result(
                command=cmd, post=post, body_html=body, images=prepared, category=category, warnings=warnings,
                photos_received=len(photos), photos_attached=len(attach),
            )

        per_section = self.cfg.image_per_section
        if cmd.no_images:
            image_count = 0
        elif per_section:  # 문단(소제목) 사이마다 — 최종 장수는 글이 나온 뒤 소제목 수로 정해진다
            image_count = max(self.cfg.max_images, len(attach))
        else:
            image_count = max(self.cfg.image_count, len(attach))

        photo_analysis = None
        if photos:
            await progress(f"📷 보낸 사진 {len(photos)}장 분석 중...")
            photo_analysis = await asyncio.to_thread(self._writer().analyze_photos, photos, cmd.instruction)

        pages: list[Page] = []
        if cmd.urls:
            from .youtube import is_youtube

            n_yt = sum(1 for u in cmd.urls if is_youtube(u))
            what = f"유튜브 {n_yt}개 대사(자막)" if n_yt == len(cmd.urls) else f"링크 {len(cmd.urls)}개"
            await progress(f"{'🎬' if n_yt else '🔗'} {what} 읽는 중...")
            pages = list(await asyncio.gather(*(asyncio.to_thread(fetch, u) for u in cmd.urls)))
            failed = [p for p in pages if not p.ok]
            if failed:
                await progress("⚠️ 읽지 못한 링크: " + ", ".join(f"{p.url} ({p.error})" for p in failed))
            warnings += [p.warning for p in pages if p.warning]
            pages = await self._youtube_fallback(pages, progress)
            # 블로그 글을 소재로 다시 쓰는 요청인데 원글을 못 읽었으면 추측해서 쓰지 않는다
            unread = [p for p in pages if p.kind == "naver_blog" and not p.ok and p.url not in cmd.insert_urls]
            if unread:
                raise RuntimeError(f"블로그 글을 읽지 못해 글을 쓰지 않았습니다 — {unread[0].url} ({unread[0].error})")
            for p in pages:
                if p.ok and p.kind == "youtube":
                    await progress(f"🎬 '{p.title[:40]}' 대사 {len(p.text):,}자 읽음 — 분석해서 글 작성")

        cats = await self._categories(cmd, warnings)

        # 순위표: '순위/TOP N' 이라고 했으면 그 개수, 아니어도 (설정이 켜져 있으면) 모든 글에 TOP 순위표
        rank_explicit = cmd.rank_target is not None
        if rank_explicit:
            rank_need = cmd.rank_target or self.cfg.ranking_min
        elif self.cfg.always_ranking:
            rank_need = self.cfg.ranking_min
        else:
            rank_need = None

        research = None
        topic = cmd.instruction
        if photo_analysis and photo_analysis.search_topic:
            topic = f"{topic}\n(사진 분석으로 파악한 주제: {photo_analysis.search_topic})".strip()
        # 링크·유튜브 글도 무슨 주제인지 알려 주고 최신 정보·순위 자료를 찾게 한다 (겉핥기 방지)
        for page in pages:
            if page.ok:
                topic += f"\n\n[참고 자료 주제] {page.title}\n{page.text[:1500]}"
        if (cmd.search or self.cfg.always_research) and topic.strip():
            await progress("🔎 최신 정보·순위 자료 검색 중...")
            research = await asyncio.to_thread(self._writer().research, topic, rank_need, rank_explicit)

        await progress("✍️ 글 작성 중...")
        write_args = (
            cmd.instruction + (f"\n(카테고리는 '{cmd.category}' 로 지정)" if cmd.category else ""),
            research,
            pages,
            cmd.insert_urls,
            image_count,
            [c.label for c in cats],
            len(attach),
            photos,
            photo_analysis,
            "section" if per_section else "fixed",
        )
        quality = {"rank": rank_need, "rank_explicit": rank_explicit, "min_chars": self.cfg.min_chars}
        post = await asyncio.to_thread(self._writer().write, *write_args, **quality)

        # 품질 검사: 표·순위표(1위부터 끝까지)·최소 분량. 어기면 고칠 점을 짚어 최대 2번 다시 쓰게 한다
        blog_sources = "\n".join(p.text for p in pages if p.ok and p.kind == "naver_blog")

        def problems_of(p) -> list[str]:
            body_ = html_utils.sanitize(p.body_html)
            out = html_utils.check_post(body_, rank_need, self.cfg.min_chars)
            copied = html_utils.copied_sentences(body_, blog_sources) if blog_sources else []
            if len(copied) >= 2:
                sample = " / ".join(c[:40] for c in copied[:3])
                out.append(f"원글 문장을 그대로 옮긴 곳이 {len(copied)}군데 있습니다 (예: {sample}). 이 문장들을 새로 쓰세요.")
            return out

        def score(p) -> tuple[int, int, int]:
            body_ = html_utils.sanitize(p.body_html)
            return (-len(problems_of(p)), html_utils.rank_rows(body_), html_utils.body_chars(body_))

        problems = problems_of(post)
        for attempt in range(2):
            if not problems:
                break
            await progress(f"✍️ 순위표·분량·문장을 보완해서 다시 쓰는 중... ({attempt + 1}/2)")
            retry = await asyncio.to_thread(
                self._writer().write, *write_args, fix_note="\n".join(f"- {x}" for x in problems), **quality
            )
            if score(retry) >= score(post):
                post = retry
            problems = problems_of(post)
        for x in problems:
            warnings.append(x.split(".")[0] + " (다시 써도 부족해서 그대로 저장)")

        body = html_utils.sanitize(post.body_html)
        if any(p.ok and p.kind == "youtube" for p in pages):
            mention = re.search(r"유튜브|유튜버|YouTube|영상에서|이 영상|채널에서", html_utils.html_to_text(body) + post.title, re.I)
            if mention:
                warnings.append(f"본문에 '{mention.group(0)}' 언급이 남아 있습니다 — 발행 전에 확인하세요")
        body = html_utils.append_links_section(body, cmd.insert_urls)  # 빠뜨린 링크 보정
        if image_count and per_section:
            # 사진 자리: 도입부 뒤 + 소제목 앞마다 (태그·출처보다 먼저 정해야 끝에 사진이 붙지 않음)
            body, image_count = images.place_by_section(body, self.cfg.max_images, len(attach))
        if self.cfg.include_sources and research and research.sources:
            items = "".join(
                f'<li><a href="{html.escape(u)}">{html.escape(t)}</a></li>' for t, u in research.sources[:8]
            )
            body += f"\n<h3>참고 자료</h3>\n<ul>{items}</ul>"
        if self.cfg.append_hashtags and post.tags:
            body += "\n<p>" + " ".join("#" + t.replace(" ", "").lstrip("#") for t in post.tags) + "</p>"
        if image_count and not per_section:
            body = images.ensure_markers(body, image_count)
        body = html_utils.add_paragraph_gaps(body, self.cfg.paragraph_gap)  # 문단 사이 빈 줄

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

    async def run(
        self,
        text: str,
        progress: Progress = _silent,
        photos: list[Path] | None = None,
        photo_mode: str | None = None,
    ) -> Result:
        """글을 쓰고 임시저장한다. (발행은 정해진 시각에 jobs 의 발행 예약이 따로 한다)"""
        cmd = parse(text)
        if photo_mode in ("attach", "analyze"):  # 대시보드에서 버튼으로 고른 경우
            cmd.photo_mode = photo_mode
        if not cmd.dry_run and self.naver is not None:
            await self.naver.ensure_login()
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
