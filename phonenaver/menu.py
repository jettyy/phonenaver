"""처음 설정 마법사 + 메뉴. (맥·윈도우 동일, 번호만 누르면 됨)"""
from __future__ import annotations

import asyncio
import getpass
import logging
import shutil
import subprocess
import time

import httpx

from .config import ROOT, Config

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


# ── .env 읽고 쓰기 ─────────────────────────────────────────

def ensure_env_file() -> None:
    if not ENV.exists():
        ENV.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


def set_env(key: str, value: str) -> None:
    """.env 의 해당 줄만 바꾼다 (주석·다른 값은 유지)."""
    ensure_env_file()
    lines = ENV.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask(question: str, current: str = "", secret: bool = False, required: bool = True) -> str:
    hint = " (엔터: 그대로 유지)" if current else (" (엔터: 건너뛰기)" if not required else "")
    while True:
        prompt = f"{question}{hint}\n> "
        value = (getpass.getpass(prompt + "(입력해도 화면에 안 보여요) ") if secret else input(prompt)).strip()
        if value:
            return value
        if current or not required:
            return current
        print("  값을 입력해 주세요.")


def yes(question: str, default: bool = True) -> bool:
    answer = input(f"{question} ({'Y/n' if default else 'y/N'})\n> ").strip().lower()
    return default if not answer else answer in ("y", "yes", "ㅛ", "네", "예", "응")


# ── 텔레그램 ────────────────────────────────────────────────

def telegram(token: str, method: str, **params):
    resp = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "텔레그램 오류"))
    return data["result"]


def setup_telegram(cfg: Config) -> None:
    print("\n── 1. 텔레그램 봇 ──────────────────────────────")
    print("휴대폰 텔레그램에서 @BotFather 검색 → /newbot → 봇 이름 정하기 → 받은 토큰을 붙여넣으세요.")
    while True:
        token = ask("텔레그램 봇 토큰", cfg.telegram_bot_token)
        try:
            me = telegram(token, "getMe")
            break
        except Exception as exc:
            print(f"  ❌ 토큰이 맞지 않습니다 ({exc}). 다시 확인해 주세요.")
            cfg.telegram_bot_token = ""
    set_env("TELEGRAM_BOT_TOKEN", token)
    cfg.telegram_bot_token = token
    username = me.get("username", "")
    print(f"  ✅ 봇 확인: @{username}")

    if cfg.allowed_chat_ids and not yes("등록된 휴대폰 채팅이 있습니다. 다시 등록할까요?", default=False):
        return
    print(f"\n📱 휴대폰 텔레그램에서 @{username} 을 열고 [시작] 을 누르거나 아무 메시지나 보내세요.")
    print("   (메시지가 오면 자동으로 내 채팅으로 등록됩니다. 최대 3분 대기)")
    offset = None
    try:  # 이전에 쌓인 메시지는 무시
        old = telegram(token, "getUpdates", timeout=0)
        if old:
            offset = old[-1]["update_id"] + 1
    except Exception:
        pass
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            updates = telegram(token, "getUpdates", timeout=25, offset=offset)
        except Exception:
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if msg and msg.get("chat", {}).get("id"):
                chat_id = msg["chat"]["id"]
                telegram(token, "getUpdates", timeout=0, offset=offset)  # 처리한 메시지 정리
                set_env("ALLOWED_CHAT_IDS", str(chat_id))
                cfg.allowed_chat_ids = {chat_id}
                telegram(token, "sendMessage", chat_id=chat_id, text="✅ 이 채팅이 등록되었습니다. PC에서 봇을 실행하면 글쓰기 지시를 받을 수 있어요.")
                print(f"  ✅ 휴대폰 채팅 등록 완료 (ID {chat_id})")
                return
    print("  ⚠️ 3분 안에 메시지가 오지 않았습니다. 메뉴의 '설정 바꾸기'에서 다시 할 수 있어요.")


