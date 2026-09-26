"""휴대폰에서 연달아 온 메시지(텔레그램이 쪼갠 긴 글, 앨범 사진)가 하나의 요청으로 합쳐지는지."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.ext import MessageHandler

from phonenaver import bot
from phonenaver.config import Config


class FakeRunner:
    def __init__(self):
        self.jobs, self.submitted = {}, []
        self.session = None

    def submit(self, text, photos, **kw):
        self.submitted.append((text, list(photos)))
        return SimpleNamespace(id="x")

    async def wait(self, job):
        raise asyncio.CancelledError()


def _update(chat_id, text):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = text
    return upd


def test_split_messages_become_one_request(monkeypatch):
    monkeypatch.setattr(bot, "MERGE_SECONDS", 0.2)
    cfg = Config(telegram_bot_token="1:x", allowed_chat_ids={7})
    runner = FakeRunner()

    async def scenario():
        # 실제 프로그램처럼 이벤트 루프 안에서 봇을 만든다 (파이썬 3.9 는 루프 밖에서 만들 수 없음)
        app = bot.build_app(lambda: cfg, runner)
        text_handler = next(h for h in app.handlers[0]
                            if isinstance(h, MessageHandler) and "TEXT" in repr(h.filters) and "COMMAND" in repr(h.filters))
        photo_handler = next(h for h in app.handlers[0] if isinstance(h, MessageHandler) and "PHOTO" in repr(h.filters))
        ctx = SimpleNamespace(chat_data={}, bot=AsyncMock())
        # 텔레그램이 긴 글을 세 조각으로 나눠 보낸 상황
        for part in ("첫 부분 전세 월세 차이", "가운데 부분 대출 금리", "마지막 부분 결론"):
            await text_handler.callback(_update(7, part), ctx)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        assert runner.submitted == [("첫 부분 전세 월세 차이\n가운데 부분 대출 금리\n마지막 부분 결론", [])]

        # 설명 없이 사진만 보내면 사진 내용으로 글쓰기 요청
        runner.submitted.clear()
        upd = _update(7, None)
        upd.message.caption = None
        upd.message.photo = [MagicMock()]
        fake_file = MagicMock(file_path="a.jpg")
        fake_file.download_to_drive = AsyncMock()
        upd.message.photo[-1].get_file = AsyncMock(return_value=fake_file)
        await photo_handler.callback(upd, ctx)
        await asyncio.sleep(0.5)
        assert len(runner.submitted) == 1
        text, photos = runner.submitted[0]
        assert text == bot.PHOTO_ONLY_REQUEST and len(photos) == 1

    asyncio.run(scenario())


def test_group_message_with_bot_mention(monkeypatch):
    """여러 컴퓨터용: 봇들이 들어 있는 그룹(음수 ID)에서 '/test@봇이름 주제' 로 보낸 경우."""
    monkeypatch.setattr(bot, "MERGE_SECONDS", 0.1)
    cfg = Config(telegram_bot_token="1:x", allowed_chat_ids={-100123}, naver_blog_id="doublepapa")
    runner = FakeRunner()

    async def scenario():
        app = bot.build_app(lambda: cfg, runner)
        cmd_handler = next(h for h in app.handlers[0] if isinstance(h, MessageHandler) and "Regex" in repr(h.filters))
        ctx = SimpleNamespace(chat_data={}, bot=AsyncMock())
        ctx.bot.username = "naver1_bot"
        await cmd_handler.callback(_update(-100123, "/test@naver1_bot 캠핑 준비물 정리"), ctx)
        await asyncio.sleep(0.4)
        assert runner.submitted == [("/test 캠핑 준비물 정리", [])]
        first_reply = ctx.bot.send_message.call_args_list[0].args[1]
        assert first_reply.startswith("[doublepapa] ")  # 어느 블로그(컴퓨터)의 답인지 표시

        # 등록 안 된 채팅은 거절
        runner.submitted.clear()
        other = _update(555, "글 써줘")
        other.effective_message.reply_text = AsyncMock()
        await cmd_handler.callback(other, ctx)
        await asyncio.sleep(0.3)
        assert runner.submitted == []

    asyncio.run(scenario())


def test_conflict_stops_bot_with_clear_message():
    from telegram.error import Conflict

    async def scenario():
        tb = bot.TelegramBot(lambda: Config(), FakeRunner())
        tb.app = SimpleNamespace(updater=SimpleNamespace(stop=AsyncMock()), stop=AsyncMock(), shutdown=AsyncMock())
        tb._on_poll_error(Conflict("terminated by other getUpdates request"))
        await asyncio.sleep(0.05)
        assert not tb.running
        assert "다른 컴퓨터" in tb.status()["error"]

    asyncio.run(scenario())
