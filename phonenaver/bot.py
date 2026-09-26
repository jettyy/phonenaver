"""텔레그램 봇: 휴대폰에서 메시지를 보내면 블로그 글을 써서 임시저장한다."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import html_utils
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

· 앞에 /test 를 붙이면 저장하지 않고 미리보기만 보냅니다.
· 말투·분량·대상도 자유롭게 지시하세요. (예: 1500자, 반말, 초보자용)
· /id 채팅 ID 확인"""


def build_app(cfg: Config) -> Application:
    if not cfg.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 이 없습니다 (.env 확인)")
    pipeline = Pipeline(cfg)
    app = Application.builder().token(cfg.telegram_bot_token).concurrent_updates(False).build()

    def allowed(update: Update) -> bool:
        return bool(update.effective_chat) and update.effective_chat.id in cfg.allowed_chat_ids

    async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP)
        if not allowed(update):
            await update.message.reply_text(
                f"⛔ 아직 허용되지 않은 채팅입니다. .env 의 ALLOWED_CHAT_IDS 에 {update.effective_chat.id} 를 넣고 봇을 재시작하세요."
            )

    async def chat_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"이 채팅 ID: {update.effective_chat.id}")

    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            await update.message.reply_text(
                f"⛔ 허용되지 않은 채팅입니다 (ID {update.effective_chat.id}). ALLOWED_CHAT_IDS 에 추가하세요."
            )
            return
        text = update.message.text or update.message.caption or ""
        status = await update.message.reply_text("🚀 작업을 시작합니다...")

        async def progress(msg: str) -> None:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            try:
                await status.edit_text(msg)
            except Exception:
                pass

        try:
            result = await pipeline.run(text, progress)
        except NaverError as exc:
            await status.edit_text(f"❌ {exc}")
            if exc.screenshot:
                with open(exc.screenshot, "rb") as f:
                    await update.message.reply_photo(f, caption="오류 당시 화면")
            return
        except Exception as exc:  # 휴대폰에 원인을 바로 알려준다
            log.exception("작업 실패")
            await status.edit_text(f"❌ 실패: {exc}")
            return

        plain = html_utils.html_to_text(result.body_html)
        lines = [f"✅ {'미리보기 (저장 안 함)' if result.command.dry_run else '임시저장 완료'}",
                 f"제목: {result.post.title}", f"분량: 약 {len(plain):,}자"]
        if result.command.insert_urls:
            lines.append(f"넣은 링크: {len(result.command.insert_urls)}개")
        if result.research and result.research.sources:
            lines.append(f"참고한 검색 출처: {len(result.research.sources)}곳")
        if result.post.tags:
            lines.append("태그: " + ", ".join(result.post.tags))
        await status.edit_text("\n".join(lines))

        if result.command.dry_run:
            for i in range(0, len(plain), 3500):
                await update.message.reply_text(plain[i:i + 3500])
        elif result.draft and result.draft.screenshot:
            with open(result.draft.screenshot, "rb") as f:
                await update.message.reply_photo(f, caption="네이버 앱 > 글쓰기 > 임시저장 글에서 확인·발행하세요")

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("id", chat_id))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle))
    # /test, /dry, /raw 는 명령어 형태지만 글쓰기 요청이다
    app.add_handler(MessageHandler(filters.Regex(r"^/(test|dry|raw)\b"), handle))
    return app


def run_bot(cfg: Config) -> None:
    app = build_app(cfg)
    log.info("텔레그램 봇 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
