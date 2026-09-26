// SLR 장터 알림 대시보드: npm start → http://localhost:3100
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { DEFAULT_BOARD_URL, matchedKeywords, splitKeywords } from './lib/parse.js';
import { Store } from './lib/store.js';
import * as telegram from './lib/telegram.js';
import { Watcher } from './lib/watcher.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const PUBLIC = path.join(HERE, 'public');
const DATA_FILE = process.env.SLR_DATA_FILE || path.join(ROOT, 'data', 'slr-alarm.json');
const HOST = '127.0.0.1';
const PORT = Number(process.env.PORT) || 3100;
const PORT_RETRIES = 10;

// phonenaver(텔레그램 글쓰기 봇)를 이미 설정해 두었다면 그 텔레그램 봇으로 알림을 보낸다
function envFromPhonenaver() {
  const out = {};
  try {
    for (const line of fs.readFileSync(path.join(ROOT, '.env'), 'utf8').split(/\r?\n/)) {
      const m = /^\s*([A-Z_]+)\s*=\s*(.*)$/.exec(line);
      if (m) out[m[1]] = m[2].trim();
    }
  } catch {
    // .env 가 없으면 그냥 넘어간다
  }
  return out;
}

export function createApp({ store, watcher }) {
  const clients = new Set(); // 화면이 열려 있는 브라우저 (새 글이 오면 바로 알려 줌)
  watcher.onHit((hit) => {
    for (const res of clients) res.write(`event: hit\ndata: ${JSON.stringify(hit)}\n\n`);
  });

  function state() {
    const s = store.settings;
    const active = store.activeKeywords();
    return {
      keywords: store.data.keywords.map((k) => ({
        ...k,
        hits: store.data.hits.filter((h) => h.keywords.includes(k.text)).length,
      })),
      hits: store.data.hits.slice(0, 200),
      unread: store.data.hits.filter((h) => !h.read).length,
      posts: watcher.lastPosts.map((p) => ({ ...p, keywords: matchedKeywords(p.title, active) })),
      status: { ...watcher.status, interval: watcher.interval, lastNo: store.data.lastNo },
      settings: {
        interval: watcher.interval,
        boardUrl: s.boardUrl,
        defaultBoardUrl: DEFAULT_BOARD_URL,
        paused: s.paused,
        sound: s.sound,
        telegramChatId: s.telegramChatId,
        telegramTokenSet: Boolean(s.telegramToken),
        telegramTokenHint: s.telegramToken ? `…${s.telegramToken.slice(-4)}` : '',
        slrId: s.slrId,
        slrPwSet: Boolean(s.slrPw),
      },
    };
  }

  const routes = {
    'GET /api/state': () => state(),

    'POST /api/keywords': (body) => {
      const added = store.addKeywords(splitKeywords(body.text));
      if (added.length) watcher.checkNow();
      return { added };
    },
    'POST /api/keywords/toggle': (body) => {
      const kw = store.toggleKeyword(body.text);
      if (!kw) throw httpError(404, '키워드를 찾지 못했습니다');
      if (kw.on) watcher.checkNow();
      return { keyword: kw };
    },
    'POST /api/keywords/delete': (body) => ({ removed: store.removeKeyword(body.text) }),

    'POST /api/check': async () => {
      await watcher.checkNow();
      return state();
    },
    'POST /api/pause': (body) => {
      store.updateSettings({ paused: Boolean(body.paused) });
      if (!body.paused) watcher.checkNow();
      return { paused: store.settings.paused };
    },

    'POST /api/settings': (body) => {
      const patch = {};
      if (body.interval !== undefined) patch.interval = Math.max(30, Number(body.interval) || 60);
      if (body.boardUrl !== undefined) {
        const url = String(body.boardUrl).trim();
        if (url && !/^https?:\/\/(www\.|m\.)?slrclub\.com\//.test(url)) throw httpError(400, 'SLR클럽 목록 주소를 넣어 주세요');
        patch.boardUrl = url;
      }
      if (body.sound !== undefined) patch.sound = Boolean(body.sound);
      if (body.telegramToken !== undefined) patch.telegramToken = String(body.telegramToken).trim();
      if (body.telegramChatId !== undefined) patch.telegramChatId = String(body.telegramChatId).trim();
      if (body.slrId !== undefined) patch.slrId = String(body.slrId).trim();
      if (body.slrPw !== undefined) patch.slrPw = String(body.slrPw);
      store.updateSettings(patch);
      if (patch.boardUrl !== undefined) {
        store.data.lastNo = 0; // 게시판이 바뀌면 기준을 새로 잡는다
        store.save();
      }
      if (patch.interval !== undefined || patch.boardUrl !== undefined) watcher.checkNow();
      return state().settings;
    },

    'POST /api/telegram/find-chat': async () => {
      if (!store.settings.telegramToken) throw httpError(400, '텔레그램 봇 토큰을 먼저 저장하세요');
      const chatId = await telegram.findChatId(store.settings.telegramToken);
      if (!chatId) throw httpError(404, '휴대폰에서 내 봇에게 아무 메시지나 보낸 뒤 다시 누르세요');
      store.updateSettings({ telegramChatId: chatId });
      return { chatId };
    },
    'POST /api/telegram/test': async () => {
      const s = store.settings;
      if (!s.telegramToken || !s.telegramChatId) throw httpError(400, '텔레그램 봇 토큰과 채팅 ID 를 먼저 저장하세요');
      await telegram.sendText(s, '✅ SLR 장터 알림 테스트 — 키워드에 맞는 새 글이 올라오면 여기로 링크를 보내 드려요.');
      return { ok: true };
    },

    'POST /api/hits/read': (body) => {
      store.markRead(body.no === undefined ? undefined : Number(body.no));
      return { ok: true };
    },
    'POST /api/hits/clear': () => {
      store.clearHits();
      return { ok: true };
    },
  };

  return http.createServer(async (req, res) => {
    // 이 컴퓨터에서 연 화면만 받는다 (다른 사이트가 몰래 보내는 요청 차단)
    const host = String(req.headers.host || '').replace(/:\d+$/, '');
    if (!['localhost', '127.0.0.1', '[::1]'].includes(host)) return send(res, 403, { error: 'forbidden' });

    const url = new URL(req.url, 'http://localhost');
    if (req.method === 'GET' && url.pathname === '/api/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
      res.write(': hello\n\n');
      clients.add(res);
      req.on('close', () => clients.delete(res));
      return undefined;
    }

    const route = routes[`${req.method} ${url.pathname}`];
    if (route) {
      if (req.method === 'POST' && !String(req.headers['content-type'] || '').startsWith('application/json')) {
        return send(res, 415, { error: 'JSON 으로 보내 주세요' });
      }
      try {
        const body = req.method === 'POST' ? await readJson(req) : {};
        return send(res, 200, await route(body));
      } catch (err) {
        return send(res, err.status || 500, { error: err.message });
      }
    }
    if (req.method === 'GET') return serveStatic(url.pathname, res);
    return send(res, 404, { error: 'not found' });
  });
}

function httpError(status, message) {
  return Object.assign(new Error(message), { status });
}

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(JSON.stringify(body));
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 100000) req.destroy();
    });
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        reject(httpError(400, '잘못된 요청'));
      }
    });
    req.on('error', reject);
  });
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

