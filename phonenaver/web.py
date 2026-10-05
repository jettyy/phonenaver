"""로컬 대시보드 (http://localhost:3100) — `npm start` 로 켠다.

한 프로그램 안에서 대시보드 · 텔레그램 봇 · 작업 대기열 · 네이버 브라우저가 같이 돈다.
브라우저를 하나만 계속 열어 두기 때문에 네이버 로그인이 유지된다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
import webbrowser
from pathlib import Path

from aiohttp import web

from . import telegram_setup
from .ai import find_claude
from .bot import TelegramBot
from .browser import BrowserSession
from .config import DATA, ROOT, Config, ensure_env_file, set_env
from .jobs import EventLogHandler, Events, JobRunner

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "web"
PRESETS_FILE = DATA / "presets.json"
UPLOADS = DATA / "images" / "uploads"

# 대시보드 [설정] 칸에서 바꿀 수 있는 값 (.env 키, 종류)
SETTINGS = {
    "IMAGE_COUNT": "int",
    "IMAGE_PER_SECTION": "bool",
    "MAX_IMAGES": "int",
    "PARAGRAPH_GAP": "int",
    "POST_DELAY_MIN": "int",
    "PUBLISH_MODE": "str",
    "PUBLISH_AT": "str",
    "PUBLISH_INTERVAL": "int",
    "PUBLISH_RANDOM": "int",
    "MIN_CHARS": "int",
    "ALWAYS_RANKING": "bool",
    "RANKING_MIN": "int",
    "ALWAYS_RESEARCH": "bool",
    "POST_DELAY_MAX": "int",
    "THUMBNAIL_CARD": "bool",
    "PEXELS_API_KEY": "secret",
    "APPEND_HASHTAGS": "bool",
    "INCLUDE_SOURCES": "bool",
    "AUTO_CATEGORY": "bool",
    "HEADLESS": "bool",
    "MAX_SEARCHES": "int",
    "NAVER_BLOG_ID": "str",
}


class State:
    def __init__(self) -> None:
        ensure_env_file()
        self.cfg = Config.load()
        self.events = Events()
        self.session = BrowserSession(lambda: self.cfg)
        self.runner = JobRunner(lambda: self.cfg, self.session, self.events)
        self.bot = TelegramBot(lambda: self.cfg, self.runner)
        self.claude = {"installed": False, "loggedIn": False, "checkedAt": 0}
        self.busy: dict[str, bool] = {}
        self.connect_status = ""

    def reload(self) -> None:
        self.cfg = Config.load()

    # ── 설정 보여주기 (비밀값은 가림) ──
    def settings(self) -> dict:
        env = {}
        for key, kind in SETTINGS.items():
            value = os.getenv(key, "")
            env[key] = {"set": bool(value)} if kind == "secret" else value
        cfg = self.cfg
        env.update({
            "IMAGE_COUNT": cfg.image_count, "IMAGE_PER_SECTION": cfg.image_per_section, "MAX_IMAGES": cfg.max_images,
            "PARAGRAPH_GAP": cfg.paragraph_gap, "POST_DELAY_MIN": cfg.post_delay_min,
            "PUBLISH_MODE": cfg.publish_mode, "PUBLISH_AT": cfg.publish_at,
            "PUBLISH_INTERVAL": cfg.publish_interval, "PUBLISH_RANDOM": cfg.publish_random,
            "MIN_CHARS": cfg.min_chars, "ALWAYS_RANKING": cfg.always_ranking, "RANKING_MIN": cfg.ranking_min,
            "ALWAYS_RESEARCH": cfg.always_research,
            "POST_DELAY_MAX": cfg.post_delay_max, "THUMBNAIL_CARD": cfg.thumbnail_card, "APPEND_HASHTAGS": cfg.append_hashtags,
            "INCLUDE_SOURCES": cfg.include_sources, "AUTO_CATEGORY": cfg.auto_category, "HEADLESS": cfg.headless,
            "MAX_SEARCHES": cfg.max_searches, "NAVER_BLOG_ID": cfg.naver_blog_id,
        })
        return env


def load_presets() -> list[dict]:
    try:
        return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_presets(items: list[dict]) -> None:
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def check_claude(state: State) -> dict:
    claude = find_claude(state.cfg.claude_bin)
    installed = Path(claude).exists() or bool(__import__("shutil").which(claude))
    logged = False
    if state.cfg.claude_oauth_token:
        logged = True
    elif installed:
        try:
            out = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30).stdout
            logged = bool(json.loads(out).get("loggedIn"))
        except Exception:
            logged = False
    state.claude = {"installed": installed, "loggedIn": logged, "checkedAt": time.time()}
    return state.claude


def json_ok(data=None, **extra) -> web.Response:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return web.json_response(payload, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def json_err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status,
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


def build(state: State, open_url: str | None = None) -> web.Application:
    routes = web.RouteTableDef()

    def background(name: str, coro) -> bool:
        """오래 걸리는 버튼 동작은 뒤에서 돌리고 바로 응답한다 (같은 버튼 중복 실행 방지)."""
        if state.busy.get(name):
            coro.close()
            return False
        state.busy[name] = True
        state.events.publish("busy", state.busy)

        async def runner():
            try:
                await coro
            except Exception as exc:
                log.error("%s", exc)
            finally:
                state.busy[name] = False
                state.events.publish("busy", state.busy)
                state.events.publish("state", snapshot())

        asyncio.get_running_loop().create_task(runner())
        return True

    def snapshot() -> dict:
        return {
            "session": state.session.read_session(),
            "blogId": state.cfg.naver_blog_id,
            "bot": state.bot.status(),
            "claude": state.claude,
            "settings": state.settings(),
            "jobs": state.runner.list(),
            "presets": load_presets(),
            "busy": state.busy,
            "connect": state.connect_status,
            "log": [e["data"] for e in state.events.recent[-150:]],
        }

    # ── 화면 ──
    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    @routes.get("/api/state")
    async def get_state(_):
        return json_ok(snapshot())

    @routes.get("/api/stream")
    async def stream(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        q = state.events.subscribe()
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    await resp.write(f"data: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            state.events.unsubscribe(q)
        return resp

    @routes.get("/api/file")
    async def get_file(request):
        """작업 이미지·화면 캡처 보기 (data 폴더 안의 파일만)."""
        path = Path(request.query.get("path", "")).resolve()
        if DATA.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    # ── 1. 네이버 로그인 ──
    async def after_login(info: dict) -> None:
        blog = info.get("blogId")
        if blog and blog != state.cfg.naver_blog_id:
            set_env("NAVER_BLOG_ID", blog)
            state.reload()
            log.info("블로그 아이디를 저장했습니다: %s", blog)
        state.events.publish("session", info)

    @routes.post("/api/login")
    async def login(_):
        async def go():
            await after_login(await state.session.open_login_window())
        if not background("login", go()):
            return json_err("로그인 창이 이미 열려 있습니다.")
        return json_ok()

    @routes.post("/api/login/verify")
    async def verify(_):
        async def go():
            await after_login(await state.session.verify())
        background("verify", go())
        return json_ok()

    @routes.post("/api/logout")
    async def logout(_):
        info = await state.session.logout()
        state.events.publish("session", info)
        return json_ok(info)

    @routes.post("/api/blog-id")
    async def blog_id(request):
        body = await request.json()
        value = str(body.get("blogId", "")).strip().rstrip("/").split("/")[-1]
        set_env("NAVER_BLOG_ID", value)
        state.reload()
        return json_ok({"blogId": value})

    # ── 2. 글쓰기 요청 ──
    @routes.post("/api/jobs")
    async def add_job(request):
        form = await request.post()
        text = str(form.get("text", "")).strip()
        mode = str(form.get("photoMode", "analyze"))
        save_mode = str(form.get("saveMode", "")) or None  # "" 설정대로 / draft / schedule
        publish_time = str(form.get("publishTime", "")).strip() or None
        dry = str(form.get("dry", "")) in ("1", "true", "on")
        photos: list[Path] = []
        UPLOADS.mkdir(parents=True, exist_ok=True)
        for field in form.getall("photos", []):
            if not getattr(field, "filename", None):
                continue
            suffix = Path(field.filename).suffix.lower() or ".jpg"
            path = UPLOADS / f"pc-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}{suffix}"
            path.write_bytes(field.file.read())
            photos.append(path)
        if not text and not photos:
            return json_err("글 주제나 지시를 입력해 주세요.")
        if not text:
            text = "보낸 사진 내용을 분석해서 그 내용으로 블로그 글을 써줘"
        if dry and not text.lower().startswith(("/test", "/dry")):
            text = "/test " + text
        job = state.runner.submit(text, photos, source="pc", photo_mode=mode if photos else None,
                                  publish_mode=save_mode if save_mode in ("draft", "schedule") else None,
                                  publish_time=publish_time if save_mode == "schedule" else None)
        return json_ok(job.public())

    @routes.post("/api/jobs/{id}/retry")
    async def retry(request):
        try:
            return json_ok(state.runner.retry(request.match_info["id"]).public())
        except KeyError:
            return json_err("작업을 찾지 못했습니다.", 404)

    @routes.delete("/api/jobs/{id}")
    async def delete_job(request):
        ok = state.runner.cancel(request.match_info["id"])
        return json_ok() if ok else json_err("진행 중인 작업은 끝날 때까지 기다려 주세요.")

    @routes.post("/api/jobs/{id}/publish")
    async def publish_now(request):
        job = state.runner.jobs.get(request.match_info["id"])
        if job is None or job.status != "done" or job.dry_run:
            return json_err("임시저장된 글만 발행할 수 있습니다.")
        if job.publish_state in ("publishing", "published"):
            return json_err("이미 발행 중이거나 발행된 글입니다.")
        if not job.body_html and not job.title:
            return json_err("발행할 내용을 찾지 못했습니다.")
        background(f"publish-{job.id}", state.runner.publish_now(job.id))
        return json_ok()

    @routes.post("/api/jobs/{id}/publish/cancel")
    async def publish_cancel(request):
        ok = state.runner.cancel_publish(request.match_info["id"])
        return json_ok() if ok else json_err("취소할 발행 예약이 없습니다.")

    @routes.post("/api/jobs/clear")
    async def clear_jobs(_):
        state.runner.clear_finished()
        return json_ok()

    # ── 자주 쓰는 요청 버튼 ──
    @routes.post("/api/presets")
    async def add_preset(request):
        body = await request.json()
        name, text = str(body.get("name", "")).strip(), str(body.get("text", "")).strip()
        if not text:
            return json_err("저장할 내용이 없습니다.")
        items = load_presets()
        items.append({"id": uuid.uuid4().hex[:8], "name": name or text.splitlines()[0][:20], "text": text})
        save_presets(items)
        return json_ok(items)

    @routes.delete("/api/presets/{id}")
    async def delete_preset(request):
        items = [p for p in load_presets() if p["id"] != request.match_info["id"]]
        save_presets(items)
        return json_ok(items)

    # ── 3. 휴대폰(텔레그램) 연결 ──
    @routes.post("/api/telegram/token")
    async def telegram_token(request):
        token = str((await request.json()).get("token", "")).strip()
        try:
            username = await telegram_setup.check_token(token)
        except telegram_setup.TelegramSetupError as exc:
            return json_err(str(exc))
        await state.bot.stop()
        set_env("TELEGRAM_BOT_TOKEN", token)
        state.reload()
        log.info("텔레그램 봇 확인: @%s", username)
        if state.cfg.allowed_chat_ids:
            await state.bot.start()
        return json_ok({"username": username})

    @routes.post("/api/telegram/connect")
    async def telegram_connect(_):
        token = state.cfg.telegram_bot_token
        if not token:
            return json_err("먼저 봇 토큰을 저장해 주세요.")

        async def go():
            was_running = state.bot.running
            await state.bot.stop()  # 봇이 메시지를 가져가 버리지 않도록 잠시 끔
            username = await telegram_setup.check_token(token)
            state.connect_status = (f"휴대폰 텔레그램에서 @{username} 에게 (여러 컴퓨터용이면 봇들이 들어 있는 그룹에) "
                                    "아무 메시지나 보내세요 (3분 대기)")
            state.events.publish("state", snapshot())
            log.info(state.connect_status)
            try:
                chat_id = await telegram_setup.wait_for_chat(token)
                ids = sorted(state.cfg.allowed_chat_ids | {chat_id})
                set_env("ALLOWED_CHAT_IDS", ",".join(str(i) for i in ids))
                state.reload()
                log.info("✅ 휴대폰 채팅 등록 완료 (ID %s)", chat_id)
                was_running = True
            except telegram_setup.TelegramSetupError as exc:
                log.warning("%s", exc)
            finally:
                state.connect_status = ""
                if was_running and state.cfg.allowed_chat_ids:
                    await state.bot.start()

        if not background("connect", go()):
            return json_err("이미 연결을 기다리는 중입니다.")
        return json_ok()

    @routes.post("/api/telegram/forget")
    async def telegram_forget(_):
        set_env("ALLOWED_CHAT_IDS", "")
        state.reload()
        await state.bot.stop()
        return json_ok()

    @routes.post("/api/bot/start")
    async def bot_start(_):
        if not state.cfg.telegram_bot_token or not state.cfg.allowed_chat_ids:
            return json_err("먼저 봇 토큰을 저장하고 [휴대폰 연결] 을 해 주세요.")
        await state.bot.start()
        return json_ok(state.bot.status()) if state.bot.running else json_err(state.bot.error or "봇을 켜지 못했습니다.")

    @routes.post("/api/bot/stop")
    async def bot_stop(_):
        await state.bot.stop()
        return json_ok(state.bot.status())

    # ── 4. 설정 ──
    @routes.post("/api/settings")
    async def save_settings(request):
        body = await request.json()
        headless_before = state.cfg.headless
        for key, kind in SETTINGS.items():
            if key not in body:
                continue
            value = body[key]
            if kind == "secret" and not str(value).strip():
                continue  # 빈 칸이면 기존 값 유지
            if kind == "bool":
                value = "true" if value in (True, "true", "1", "on") else "false"
            elif kind == "int":
                value = str(int(value))
            set_env(key, str(value))
        state.reload()
        if state.cfg.headless != headless_before:
            log.info("다음 작업부터 브라우저 창을 %s", "숨깁니다" if state.cfg.headless else "보이게 엽니다")
        log.info("설정을 저장했습니다.")
        return json_ok(state.settings())

    @routes.post("/api/claude/check")
    async def claude_check(_):
        return json_ok(await asyncio.get_running_loop().run_in_executor(None, check_claude, state))

    @routes.post("/api/claude/login")
    async def claude_login(_):
        claude = find_claude(state.cfg.claude_bin)

        async def go():
            log.info("Claude 로그인 페이지를 엽니다. 브라우저에서 구독 계정으로 로그인하세요.")
            proc = await asyncio.create_subprocess_exec(claude, "auth", "login")
            await proc.wait()
            await asyncio.get_running_loop().run_in_executor(None, check_claude, state)
            log.info("Claude 로그인 %s", "완료" if state.claude["loggedIn"] else "확인 안 됨")

        background("claude", go())
        return json_ok()

    @routes.post("/api/categories")
    async def categories(_):
        from .naver import NaverBlog

        if not state.cfg.naver_blog_id:
            return json_err("블로그 아이디가 없습니다. 네이버 로그인을 먼저 해 주세요.")
        try:
            cats = await NaverBlog(state.cfg, state.session).categories(refresh=True)
        except Exception as exc:
            return json_err(str(exc))
        return json_ok([c.label for c in cats])

    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC)

    async def on_start(_app):
        loop = asyncio.get_running_loop()
        handler = EventLogHandler(state.events, loop)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        state.runner.start()
        loop.run_in_executor(None, check_claude, state)
        if state.cfg.telegram_bot_token and state.cfg.allowed_chat_ids:
            await state.bot.start()
        if open_url:
            loop.call_later(0.8, webbrowser.open, open_url)

    async def on_stop(_app):
        await state.bot.stop()
        await state.runner.stop()
        await state.session.close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def serve(port: int = 0, open_browser: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "telegram", "apscheduler", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)  # 파이썬 3.9 호환
    state = State()  # .env 를 먼저 읽는다
    port = port or int(os.getenv("PORT", "").strip() or 3100)

    import socket

    for candidate in range(port, port + 10):  # 포트가 쓰이고 있으면 다음 번호로
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", candidate)) != 0:
                port = candidate
                break
    url = f"http://localhost:{port}"
    print("=" * 56)
    print(f"  📝 네이버 블로그 자동 글쓰기 대시보드: {url}")
    print("  이 창을 닫으면 프로그램(봇 포함)이 꺼집니다.")
    print("=" * 56, flush=True)
    app = build(state, url if open_browser else None)
    web.run_app(app, host="127.0.0.1", port=port, print=None, handle_signals=True, loop=loop)
