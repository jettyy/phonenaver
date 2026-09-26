// 실제 브라우저(이 컴퓨터에 깔린 크롬)로 장터 목록을 읽는다.
// 단순 요청으로는 목록이 안 보일 때(로그인 필요, 자동 접속 차단, 스크립트로 그리는 화면) 쓰는 길.
// 로그인은 사용자가 [SLR클럽 로그인 창 열기] 로 뜬 창에서 직접 하고, 세션은 data/slr-browser/ 에 남는다.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright-core';

const LOGIN_TIMEOUT_MS = 10 * 60 * 1000;

export class Browser {
  constructor(profileDir) {
    this.profileDir = profileDir;
    this.context = null;
    this.page = null;
    this.loginOpen = false;
    this.queue = Promise.resolve();
  }

  // 크롬 한 개를 여러 곳에서 동시에 만지지 않도록 한 번에 하나씩
  exclusive(fn) {
    const run = this.queue.then(fn, fn);
    this.queue = run.catch(() => {});
    return run;
  }

  async launch(headless) {
    fs.mkdirSync(this.profileDir, { recursive: true });
    const base = { headless, viewport: { width: 1280, height: 900 }, locale: 'ko-KR' };
    const tries = process.env.SLR_BROWSER_PATH
      ? [{ executablePath: process.env.SLR_BROWSER_PATH }]
      : [{ channel: 'chrome' }, { channel: 'msedge' }, {}];
    let lastErr;
    for (const opt of tries) {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          return await chromium.launchPersistentContext(this.profileDir, { ...base, ...opt });
        } catch (err) {
          lastErr = err;
          // 지난번에 띄운 크롬이 아직 이 프로필을 쥐고 있다 → 그 크롬을 끄고 한 번 더
          if (attempt === 0 && isProfileBusy(err)) {
            console.log('ℹ️ 지난번에 띄운 크롬이 남아 있어서 끄고 다시 엽니다.');
            await this.releaseProfile();
            continue;
          }
          break;
        }
      }
      if (isProfileBusy(lastErr) || !isMissingBrowser(lastErr)) break; // 크롬은 있는데 다른 이유로 실패
    }
    const reason = String(lastErr?.message || '').split('\n')[0];
    if (isProfileBusy(lastErr)) {
      throw new Error('크롬 로그인 프로필(data/slr-browser)을 다른 크롬이 쓰고 있습니다. '
        + 'SLR클럽 로그인 창이 열려 있으면 닫고, 그래도 안 되면 크롬을 완전히 종료(⌘Q)한 뒤 다시 시도하세요');
    }
    if (isMissingBrowser(lastErr)) throw new Error(`크롬을 찾지 못했습니다. Google Chrome 을 설치해 주세요. (${reason})`);
    throw new Error(`크롬을 띄우지 못했습니다: ${reason}`);
  }

  // 이 프로필 폴더로 떠 있는 크롬(지난번 실행이 남긴 것)을 끄고, 남은 잠금 파일을 지운다.
  // 사용자가 평소 쓰는 크롬은 다른 프로필이라 건드리지 않는다.
  async releaseProfile() {
    const dir = path.resolve(this.profileDir);
    if (process.platform !== 'win32') {
      try {
        const out = execFileSync('ps', ['-ax', '-o', 'pid=,command='], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
        for (const line of out.split('\n')) {
          const m = /^\s*(\d+)\s+(.*)$/.exec(line);
          if (!m || Number(m[1]) === process.pid) continue;
          const cmd = m[2];
          if (cmd.includes(`--user-data-dir=${dir} `) || cmd.endsWith(`--user-data-dir=${dir}`)) {
            try { process.kill(Number(m[1]), 'SIGTERM'); } catch { /* 이미 꺼짐 */ }
          }
        }
      } catch {
        // ps 를 못 쓰면 잠금 파일만 지운다
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    for (const name of ['SingletonLock', 'SingletonSocket', 'SingletonCookie']) {
      try { fs.rmSync(path.join(dir, name), { force: true }); } catch { /* 없음 */ }
    }
  }

  html(url) {
    return this.exclusive(() => this.read(url));
  }

  async read(url) {
    if (this.loginOpen) throw new Error('SLR클럽 로그인 창이 열려 있습니다. 로그인하고 창을 닫아 주세요');
    if (!this.context) {
      this.context = await this.launch(true);
      this.context.on('close', () => {
        this.context = null;
        this.page = null;
      });
    }
    if (!this.page || this.page.isClosed()) this.page = await this.context.newPage();
    try {
      await this.page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    } catch (err) {
      const reason = String(err.message || '').split('\n')[0].replace(/^page\.goto:\s*/, '').replace(/ at https?:\S+$/, '');
      throw new Error(`크롬으로 SLR클럽을 열지 못했습니다 (인터넷 연결 확인): ${reason}`);
    }
    // 목록을 스크립트로 그리는 경우를 위해 글 링크가 나타날 때까지 잠깐 기다린다
    await this.page.waitForSelector('a[href*="no="]', { timeout: 8000 }).catch(() => {});
    return this.page.content();
  }

  async close() {
    const ctx = this.context;
    this.context = null;
    this.page = null;
    if (ctx) await ctx.close().catch(() => {});
  }

  // 대시보드를 끌 때: 로그인 창까지 모두 닫는다 (남아 있으면 다음 실행 때 프로필이 잠겨 있다)
  async closeAll() {
    await this.close();
    if (this.loginContext) await this.loginContext.close().catch(() => {});
  }

  // 사용자가 직접 로그인할 창을 띄운다. 창을 닫으면 onClosed 가 불린다.
  openLogin(url, onClosed) {
    return this.exclusive(() => this.showLogin(url, onClosed));
  }

  async showLogin(url, onClosed) {
    if (this.loginOpen) return;
    await this.close();
    const ctx = await this.launch(false);
    this.loginContext = ctx;
    this.loginOpen = true;
    const done = () => {
      if (!this.loginOpen) return;
      this.loginOpen = false;
      this.loginContext = null;
      clearTimeout(timer);
      onClosed?.();
    };
    const timer = setTimeout(() => ctx.close().catch(() => {}), LOGIN_TIMEOUT_MS);
    ctx.on('close', done);
    const page = ctx.pages()[0] || await ctx.newPage();
    // 마지막 탭을 닫으면 창을 닫은 것으로 본다 (맥은 탭을 닫아도 앱이 남아 있다)
    ctx.on('page', (p) => p.on('close', () => { if (!ctx.pages().length) ctx.close().catch(() => {}); }));
    page.on('close', () => { if (!ctx.pages().length) ctx.close().catch(() => {}); });
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
  }
}

function isProfileBusy(err) {
  return /ProcessSingleton|SingletonLock|profile (is|appears to be) in use|user data directory is already in use/i.test(String(err?.message || ''));
}

function isMissingBrowser(err) {
  return /is not found|Executable doesn't exist|not installed|ENOENT|Failed to launch.*(no such file|not found)/i.test(String(err?.message || ''));
}
