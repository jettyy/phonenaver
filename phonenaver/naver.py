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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Frame, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from .config import Config
from .html_utils import html_to_text

log = logging.getLogger(__name__)

SELECTORS = {
    "editor_ready": ".se-content, .se-main-container, .se-documentTitle",
    "title": ".se-documentTitle .se-text-paragraph, .se-title-text .se-text-paragraph, .se-documentTitle",
    "body": ".se-component.se-text .se-text-paragraph, .se-section-text .se-text-paragraph",
    "content": ".se-content",
    # 팝업: '작성 중인 글이 있습니다' → 취소(새 글), 도움말 패널 닫기
    "popup_cancel": ".se-popup-button-cancel",
    "help_close": ".se-help-panel-close-button, button.se-help-close-button",
    "save_button": (
        "button[class*='save_btn']:not([class*='count']), "
        "button[data-click-area='tpb.save'], "
        "button:has(span:text-is('저장'))"
    ),
}

LOGIN_HOST = "nid.naver.com"


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
    await ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://blog.naver.com")
    return ctx


# ── 로그인 관리 ───────────────────────────────────────────────

async def interactive_login(cfg: Config, timeout_s: int = 300) -> bool:
    """브라우저 창을 띄워 사용자가 직접 로그인하게 한다 (PC 에서 1회)."""
    async with async_playwright() as pw:
        ctx = await _open(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://nid.naver.com/nidlogin.login?url=https://blog.naver.com")
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

    async def save_draft(self, title: str, body_html: str) -> DraftResult:
        async with self._lock:
            async with async_playwright() as pw:
                ctx = await _open(pw, self.cfg, headless=self.cfg.headless)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    return await self._save(page, title, body_html)
                except NaverError as exc:
                    if exc.screenshot is None:
                        exc.screenshot = await self._shot(page, "error")
                    raise
                except Exception as exc:
                    raise NaverError(f"임시저장 중 오류: {exc}", await self._shot(page, "error")) from exc
                finally:
                    await ctx.close()

    async def _save(self, page: Page, title: str, body_html: str) -> DraftResult:
        await page.goto(
            f"https://blog.naver.com/{self.cfg.naver_blog_id}?Redirect=Write&", wait_until="domcontentloaded"
        )
        if LOGIN_HOST in page.url:
            raise NotLoggedIn("네이버 로그인이 필요합니다. PC에서 `python -m phonenaver login` 을 실행하세요.")

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

        await self._click_save(page, frame)
        shot = await self._shot(page, "saved")
        return DraftResult(title=title, screenshot=shot, editor_url=page.url)

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
