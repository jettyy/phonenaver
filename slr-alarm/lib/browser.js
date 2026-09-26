// 실제 브라우저(이 컴퓨터에 깔린 크롬)로 장터 목록을 읽는다.
// 단순 요청으로는 목록이 안 보일 때(로그인 필요, 자동 접속 차단, 스크립트로 그리는 화면) 쓰는 길.
// 로그인은 사용자가 [SLR클럽 로그인 창 열기] 로 뜬 창에서 직접 하고, 세션은 data/slr-browser/ 에 남는다.
import fs from 'node:fs';
import { chromium } from 'playwright-core';

const LOGIN_TIMEOUT_MS = 10 * 60 * 1000;

export class Browser {
  constructor(profileDir) {
    this.profileDir = profileDir;
    this.context = null;
    this.page = null;
    this.loginOpen = false;
  }

  async launch(headless) {
    fs.mkdirSync(this.profileDir, { recursive: true });
    const base = { headless, viewport: { width: 1280, height: 900 }, locale: 'ko-KR' };
    const tries = process.env.SLR_BROWSER_PATH
      ? [{ executablePath: process.env.SLR_BROWSER_PATH }]
      : [{ channel: 'chrome' }, { channel: 'msedge' }, {}];
    let lastErr;
    for (const opt of tries) {
      try {
        return await chromium.launchPersistentContext(this.profileDir, { ...base, ...opt });
      } catch (err) {
        lastErr = err;
      }
    }
    throw new Error(`크롬을 찾지 못했습니다. Google Chrome 을 설치해 주세요. (${String(lastErr?.message || '').split('\n')[0]})`);
  }

  async html(url) {
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

  // 사용자가 직접 로그인할 창을 띄운다. 창을 닫으면 onClosed 가 불린다.
  async openLogin(url, onClosed) {
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