function serveStatic(pathname, res) {
  const file = path.join(PUBLIC, pathname === '/' ? 'index.html' : pathname);
  if (!file.startsWith(PUBLIC + path.sep) || !fs.existsSync(file)) return send(res, 404, { error: 'not found' });
  res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream', 'cache-control': 'no-cache' });
  return fs.createReadStream(file).pipe(res);
}

function openBrowser(url) {
  if (process.env.NO_OPEN) return;
  const [cmd, args] = process.platform === 'darwin' ? ['open', [url]]
    : process.platform === 'win32' ? ['cmd', ['/c', 'start', '', url]]
      : ['xdg-open', [url]];
  try {
    spawn(cmd, args, { stdio: 'ignore', detached: true }).on('error', () => {}).unref();
  } catch {
    // 브라우저를 못 열어도 주소는 창에 찍혀 있다
  }
}

function main() {
  const store = new Store(DATA_FILE);
  const s = store.settings;
  if (!s.telegramToken && !s.telegramChatId) {
    const env = envFromPhonenaver();
    const chat = (env.ALLOWED_CHAT_IDS || '').split(',')[0].trim();
    if (env.TELEGRAM_BOT_TOKEN && chat) {
      store.updateSettings({ telegramToken: env.TELEGRAM_BOT_TOKEN, telegramChatId: chat });
      console.log('📱 phonenaver 의 텔레그램 봇 설정을 가져왔습니다. 새 글 알림이 휴대폰으로도 갑니다.');
    }
  }

  const watcher = new Watcher(store);
  const server = createApp({ store, watcher });

  const listen = (port, retriesLeft) => {
    server.once('error', (err) => {
      if (err.code === 'EADDRINUSE' && retriesLeft > 0) return listen(port + 1, retriesLeft - 1);
      console.error(`❌ 대시보드를 열지 못했습니다: ${err.message}`);
      process.exit(1);
    });
    server.listen(port, HOST, () => {
      const url = `http://localhost:${port}`;
      console.log(`🔔 SLR 장터 알림 대시보드가 열렸습니다 → ${url}`);
      console.log('   이 창을 닫으면 알림이 멈춥니다. (컴퓨터가 잠자기에 들어가지 않게 해 주세요)');
      watcher.start();
      openBrowser(url);
    });
  };
  listen(PORT, PORT_RETRIES);

  watcher.onHit(({ post, keywords }) => console.log(`🔔 [${keywords.join(', ')}] ${post.title}\n   ${post.url}`));
  const bye = () => {
    watcher.stop();
    process.exit(0);
  };
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
