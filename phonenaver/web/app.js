// 폰네이버 대시보드
const $ = (id) => document.getElementById(id);
const state = { jobs: [], open: new Set(), busy: {}, settings: {}, photos: [] };

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add('hidden'), ms);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || 'POST', headers: {} };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  if (opts.method === 'GET') delete init.body;
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({ ok: false, error: '서버 응답을 읽지 못했습니다' }));
  if (!data.ok) throw new Error(data.error || '실패했습니다');
  return data.data;
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    toast('❌ ' + e.message, 4200);
  }
}

// ── 상단 상태 ─────────────────────────────
function pill(el, cls, text) {
  el.className = 'pill ' + (cls || '');
  el.textContent = text;
}

function renderSession(s) {
  if (!s) return;
  pill($('pill-session'), s.loggedIn ? 'ok' : 'bad', s.loggedIn ? '네이버 로그인됨' : '네이버 로그인 필요');
  $('session-detail').textContent = s.loggedIn
    ? `로그인 유지 중${s.blogId ? ' · 블로그 ' + s.blogId : ''}${s.checkedAt ? ' · 확인 ' + s.checkedAt : ''}`
    : '로그인이 필요합니다. [네이버 로그인 창 열기] 를 눌러 한 번만 로그인하세요.';
  if (s.blogId && !$('s-blog-id').value) $('s-blog-id').value = s.blogId;
}

function renderBot(b) {
  if (!b) return;
  let cls = 'warn', text = '휴대폰 봇 꺼짐';
  if (!b.hasToken) text = '휴대폰 봇 설정 필요';
  else if (!b.chatIds.length) text = '휴대폰 연결 필요';
  else if (b.running) { cls = 'ok'; text = '휴대폰 봇 켜짐'; }
  if (b.error) cls = 'bad';
  pill($('pill-bot'), cls, text);
  const parts = [];
  if (b.username) parts.push('@' + b.username);
  parts.push(b.hasToken ? '토큰 저장됨' : '토큰 없음');
  parts.push(b.chatIds.length ? `휴대폰 ${b.chatIds.length}대 연결됨` : '휴대폰 연결 안 됨');
  if (b.error) parts.push('오류: ' + b.error);
  $('bot-detail').textContent = parts.join(' · ');
  $('btn-bot-start').disabled = b.running;
  $('btn-bot-stop').disabled = !b.running;
}

function renderClaude(c) {
  if (!c || !c.checkedAt) return;
  const cls = c.installed && c.loggedIn ? 'ok' : 'bad';
  const text = !c.installed ? 'Claude 설치 필요' : c.loggedIn ? 'Claude 구독 연결됨' : 'Claude 로그인 필요';
  pill($('pill-claude'), cls, text);
  $('claude-detail').textContent = text + (c.installed ? '' : ' — 터미널에서 npm install 을 다시 실행하세요');
}

function renderBusy(b) {
  state.busy = b || {};
  $('btn-login').disabled = !!state.busy.login;
  $('btn-login').textContent = state.busy.login ? '로그인 창에서 로그인해 주세요...' : '네이버 로그인 창 열기';
  $('btn-verify').disabled = !!state.busy.verify;
  $('btn-connect').disabled = !!state.busy.connect;
  $('btn-connect').textContent = state.busy.connect ? '휴대폰 메시지 기다리는 중...' : '휴대폰 연결';
  $('btn-claude-login').disabled = !!state.busy.claude;
}

function renderConnect(msg) {
  $('connect-msg').textContent = msg || '';
  $('connect-msg').classList.toggle('hidden', !msg);
}

// ── 설정 ─────────────────────────────────
const BOOLS = ['IMAGE_PER_SECTION', 'THUMBNAIL_CARD', 'AUTO_CATEGORY', 'APPEND_HASHTAGS', 'INCLUDE_SOURCES', 'HEADLESS'];
const VALUES = ['IMAGE_COUNT', 'MAX_IMAGES', 'PARAGRAPH_GAP', 'MAX_SEARCHES', 'CLAUDE_MODEL'];

function renderSettings(s) {
  if (!s) return;
  state.settings = s;
  BOOLS.forEach((k) => { $('s-' + k).checked = !!s[k] && s[k] !== 'false'; });
  VALUES.forEach((k) => { $('s-' + k).value = s[k] ?? ''; });
  const pex = s.PEXELS_API_KEY || {};
  $('s-PEXELS_API_KEY').placeholder = pex.set ? '저장됨 (바꿀 때만 입력)' : '없음';
  if (s.NAVER_BLOG_ID && !$('s-blog-id').value) $('s-blog-id').value = s.NAVER_BLOG_ID;
}

