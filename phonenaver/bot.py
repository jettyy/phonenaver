"""텔레그램 봇: 휴대폰에서 메시지를 보내면 블로그 글을 써서 임시저장한다."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import html_utils, images
from .config import Config
from .naver import NaverError
from .pipeline import Pipeline

log = logging.getLogger(__name__)

HELP = """📝 네이버 블로그 자동 글쓰기 봇

그냥 메시지를 보내면 글을 써서 '임시저장'합니다.

1) 키워드/문장 → 최신 정보 검색 후 작성
   예) 2026 청년도약계좌 조건 정리해줘

2) 링크만 보내기 → 링크 내용 분석 후 작성
   예) https://... 이거 분석해서 후기 글로 써줘

3) 링크를 글에 넣기 → '넣어/삽입/걸어' 라고 말하기
   예) 제주 한달살기 준비물 글 써줘. 이 링크 넣어줘 https://...
   (여러 줄이면 '넣어'가 적힌 줄의 링크만 넣고, 나머지 링크는 분석용)

4) 내가 쓴 글 그대로 저장
   그대로: 첫 줄은 제목
   둘째 줄부터 본문

🖼 이미지: 글마다 3장 자동 삽입
   · 사진을 보내면(앨범 가능) AI가 사진을 분석해서 글 내용에 반영
     - "첨부해줘" (또는 아무 말 없으면) → 분석 + 내 사진을 글에 첨부
     - "분석만 해줘" / "첨부는 하지 마" → 분석해서 내용에만 반영, 첨부 안 함
     사진 설명(캡션)에 지시를 쓰거나, 사진 먼저 보내고 지시를 보내도 됨
   · 부족한 장수는 무료 사진·카드 이미지로 채움
   · '사진 없이' 라고 쓰면 이미지 생략

📂 카테고리: 글 내용에 맞게 자동 선택
   · 직접 지정: 카테고리: 여행
   · /categories 내 블로그 카테고리 새로고침

· 앞에 /test 를 붙이면 저장하지 않고 미리보기만 보냅니다.
· 말투·분량·대상도 자유롭게 지시하세요. (예: 1500자, 반말, 초보자용)
· /id 채팅 ID 확인"""


def build_app(cfg: Config) -> Application:
    if not cfg.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 이 없습니다 (.env 확인)")
    pipeline = Pipeline(cfg)
    upload_dir = cfg.image_dir / "uploads"
    app = Application.builder().token(cfg.telegram_bot_token).build()
    locks: dict = {}  # 글은 한 번에 하나씩 처리 (Lock 은 봇 루프 안에서 처음 쓸 때 생성 - 파이썬 3.9 호환)

    def job_lock() -> asyncio.Lock:
        if "job" not in locks:
            locks["job"] = asyncio.Lock()
        return locks["job"]

    def allowed(update: Update) -> bool:
        return bool(update.effective_chat) and update.effective_chat.id in cfg.allowed_chat_ids

    async def deny(update: Update) -> None:
        await update.effective_message.reply_text(
            f"⛔ 허용되지 않은 채팅입니다 (ID {update.effective_chat.id}). .env 의 ALLOWED_CHAT_IDS 에 넣고 봇을 재시작하세요."
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
        if pipeline.naver is None:
            await update.message.reply_text("NAVER_BLOG_ID 가 설정되지 않았습니다.")
            return
        msg = await update.message.reply_text("📂 카테고리 불러오는 중...")
        try:
            cats = await pipeline.naver.categories(refresh=True)
        except Exception as exc:
            await msg.edit_text(f"❌ 카테고리를 불러오지 못했습니다: {exc}")
            return
        if not cats:
            await msg.edit_text("카테고리를 찾지 못했습니다. .env 의 NAVER_CATEGORIES 에 직접 적어 주세요.")
            return
        await msg.edit_text("📂 내 블로그 카테고리\n" + "\n".join(f"· {c.label}" for c in cats))

    async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, photos: list[Path]) -> None:
        chat = update.effective_chat.id
        status = await context.bot.send_message(chat, "🚀 작업을 시작합니다..." + (f" (내 사진 {len(photos)}장)" if photos else ""))

        async def progress(msg: str) -> None:
            await context.bot.send_chat_action(chat, ChatAction.TYPING)
            try:
                await status.edit_text(msg)
            except Exception:
                pass

        async with job_lock():
            try:
                result = await pipeline.run(text, progress, photos=photos)
            except NaverError as exc:
                await status.edit_text(f"❌ {exc}")
                if exc.screenshot:
                    with open(exc.screenshot, "rb") as f:
                        await context.bot.send_photo(chat, f, caption="오류 당시 화면")
                return
            except Exception as exc:  # 휴대폰에 원인을 바로 알려준다
                log.exception("작업 실패")
                await status.edit_text(f"❌ 실패: {exc}")
                return

        plain = images.strip_markers(html_utils.html_to_text(result.body_html))
        lines = [f"✅ {'미리보기 (저장 안 함)' if result.command.dry_run else '임시저장 완료'}",
                 f"제목: {result.post.title}", f"분량: 약 {len(plain):,}자"]
        cat = result.draft.category if result.draft else (result.category.label if result.category else None)
        lines.append(f"카테고리: {cat or '기본 카테고리'}")
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
                await context.bot.send_message(chat, plain[i:i + 3500])
        elif result.draft and result.draft.screenshot:
            with open(result.draft.screenshot, "rb") as f:
                await context.bot.send_photo(chat, f, caption="네이버 앱 > 글쓰기 > 임시저장 글에서 확인·발행하세요")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            return await deny(update)
        photos = context.chat_data.pop("photos", [])  # 먼저 보내 둔 사진이 있으면 함께 사용
        await process(update, context, update.message.text or "", photos)

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """사진(앨범 포함)을 받아 모은다. 앨범은 여러 메시지로 나눠 오므로 잠시 기다렸다 한 번에 처리."""
        if not allowed(update):
            return await deny(update)
        msg = update.message
        file = await (msg.photo[-1] if msg.photo else msg.document).get_file()
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.file_path or "").suffix or ".jpg"
        path = upload_dir / f"{msg.chat_id}-{msg.message_id}{suffix}"
        await file.download_to_drive(path)

        data = context.chat_data
        data.setdefault("photos", []).append(path)
        if msg.caption:
            data["caption"] = msg.caption
        if data.get("timer"):
            data["timer"].cancel()

        async def flush() -> None:
            await asyncio.sleep(3)
            data.pop("timer", None)
            caption = data.pop("caption", None)
            if caption:
                await process(update, context, caption, data.pop("photos", []))
            else:
                n = len(data.get("photos", []))
                await context.bot.send_message(
                    msg.chat_id, f"📷 사진 {n}장 받았어요. 이제 글 주제나 링크를 보내 주세요. (이 사진들을 글에 넣습니다)"
                )

        data["timer"] = asyncio.create_task(flush())

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("id", chat_id))
    app.add_handler(CommandHandler(["categories", "category"], categories))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # /test, /dry, /raw 는 명령어 형태지만 글쓰기 요청이다
    app.add_handler(MessageHandler(filters.Regex(r"^/(test|dry|raw)\b"), handle_text))
    return app


def run_bot(cfg: Config) -> None:
    # 메뉴에서 봇을 껐다 다시 켤 수 있도록 매번 새 이벤트 루프 사용.
    # (파이썬 3.9 는 봇을 만들 때 루프가 있어야 하므로 build_app 보다 먼저)
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = build_app(cfg)
    log.info("텔레그램 봇 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
