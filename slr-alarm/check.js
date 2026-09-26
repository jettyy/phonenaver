// 자체 점검: npm run check
// SLR클럽에 접속하지 않고 가짜 목록으로 해석 · 키워드 · 새 글 판별 · 대시보드 API 를 한 번씩 돌려 본다.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  DEFAULT_BOARD_URL, decodeHtml, matches, pageUrl, parseList, postUrl, splitKeywords,
} from './lib/parse.js';
import { Store } from './lib/store.js';
import { hitText } from './lib/telegram.js';
import { Watcher } from './lib/watcher.js';
import { createApp } from './server.js';

let passed = 0;
async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ✅ ${name}`);
  } catch (err) {
    console.error(`  ❌ ${name}\n${err.stack}`);
    process.exitCode = 1;
  }
}

const row = (no, title) => `<tr>
  <td class="list_num">${no}</td>
  <td class="sbj"><a href="/bbs/vx2.php?id=used_market&page=1&divpage=1000&category=1&no=${no}">${title}</a> <span>[1]</span></td>
  <td class="list_name"><span>작성자${no}</span></td>
  <td class="list_date">10:24:20</td><td class="list_vote">0</td><td class="list_click">15</td></tr>`;
const notice = `<tr><td class="list_num">공지</td><td class="sbj"><a href="/bbs/vx2.php?id=help&no=31">회원장터 이용 유의사항</a></td>
  <td class="list_name">SLR</td><td class="list_date">2013/09/03</td></tr>`;
const listHtml = (rows) => `<html><body><table id="bbs_list"><tbody>${notice}${rows.map(([n, t]) => row(n, t)).join('')}</tbody></table></body></html>`;
const post = (no, title) => ({ no, title, url: postUrl('used_market', no), author: '', date: '' });
const tmpFile = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'slr-')), 'state.json');

console.log('목록 해석');
await test('공지는 빼고 최신 글부터, 링크는 글 번호로', () => {
  const posts = parseList(listHtml([[10028144, '후지 gfx100rf 팝니다'], [10028145, '라이카 니켈엘마']]));
  assert.deepEqual(posts.map((p) => p.no), [10028145, 10028144]);
  assert.equal(posts[0].title, '라이카 니켈엘마');
  assert.equal(posts[0].url, 'https://www.slrclub.com/bbs/vx2.php?id=used_market&no=10028145');
  assert.equal(posts[0].author, '작성자10028145');
  assert.equal(posts[0].date, '10:24:20');
});
await test('표가 없어도 글 링크로 찾음 (화면이 바뀌었을 때)', () => {
  const posts = parseList('<a href="/bbs/vx2.php?id=used_market&no=77">소니 a7c2</a><a href="/bbs/vx2.php?id=help&no=31">공지</a>');
  assert.deepEqual(posts.map((p) => [p.no, p.title]), [[77, '소니 a7c2']]);
});
await test('EUC-KR 페이지도 한글이 깨지지 않음', () => {
  const eucKr = Buffer.from([0xb4, 0xcf, 0xc4, 0xdc]); // "니콘"
  const html = Buffer.concat([Buffer.from('<meta charset="euc-kr"><table id="bbs_list"><tr><td class="list_num">5</td><td class="sbj"><a href="?no=5">'), eucKr, Buffer.from(' z5</a></td></tr></table>')]);
  assert.equal(parseList(decodeHtml(html))[0].title, '니콘 z5');
});
await test('페이지 주소', () => {
  const u = pageUrl(DEFAULT_BOARD_URL, 2);
  assert.ok(u.includes('id=used_market') && u.includes('category=1') && u.includes('page=2'));
  assert.ok(!pageUrl(u, 1).includes('page='));
});

console.log('키워드');
await test('띄어쓰기·대소문자 무시, | 는 또는, - 는 빼기', () => {
  const t = '[아산]소니 A7M5 + 2870gm , DJI rs4mini combo';
  assert.ok(matches(t, 'a7m5'));
  assert.ok(matches(t, '소니 A7 M5'));
  assert.ok(matches(t, 'a7v|a7m5'));
  assert.ok(!matches(t, '소니 a7c2'));
  assert.ok(!matches(t, 'a7m5 -dji'));
  assert.ok(!matches(t, '-dji'));
  assert.ok(matches('니콘 z2470 24-70s f2.8', '24-70'));
});
await test('쉼표·줄바꿈으로 여러 개', () => {
  assert.deepEqual(splitKeywords(' 소니  a7m5, 라이카 q2\nx100v ,'), ['소니 a7m5', '라이카 q2', 'x100v']);
});
await test('텔레그램 알림 문구', () => {
  assert.equal(hitText({ title: '소니 A7M5', url: 'https://x/1', author: '200F8', date: '09:55:53' }, ['a7m5']),
    '🔔 SLR 장터 새 글  [a7m5]\n소니 A7M5\n200F8 · 09:55:53\nhttps://x/1');
});

console.log('새 글 판별');
await test('처음엔 기준만 잡고, 그 뒤 올라온 글만 한 번씩 알림', async () => {
  const store = new Store(tmpFile());
  store.addKeywords(['a7m5']);
  let pages = { 1: [post(10, '소니 a7m5'), post(9, '캐논')] };
  const w = new Watcher(store, { fetchPage: async (p) => pages[p] || [] });
  assert.deepEqual(await w.poll(), []);
  assert.equal(store.data.lastNo, 10);
  pages = { 1: [post(12, '캐논 r5'), post(11, 'A7M5 미개봉'), post(10, '소니 a7m5')] };
  const hits = await w.poll();
  assert.deepEqual(hits.map((h) => [h.post.no, h.keywords]), [[11, ['a7m5']]]);
  assert.deepEqual(await w.poll(), []);
  assert.equal(new Store(store.file).data.hits[0].no, 11); // 파일에 저장됨
});
await test('글이 많이 올라왔으면 뒤 페이지까지 봄', async () => {
  const store = new Store(tmpFile());
  store.addKeywords(['라이카']);
  store.data.lastNo = 5;
  const fetched = [];
  const pages = { 1: [post(9, '니콘'), post(8, '캐논')], 2: [post(7, '라이카 q2'), post(6, '소니')], 3: [post(5, '라이카 옛글')] };
  const w = new Watcher(store, { fetchPage: async (p) => { fetched.push(p); return pages[p] || []; } });
  const hits = await w.poll();
  assert.deepEqual(fetched, [1, 2, 3]);
  assert.deepEqual(hits.map((h) => h.post.no), [7]);
  assert.equal(store.data.lastNo, 9);
});
await test('꺼 둔 키워드는 알리지 않음, 쉬는 동안은 기준을 지움', async () => {
  const store = new Store(tmpFile());
  store.addKeywords(['a7m5', '라이카']);
  store.toggleKeyword('a7m5');
  store.data.lastNo = 1;
  const w = new Watcher(store, { fetchPage: async () => [post(3, '소니 a7m5'), post(2, '라이카 m6')] });
  const hits = await w.poll();
  assert.deepEqual(hits.map((h) => h.post.no), [2]);
  store.toggleKeyword('라이카');
  await w.tick();
  assert.equal(store.data.lastNo, 0);
});

await test('목록이 안 보이면 받은 화면을 남기고 로그인을 안내', async () => {
  const file = tmpFile();
  const debugFile = path.join(path.dirname(file), 'last.html');
  const w = new Watcher(new Store(file), { debugFile });
  w.http.request = async () => '<html><body><form><input type="password"></form>로그인 후 이용하세요</body></html>';
  await assert.rejects(() => w.fetchPage(1), /로그인/);
  assert.ok(w.status.needLogin);
  assert.ok(fs.readFileSync(debugFile, 'utf8').includes('로그인 후'));
});
await test('단순 요청으로 안 보이면 크롬으로 읽음', async () => {
  const w = new Watcher(new Store(tmpFile()), {
    browser: { html: async () => listHtml([[30, '라이카 Q3 팝니다']]) },
  });
  w.http.request = async () => '<html><body>빈 화면</body></html>';
  const posts = await w.fetchPage(1);
  assert.equal(w.mode, 'browser');
  assert.deepEqual(posts.map((p) => p.title), ['라이카 Q3 팝니다']);
});

console.log('대시보드 API');
await test('키워드 추가 · 끄기 · 삭제 · 상태', async () => {
  const store = new Store(tmpFile());
  const watcher = new Watcher(store, { fetchPage: async () => [post(20, '소니 a7m5 팝니다')] });
  const server = createApp({ store, watcher });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://localhost:${server.address().port}`;
  const post_ = (p, body) => fetch(base + p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }).then((r) => r.json());
  try {
    assert.deepEqual((await post_('/api/keywords', { text: 'a7m5, 라이카' })).added, ['a7m5', '라이카']);
    await post_('/api/check', {});
    let st = await fetch(`${base}/api/state`).then((r) => r.json());
    assert.equal(st.keywords.length, 2);
    assert.deepEqual(st.posts[0].keywords, ['a7m5']);
    assert.equal(st.status.lastNo, 20);
    await post_('/api/keywords/toggle', { text: '라이카' });
    await post_('/api/keywords/delete', { text: 'a7m5' });
    st = await fetch(`${base}/api/state`).then((r) => r.json());
    assert.deepEqual(st.keywords.map((k) => [k.text, k.on]), [['라이카', false]]);
    await post_('/api/settings', { telegramToken: '123:SECRETTOKEN', telegramChatId: '42' });
    st = await fetch(`${base}/api/state`).then((r) => r.json());
    assert.equal(st.settings.telegramTokenHint, '…OKEN');
    assert.ok(!JSON.stringify(st).includes('SECRETTOKEN')); // 토큰은 화면으로 안 내보냄
    const html = await fetch(base).then((r) => r.text());
    assert.ok(html.includes('SLR 장터 알림'));
    const bad = await fetch(`${base}/api/keywords`, { method: 'POST', body: 'text=x', headers: { 'content-type': 'application/x-www-form-urlencoded' } });
    assert.equal(bad.status, 415); // 다른 사이트가 몰래 보내는 폼 요청 차단
  } finally {
    watcher.stop();
    server.close();
  }
});

console.log(process.exitCode ? '\n❌ 실패한 항목이 있습니다' : `\n✅ ${passed}개 모두 통과`);