def setup_naver(cfg: Config) -> None:
    print("\n── 2. 네이버 블로그 ────────────────────────────")
    blog = ask("블로그 주소의 아이디 (blog.naver.com/여기 부분)", cfg.naver_blog_id)
    blog = blog.rstrip("/").split("/")[-1]
    set_env("NAVER_BLOG_ID", blog)
    nid = ask("네이버 로그인 아이디", cfg.naver_id)
    set_env("NAVER_ID", nid)
    pw = ask("네이버 비밀번호", cfg.naver_pw, secret=True)
    set_env("NAVER_PW", pw)
    print("  ✅ 저장했습니다. (이 정보는 이 컴퓨터의 .env 파일에만 저장됩니다)")


def setup_optional(cfg: Config) -> None:
    print("\n── 3. 무료 사진 (선택) ─────────────────────────")
    print("pexels.com/api 에서 무료 키를 받으면 글에 실제 사진이 들어갑니다. 없으면 카드 이미지로 채워요.")
    key = ask("Pexels API 키", cfg.pexels_api_key, required=False)
    set_env("PEXELS_API_KEY", key)


def naver_login(cfg: Config) -> None:
    from . import naver

    print("\n브라우저 창이 열리고 네이버 로그인이 자동으로 입력됩니다.")
    print("캡차·새 기기 인증·2단계 인증이 나오면 창에서 직접 처리해 주세요.")
    ok = asyncio.run(naver.interactive_login(cfg))
    print("✅ 네이버 로그인 완료" if ok else "⚠️ 로그인이 확인되지 않았습니다. 다시 시도해 주세요.")


def wizard(cfg: Config) -> Config:
    print("\n처음 설정을 시작합니다. 질문에 답하고 엔터를 누르세요.")
    setup_telegram(cfg)
    setup_naver(Config.load())
    setup_optional(Config.load())
    cfg = Config.load()
    if yes("\n지금 네이버 로그인을 확인할까요? (처음 한 번 추천)"):
        naver_login(cfg)
    return Config.load()


def configured(cfg: Config) -> bool:
    return bool(cfg.telegram_bot_token and cfg.allowed_chat_ids and cfg.naver_blog_id)


# ── 메뉴 동작 ──────────────────────────────────────────────

def run_bot(cfg: Config) -> None:
    from .bot import run_bot as _run

    print("\n🤖 봇 실행 중 — 휴대폰 텔레그램으로 글쓰기 지시를 보내세요.")
    if cfg.slr_watch:
        print(f"   🔔 SLR클럽 장터 알림도 함께 동작합니다 ({cfg.slr_interval}초마다, 키워드는 휴대폰에서 /watch)")
    print("   이 창을 닫거나 Ctrl+C 를 누르면 멈춥니다. (컴퓨터가 잠자기에 들어가지 않게 해 주세요)\n")
    _run(cfg)


def test_write(cfg: Config) -> None:
    from .html_utils import html_to_text
    from .images import strip_markers
    from .pipeline import Pipeline

    topic = input("\n글 주제나 지시를 입력하세요 (예: 캠핑 초보 준비물 정리)\n> ").strip()
    if not topic:
        return
    save = yes("네이버에 임시저장까지 할까요? (n 이면 미리보기만)", default=False)

    async def progress(msg: str) -> None:
        print(msg)

    try:
        result = asyncio.run(Pipeline(cfg).run(topic if save else "/test " + topic, progress))
    except Exception as exc:
        print(f"❌ 실패: {exc}")
        return
    print("\n제목:", result.post.title)
    print("카테고리:", result.category.label if result.category else "기본")
    for img in result.images:
        print(f"이미지({img.source}):", img.path)
    for w in result.warnings:
        print("⚠️", w)
    print("-" * 50)
    print(strip_markers(html_to_text(result.body_html)))
    print("-" * 50)
    if result.draft:
        print("✅ 임시저장 완료 — 네이버 앱 > 글쓰기 > 임시저장 글에서 확인하세요.")


def show_categories(cfg: Config) -> None:
    from .naver import NaverBlog

    try:
        cats = asyncio.run(NaverBlog(cfg).categories(refresh=True))
    except Exception as exc:
        print(f"❌ {exc}")
        return
    print("\n".join(f"  · {c.label}" for c in cats) or "카테고리를 찾지 못했습니다.")


