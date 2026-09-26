"""휴대폰(텔레그램) 연결: 토큰 확인과, 봇에게 온 첫 메시지로 내 채팅을 자동 등록."""
from __future__ import annotations

import asyncio
import time

import httpx


class TelegramSetupError(RuntimeError):
    pass


async def call(token: str, method: str, **params):
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramSetupError(data.get("description", "텔레그램 오류"))
    return data["result"]


async def check_token(token: str) -> str:
    """토큰이 맞으면 봇 아이디(@username)를 돌려준다."""
    try:
        me = await call(token.strip(), "getMe")
    except TelegramSetupError as exc:
        raise TelegramSetupError(f"토큰이 맞지 않습니다 ({exc})") from exc
    except httpx.HTTPError as exc:
        raise TelegramSetupError(f"텔레그램에 접속하지 못했습니다 ({exc})") from exc
    return me.get("username", "")


async def wait_for_chat(token: str, timeout_s: int = 180) -> int:
    """봇에게 새 메시지가 오면 그 채팅 ID 를 돌려준다. (예전에 쌓인 메시지는 무시)"""
    offset = None
    try:
        old = await call(token, "getUpdates", timeout=0)
        if old:
            offset = old[-1]["update_id"] + 1
    except Exception:
        pass
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            updates = await call(token, "getUpdates", timeout=25, offset=offset)
        except Exception:
            await asyncio.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if msg and msg.get("chat", {}).get("id"):
                chat_id = int(msg["chat"]["id"])
                try:
                    await call(token, "getUpdates", timeout=0, offset=offset)  # 처리한 메시지 정리
                    await call(token, "sendMessage", chat_id=chat_id,
                               text="✅ 이 채팅이 등록되었습니다. 이제 여기로 글쓰기 지시를 보내세요.")
                except Exception:
                    pass
                return chat_id
    raise TelegramSetupError("3분 안에 휴대폰에서 메시지가 오지 않았습니다. 다시 시도해 주세요.")
