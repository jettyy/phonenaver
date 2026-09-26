"""Playwright 로 네이버 블로그 스마트에디터 ONE 을 열어 글을 붙여넣고 임시저장한다.

네이버는 글쓰기(임시저장) 공개 API 가 없어서 실제 브라우저를 조작한다.
로그인은 캡차/보안 때문에 자동으로 하지 않고, 한 번 직접 로그인한 세션(쿠키)을
BROWSER_PROFILE_DIR 에 저장해 계속 재사용한다.

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

from playwright.async_api import BrowserContext, Frame, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

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
    # 위에서부터 차례로 시도 (label 을 눌러야 선택되는 경우가 많음)
    "category_option": ["[class*='option_list'] label", "[role='option']", "[class*='option_list'] li"],
    "save_button": (
        "button[class*='save_btn']:not([class*='count']), "
        "button[data-click-area='tpb.save'], "
        "button:has(span:text-is('저장'))"
    ),
}

LOGIN_HOST = "nid.naver.com"
LOGIN_URL = "https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fblog.naver.com%2F"
LOGIN_SELECTORS = {
    "id": "#id",
    "pw": "#pw",
    "keep": "#keep, #stay",
    "keep_label": "label[for='keep'], label[for='stay'], .keep_check",
    "submit": (
        "#log\\.login, button.btn_login, button[type='submit'], input[type='submit'], "
        "button:has-text('로그인'), a:has-text('로그인'):not([href*='http'])"
    ),
    # 로그인 후 '새로운 기기 등록' 화면
    "new_device_save": "#new\\.save, a:has-text('등록')",
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


def _launch_args(cfg: Config, headless: bool) -> dict:
    args: dict = {
        "user_data_dir": str(cfg.browser_profile_dir),
        "headless": headless,
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
        "viewport": {"width": 1400, "height": 1000},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if cfg.browser_executable:
        args["executable_path"] = cfg.browser_executable
    return args


async def _open(pw, cfg: Config, headless: bool) -> BrowserContext:
    cfg.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    ctx = await pw.chromium.launch_persistent_context(**_launch_args(cfg, headless))
    await ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    return ctx


# ── 로그인 관리 ───────────────────────────────────────────────

async def interactive_login(cfg: Config, timeout_s: int = 300) -> bool:
    """브라우저 창을 띄워 사용자가 직접 로그인하게 한다 (PC 에서 1회)."""
    async with async_playwright() as pw:
        ctx = await _open(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(LOGIN_URL)
        if cfg.naver_id and cfg.naver_pw:
            print(".env 의 아이디/비밀번호로 로그인합니다. 캡차나 2단계 인증이 나오면 창에서 직접 처리하세요.")
            try:
                await auto_login(page, cfg, wait_s=timeout_s)
            except Exception as exc:  # 자동 입력이 막혀도 창에서 직접 로그인할 수 있게 계속 대기
                print(f"자동 로그인이 끝나지 않았습니다({type(exc).__name__}). 창에서 직접 로그인을 마무리하세요.")
        else:
            print(f"브라우저 창에서 네이버에 로그인하세요. ({timeout_s}초 대기, '로그인 상태 유지' 체크 추천)")
        for _ in range(timeout_s):
            if await _has_login_cookie(ctx):
                print("로그인 확인! 세션을 저장했습니다.")
                await asyncio.sleep(2)
                await ctx.close()
                return True
            await asyncio.sleep(1)
        await ctx.close()
        return False


async def import_cookies(cfg: Config, cookie_file: Path) -> int:
    """PC 브라우저에서 내보낸 쿠키(JSON)를 서버 브라우저 프로필에 넣는다.

    'Cookie-Editor' / 'EditThisCookie' 확장 프로그램의 JSON 내보내기 형식을 지원한다.
    """
    raw = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
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
    async with async_playwright() as pw:
        ctx = await _open(pw, cfg, headless=True)
        await ctx.add_cookies(cookies)
        await ctx.close()
    return len(cookies)


async def _has_login_cookie(ctx: BrowserContext) -> bool:
    cookies = await ctx.cookies("https://naver.com")
    return any(c["name"] == "NID_AUT" for c in cookies)


async def _paste_into(page: Page, selector: str, value: str) -> None:
    """아이디/비밀번호를 한 글자씩 치지 않고 붙여넣는다 (자동입력 방지 캡차를 덜 부름)."""
    field_el = page.locator(selector).first
    await field_el.click()
    try:
        await page.evaluate("v => navigator.clipboard.writeText(v)", value)
        await page.keyboard.press("ControlOrMeta+V")
        await asyncio.sleep(0.3)
    except Exception:
        pass
    if await field_el.input_value() != value:
        await field_el.fill(value)
    await page.evaluate("() => navigator.clipboard.writeText('')")  # 클립보드에 비밀번호 남기지 않기


async def auto_login(page: Page, cfg: Config, wait_s: int = 90) -> None:
    """NAVER_ID / NAVER_PW 로 로그인. 캡차·2단계 인증이 뜨면 wait_s 동안 기다린다(네이버 앱 승인 등)."""
    if not (cfg.naver_id and cfg.naver_pw):
        raise NotLoggedIn(
            "네이버 로그인이 필요합니다. .env 에 NAVER_ID / NAVER_PW 를 넣거나 "
            "`python -m phonenaver login` 으로 한 번 로그인하세요."
        )
    ctx = page.context
    log.info("네이버 자동 로그인 시도")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await _paste_into(page, LOGIN_SELECTORS["id"], cfg.naver_id)
    await _paste_into(page, LOGIN_SELECTORS["pw"], cfg.naver_pw)
    try:  # 로그인 상태 유지
        keep = page.locator(LOGIN_SELECTORS["keep"]).first
        if await keep.count() and not await keep.is_checked():
            await page.locator(LOGIN_SELECTORS["keep_label"]).first.click()
    except Exception:
        pass
    # 로그인 버튼 (화면이 바뀌어 버튼을 못 찾으면 비밀번호 칸에서 Enter)
    submit = page.locator(LOGIN_SELECTORS["submit"]).first
    try:
        await submit.click(timeout=5000)
    except PWTimeout:
        log.info("로그인 버튼을 찾지 못해 Enter 로 제출")
        await page.locator(LOGIN_SELECTORS["pw"]).first.press("Enter")

    for _ in range(wait_s):
        await asyncio.sleep(1)
        if await _has_login_cookie(ctx) and LOGIN_HOST not in page.url:
            log.info("네이버 로그인 성공")
            return
        try:
            new_device = page.locator(LOGIN_SELECTORS["new_device_save"])
            if LOGIN_HOST in page.url and await new_device.count() and await new_device.first.is_visible():
                await new_device.first.click()
        except Exception:
            pass
        if await _has_login_cookie(ctx):
            return
    raise NotLoggedIn(
        "네이버 자동 로그인에 실패했습니다. 아이디/비밀번호를 확인하거나, 캡차·2단계 인증이 뜬 경우 "
        "PC에서 `python -m phonenaver login` 으로 한 번 직접 로그인하세요. (네이버 앱 로그인 승인 요청이 오면 승인)"
    )


async def check_login(cfg: Config) -> bool:
    async with async_playwright() as pw:
        ctx = await _open(pw, cfg, headless=True)
        try:
            return await _has_login_cookie(ctx)
        finally:
            await ctx.close()


# ── 임시저장 ────────────────────────────────────────────────

class NaverBlog:
    def __init__(self, cfg: Config):
        if not cfg.naver_blog_id:
            raise NaverError("NAVER_BLOG_ID 가 설정되지 않았습니다 (.env 확인)")
        self.cfg = cfg
        self._lock = asyncio.Lock()  # 브라우저 프로필은 동시에 하나만 열 수 있음

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
        async with self._lock:
            async with async_playwright() as pw:
                ctx = await _open(pw, self.cfg, headless=True)
                try:
                    items = await self._fetch_categories(ctx)
                finally:
                    await ctx.close()
        if items:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps({"fetched": time.time(), "items": [c.__dict__ for c in items]}, ensure_ascii=False),
                encoding="utf-8",
            )
        return items

    async def _fetch_categories(self, ctx: BrowserContext) -> list[Category]:
        blog_id = self.cfg.naver_blog_id
        if not await _has_login_cookie(ctx):
            await auto_login(ctx.pages[0] if ctx.pages else await ctx.new_page(), self.cfg)
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
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
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
        return [Category(name=n) for n in dict.fromkeys(n for n in names if n)]

    # ── 임시저장 ──
    async def save_draft(
        self,
        title: str,
        body_html: str,
        images: list[Path] | None = None,
        category: Category | None = None,
    ) -> DraftResult:
        async with self._lock:
            async with async_playwright() as pw:
                ctx = await _open(pw, self.cfg, headless=self.cfg.headless)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    return await self._save(page, title, body_html, images or [], category)
                except NaverError as exc:
                    if exc.screenshot is None:
                        exc.screenshot = await self._shot(page, "error")
                    raise
                except Exception as exc:
                    raise NaverError(f"임시저장 중 오류: {exc}", await self._shot(page, "error")) from exc
                finally:
                    await ctx.close()

    async def _save(
        self, page: Page, title: str, body_html: str, images: list[Path], category: Category | None
    ) -> DraftResult:
        url = f"https://blog.naver.com/{self.cfg.naver_blog_id}?Redirect=Write&"
        if category and category.no:
            url += f"categoryNo={category.no}"  # 카테고리를 미리 선택한 채로 에디터 열기
        if not await _has_login_cookie(page.context):
            await auto_login(page, self.cfg)
        await page.goto(url, wait_until="domcontentloaded")
        if LOGIN_HOST in page.url:  # 쿠키가 만료된 경우
            await auto_login(page, self.cfg)
            await page.goto(url, wait_until="domcontentloaded")

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
                raise NotLoggedIn("네이버 로그인이 필요합니다. PC에서 `python -m phonenaver login` 을 실행하세요.")
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
