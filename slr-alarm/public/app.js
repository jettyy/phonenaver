// SLR 장터 알림 대시보드 화면
const $ = (id) => document.getElementById(id);
let state = null;
let formsFilled = false;

// ── 서버 호출 ──
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `오류 ${res.status}`);
  return data;
}

function toast(msg, bad = false) {
  const el = $('toast');
  el.textContent = msg;
  el.className = `toast${bad ? ' bad' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add('hidden'), 3200);
}

async function act(fn, okMsg) {
  try {
    const out = await fn();
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(out) : okMsg);
    await refresh();
    return out;
  } catch (err) {
    toast(err.message, true);
    return null;
  }
}

// ── 그리기 ──
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function time(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  const hms = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return new Date().toDateString() === d.toDateString() ? hms : `${d.getMonth() + 1}/${d.getDate()} ${hms.slice(0, 5)}`;
}

function pill(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = `pill${kind ? ` ${kind}` : ''}`;
}

function render() {
  const { keywords, hits, posts, status, settings, unread } = state;
  const active = keywords.filter((k) => k.on).length;

  // 위쪽 상태
  if (settings.paused) pill('pill-watch', '일시정지', 'warn');
  else if (!active) pill('pill-watch', '키워드 없음', 'warn');
  else if (status.lastError) pill('pill-watch', '확인 실패', 'bad');
  else pill('pill-watch', `감시 중 · 키워드 ${active}개`, 'ok');
  pill('pill-check', `마지막 확인 ${time(status.lastCheck)}`, status.lastError ? 'bad' : '');
  pill('pill-telegram', settings.telegramTokenSet && settings.telegramChatId ? '텔레그램 연결됨' : '텔레그램 꺼짐',
    settings.telegramTokenSet && settings.telegramChatId ? 'ok' : '');
  const perm = 'Notification' in window ? Notification.permission : 'unsupported';
  pill('pill-notify', perm === 'granted' ? '브라우저 알림 켜짐' : '브라우저 알림 꺼짐', perm === 'granted' ? 'ok' : '');
  $('btn-notify').classList.toggle('hidden', perm === 'granted' || perm === 'unsupported');

  const badge = $('unread-badge');
  badge.textContent = `새 글 ${unread}`;
  badge.classList.toggle('hidden', !unread);
  document.title = unread ? `(${unread}) SLR 장터 알림` : 'SLR 장터 알림';

  // 1. 키워드
  $('kw-list').innerHTML = keywords.length ? keywords.map((k) => `
    <li class="${k.on ? '' : 'off'}">
      <label class="switch" title="${k.on ? '끄기' : '켜기'}">
        <input type="checkbox" data-toggle="${esc(k.text)}" ${k.on ? 'checked' : ''}><span></span>
      </label>
      <span class="kw-text">${esc(k.text)}</span>
      <span class="kw-count">알림 ${k.hits}건</span>
      <button class="btn small ghost danger" data-delete="${esc(k.text)}">삭제</button>
    </li>`).join('') : '<li class="empty">아직 키워드가 없습니다. 위 칸에 찾는 물건을 넣고 [추가] 를 누르세요.</li>';

  // 2. 알림 받은 글
  $('hit-list').innerHTML = hits.length ? hits.map((h) => `
    <li class="${h.read ? '' : 'unread'}">
      <div class="hit-main">
        <a class="hit-title" href="${esc(h.url)}" target="_blank" rel="noopener" data-read="${h.no}">${esc(h.title)}</a>
        <div class="hit-meta">
          ${h.read ? '' : '<span class="badge new">NEW</span>'}
          ${h.keywords.map((k) => `<span class="badge hit">${esc(k)}</span>`).join('')}
          <span>${esc([h.author, h.date && `작성 ${h.date}`].filter(Boolean).join(' · '))}</span>
          <span>· 찾은 시각 ${time(h.foundAt)}</span>
        </div>
      </div>
      <a class="btn small" href="${esc(h.url)}" target="_blank" rel="noopener" data-read="${h.no}">열기</a>
    </li>`).join('') : '<li class="empty">키워드에 맞는 새 글이 올라오면 여기에 쌓입니다.</li>';

  // 3. 지금 장터 목록
  const parts = [];
  if (status.checking) parts.push('확인 중...');
  else parts.push(`마지막 확인 ${time(status.lastCheck)}`);
  if (!settings.paused && active && status.nextCheck) parts.push(`다음 확인 ${time(status.nextCheck)}`);
  parts.push(`${status.interval}초마다`);
  if (status.mode === 'browser') parts.push('크롬으로 읽는 중');
  $('status-line').innerHTML = esc(parts.join(' · '))
    + (status.lastError ? ` <span class="err">⚠️ ${esc(status.lastError)}</span>` : '')
    + (status.lastError && status.debugPage ? ' <a href="/debug/last-page" target="_blank">받아 온 화면 보기</a>' : '')
    + (status.needLogin && !status.loginOpen ? ' <button class="btn small primary" data-login>SLR클럽 로그인 창 열기</button>' : '');
  $('login-state').textContent = status.loginOpen ? '로그인 창이 열려 있습니다 — 로그인하고 창을 닫아 주세요'
    : (status.mode === 'browser' && !status.lastError ? '크롬으로 목록을 읽고 있습니다 ✅' : '');
  $('btn-login').disabled = status.loginOpen;
  $('btn-pause').textContent = settings.paused ? '다시 시작' : '일시정지';
  $('post-list').innerHTML = posts.length ? posts.map((p) => `
    <li class="${p.keywords.length ? 'hit' : ''}">
      <span class="post-no">${p.no}</span>
      <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>
      <span class="post-side">${p.keywords.map((k) => `<span class="badge hit">${esc(k)}</span> `).join('')}${esc(p.author)} ${esc(p.date)}</span>
    </li>`).join('') : '<li class="empty">[지금 확인] 을 누르면 장터 첫 페이지를 불러옵니다.</li>';

  // 4. 설정 (입력 중인 칸을 덮어쓰지 않도록 처음 한 번만 채움)
  if (!formsFilled) {
    formsFilled = true;
    $('s-interval').value = settings.interval;
    $('s-board').value = settings.boardUrl;
    $('s-board').placeholder = settings.defaultBoardUrl;
    $('s-chat').value = settings.telegramChatId;
  }
  $('s-sound').checked = settings.sound;
  $('token-hint').textContent = settings.telegramTokenSet
    ? `저장됨 (${settings.telegramTokenHint}) — 바꿀 때만 새로 입력` : '텔레그램 @BotFather → /newbot 으로 받은 토큰';
}

async function refresh() {
  try {
    state = await api('/api/state');
    render();
  } catch {
    pill('pill-watch', '대시보드 꺼짐 — npm start', 'bad');
  }
}

// ── 새 글 알림 (소리 + 브라우저 알림) ──
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.18].forEach((at, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = i ? 1320 : 880;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.16);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + at);
      osc.stop(ctx.currentTime + at + 0.17);
    });
  } catch {
    // 소리를 못 내도 괜찮다
  }
}

function announce({ post, keywords }) {
  if (state?.settings.sound) beep();
  if ('Notification' in window && Notification.permission === 'granted') {
    const n = new Notification(`🔔 SLR 장터 [${keywords.join(', ')}]`, { body: post.title, tag: `slr-${post.no}` });
    n.onclick = () => {
      window.open(post.url, '_blank');
      api('/api/hits/read', { no: post.no }).then(refresh);
      n.close();
    };
  }
  toast(`🔔 ${post.title}`);
  refresh();
}

function listen() {
  const es = new EventSource('/api/events');
  es.addEventListener('hit', (e) => announce(JSON.parse(e.data)));
  es.onerror = () => {
    es.close();
    setTimeout(listen, 5000);
  };
}

// ── 버튼 ──
async function addKeywords() {
  const input = $('kw-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  await act(() => api('/api/keywords', { text }), (out) => (out.added.length ? `추가: ${out.added.join(', ')}` : '이미 있는 키워드예요'));
  setTimeout(refresh, 2500); // 추가하면 바로 목록을 다시 읽으므로 잠시 뒤 한 번 더
}

$('btn-add').onclick = addKeywords;
$('kw-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.isComposing) addKeywords();
});
$('kw-input').addEventListener('paste', (e) => {
  const text = e.clipboardData.getData('text');
  if (text.includes('\n')) { // 여러 줄 붙여넣기 → 줄마다 키워드
    e.preventDefault();
    $('kw-input').value = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean).join(', ');
  }
});

$('kw-list').addEventListener('click', (e) => {
  const del = e.target.closest('[data-delete]');
  if (del && confirm(`'${del.dataset.delete}' 키워드를 지울까요?`)) {
    act(() => api('/api/keywords/delete', { text: del.dataset.delete }), '삭제했습니다');
  }
});
$('kw-list').addEventListener('change', (e) => {
  const t = e.target.closest('[data-toggle]');
  if (t) act(() => api('/api/keywords/toggle', { text: t.dataset.toggle }), (o) => (o.keyword.on ? '켰습니다' : '껐습니다 (저장은 그대로)'));
});

$('hit-list').addEventListener('click', (e) => {
  const a = e.target.closest('[data-read]');
  if (a) api('/api/hits/read', { no: Number(a.dataset.read) }).then(refresh);
});
$('btn-read-all').onclick = () => act(() => api('/api/hits/read', {}));
$('btn-clear-hits').onclick = () => confirm('알림 받은 글 목록을 비울까요? (키워드는 그대로)') && act(() => api('/api/hits/clear', {}), '비웠습니다');

$('btn-check').onclick = async () => {
  $('btn-check').disabled = true;
  $('status-line').textContent = '확인 중...';
  await act(() => api('/api/check', {}));
  $('btn-check').disabled = false;
};
$('btn-pause').onclick = () => act(() => api('/api/pause', { paused: !state.settings.paused }),
  (o) => (o.paused ? '일시정지했습니다' : '다시 감시합니다'));

$('btn-save-check').onclick = () => act(() => api('/api/settings', {
  interval: Number($('s-interval').value), boardUrl: $('s-board').value,
}), '저장했습니다');
$('s-sound').onchange = () => act(() => api('/api/settings', { sound: $('s-sound').checked }));
$('btn-test-sound').onclick = beep;
$('btn-notify').onclick = async () => {
  const perm = await Notification.requestPermission();
  toast(perm === 'granted' ? '브라우저 알림을 켰습니다' : '브라우저 설정에서 알림을 허용해 주세요', perm !== 'granted');
  render();
};

$('btn-save-telegram').onclick = () => {
  const body = { telegramChatId: $('s-chat').value };
  if ($('s-token').value.trim()) body.telegramToken = $('s-token').value;
  $('s-token').value = '';
  act(() => api('/api/settings', body), '텔레그램 설정을 저장했습니다');
};
$('btn-find-chat').onclick = async () => {
  const out = await act(async () => {
    if ($('s-token').value.trim()) {
      await api('/api/settings', { telegramToken: $('s-token').value });
      $('s-token').value = '';
    }
    return api('/api/telegram/find-chat', {});
  }, (o) => `채팅 ID ${o.chatId} 를 찾아 저장했습니다`);
  if (out) $('s-chat').value = out.chatId;
};
$('btn-test-telegram').onclick = () => act(() => api('/api/telegram/test', {}), '휴대폰 텔레그램을 확인하세요');

async function openLogin() {
  await act(() => api('/api/login-window', {}), '크롬 창에서 SLR클럽에 로그인한 뒤 창을 닫아 주세요');
}
$('btn-login').onclick = openLogin;
$('status-line').addEventListener('click', (e) => {
  if (e.target.closest('[data-login]')) openLogin();
});

refresh();
listen();
setInterval(refresh, 5000);
