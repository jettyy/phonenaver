// 장터 목록을 주기적으로 읽어서, 지난번 확인 이후 올라온 글 중 키워드에 맞는 글을 알린다.
import fs from 'node:fs';
import path from 'node:path';
import * as cheerio from 'cheerio';
import {
  DEFAULT_BOARD_URL, SITE, boardId, decodeHtml, matchedKeywords, pageUrl, parseList,
} from './parse.js';
import * as telegram from './telegram.js';

const USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
  + '(KHTML, like Gecko) Version/18.0 Safari/605.1.15';
const MAX_PAGES = 3; // 확인 사이에 글이 많이 올라왔으면 뒤 페이지까지
const MIN_INTERVAL = 30;

// 쿠키를 기억하는 아주 작은 fetch (로그인이 필요할 때만 의미가 있다)
class Http {
  constructor() {
    this.cookies = new Map();
  }

  async request(url, { method = 'GET', form } = {}) {
    let target = url;
    let opts = { method, body: form ? new URLSearchParams(form).toString() : undefined };
    for (let hop = 0; hop < 6; hop += 1) {
      const headers = {
        'user-agent': USER_AGENT,
        'accept-language': 'ko-KR,ko;q=0.9',
        referer: `${SITE}/`,
      };
      if (opts.body) headers['content-type'] = 'application/x-www-form-urlencoded';
      if (this.cookies.size) headers.cookie = [...this.cookies].map(([k, v]) => `${k}=${v}`).join('; ');
      const res = await fetch(target, { ...opts, headers, redirect: 'manual', signal: AbortSignal.timeout(20000) });
      for (const line of res.headers.getSetCookie?.() || []) {
        const [pair] = line.split(';');
        const eq = pair.indexOf('=');
        if (eq > 0) this.cookies.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
      }
      const location = res.headers.get('location');
      if (res.status >= 300 && res.status < 400 && location) {
        target = new URL(location, target).toString();
        opts = { method: 'GET' };
        continue;
      }
      if (!res.ok) throw new Error(`SLR클럽 응답 오류 ${res.status}`);
      return decodeHtml(await res.arrayBuffer(), res.headers.get('content-type') || '');
    }
    throw new Error('SLR클럽 주소가 계속 다른 곳으로 넘어갑니다');
  }
}

export class Watcher {
  constructor(store, { fetchPage, browser, debugFile } = {}) {
    this.store = store;
    this.http = new Http();
    this.browser = browser || null; // 단순 요청으로 목록이 안 보이면 크롬으로 읽는다
    this.debugFile = debugFile || '';
    this.mode = 'http';
    this.loggedIn = false;
    if (fetchPage) this.fetchPage = fetchPage; // 점검용 가짜 목록
    this.status = { checking: false, lastCheck: null, lastError: '', nextCheck: null, fails: 0, needLogin: false };
    this.lastPosts = [];
    this.timer = null;
    this.stopped = true;
    this.listeners = new Set();
  }

  get boardUrl() {
    return this.store.settings.boardUrl || DEFAULT_BOARD_URL;
  }

  get interval() {
    return Math.max(MIN_INTERVAL, Number(this.store.settings.interval) || 60);
  }

  onHit(fn) {
    this.listeners.add(fn);
  }

  async login() {
    const { slrId, slrPw } = this.store.settings;
    if (!slrId || !slrPw) return false;
    const html = await this.http.request(`${SITE}/login/auth.php`, {
      method: 'POST', form: { user_id: slrId, password: slrPw },
    });
    const code = cheerio.load(html)('input[name="code"]').attr('value');
    if (!code) throw new Error('SLR클럽 로그인 실패 — 설정의 아이디/비밀번호를 확인하세요');
    await this.http.request(`${SITE}/login/login_center.php`, { method: 'POST', form: { code } });
    this.loggedIn = true;
    return true;
  }

  async fetchPage(page = 1) {
    const board = boardId(this.boardUrl);
    const url = pageUrl(this.boardUrl, page);
    let html = '';
    if (this.mode === 'http') {
      let posts = [];
      try {
        html = await this.http.request(url);
        posts = parseList(html, board);
        if (!posts.length && !this.loggedIn && await this.login()) {
          html = await this.http.request(url);
          posts = parseList(html, board);
        }
      } catch (err) {
        if (!this.browser) throw err;
      }
      if (posts.length || !this.browser) return this.checked(posts, html, page);
      this.mode = 'browser';
      console.log('ℹ️ 단순 요청으로는 목록이 안 보여서 크롬 브라우저로 읽습니다.');
    }
    html = await this.browser.html(url);
    return this.checked(parseList(html, board), html, page);
  }

