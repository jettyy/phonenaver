"""텔레그램 봇: 휴대폰에서 메시지를 보내면 블로그 글을 써서 임시저장한다."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import html_utils, images
from .command import period_label
from .config import Config
from .jobs import ChannelResult, JobRunner
from .naver import NaverBlog, NaverError, NotLoggedIn

log = logging.getLogger(__name__)

# 설명 없이 사진만 보냈을 때의 기본 요청
PHOTO_ONLY_REQUEST = "보낸 사진 내용을 분석해서 그 내용으로 블로그 글을 써줘"
# 연달아 온 메시지(쪼개진 긴 글, 앨범 사진)를 하나로 합치기 위해 기다리는 시간(초)
MERGE_SECONDS = 4

HELP = """📝 네이버 블로그 자동 글쓰기 봇

그냥 메시지를 보내면 글을 써서 '임시저장'합니다.

1) 키워드/문장 → 최신 정보 검색 후 작성
   예) 2026 청년도약계좌 조건 정리해줘

2) 링크만 보내기 → 링크 내용 분석 후 작성
   예) https://... 이거 분석해서 후기 글로 써줘
   🎬 유튜브 링크 → 영상 대사(자막)를 끝까지 읽고 분석해서 작성
   예) https://youtu.be/... 이 영상 내용으로 블로그 글 써줘
   📺 유튜브 채널 링크 → 최근 6개월 영상마다 글 하나씩 (같은 채널을 다시 보내면 다시 씀)
   예) https://www.youtube.com/@채널이름   (최근 3개월 / 10개만 처럼 바꿀 수 있음)
   📗 네이버 블로그 주소 → 그 블로그 글마다 내용을 소재로 새 글 하나씩 (기본 최근 6개월)
   예) https://m.blog.naver.com/아이디 최근 3개월   (2주 / 10일 / 1년 / 전부 / 20개만)
   글 한 편 주소(…/아이디/글번호)를 보내면 그 글만 씁니다

3) 링크를 글에 넣기 → '넣어/삽입/걸어' 라고 말하기
   예) 제주 한달살기 준비물 글 써줘. 이 링크 넣어줘 https://...
   (여러 줄이면 '넣어'가 적힌 줄의 링크만 넣고, 나머지 링크는 분석용)

4) 내가 쓴 글 그대로 저장
   그대로: 첫 줄은 제목
   둘째 줄부터 본문

📷 사진 보내기 → 사진 내용을 분석해서 그 내용으로 글 작성
   · 사진 자체는 글에 첨부하지 않습니다
   · 사진만 보내도 바로 글을 씁니다 (앨범 가능)
   · 사진 설명(캡션)에 지시를 같이 쓰면 반영 (예: 카페 후기로 써줘, 1500자)
   · 꼭 사진을 넣고 싶으면 "사진도 첨부해줘" 라고 쓰기

📄 긴 글을 통째로 붙여넣으면 → 전체를 분석하고 최신 정보로 확인해서 새로 작성
   (텔레그램이 여러 메시지로 쪼개도 4초 안에 온 것은 하나로 합쳐서 처리)

🖼 문단 사이 빈 줄 2개 + 도입부 뒤·소제목마다 사진 자동 삽입
   · '사진 없이' 라고 쓰면 이미지 생략

📂 카테고리: 글 내용에 맞게 자동 선택
   · 직접 지정: 카테고리: 여행
   · /categories 내 블로그 카테고리 새로고침

⏰ 발행: 글은 항상 먼저 임시저장합니다. 정해진 시각이 되면 발행하게 할 수 있어요
   · "21시에 발행", "오후 9시 30분 발행", "바로 발행해줘" → 그 글만 그 시각에 발행 (+랜덤 대기)
   · "임시저장만" → 발행 안 함
   · 기본값과 시작 시각·간격·랜덤 대기는 PC 대시보드 4번 칸에서