$('btn-toggle-settings').onclick = () => {
  const box = $('settings');
  box.classList.toggle('hidden');
  $('btn-toggle-settings').textContent = box.classList.contains('hidden') ? '펼치기' : '접기';
};

$('btn-save-settings').onclick = () => act(async () => {
  const body = {};
  BOOLS.forEach((k) => { body[k] = $('s-' + k).checked; });
  VALUES.forEach((k) => { body[k] = $('s-' + k).value; });
  body.PEXELS_API_KEY = $('s-PEXELS_API_KEY').value;
  renderSettings(await api('/api/settings', { body }));
  $('s-PEXELS_API_KEY').value = '';
}, '설정을 저장했습니다');

$('btn-categories').onclick = () => act(async () => {
  $('cat-list').innerHTML = '<span>불러오는 중...</span>';
  const cats = await api('/api/categories');
  $('cat-list').innerHTML = cats.length ? cats.map((c) => `<span>${esc(c)}</span>`).join('') : '<span>카테고리를 찾지 못했습니다</span>';
});

$('btn-claude-check').onclick = () => act(async () => renderClaude(await api('/api/claude/check')), '확인했습니다');
$('btn-claude-login').onclick = () => act(() => api('/api/claude/login'), '브라우저에서 Claude 구독 계정으로 로그인하세요');

// ── 1. 네이버 로그인 ─────────────────────
$('btn-login').onclick = () => act(() => api('/api/login'), '브라우저 창에서 네이버에 로그인하세요');
$('btn-verify').onclick = () => act(() => api('/api/login/verify'), '세션을 확인하는 중...');
$('btn-logout').onclick = () => {
  if (!confirm('저장된 네이버 로그인 세션을 지울까요? 다음에 다시 로그인해야 합니다.')) return;
  act(async () => renderSession(await api('/api/logout')), '세션을 지웠습니다');
};
$('btn-blog-id').onclick = () => act(() => api('/api/blog-id', { body: { blogId: $('s-blog-id').value } }), '블로그 아이디를 저장했습니다');

// ── 2. 글쓰기 요청 ───────────────────────
function renderPhotos() {
  const thumbs = $('photo-thumbs');
  thumbs.innerHTML = '';
  state.photos.forEach((f) => {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(f);
    thumbs.appendChild(img);
  });
  $('photo-mode').classList.toggle('hidden', !state.photos.length);
}
$('req-photos').onchange = (e) => {
  state.photos = state.photos.concat(Array.from(e.target.files || []));
  e.target.value = '';
  renderPhotos();
};
$('btn-photo-clear').onclick = () => { state.photos = []; renderPhotos(); };

async function submit(dry) {
  const text = $('req-text').value.trim();
  if (!text && !state.photos.length) return toast('글 주제나 지시를 입력해 주세요');
  const form = new FormData();
  form.append('text', text);
  form.append('dry', dry ? '1' : '');
  form.append('photoMode', document.querySelector('input[name=photoMode]:checked').value);
  state.photos.forEach((f) => form.append('photos', f, f.name));
  await act(async () => {
    await api('/api/jobs', { form });
    $('req-text').value = '';
    state.photos = [];
    renderPhotos();
  }, dry ? '미리보기 작업을 넣었습니다' : '작업을 넣었습니다. 아래 5번에서 진행 상황을 볼 수 있어요');
}
$('btn-submit').onclick = () => submit(false);
$('btn-preview').onclick = () => submit(true);
$('btn-req-clear').onclick = () => { $('req-text').value = ''; };
$('req-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit(false);
});

// 자주 쓰는 요청 버튼
function renderPresets(items) {
  const bar = $('preset-bar');
  if (!items || !items.length) {
    bar.innerHTML = '<span class="preset-empty">⭐ 자주 쓰는 요청을 버튼으로 저장해 두면 여기에 나타납니다.</span>';
    return;
  }
  bar.innerHTML = items.map((p) => `
    <span class="preset" title="${esc(p.text)}">
      <button class="use" data-id="${esc(p.id)}">${esc(p.name)}</button>
      <button class="del" data-del="${esc(p.id)}" title="버튼 지우기">✕</button>
    </span>`).join('');
  bar.querySelectorAll('.use').forEach((b) => {
    b.onclick = () => {
      const p = items.find((x) => x.id === b.dataset.id);
      $('req-text').value = p.text;
      $('req-text').focus();
    };
  });
  bar.querySelectorAll('.del').forEach((b) => {
    b.onclick = () => {
      if (!confirm('이 버튼을 지울까요?')) return;
      act(async () => renderPresets(await api('/api/presets/' + b.dataset.del, { method: 'DELETE' })));
    };
  });
}
$('btn-preset-save').onclick = () => {
  const text = $('req-text').value.trim();
  if (!text) return toast('먼저 요청 내용을 입력하세요');
  const name = prompt('버튼 이름을 정해 주세요', text.split('\n')[0].slice(0, 20));
  if (name === null) return;
  act(async () => renderPresets(await api('/api/presets', { body: { name, text } })), '버튼으로 저장했습니다');
};