  // 글을 못 찾았으면 받은 화면을 남겨 두고(원인 확인용) 이유를 알려 준다
  checked(posts, html, page) {
    if (posts.length || page > 1) {
      if (posts.length) this.status.needLogin = false;
      return posts;
    }
    if (this.debugFile && html) {
      try {
        fs.mkdirSync(path.dirname(this.debugFile), { recursive: true });
        fs.writeFileSync(this.debugFile, html);
      } catch {
        // 못 남겨도 괜찮다
      }
    }
    const text = cheerio.load(html || '')('body').text();
    this.status.needLogin = /로그인|login|회원/i.test(text) || /type=["']?password/i.test(html || '');
    throw new Error(this.status.needLogin
      ? '장터 목록이 안 보입니다. SLR클럽 로그인이 필요해 보여요 — 4번 칸 [SLR클럽 로그인 창 열기] 로 로그인하세요'
      : '장터 목록에서 글을 찾지 못했습니다 (사이트 화면이 바뀜)');
  }

  // 한 번 확인. 새로 찾은 [{post, keywords}] 를 돌려준다 (오래된 글부터).
  async poll() {
    const { store } = this;
    let posts = await this.fetchPage(1);
    let page = 1;
    while (store.data.lastNo && posts.length && posts.at(-1).no > store.data.lastNo && page < MAX_PAGES) {
      page += 1;
      const more = await this.fetchPage(page);
      if (!more.length) break;
      posts = posts.concat(more);
    }
    const unique = [...new Map(posts.map((p) => [p.no, p])).values()].sort((a, b) => b.no - a.no);
    this.lastPosts = unique.slice(0, 40);

    const last = store.data.lastNo;
    const keywords = store.activeKeywords();
    const notified = new Set(store.data.notified);
    const hits = [];
    this.status.lastNew = last ? unique.filter((p) => p.no > last).length : 0;
    this.status.baseline = !last;
    if (last) { // 처음 확인할 때는 기준만 잡는다 (이미 올라와 있던 글로 알림 폭탄 방지)
      for (const post of [...unique].reverse()) {
        if (post.no <= last || notified.has(post.no)) continue;
        const kws = matchedKeywords(post.title, keywords);
        if (!kws.length) continue;
        hits.push({ post, keywords: kws });
        store.addHit(post, kws);
      }
    }
    if (posts.length) store.data.lastNo = Math.max(last, ...posts.map((p) => p.no));
    store.save();
    return hits;
  }

  async notify(hits) {
    const s = this.store.settings;
    for (const hit of hits) {
      for (const fn of this.listeners) fn(hit);
      if (s.telegramToken && s.telegramChatId) {
        try {
          await telegram.sendHit(s, hit.post, hit.keywords);
        } catch (err) {
          console.warn(`텔레그램 전송 실패: ${err.message}`);
        }
      }
    }
  }

  // 한 바퀴: 일시정지나 키워드가 없으면 쉬고, 아니면 확인 → 알림
  async tick() {
    if (this.status.checking) return;
    const { store } = this;
    if (store.settings.paused || !store.activeKeywords().length) {
      // 쉬는 동안은 기준을 지워서, 다시 켰을 때 예전 글로 알림이 몰리지 않게
      if (store.data.lastNo) {
        store.data.lastNo = 0;
        store.save();
      }
      return;
    }
    this.status.checking = true;
    try {
      const hits = await this.poll();
      this.logCheck(hits);
      this.status.lastError = '';
      if (this.status.fails >= 5) await this.sendText('✅ SLR 장터 확인이 다시 정상으로 돌아왔어요.');
      this.status.fails = 0;
      await this.notify(hits);
    } catch (err) {
      this.status.fails += 1;
      if (err.message !== this.status.lastError || this.status.fails % 30 === 1) {
        console.warn(`장터 확인 실패 (${this.status.fails}번째): ${err.message}`);
      }
      this.status.lastError = err.message;
      if (this.status.fails === 5) await this.sendText(`⚠️ SLR 장터 확인이 계속 실패하고 있어요: ${err.message}`);
    } finally {
      this.status.checking = false;
      this.status.lastCheck = new Date().toISOString();
    }
  }

  // 창에 한 줄씩 남겨서 멈춘 게 아니라 계속 돌고 있다는 걸 보여 준다
  logCheck(hits) {
    const d = new Date();
    const hms = [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
    if (this.status.baseline) {
      console.log(`✓ ${hms} 장터 확인 시작 — 지금 목록(최신 글 ${this.store.data.lastNo}번)까지는 이미 본 글로 두고, 이후 새 글을 봅니다`);
      return;
    }
    const tail = hits.length ? ` → 🔔 알림 ${hits.length}건` : '';
    console.log(`✓ ${hms} 확인 — 새 글 ${this.status.lastNew}개${tail} (다음 확인 ${this.interval}초 뒤)`);
  }

  async sendText(text) {
    const s = this.store.settings;
    if (!s.telegramToken || !s.telegramChatId) return;
    try {
      await telegram.sendText(s, text);
    } catch (err) {
      console.warn(`텔레그램 전송 실패: ${err.message}`);
    }
  }

  // 지금 바로 한 번 보기 (키워드가 없어도 목록은 읽어 온다)
  async checkNow() {
    if (this.store.activeKeywords().length && !this.store.settings.paused) {
      await this.tick();
    } else {
      try {
        this.lastPosts = (await this.fetchPage(1)).slice(0, 40);
        this.status.lastError = '';
      } catch (err) {
        this.status.lastError = err.message;
      }
      this.status.lastCheck = new Date().toISOString();
    }
    this.schedule();
  }

  schedule() {
    clearTimeout(this.timer);
    if (this.stopped) return;
    const ms = this.interval * 1000;
    this.status.nextCheck = new Date(Date.now() + ms).toISOString();
    this.timer = setTimeout(async () => {
      await this.tick();
      this.schedule();
    }, ms);
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.checkNow();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
  }
}
