"""Playwright 로 네이버 블로그 스마트에디터 ONE 을 열어 글을 붙여넣고 임시저장한다.

네이버는 글쓰기(임시저장) 공개 API 가 없어서 실제 브라우저를 조작한다.
브라우저와 로그인 세션은 browser.BrowserSession 이 계속 열어 두고 관리한다.
(로그인은 사람이 창에서 한 번만 직접 한다. 프로그램이 아이디/비밀번호를 치지 않는다.)

에디터 화면 구조가 바뀌면 아래 SELECTORS 만 고치면 된다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Frame, Page
from playwright.async_api import TimeoutError as PWTimeout

from .browser import LOGIN_HOST, NOT_LOGGED_IN, BrowserSession
from .config import Config
from .html_utils import html_to_text
from .images import marker

log = logging.getLogger(__name__)

SELECTORS = {
    "editor_ready": ".se-content, .se-main-container, .se-documentTitle",
    "title": ".se-documentTitle .se-text-paragraph, .se-title-text .se-text-paragraph, .se-documentTitle",
    "body": ".se-component.se-text .se-text-paragraph, .se-section-text .se-text-paragraph",
    "content": ".se-content",
    # 팝업: '작성 중인 글이 있습니다' → 취소(새 글), 도움말 패널 닫기
    "popup_cancel": ".se-popup-button-cancel",
    "help_close": ".se-help-panel-close-button, button.se-help-close-button",
    "paragraph": ".se-text-paragraph",
    # 사진 업로드: 툴바 [사진] 버튼 → 파일 선택
    "image_button": (
        "button[data-name='image'], button.se-image-toolbar-button, "
        "button[class*='image-toolbar-button']"
    ),
    "file_input": "input[type='file']",
    "image_component": ".se-component.se-image, .se-module-image",
    # 카테고리: [발행] 레이어 안의 카테고리 선택 상자 (발행 확인 버튼은 절대 누르지 않음)
    "publish_button": "button[class*='publish_btn'], button[data-click-area='tpb.publish']",
    "publish_layer": "[class*='layer_publish'], [class*='publish_layer']",
    "publish_layer_close": "[class*='layer_publish'] button[class*='close'], [class*='publish_layer'] button[class*='close']",
    "category_select": "[class*='option_category'] button, button[class*='selectbox_button']",
    # [발행] 레이어 안의 '진짜 발행' 버튼 — 발행 모드에서만 누른다
    "publish_confirm": [
        "[class*='layer_publish'] button[class*='confirm_btn']",
        "[class*='publish_layer'] button[class*='confirm_btn']",
        "button[class*='confirm_btn']",
        "button[data-testid='seOnePublishBtn']",
        "[class*='layer_publish'] button:text-is('발행')",
    ],
    # 임시저장 글 목록: 헤더의 [저장] 옆 숫자 버튼 → 목록에서 제목으로 찾아 불러오기
    "draft_list_button": [
        "button[class*='save_count_btn']",
        "button[data-click-area='tpb.savecount']",
        "button[class*='temp_count']",
    ],
    "draft_item": ["[class*='temp_post'] li", "[class*='save_list'] li", "[class*='draft'] li", "[class*='list_item']"],
    "popup_confirm": ".se-popup-button-confirm, button:has-text('확인')",
    "tag_input": "[class*='layer_publish'] input[placeholder*='태그'], [class*='tag_input'] input, input[placeholder*='태그']",
    # 위에서부터 차례로 시도 (label 을 눌러야 선택되는 경우가 많음)
    "category_option": ["[class*='option_list'] label", "[role='option']", "[class*='option_list'] li"],
    "save_button": (
        "button[class*='save_btn']:not([class*='count']), "
        "button[data-click-area='tpb.save'], "
        "button:has(span:text-is('저장'))"
    ),
}

class NaverError(RuntimeError):
    def __init__(self, message: str, screenshot: Path | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class NotLoggedIn(NaverError):
    pass


@dataclass
class DraftResult:
    title: str
    screenshot: Path | None
    editor_url: str
    images_inserted: int = 0
    category: str | None = None
    warnings: list[str] = field(default_factory=list)
    published: bool = False
    post_url: str = ""


@dataclass
class Category:
    name: str
    no: int | None = None
    parent: str | None = None

    @property
    def label(self) -> str:
        return f"{self.parent} > {self.name}" if self.parent else self.name


def parse_category_json(data) -> list[Category]:
    """블로그 카테고리 API 응답에서 (번호, 이름)을 모두 찾는다. 응답 모양이 바뀌어도 최대한 버티도록 재귀 탐색."""
    found: list[Category] = []

    def walk(node, parent: str | None = None) -> None:
        if isinstance(node, dict):
            name = node.get("categoryName")
            no = node.get("categoryNo")
            is_line = node.get("divisionLine") or node.get("isDivisionLine")
            here = parent
            if name and no is not None and not is_line and str(no) != "0":
                found.append(Category(name=str(name).strip(), no=int(no), parent=parent))
                here = str(name).strip()
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value, here)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent)

    walk(data)
    # parentCategoryNo 로 부모를 알려주는 형식 처리
    by_no = {c.no: c.name for c in found}

    def fix(node) -> None:
        if isinstance(node, dict):
            pno = node.get("parentCategoryNo")
            if pno and node.get("categoryNo") is not None:
                for c in found:
                    if c.no == int(node["categoryNo"]) and c.parent is None and int(pno) in by_no:
                        c.parent = by_no[int(pno)]
            for value in node.values():
                fix(value)
        elif isinstance(node, list):
            for item in node:
                fix(item)

    fix(data)
    unique: dict[int | None, Category] = {}
    for c in found:
        unique.setdefault(c.no, c)
    return list(unique.values())


def match_category(name: str | None, categories: list[Category]) -> Category | None:
    """AI 가 고른 이름을 실제 카테고리와 맞춘다 (공백·대소문자·'부모 > 자식' 표기 허용)."""
    if not name:
        return None

    def norm(x: str) -> str:
        return re.sub(r"\s+", "", x).lower()

    target = norm(name)
    for c in categories:
        if norm(c.name) == target or norm(c.label) == target:
            return c
    tail = norm(name.split(">")[-1])
    for c in categories:
        if norm(c.name) == tail:
            return c
    for c in categories:
        if tail and (tail in norm(c.name) or norm(c.name) in tail):
            return c
    return None


def parse_exported_cookies(raw: list[dict]) -> list[dict]:
    """'Cookie-Editor' / 'EditThisCookie' 확장 프로그램으로 내보낸 쿠키(JSON)를 Playwright 형식으로."""
    cookies = []
    for c in raw:
        domain = c.get("domain", "")
        if "naver.com" not in domain:
            continue
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": domain,
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        expires = c.get("expirationDate") or c.get("expires")
        if expires and not c.get("session", False):
            cookie["expires"] = float(expires)
        same_site = str(c.get("sameSite", "")).lower()
        cookie["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}.get(
            same_site, "Lax"
        )
        cookies.append(cookie)
    if not cookies:
        raise ValueError("naver.com 쿠키가 없습니다. 네이버에 로그인한 상태에서 내보냈는지 확인하세요.")
    return cookies


# ── 임시저장 ────────────────────────────────────────────────

class NaverBlog:
    def __init__(self, cfg: Config, session: BrowserSession | None = None):
        if not cfg.naver_blog_id:
            raise NaverError("블로그 아이디가 설정되지 않았습니다. 대시보드에서 네이버 로그인을 하면 자동으로 채워집니다.")
        self.cfg = cfg
        self.session = session or BrowserSession(cfg)

    async def ensure_login(self) -> None:
        """글을 쓰기 전에 로그인부터 확인 (로그인이 풀렸는데 AI 사용량만 쓰는 일을 막는다)."""
        async with self.session.lock():
            await self._logged_in_context()

    async def _logged_in_context(self) -> BrowserContext:
        ctx = await self.session.context()
        if not await self.session.has_login(ctx):
            self.session.write_session(loggedIn=False)
            raise NotLoggedIn(NOT_LOGGED_IN)
        return ctx

    # ── 카테고리 목록 ──
    @property
    def _category_cache(self) -> Path:
        return self.cfg.browser_profile_dir.parent / f"categories-{self.cfg.naver_blog_id}.json"

    async def categories(self, refresh: bool = False) -> list[Category]:
        """내 블로그 카테고리 목록 (하루 동안 캐시). NAVER_CATEGORIES 가 있으면 그것을 사용."""
        if self.cfg.naver_categories:
            return [Category(name=n) for n in self.cfg.naver_categories]
        cache = self._category_cache
        if not refresh and cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched", 0) < 86400 and data.get("items"):
                return [Category(**c) for c in data["items"]]
        async with self.session.lock():
            items = await self._fetch_categories(await self._logged_in_context())
        if items:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps({"fetched": time.time(), "items": [c.__dict__ for c in items]}, ensure_ascii=False),
                encoding="utf-8",
            )
        return items

    async def _fetch_categories(self, ctx: BrowserContext) -> list[Category]:
        blog_id = self.cfg.naver_blog_id
        for url in (
            f"https://m.blog.naver.com/api/blogs/{blog_id}/category-list",
            f"https://blog.naver.com/WidgetListAsync.naver?blogId={blog_id}&listNumVisitor=1&isCategoryOpen=true",
        ):
            try:
                resp = await ctx.request.get(url, headers={"Referer": f"https://m.blog.naver.com/{blog_id}"})
                if resp.ok:
                    text = await resp.text()
                    items = parse_category_json(json.loads(text[text.find("{"):]))
                    if items:
                        return items
            except Exception as exc:  # 형식이 다르면 다음 방법으로
                log.info("카테고리 API 실패 %s: %s", url, exc)
        # 마지막 수단: 에디터의 [발행] 레이어에서 카테고리 이름 읽기
        page = await ctx.new_page()
        try:
            await page.goto(f"https://blog.naver.com/{blog_id}?Redirect=Write&", wait_until="domcontentloaded")
            frame = await self._editor_frame(page)
            await self._dismiss_popups(frame)
            scope = await self._open_publish_layer(page, frame)
            if scope is None:
                return []
            await scope.locator(SELECTORS["category_select"]).first.click()
            await asyncio.sleep(0.8)
            names: list[str] = []
            for sel in SELECTORS["category_option"]:
                names = [n.strip() for n in await scope.locator(sel).all_inner_texts()]
                if names:
                    break
            await self._close_publish_layer(page, scope)
            return [Category(name=n) for n in dict.fromkeys(n for n in names if n)]
        finally:
            await page.close()

    # ── 임시저장 ──
    async def save_draft(
        self,
        title: str,
        body_html: str,
        images: list[Path] | None = None,
        category: Category | None = None,
        publish: bool = False,
        tags: list[str] | None = None,
    ) -> DraftResult:
        """publish=False: 임시저장, True: [발행] 레이어에서 실제로 발행."""
        async with self.session.lock():
            ctx = await self._logged_in_context()
            page = await ctx.new_page()  # 브라우저는 계속 열어 두고 탭만 열고 닫는다
            try:
                result = await self._save(page, title, body_html, images or [], category, publish, tags or [])
                await self.session.save_cookies()  # 네이버가 갱신한 쿠키를 다시 보관
                return result
            except NaverError as exc:
                if exc.screenshot is None:
                    exc.screenshot = await self._shot(page, "error")
                raise
            except Exception as exc:
                what = "발행" if publish else "임시저장"
                raise NaverError(f"{what} 중 오류: {exc}", await self._shot(page, "error")) from exc
            finally:
                await page.close()

    async def _save(
        self, page: Page, title: str, body_html: str, images: list[Path], category: Category | None,
        publish: bool = False, tags: list[str] | None = None,
    ) -> DraftResult:
        url = f"https://blog.naver.com/{self.cfg.naver_blog_id}?Redirect=Write&"
        if category and category.no:
            url += f"categoryNo={category.no}"  # 카테고리를 미리 선택한 채로 에디터 열기
        await page.goto(url, wait_until="domcontentloaded")
        if LOGIN_HOST in page.url:  # 세션이 만료된 경우: 자동으로 다시 로그인하지 않는다 (차단 방지)
            self.session.write_session(loggedIn=False)
            raise NotLoggedIn(NOT_LOGGED_IN)

        frame = await self._editor_frame(page)
        await self._dismiss_popups(frame)

        # 제목
        title_el = frame.locator(SELECTORS["title"]).first
        await title_el.click()
        await page.keyboard.insert_text(title)

        # 본문
        body_el = frame.locator(SELECTORS["body"]).first
        await body_el.click()
        before = await self._content_length(frame)
        plain = html_to_text(body_html)
        await self._paste_html(page, frame, body_html, plain)
        await asyncio.sleep(1.5)
        after = await self._content_length(frame)
        if after - before < min(50, len(plain) // 2):
            log.warning("HTML 붙여넣기 실패로 보임 → 일반 텍스트 입력으로 대체")
            await body_el.click()
            await page.keyboard.insert_text(plain)

        result = DraftResult(title=title, screenshot=None, editor_url=page.url)

        # 이미지: 본문의 [[IMAGEn]] 자리에 하나씩 업로드
        for i, path in enumerate(images, 1):
            try:
                if await self._insert_image(page, frame, i, path):
                    result.images_inserted += 1
                else:
                    result.warnings.append(f"이미지 {i} 자리 표시를 찾지 못함")
            except Exception as exc:
                log.warning("이미지 %d 업로드 실패: %s", i, exc)
                result.warnings.append(f"이미지 {i} 업로드 실패")
        await self._remove_leftover_markers(page, frame)

        if publish:  # 발행: 레이어를 열어 카테고리·태그를 정하고 [발행] 확정
            await self._publish(page, frame, category, tags or [], result)
            result.screenshot = await self._shot(page, "published")
            return result

        # 카테고리
        if category:
            try:
                if await self._select_category(page, frame, category):
                    result.category = category.label
                elif category.no:
                    result.category = category.label + " (주소로 지정)"
                else:
                    result.warnings.append(f"카테고리 '{category.name}' 선택 실패 → 기본 카테고리")
            except Exception as exc:
                log.warning("카테고리 선택 실패: %s", exc)
                result.warnings.append(f"카테고리 '{category.name}' 선택 실패")

        await self._click_save(page, frame)
        result.screenshot = await self._shot(page, "saved")
        return result

    async def _find_marker(self, frame: Frame | Page, i: int):
        loc = frame.locator(SELECTORS["paragraph"], has_text=marker(i))
        return loc.first if await loc.count() else None

    async def _clear_paragraph(self, page: Page, para) -> None:
        """표시 문구가 든 문단을 비우고 커서를 그 자리에 둔다."""
        await para.click()
        await page.keyboard.press("End")
        text = await para.inner_text()
        for _ in range(len(text.strip())):
            await page.keyboard.press("Backspace")

    async def _insert_image(self, page: Page, frame: Frame | Page, i: int, path: Path) -> bool:
        para = await self._find_marker(frame, i)
        if para is None:
            return False
        await self._clear_paragraph(page, para)
        images = frame.locator(SELECTORS["image_component"])
        before = await images.count()
        try:
            async with page.expect_file_chooser(timeout=8000) as chooser:
                await frame.locator(SELECTORS["image_button"]).first.click()
            await (await chooser.value).set_files(str(path))
        except PWTimeout:
            # 파일 선택창 대신 숨은 input 을 쓰는 경우
            await frame.locator(SELECTORS["file_input"]).last.set_input_files(str(path))
        for _ in range(60):  # 업로드 완료 대기 (최대 30초)
            if await images.count() > before:
                await asyncio.sleep(1)
                return True
            await asyncio.sleep(0.5)
        raise NaverError(f"이미지 {i} 업로드가 끝나지 않았습니다")

    async def _remove_leftover_markers(self, page: Page, frame: Frame | Page) -> None:
        for _ in range(20):
            loc = frame.locator(SELECTORS["paragraph"], has_text=re.compile(r"\[\[IMAGE\d+\]\]"))
            if not await loc.count():
                return
            await self._clear_paragraph(page, loc.first)

    async def _open_publish_layer(self, page: Page, frame: Frame | Page) -> Frame | Page | None:
        for scope in (frame, page):
            btn = scope.locator(SELECTORS["publish_button"])
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(1.5)
                return scope
        return None

    async def _close_publish_layer(self, page: Page, scope: Frame | Page) -> None:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        layer = scope.locator(SELECTORS["publish_layer"])
        if await layer.count() and await layer.first.is_visible():
            close = scope.locator(SELECTORS["publish_layer_close"])
            if await close.count():
                await close.first.click()
            else:
                await scope.locator(SELECTORS["publish_button"]).first.click()  # 토글로 닫기
            await asyncio.sleep(0.5)

    async def publish_saved(
        self,
        title: str,
        body_html: str,
        images: list[Path] | None = None,
        category: Category | None = None,
        tags: list[str] | None = None,
    ) -> DraftResult:
        """임시저장해 둔 글을 발행한다.

        ① 에디터의 임시저장 목록에서 같은 제목의 글을 불러와 그대로 발행 (발행되면 임시저장 목록에서도 빠짐)
        ② 목록에서 못 찾으면 저장해 둔 내용으로 다시 써서 발행 (이때는 임시저장 목록에 원래 글이 남을 수 있음)
        """
        async with self.session.lock():
            ctx = await self._logged_in_context()
            page = await ctx.new_page()
            try:
                url = f"https://blog.naver.com/{self.cfg.naver_blog_id}?Redirect=Write&"
                if category and category.no:
                    url += f"categoryNo={category.no}"
                await page.goto(url, wait_until="domcontentloaded")
                if LOGIN_HOST in page.url:
                    self.session.write_session(loggedIn=False)
                    raise NotLoggedIn(NOT_LOGGED_IN)
                frame = await self._editor_frame(page)
                await self._dismiss_popups(frame)
                result = DraftResult(title=title, screenshot=None, editor_url=page.url)
                if await self._load_draft(page, frame, title):
                    log.info("임시저장 글을 불러와 발행합니다: %s", title)
                    await self._publish(page, frame, category, tags or [], result)
                else:
                    log.info("임시저장 목록에서 글을 못 찾아, 저장해 둔 내용으로 다시 써서 발행합니다: %s", title)
                    await page.close()
                    page = await ctx.new_page()
                    result = await self._save(page, title, body_html, images or [], category, True, tags or [])
                    result.warnings.append("임시저장 목록에서 원래 글을 못 찾아 다시 써서 발행했습니다 (임시저장 목록에 원래 글이 남아 있을 수 있음)")
                result.screenshot = await self._shot(page, "published")
                await self.session.save_cookies()
                return result
            except NaverError as exc:
                if exc.screenshot is None:
                    exc.screenshot = await self._shot(page, "error")
                raise
            except Exception as exc:
                raise NaverError(f"발행 중 오류: {exc}", await self._shot(page, "error")) from exc
            finally:
                await page.close()

    async def _load_draft(self, page: Page, frame: Frame | Page, title: str) -> bool:
        """[저장] 옆 숫자 버튼 → 임시저장 목록에서 제목이 같은 글을 불러온다."""
        key = re.sub(r"\s+", " ", title).strip()[:25]
        for scope in (frame, page):
            for sel in SELECTORS["draft_list_button"]:
                btn = scope.locator(sel)
                try:
                    if not await btn.count():
                        continue
                    await btn.first.click()
                    await asyncio.sleep(1.2)
                    for item_sel in SELECTORS["draft_item"]:
                        item = scope.locator(item_sel).filter(has_text=key)
                        if await item.count():
                            await item.first.click()
                            await asyncio.sleep(1)
                            confirm = scope.locator(SELECTORS["popup_confirm"])
                            if await confirm.count() and await confirm.first.is_visible():
                                await confirm.first.click()  # "불러오시겠습니까?" → 확인
                            await asyncio.sleep(2)
                            loaded = await frame.locator(SELECTORS["title"]).first.inner_text()
                            return key[:10] in re.sub(r"\s+", " ", loaded)
                    return False
                except Exception as exc:
                    log.info("임시저장 목록 열기 실패: %s", exc)
                    return False
        return False

    async def _choose_category_in_layer(self, scope: Frame | Page, category: Category) -> bool:
        select = scope.locator(SELECTORS["category_select"])
        if not await select.count():
            return False
        await select.first.click()
        await asyncio.sleep(0.8)
        name_re = re.compile(rf"^\s*{re.escape(category.name)}\s*$")
        for sel in SELECTORS["category_option"]:
            option = scope.locator(sel).filter(has_text=name_re)
            if await option.count():
                await option.first.click()
                await asyncio.sleep(0.5)
                return True
        return False

    async def _publish(self, page: Page, frame: Frame | Page, category: Category | None, tags: list[str],
                       result: DraftResult) -> None:
        """[발행] 레이어: 카테고리 → 태그 → 레이어 안의 [발행] 버튼. 발행된 글 주소를 기록한다."""
        scope = await self._open_publish_layer(page, frame)
        if scope is None:
            raise NaverError("[발행] 버튼을 찾지 못했습니다")
        if category:
            try:
                if await self._choose_category_in_layer(scope, category):
                    result.category = category.label
                elif category.no:
                    result.category = category.label + " (주소로 지정)"
                else:
                    result.warnings.append(f"카테고리 '{category.name}' 선택 실패 → 기본 카테고리")
            except Exception as exc:
                result.warnings.append(f"카테고리 선택 실패: {exc}")
        if tags:
            try:
                tag_box = scope.locator(SELECTORS["tag_input"]).first
                if await tag_box.count():
                    for t in tags[:10]:  # 네이버 태그는 최대 10개
                        await tag_box.click()
                        await page.keyboard.insert_text(t.replace(" ", "").lstrip("#"))
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.2)
            except Exception as exc:
                log.info("태그 입력 실패(본문 끝 #태그는 그대로 있음): %s", exc)
        before = page.url
        for sel in SELECTORS["publish_confirm"]:
            btn = scope.locator(sel)
            try:
                if await btn.count() and await btn.last.is_visible():
                    await btn.last.click()
                    break
            except Exception:
                continue
        else:
            raise NaverError("[발행] 레이어의 발행 확인 버튼을 찾지 못했습니다")
        # 발행되면 글 보기 화면으로 넘어간다
        post_re = re.compile(rf"blog\.naver\.com/(?:{re.escape(self.cfg.naver_blog_id)}/\d+|PostView|.*logNo=\d+)")
        for _ in range(60):
            await asyncio.sleep(0.5)
            urls = [page.url] + [f.url for f in page.frames]
            hit = next((u for u in urls if post_re.search(u) and u != before), None)
            if hit:
                result.published, result.post_url = True, hit
                return
        # 주소가 안 바뀌어도 발행됐을 수 있다 (화면 구조에 따라) — 확인 필요로 남긴다
        result.published = True
        result.warnings.append("발행 버튼은 눌렀지만 발행된 글 주소를 확인하지 못했습니다. 블로그에서 확인해 주세요")

    async def _select_category(self, page: Page, frame: Frame | Page, category: Category) -> bool:
        """[발행] 레이어를 열어 카테고리만 고르고 닫는다. 발행 확인 버튼은 누르지 않는다."""
        scope = await self._open_publish_layer(page, frame)
        if scope is None:
            return False
        try:
            select = scope.locator(SELECTORS["category_select"])
            if not await select.count():
                return False
            await select.first.click()
            await asyncio.sleep(0.8)
            name_re = re.compile(rf"^\s*{re.escape(category.name)}\s*$")
            for sel in SELECTORS["category_option"]:
                option = scope.locator(sel).filter(has_text=name_re)
                if await option.count():
                    await option.first.click()
                    await asyncio.sleep(0.5)
                    return True
            return False
        finally:
            await self._close_publish_layer(page, scope)

    async def _editor_frame(self, page: Page) -> Frame | Page:
        """글쓰기 화면은 mainFrame iframe 안에 있거나(구형) 페이지에 바로 있다."""
        deadline = asyncio.get_running_loop().time() + 40
        while asyncio.get_running_loop().time() < deadline:
            if LOGIN_HOST in page.url:
                raise NotLoggedIn(NOT_LOGGED_IN)
            candidates: list[Frame | Page] = [page]
            frame = page.frame(name="mainFrame")
            if frame:
                candidates.insert(0, frame)
            for cand in candidates:
                try:
                    if await cand.locator(SELECTORS["editor_ready"]).count() > 0:
                        return cand
                except Exception:  # 프레임이 이동 중일 때
                    pass
            await asyncio.sleep(1)
        raise NaverError("글쓰기 에디터를 찾지 못했습니다 (블로그 ID 나 로그인 상태를 확인하세요)")

    async def _dismiss_popups(self, frame: Frame | Page) -> None:
        await asyncio.sleep(1.5)
        for key in ("popup_cancel", "help_close"):
            loc = frame.locator(SELECTORS[key])
            try:
                if await loc.count() and await loc.first.is_visible():
                    await loc.first.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

    async def _content_length(self, frame: Frame | Page) -> int:
        try:
            text = await frame.locator(SELECTORS["content"]).first.inner_text(timeout=3000)
        except PWTimeout:
            return 0
        return len(re.sub(r"\s+", "", text))

    async def _paste_html(self, page: Page, frame: Frame | Page, body_html: str, plain: str) -> None:
        """클립보드에 HTML 을 넣고 Ctrl+V → 에디터가 서식(소제목·굵게·링크)을 살려서 변환한다."""
        target = frame if isinstance(frame, Frame) else page.main_frame
        try:
            await target.evaluate(
                """async ([html, text]) => {
                    const item = new ClipboardItem({
                        'text/html': new Blob([html], {type: 'text/html'}),
                        'text/plain': new Blob([text], {type: 'text/plain'}),
                    });
                    await navigator.clipboard.write([item]);
                }""",
                [body_html, plain],
            )
            await page.keyboard.press("ControlOrMeta+V")
            return
        except Exception as exc:
            log.info("클립보드 붙여넣기 실패(%s) → paste 이벤트로 시도", exc)
        await target.evaluate(
            """([html, text]) => {
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', text);
                const el = document.activeElement || document.body;
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
            }""",
            [body_html, plain],
        )

    async def _click_save(self, page: Page, frame: Frame | Page) -> None:
        for scope in (frame, page):
            loc = scope.locator(SELECTORS["save_button"])
            try:
                if await loc.count():
                    await loc.first.click()
                    await asyncio.sleep(3)
                    return
            except Exception:
                continue
        # 버튼을 못 찾으면 단축키(Ctrl+S)로 시도
        await page.keyboard.press("ControlOrMeta+S")
        await asyncio.sleep(3)

    async def _shot(self, page: Page, label: str) -> Path | None:
        try:
            self.cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.cfg.screenshot_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.png"
            await page.screenshot(path=str(path))
            return path
        except Exception:
            return None
