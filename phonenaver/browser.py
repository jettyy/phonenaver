"""네이버 로그인 세션을 유지하는 브라우저 (인포러시와 같은 방식).

로그인이 자꾸 풀려 매번 다시 로그인하다 차단되는 것을 막기 위해:

1. **로그인은 사람이 창에서 직접 한 번만** 한다. 프로그램이 아이디/비밀번호를 치지 않는다.
2. **브라우저는 프로그램이 켜져 있는 동안 계속 열어 둔다.** 글마다 새로 띄우지 않는다.
3. **로그인 쿠키를 파일로 따로 보관**했다가, 프로필에 없으면 되살린다.
   (크로미움은 '세션 쿠키'를 브라우저를 닫으면 버리고, 비정상 종료 시 프로필에 쿠키를
   기록하지 못하는 일도 있다. 그래서 창을 껐다 켜면 로그인이 풀린 것처럼 보였다.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import Config

log = logging.getLogger(__name__)

LOGIN_HOST = "nid.naver.com"
LOGIN_URL = "https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fwww.naver.com"
NOT_LOGGED_IN = (
    "네이버 로그인이 필요합니다. 대시보드의 [네이버 로그인 창 열기] 를 눌러 한 번만 직접 로그인해 주세요."
)
USER_AGENT_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
USER_AGENT_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _user_agent() -> str:
    import platform

    return USER_AGENT_MAC if platform.system() == "Darwin" else USER_AGENT_WIN


class BrowserSession:
    """프로그램 전체에서 하나만 쓰는 네이버용 브라우저."""

    def __init__(self, cfg: Config | Callable[[], Config]):
        self._cfg = cfg
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._headless: bool | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop = None

    # ── 기본 ──
    @property
    def cfg(self) -> Config:
        return self._cfg() if callable(self._cfg) else self._cfg

    @property
    def data_dir(self) -> Path:
        return self.cfg.browser_profile_dir.parent

    @property
    def cookie_file(self) -> Path:
        return self.data_dir / "naver-cookies.json"

    @property
    def session_file(self) -> Path:
        return self.data_dir / "naver-session.json"

    def lock(self) -> asyncio.Lock:
        """브라우저는 한 번에 한 작업만 (파이썬 3.9 호환: 실행 중인 루프에서 처음 쓸 때 생성)."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def context(self, headless: bool | None = None) -> BrowserContext:
        want = self.cfg.headless if headless is None else headless
        if self._ctx is not None and self._headless == want:
            return self._ctx
        if self._ctx is not None:
            log.info("브라우저를 %s 모드로 다시 엽니다.", "숨김" if want else "창 보임")
            await self._close_context()
        if self._pw is None:
            self._pw = await async_playwright().start()

        cfg = self.cfg
        cfg.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        args = {
            "user_data_dir": str(cfg.browser_profile_dir),
            "headless": want,
            "locale": "ko-KR",
            "timezone_id": "Asia/Seoul",
            "viewport": {"width": 1400, "height": 1000},
            "user_agent": _user_agent(),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=ko-KR",
            ],
        }
        if cfg.browser_executable:
            args["executable_path"] = cfg.browser_executable
        ctx = await self._pw.chromium.launch_persistent_context(**args)
        try:
            await ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        ctx.on("close", lambda _: self._forget(ctx))
        self._ctx, self._headless = ctx, want

        if not await self.has_login(ctx) and await self._restore_cookies(ctx):
            log.info("보관해 둔 네이버 로그인 쿠키를 되살렸습니다.")
        return ctx

    def _forget(self, ctx) -> None:
        if self._ctx is ctx:
            self._ctx, self._headless = None, None

    async def _close_context(self) -> None:
        if self._ctx is not None:
            await self.save_cookies()
            try:
                await self._ctx.close()  # 정상 종료해야 프로필 폴더에도 쿠키가 기록된다
            except Exception:
                pass
        self._ctx, self._headless = None, None

    async def close(self) -> None:
        await self._close_context()
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # ── 쿠키 ──
    @staticmethod
    async def has_login(ctx: BrowserContext) -> bool:
        names = {c["name"] for c in await ctx.cookies("https://www.naver.com")}
        return "NID_AUT" in names and "NID_SES" in names

    async def save_cookies(self) -> bool:
        if self._ctx is None:
            return False
        try:
            cookies = [c for c in await self._ctx.cookies() if "naver.com" in c.get("domain", "")]
            if not any(c["name"] == "NID_AUT" for c in cookies):
                return False  # 로그아웃 상태로 좋은 백업을 덮어쓰지 않는다
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_file.write_text(json.dumps({"cookies": cookies}, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception as exc:
            log.warning("쿠키를 보관하지 못했습니다: %s", exc)
            return False

    async def _restore_cookies(self, ctx: BrowserContext) -> bool:
        if not self.cookie_file.exists():
            return False
        try:
            cookies = json.loads(self.cookie_file.read_text(encoding="utf-8")).get("cookies", [])
            now = time.time()
            alive = [c for c in cookies if not c.get("expires") or c["expires"] < 0 or c["expires"] > now]
            if not alive:
                return False
            await ctx.add_cookies(alive)
            return await self.has_login(ctx)
        except Exception as exc:
            log.warning("보관한 쿠키를 되살리지 못했습니다: %s", exc)
            return False

    async def import_cookie_list(self, cookies: list[dict]) -> None:
        async with self.lock():
            ctx = await self.context()
            await ctx.add_cookies(cookies)
            await self.save_cookies()

    # ── 세션 상태 ──
    def read_session(self) -> dict:
        try:
            return json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return {"loggedIn": False, "blogId": "", "checkedAt": None}

    def write_session(self, **info) -> dict:
        data = {**self.read_session(), **info, "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    async def detect_blog_id(self, page: Page) -> str:
        """로그인한 계정의 블로그 아이디를 알아낸다."""
        for url in ("https://blog.naver.com/MyBlog.naver", "https://section.blog.naver.com/BlogHome.naver"):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(1.2)
                m = re.search(r"blog\.naver\.com/([A-Za-z0-9_-]{3,})", page.url)
                if m and not re.match(r"^(MyBlog|PostList|section|BlogHome)", m.group(1), re.I):
                    return m.group(1)
                href = await page.evaluate(
                    """() => (document.querySelector('a[href*="blog.naver.com/"][class*="my"], .item_my_blog a, a.link_my')
                            || {}).href || ''"""
                )
                m = re.search(r"blog\.naver\.com/([A-Za-z0-9_-]{3,})", href or "")
                if m:
                    return m.group(1)
            except Exception:
                continue
        return ""

    async def verify(self) -> dict:
        """저장된 세션이 살아 있는지 확인 (확인이 실패해도 멀쩡한 세션을 로그아웃으로 바꾸지 않는다)."""
        async with self.lock():
            try:
                ctx = await self.context()
                page = await ctx.new_page()
                try:
                    await page.goto("https://www.naver.com", wait_until="domcontentloaded", timeout=20000)
                    logged = False
                    for _ in range(3):  # 화면이 뜬 직후엔 쿠키가 잠깐 비어 보일 수 있다
                        logged = await self.has_login(ctx)
                        if logged:
                            break
                        await asyncio.sleep(1.2)
                    if not logged:
                        return self.write_session(loggedIn=False)
                    blog_id = await self.detect_blog_id(page) or self.read_session().get("blogId", "")
                    await self.save_cookies()
                    return self.write_session(loggedIn=True, blogId=blog_id)
                finally:
                    await page.close()
            except Exception as exc:
                log.warning("세션 확인을 건너뜁니다 (%s). 저장된 상태를 그대로 씁니다.", exc)
                return self.read_session()

    async def open_login_window(self, timeout_s: int = 300) -> dict:
        """실제 브라우저 창을 띄워 사용자가 직접 로그인하게 한다 (2단계 인증도 그대로)."""
        async with self.lock():
            await self._close_context()
            ctx = await self.context(headless=False)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if await self.has_login(ctx):
                log.info("이미 로그인되어 있습니다.")
            else:
                log.info("네이버 로그인 창을 띄웠습니다. 창에서 직접 로그인해 주세요. ('로그인 상태 유지' 체크 추천)")
                try:
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                except Exception as exc:  # 인터넷이 잠깐 끊겨도 창은 열어 두고 계속 기다린다
                    log.warning("로그인 화면을 불러오지 못했습니다 (%s). 창에서 새로고침해 주세요.", str(exc).splitlines()[0])
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if page.is_closed():
                    break
                if await self.has_login(ctx):
                    blog_id = await self.detect_blog_id(page) or self.read_session().get("blogId", "")
                    await self.save_cookies()
                    info = self.write_session(loggedIn=True, blogId=blog_id)
                    log.info("로그인 성공. 세션을 저장했습니다.%s", f" (블로그 아이디: {blog_id})" if blog_id else "")
                    # 정상 종료로 프로필에 쿠키를 기록하고, 이후엔 설정한 모드로 다시 연다
                    await self._close_context()
                    return info
                await asyncio.sleep(2)
            log.warning("로그인이 완료되지 않았습니다 (시간 초과 또는 창 닫힘).")
            await self._close_context()
            return self.write_session(loggedIn=False)

    async def logout(self) -> dict:
        import shutil

        async with self.lock():
            await self._close_context()
            shutil.rmtree(self.cfg.browser_profile_dir, ignore_errors=True)
            self.cookie_file.unlink(missing_ok=True)
            log.info("저장된 네이버 세션을 삭제했습니다.")
            return self.write_session(loggedIn=False, blogId="")