· 앞에 /test 를 붙이면 저장하지 않고 미리보기만 보냅니다.
· 말투·분량·대상도 자유롭게 지시하세요. (예: 1500자, 반말, 초보자용)
· 여러 컴퓨터(블로그)에 한 번에: 컴퓨터마다 봇을 만들어 한 그룹에 넣고 그룹에 보내기
· /id 채팅 ID 확인
· 진행 상황과 기록은 PC 대시보드(npm start)에서도 볼 수 있어요."""


def strip_bot_mention(text: str, username: str | None) -> str:
    """그룹에서 붙는 '/test@봇이름', '@봇이름' 을 떼어 낸다."""
    text = re.sub(r"^/(\w+)@\w+", r"/\1", text.strip())
    if isinstance(username, str) and username:
        text = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE)
    return text.strip()


def build_app(get_cfg, runner: JobRunner) -> Application:
    cfg = get_cfg()
    if not cfg.telegram_bot_token:
        raise RuntimeError("텔레그램 봇 토큰이 없습니다")
    upload_dir = cfg.image_dir / "uploads"
    app = Application.builder().token(cfg.telegram_bot_token).build()

    def allowed(update: Update) -> bool:
        return bool(update.effective_chat) and update.effective_chat.id in get_cfg().allowed_chat_ids

    async def deny(update: Update) -> None:
        await update.effective_message.reply_text(
            f"⛔ 등록되지 않은 채팅입니다 (ID {update.effective_chat.id}). PC 대시보드의 [휴대폰 연결] 을 눌러 등록하세요."
        )

    async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP)
        if not allowed(update):
            await deny(update)

    async def chat_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"이 채팅 ID: {update.effective_chat.id}")

    async def categories(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            return await deny(update)
        c = get_cfg()
        if not c.naver_blog_id:
            await update.message.reply_text("블로그 아이디가 없습니다. PC 대시보드에서 네이버 로그인을 먼저 해 주세요.")
            return
        msg = await update.message.reply_text("📂 카테고리 불러오는 중...")
        try:
            cats = await NaverBlog(c, runner.session).categories(refresh=True)
        except Exception as exc:
            await msg.edit_text(f"❌ 카테고리를 불러오지 못했습니다: {exc}")
            return
        if not cats:
            await msg.edit_text("카테고리를 찾지 못했습니다.")
            return
        await msg.edit_text("📂 내 블로그 카테고리\n" + "\n".join(f"· {x.label}" for x in cats))

    def label() -> str:
        """여러 컴퓨터(블로그)가 한 그룹에서 같이 답할 때 누가 답했는지 보이게."""
        return f"[{get_cfg().naver_blog_id or 'PC'}] "

    async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, photos: list[Path]) -> None:
        chat = update.effective_chat.id
        tag = label()
        waiting = sum(1 for j in runner.jobs.values() if j.status in ("queued", "running"))
        first = "🚀 작업을 시작합니다..." if not waiting else f"⏳ 앞에 작업 {waiting}개가 있어요. 차례가 되면 시작합니다."
        status_msg = await context.bot.send_message(chat, tag + first + (f" (내 사진 {len(photos)}장)" if photos else ""))

        class Status:
            @staticmethod
            async def edit_text(msg: str) -> None:
                await status_msg.edit_text(tag + msg)

        status = Status()

        async def progress(msg: str) -> None:
            try:
                await context.bot.send_chat_action(chat, ChatAction.TYPING)
                await status.edit_text(msg)
            except Exception:
                pass

        job = runner.submit(text, photos, source="phone", progress=progress, want_result=True, chat_id=chat)
        try:
            result = await runner.wait(job)
        except NotLoggedIn:
            await status.edit_text("🔒 네이버 로그인이 풀렸습니다. PC 대시보드에서 [네이버 로그인 창 열기] 로 한 번 로그인한 뒤, "
                                   "대시보드 작업표의 [다시] 를 누르거나 이 메시지를 다시 보내 주세요.")
            return
        except NaverError as exc:
            await status.edit_text(f"❌ {exc}")
            if exc.screenshot:
                with open(exc.screenshot, "rb") as f:
                    await context.bot.send_photo(chat, f, caption=tag + "오류 당시 화면")
            return
        except asyncio.CancelledError:
            await status.edit_text("취소되었습니다.")
            return
        except Exception as exc:
            await status.edit_text(f"❌ 실패: {exc}")
            return

        if isinstance(result, ChannelResult):
            await report_channel(chat, context, status, tag, result)
            return

        plain = images.strip_markers(html_utils.html_to_text(result.body_html))
        done = ("미리보기 (저장 안 함)" if result.command.dry_run else
                "발행 완료" if result.draft and result.draft.published else "임시저장 완료")
        lines = [f"✅ {done}",
                 f"제목: {result.post.title}", f"분량: 약 {len(plain):,}자"]
        cat = result.draft.category if result.draft else (result.category.label if result.category else None)
        lines.append(f"카테고리: {cat or '기본 카테고리'}")
        if result.draft and result.draft.post_url:
            lines.append(f"주소: {result.draft.post_url}")
        planned = runner.jobs.get(job.id)
        if planned and planned.publish_state == "scheduled" and planned.scheduled_at:
            from .schedule import fmt

            lines.append(f"⏰ 발행 예정: {fmt(planned.scheduled_at)} (발행되면 알려 드려요)")
        if result.photos_received:
            if result.photos_attached:
                lines.append(f"보낸 사진: {result.photos_received}장 분석, {result.photos_attached}장 첨부")
            else:
                lines.append(f"보낸 사진: {result.photos_received}장 분석만 (첨부 안 함)")
        if result.images:
            kinds = ", ".join(img.source for img in result.images)
            done = f"{result.draft.images_inserted}/" if result.draft else ""
            lines.append(f"이미지: {done}{len(result.images)}장 ({kinds})")
        if result.command.insert_urls:
            lines.append(f"넣은 링크: {len(result.command.insert_urls)}개")
        if result.research and result.research.sources:
            lines.append(f"참고한 검색 출처: {len(result.research.sources)}곳")
        if result.post.tags:
            lines.append("태그: " + ", ".join(result.post.tags))
        lines += [f"⚠️ {w}" for w in dict.fromkeys(result.warnings)]
        await status.edit_text("\n".join(lines))

        if result.command.dry_run:
            if result.images:
                media = [InputMediaPhoto(img.path.read_bytes()) for img in result.images[:10]]
                await context.bot.send_media_group(chat, media)
            for i in range(0, len(plain), 3500):
                await context.bot.send_message(chat, (tag if i == 0 else "") + plain[i:i + 3500])
        elif result.draft and result.draft.screenshot:
            with open(result.draft.screenshot, "rb") as f:
                caption = ("발행된 글 화면" if result.draft.published
                           else "네이버 앱 > 글쓰기 > 임시저장 글에서 확인·발행하세요")
                await context.bot.send_photo(chat, f, caption=tag + caption)

    async def report_channel(chat, context, status, tag: str, result: ChannelResult) -> None:
        """유튜브 채널·블로그 요청: 몇 개를 넣었는지 알리고, 글이 하나 끝날 때마다 짧게 알린다."""
        total = len(result.children)
        icon, unit = ("📗", "글") if result.kind == "blog" else ("📺", "영상")
        period = period_label(result.months)
        head = f"{icon} {result.channel}\n{period} {unit} {result.found}개 → 글 {total}개를 차례로 씁니다"
        if result.note:
            head += f"\n({result.note})"
        if result.skipped:
            head += f"\n(이미 쓴 {unit} {result.skipped}개는 건너뜀)"
        if not total:
            head += f"\n새로 쓸 {unit}이 없습니다."
        await status.edit_text(head)
        ok = 0
        for i, child in enumerate(result.children, 1):
            try:
                r = await runner.wait(child)
                ok += 1
                await context.bot.send_message(chat, f"{tag}✅ ({i}/{total}) {r.post.title}")
            except asyncio.CancelledError:
                await context.bot.send_message(chat, f"{tag}⏹ ({i}/{total}) 취소됨")
            except NotLoggedIn:
                await context.bot.send_message(chat, f"{tag}🔒 ({i}/{total}) 네이버 로그인이 풀려 멈췄습니다. 대시보드에서 로그인 후 [다시] 를 눌러 주세요.")
            except Exception as exc:
                await context.bot.send_message(chat, f"{tag}❌ ({i}/{total}) 실패: {str(exc)[:200]}")
        if total:
            what = "블로그" if result.kind == "blog" else "채널"
            await context.bot.send_message(chat, f"{tag}{icon} {what} 글쓰기 끝: {ok}/{total}개 완료")

    def buffer_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "", photo: Path | None = None) -> None:
        """휴대폰에서 연달아 온 메시지를 모아 하나의 요청으로 처리한다.

        텔레그램은 긴 글을 붙여넣으면 여러 메시지로 쪼개 보내고, 앨범 사진도 한 장씩 따로 온다.
        마지막 메시지 뒤 MERGE_SECONDS 동안 더 오는 것이 없으면 합쳐서 글 하나로 처리한다.
        """
        data = context.chat_data
        buf = data.setdefault("buf", {"texts": [], "photos": []})
        if text.strip():
            buf["texts"].append(text)
        if photo is not None:
            buf["photos"].append(photo)
        if data.get("timer"):
            data["timer"].cancel()

        async def flush() -> None:
            await asyncio.sleep(MERGE_SECONDS)
            data.pop("timer", None)
            got = data.pop("buf", {"texts": [], "photos": []})
            merged = "\n".join(got["texts"]).strip()
            if not merged and got["photos"]:
                merged = PHOTO_ONLY_REQUEST  # 사진만 보내도 바로 사진 내용으로 글을 쓴다 (첨부는 안 함)
            if merged:
                await process(update, context, merged, got["photos"])

        data["timer"] = asyncio.create_task(flush())

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            return await deny(update)
        buffer_input(update, context, text=strip_bot_mention(update.message.text or "", context.bot.username))

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            return await deny(update)
        msg = update.message
        file = await (msg.photo[-1] if msg.photo else msg.document).get_file()
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.file_path or "").suffix or ".jpg"
        path = upload_dir / f"{msg.chat_id}-{msg.message_id}{suffix}"
        await file.download_to_drive(path)
        buffer_input(update, context, text=strip_bot_mention(msg.caption or "", context.bot.username), photo=path)

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("id", chat_id))
    app.add_handler(CommandHandler(["categories", "category"], categories))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # /test, /dry, /raw 는 명령어 형태지만 글쓰기 요청이다
    app.add_handler(MessageHandler(filters.Regex(r"^/(test|dry|raw)\b"), handle_text))
    return app


class TelegramBot:
    """대시보드에서 켜고 끄는 텔레그램 봇 (대시보드와 같은 이벤트 루프에서 돈다)."""

    def __init__(self, get_cfg, runner: JobRunner):
        self.get_cfg = get_cfg
        self.runner = runner
        self.app: Application | None = None
        self.username = ""
        self.error = ""

    @property
    def running(self) -> bool:
        return self.app is not None

    async def _notify_published(self, job) -> None:
        """정해진 시각에 발행됐을 때(또는 실패했을 때) 요청한 채팅으로 알린다."""
        if self.app is None or not job.chat_id:
            return
        tag = f"[{self.get_cfg().naver_blog_id or 'PC'}] "
        if job.publish_state == "published":
            text = f"{tag}🚀 발행 완료: {job.title}" + (f"\n{job.post_url}" if job.post_url else "")
        else:
            text = f"{tag}❌ 발행 실패: {job.title}\n{job.message}"
        await self.app.bot.send_message(job.chat_id, text)

    async def start(self) -> None:
        if self._notify_published not in self.runner.on_published:
            self.runner.on_published.append(self._notify_published)
        if self.app is not None:
            return
        self.error = ""
        try:
            app = build_app(self.get_cfg, self.runner)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=False, error_callback=self._on_poll_error)
            self.username = app.bot.username or ""
            self.app = app
            log.info("🤖 텔레그램 봇 켜짐 (@%s) — 휴대폰으로 지시를 보내세요.", self.username)
        except Exception as exc:
            self.error = str(exc)
            log.error("텔레그램 봇을 켜지 못했습니다: %s", exc)

    def _on_poll_error(self, exc: Exception) -> None:
        """폴링 오류. 같은 봇을 다른 컴퓨터에서도 켠 경우(Conflict)는 알아듣게 알리고 이 봇을 끈다."""
        if isinstance(exc, Conflict):
            if not self.error:
                self.error = (
                    "같은 텔레그램 봇이 다른 컴퓨터에서도 켜져 있습니다. 봇 하나는 한 컴퓨터에서만 쓸 수 있어요. "
                    "여러 컴퓨터에서 동시에 쓰려면 컴퓨터마다 봇을 따로 만들고, 봇들을 한 그룹에 넣으세요 (대시보드 3번 칸 안내)."
                )
                log.error("🚫 %s", self.error)
                asyncio.get_event_loop().create_task(self.stop(keep_error=True))
            return
        log.warning("텔레그램 연결 오류 (자동으로 다시 시도): %s", exc)

    async def stop(self, keep_error: bool = False) -> None:
        if not keep_error:
            self.error = ""
        app, self.app = self.app, None
        if app is None:
            return
        for step in (app.updater.stop, app.stop, app.shutdown):
            try:
                await step()
            except Exception:
                pass
        log.info("텔레그램 봇 꺼짐")

    def status(self) -> dict:
        cfg = self.get_cfg()
        return {
            "running": self.running,
            "username": self.username,
            "hasToken": bool(cfg.telegram_bot_token),
            "chatIds": sorted(cfg.allowed_chat_ids),
            "error": self.error,
        }