def slr_keywords(cfg: Config) -> None:
    from . import slrwatch

    store = slrwatch.WatchStore(cfg.slr_watch_file)
    while True:
        kws = store.keywords
        print("\n── SLR클럽 장터 알림 키워드 ──")
        print("\n".join(f"  {i}. {kw}" for i, kw in enumerate(kws, 1)) or "  (없음)")
        print("  봇이 켜져 있는 동안 제목에 키워드가 들어간 새 글이 올라오면 휴대폰으로 링크를 보내요.")
        print("  규칙: 띄어쓰기 = 모두 포함, a7m5|a7v = 둘 중 하나, -배터리 = 제외")
        print("  a. 추가   d. 삭제   t. 지금 목록에서 찾아보기   엔터. 돌아가기")
        choice = input("> ").strip().lower()
        if choice == "a":
            added = store.add(slrwatch.split_keywords(input("추가할 키워드 (쉼표로 여러 개)\n> ")))
            print("✅ 추가: " + ", ".join(added) if added else "추가된 키워드가 없어요.")
        elif choice == "d":
            removed = store.remove(input("지울 번호나 키워드\n> "))
            print(f"🗑 삭제: {removed}" if removed else "찾지 못했어요.")
        elif choice == "t":
            show_slr_matches(cfg, kws)
        else:
            return


def show_slr_matches(cfg: Config, keywords: list[str]) -> None:
    from . import slrwatch

    async def run() -> list:
        watcher = slrwatch.SlrWatcher(cfg)
        try:
            return await watcher.fetch_page(1)
        finally:
            await watcher.close()

    try:
        posts = asyncio.run(run())
    except Exception as exc:
        print(f"❌ 장터 목록을 읽지 못했습니다: {exc}")
        return
    print(f"첫 페이지 글 {len(posts)}개")
    for p in posts:
        kws = slrwatch.matched_keywords(p.title, keywords) if keywords else []
        if kws or not keywords:
            print(f"  {'🔔 ' if kws else ''}{p.title}  ({p.author} {p.date})\n     {p.url}")


def claude_login() -> None:
    from .ai import find_claude

    claude = find_claude()
    if not shutil.which(claude) and claude == "claude":
        print("Claude Code 가 설치되어 있지 않습니다. start 파일을 다시 실행하면 자동 설치됩니다.")
        return
    subprocess.call([claude, "auth", "login"])


def update() -> None:
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        print("git 으로 받은 폴더가 아니라서 자동 업데이트를 할 수 없습니다.")
        return
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    code = subprocess.call(["git", "pull", "origin", branch or "HEAD"], cwd=ROOT)
    if code == 0:
        print("✅ 업데이트 완료. 창을 닫고 start 파일을 다시 실행하세요. (필요한 설치는 자동으로 진행)")
        raise SystemExit(0)
    print("❌ 업데이트 실패")


MENU = """
──────────── 메뉴 ────────────
 1. 봇 실행 (휴대폰 지시 받기)   ← 엔터
 2. 테스트 글쓰기
 3. 네이버 로그인 다시 하기
 4. 설정 바꾸기 (텔레그램·네이버·사진)
 5. 내 블로그 카테고리 보기
 6. Claude 로그인 다시 하기
 7. 최신 버전으로 업데이트
 8. SLR클럽 장터 알림 키워드
 0. 종료
──────────────────────────────"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ensure_env_file()
    cfg = Config.load()
    if not configured(cfg):
        cfg = wizard(cfg)
        if configured(cfg) and yes("\n설정 끝! 바로 봇을 실행할까요?"):
            run_bot(cfg)

    actions = {
        "1": lambda: run_bot(cfg),
        "2": lambda: test_write(cfg),
        "3": lambda: naver_login(cfg),
        "4": lambda: wizard(cfg),
        "5": lambda: show_categories(cfg),
        "6": claude_login,
        "7": update,
        "8": lambda: slr_keywords(cfg),
    }
    while True:
        print(MENU)
        choice = input("번호 선택 > ").strip() or "1"
        if choice == "0":
            return 0
        action = actions.get(choice)
        if action is None:
            print("메뉴에 있는 번호를 입력해 주세요.")
            continue
        try:
            action()
        except KeyboardInterrupt:
            print("\n(중지됨)")
        except SystemExit:
            raise
        except Exception as exc:
            print(f"❌ 오류: {exc}")
        cfg = Config.load()
