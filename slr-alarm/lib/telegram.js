// 텔레그램으로 알림 보내기 (선택). 봇 토큰과 채팅 ID 가 있을 때만 쓴다.

async function call(token, method, params) {
  const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(params),
    signal: AbortSignal.timeout(20000),
  });
  const data = await res.json().catch(() => ({}));
  if (!data.ok) throw new Error(data.description || `텔레그램 오류 (${res.status})`);
  return data.result;
}

export function hitText(post, keywords) {
  const meta = [post.author, post.date].filter(Boolean).join(' · ');
  return [`🔔 SLR 장터 새 글  [${keywords.join(', ')}]`, post.title, meta, post.url].filter(Boolean).join('\n');
}

export async function sendHit(settings, post, keywords) {
  return call(settings.telegramToken, 'sendMessage', {
    chat_id: settings.telegramChatId,
    text: hitText(post, keywords),
    reply_markup: { inline_keyboard: [[{ text: '🔗 글 바로 보기', url: post.url }]] },
  });
}

export async function sendText(settings, text) {
  return call(settings.telegramToken, 'sendMessage', { chat_id: settings.telegramChatId, text });
}

// 휴대폰에서 봇에게 보낸 마지막 메시지의 채팅 ID 를 찾는다.
export async function findChatId(token) {
  const updates = await call(token, 'getUpdates', { timeout: 0 });
  for (const upd of [...updates].reverse()) {
    const msg = upd.message || upd.edited_message || upd.channel_post;
    if (msg?.chat?.id) return String(msg.chat.id);
  }
  return '';
}