// ── 3. 휴대폰 연결 ───────────────────────
$('btn-token').onclick = () => act(async () => {
  const r = await api('/api/telegram/token', { body: { token: $('s-token').value } });
  $('s-token').value = '';
  toast(`✅ 봇 확인: @${r.username}. 이제 [휴대폰 연결] 을 누르세요`, 4000);
});
$('btn-connect').onclick = () => act(() => api('/api/telegram/connect'), '휴대폰에서 내 봇에게 아무 메시지나 보내세요');
$('btn-bot-start').onclick = () => act(async () => renderBot(await api('/api/bot/start')), '봇을 켰습니다');
$('btn-bot-stop').onclick = () => act(async () => renderBot(await api('/api/bot/stop')), '봇을 껐습니다');
$('btn-forget').onclick = () => {
  if (!confirm('연결된 휴대폰을 지울까요? 다시 [휴대폰 연결] 을 해야 합니다.')) return;
  act(() => api('/api/telegram/forget'), '지웠습니다');
};

// ── 5. 작업 목록 ─────────────────────────
const STATUS = { queued: '대기', running: '진행 중', done: '완료', failed: '실패', canceled: '취소' };

function fileUrl(p) { return '/api/file?path=' + encodeURIComponent(p); }
function hhmm(ts) {
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function jobRow(j) {
  const req = (j.text || '').split('\n')[0].replace(/^\/(test|dry)\s*/i, '');
  const full = (j.text || '').replace(/^\/(test|dry)\s*/i, '').trim();
  const lines = full ? full.split('\n').length : 0;
  const more = full.length > req.length ? ` <span class="job-more">(요청 전체 ${full.length.toLocaleString()}자${lines > 1 ? ', ' + lines + '줄' : ''})</span>` : '';
  const photos = j.photos ? ` <span class="job-more">📷 ${j.photos}장</span>` : '';
  const title = j.title ? `<div class="job-title">${esc(j.title)}</div><div class="job-req" title="${esc(full)}">요청: ${esc(req)}${more}${photos}</div>`
    : `<div class="job-title" title="${esc(full)}">${esc(req) || '(사진만)'}${more}${photos}</div>`;
  const msg = j.status === 'running' || j.status === 'queued' ? `<div class="job-msg">${esc(j.message)}</div>` : '';
  const err = j.error ? `<div class="job-warn">${esc(j.error)}</div>` : '';
  const warn = (j.warnings || []).map((w) => `<div class="job-warn">⚠️ ${esc(w)}</div>`).join('');
  const login = j.needs_login ? '<button class="btn small primary" data-login="1">네이버 로그인</button>' : '';
  const actions = [
    `<button class="btn small ghost" data-view="${j.id}">${state.open.has(j.id) ? '접기' : '보기'}</button>`,
    j.status === 'failed' || j.status === 'canceled' || j.status === 'done' ? `<button class="btn small" data-retry="${j.id}">다시</button>` : '',
    j.status !== 'running' ? `<button class="btn small ghost danger" data-del="${j.id}">${j.status === 'queued' ? '취소' : '삭제'}</button>` : '',
    login,
  ].join(' ');
  const imgs = j.images && j.images.length ? `${j.images.length}장` : '-';
  let html = `<tr id="job-${j.id}">
    <td class="src">${hhmm(j.created)}</td>
    <td class="src">${j.source === 'phone' ? '📱 휴대폰' : '💻 PC'}${j.dry_run ? '<br>미리보기' : ''}</td>
    <td><span class="status ${j.status}">${STATUS[j.status] || j.status}</span></td>
    <td class="msg">${title}${msg}${err}${warn}</td>
    <td class="src">${esc(j.category || '-')}</td>
    <td class="src" title="${esc((j.image_sources || []).join(', '))}">${imgs}</td>
    <td class="src">${j.chars ? j.chars.toLocaleString() : '-'}</td>
    <td style="white-space:nowrap">${actions}</td>
  </tr>`;
  if (state.open.has(j.id)) {
    const images = (j.images || []).map((p, i) => `<a href="${fileUrl(p)}" target="_blank" title="${esc((j.image_sources || [])[i] || '')}"><img src="${fileUrl(p)}"></a>`).join('');
    const shot = j.screenshot ? `<a class="post-link" href="${fileUrl(j.screenshot)}" target="_blank">에디터 화면 캡처 보기</a>` : '';
    const tags = j.tags && j.tags.length ? `<div class="job-msg">태그: ${esc(j.tags.join(', '))}</div>` : '';
    const request = `<details class="job-request"${j.preview ? '' : ' open'}><summary>보낸 요청 전체 보기 (${full.length.toLocaleString()}자)</summary><pre>${esc(full)}</pre></details>`;
    html += `<tr class="job-detail"><td colspan="8">${request}
      <div class="job-images">${images}</div>${shot}${tags}<pre>${esc(j.preview || '')}</pre></td></tr>`;
  }
  return html;
}

function renderJobs() {
  const body = $('job-body');
  const jobs = state.jobs;
  if (!jobs.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">아직 작업이 없습니다. 2번 칸이나 휴대폰으로 요청을 보내 보세요.</td></tr>';
  } else {
    body.innerHTML = jobs.map(jobRow).join('');
  }
  const active = jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
  const done = jobs.filter((j) => j.status === 'done').length;
  const failed = jobs.filter((j) => j.status === 'failed').length;
  $('job-stats').textContent = `진행/대기 ${active} · 완료 ${done} · 실패 ${failed}`;
  pill($('pill-jobs'), active ? 'warn' : '', active ? `작업 ${active}건 진행 중` : `작업 ${jobs.length}건`);
}

$('job-body').onclick = (e) => {
  const t = e.target.closest('button');
  if (!t) return;
  if (t.dataset.view) {
    state.open.has(t.dataset.view) ? state.open.delete(t.dataset.view) : state.open.add(t.dataset.view);
    renderJobs();
  } else if (t.dataset.retry) {
    act(() => api(`/api/jobs/${t.dataset.retry}/retry`), '다시 넣었습니다');
  } else if (t.dataset.del) {
    act(() => api('/api/jobs/' + t.dataset.del, { method: 'DELETE' }));
  } else if (t.dataset.login) {
    $('btn-login').click();
  }
};
$('btn-clear-jobs').onclick = () => act(() => api('/api/jobs/clear'), '끝난 작업을 정리했습니다');

// ── 로그 ─────────────────────────────────
function addLog(item) {
  const c = $('console');
  const div = document.createElement('div');
  const level = item.level === 'warning' ? 'warn' : item.level;
  div.className = 'line ' + (level || 'info');
  div.innerHTML = `<span class="ts">${esc(item.ts)}</span>${esc(item.msg)}`;
  const atBottom = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
  c.appendChild(div);
  while (c.childNodes.length > 400) c.removeChild(c.firstChild);
  if (atBottom) c.scrollTop = c.scrollHeight;
}
$('btn-clear-log').onclick = () => { $('console').innerHTML = ''; };

// ── 전체 상태 + 실시간 소식 ────────────────
function applyState(s) {
  renderSession(s.session);
  renderBot(s.bot);
  renderClaude(s.claude);
  renderSettings(s.settings);
  renderPresets(s.presets);
  renderBusy(s.busy);
  renderConnect(s.connect);
  if (s.blogId) $('s-blog-id').value = s.blogId;
  state.jobs = s.jobs || [];
  renderJobs();
}

async function load() {
  const s = await api('/api/state', { method: 'GET' });
  applyState(s);
  $('console').innerHTML = '';
  (s.log || []).forEach(addLog);
}

function listen() {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === 'log') addLog(m.data);
    else if (m.type === 'job') {
      const i = state.jobs.findIndex((j) => j.id === m.data.id);
      if (i >= 0) state.jobs[i] = m.data; else state.jobs.unshift(m.data);
      renderJobs();
    } else if (m.type === 'jobs') { state.jobs = m.data; renderJobs(); }
    else if (m.type === 'session') renderSession(m.data);
    else if (m.type === 'busy') renderBusy(m.data);
    else if (m.type === 'state') applyState(m.data);
  };
  es.onerror = () => {
    pill($('pill-jobs'), 'bad', '프로그램 꺼짐?');
    es.close();
    setTimeout(() => load().then(listen).catch(listen), 3000);
  };
}

load().then(listen).catch((e) => { toast('❌ ' + e.message, 5000); listen(); });
// 봇·Claude 상태는 가끔 새로고침
setInterval(() => api('/api/state', { method: 'GET' }).then((s) => {
  renderBot(s.bot); renderClaude(s.claude); renderSession(s.session); renderConnect(s.connect);
}).catch(() => {}), 15000);
