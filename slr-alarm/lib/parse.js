// SLR클럽 목록 페이지 해석과 키워드 판정. (네트워크 없이 도는 순수 함수만 모아 둔다)
import * as cheerio from 'cheerio';

export const SITE = 'https://www.slrclub.com';
export const DEFAULT_BOARD_URL = `${SITE}/bbs/zboard.php?id=used_market&category=1`;
const NO_RE = /[?&]no=(\d+)/;

// ── 키워드 ────────────────────────────────────────────────
// 대소문자·띄어쓰기는 무시한다.
//   소니 a7m5     → 두 단어가 모두 들어간 제목
//   a7m5|a7v      → 둘 중 하나라도 들어간 제목
//   라이카 -배터리  → '라이카'는 있고 '배터리'는 없는 제목

export function norm(text) {
  return String(text || '').replace(/\s+/g, '').toLowerCase();
}

export function matches(title, keyword) {
  const t = norm(title);
  const include = [];
  const exclude = [];
  for (const word of String(keyword || '').split(/\s+/).filter(Boolean)) {
    if (word.startsWith('-') && word.length > 1) exclude.push(word.slice(1));
    else include.push(word);
  }
  if (!include.length) return false;
  const has = (word) => word.split('|').some((alt) => alt && t.includes(norm(alt)));
  return include.every(has) && !exclude.some(has);
}

export function matchedKeywords(title, keywords) {
  return keywords.filter((kw) => matches(title, kw));
}

// "a7m5, 라이카 q2" 또는 여러 줄 붙여넣기 → 키워드 여러 개
export function splitKeywords(text) {
  return String(text || '')
    .split(/[,\n]/)
    .map((part) => part.split(/\s+/).filter(Boolean).join(' '))
    .filter(Boolean);
}

// ── 주소 ──────────────────────────────────────────────────

export function boardId(boardUrl) {
  try {
    return new URL(boardUrl).searchParams.get('id') || 'used_market';
  } catch {
    return 'used_market';
  }
}

export function postUrl(board, no) {
  return `${SITE}/bbs/vx2.php?id=${board}&no=${no}`;
}

export function pageUrl(boardUrl, page) {
  const url = new URL(boardUrl);
  if (page > 1) url.searchParams.set('page', String(page));
  else url.searchParams.delete('page');
  return url.toString();
}

// ── 목록 HTML ─────────────────────────────────────────────

// 응답 바이트를 문자열로. 옛 게시판이라 EUC-KR 일 수도 있어서 헤더와 <meta> 를 모두 본다.
export function decodeHtml(buffer, contentType = '') {
  const bytes = Buffer.from(buffer);
  let charset = /charset=([\w-]+)/i.exec(contentType)?.[1];
  if (!charset) {
    const head = bytes.subarray(0, 4096).toString('latin1');
    charset = /<meta[^>]+charset=["']?([\w-]+)/i.exec(head)?.[1];
  }
  charset = (charset || 'utf-8').toLowerCase();
  if (charset === 'ks_c_5601-1987' || charset === 'cp949') charset = 'euc-kr';
  try {
    return new TextDecoder(charset).decode(bytes);
  } catch {
    return new TextDecoder('utf-8').decode(bytes);
  }
}

const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();

// 글 목록(공지 제외)을 최신 글부터 돌려준다.
export function parseList(html, board = 'used_market') {
  const $ = cheerio.load(html);
  const posts = new Map();

  $('#bbs_list tr').each((_, tr) => {
    const row = $(tr);
    const link = row.find('td.sbj a[href]').first();
    if (!link.length) return;
    const num = clean(row.find('td.list_num').first().text());
    if (num && !/^\d+$/.test(num)) return; // 공지
    const fromHref = NO_RE.exec(link.attr('href'))?.[1];
    const no = Number(num || fromHref || 0);
    if (!no) return;
    posts.set(no, {
      no,
      title: clean(link.text()),
      url: postUrl(board, no),
      author: clean(row.find('td[class*="name"]').first().text()),
      date: clean(row.find('td.list_date').first().text()),
    });
  });

  // 사이트 화면이 바뀌었을 때를 대비: 이 게시판 글로 가는 링크를 전부 줍는다
  if (!posts.size) {
    $('a[href]').each((_, a) => {
      const href = $(a).attr('href');
      const m = NO_RE.exec(href);
      if (!m || !href.includes(`id=${board}`)) return;
      const title = clean($(a).text());
      const no = Number(m[1]);
      if (title && !posts.has(no)) posts.set(no, { no, title, url: postUrl(board, no), author: '', date: '' });
    });
  }
  return [...posts.values()].sort((a, b) => b.no - a.no);
}
