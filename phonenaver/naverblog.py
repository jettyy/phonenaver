"""네이버 블로그 주소(블로그 홈·카테고리) → 그 블로그의 글 목록.

글 한 편 주소(blog.naver.com/아이디/글번호)는 여기서 다루지 않는다 — 그건 일반 링크처럼 한 편만 읽는다.
목록은 세 가지 방법을 차례로 시도한다: 모바일 목록 API → PC 목록 API → RSS (최근 글만, 카테고리 구분 없음).
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx

log = logging.getLogger(__name__)

BLOG_HOSTS = ("blog.naver.com", "m.blog.naver.com", "www.blog.naver.com")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
PAGE_SIZE = 30
MAX_PAGES = 200  # 한 블로그에서 최대 6,000편까지
UA = ("Mozilla/5.0 (Linux; Android 14; SM-S928N) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36")


@dataclass
class BlogTarget:
    blog_id: str
    category: int = 0  # 0 = 전체


@dataclass
class BlogPostRef:
    blog_id: str
    log_no: str
    title: str = ""
    timestamp: float | None = None

    @property
    def url(self) -> str:
        return f"https://blog.naver.com/{self.blog_id}/{self.log_no}"


def blog_target(url: str) -> BlogTarget | None:
    """블로그 홈·글 목록·카테고리 주소면 (아이디, 카테고리 번호), 글 한 편 주소나 다른 주소면 None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower().split(":")[0]
    qs = parse_qs(parsed.query)
    if qs.get("logNo"):
        return None

    def cat() -> int:
        raw = (qs.get("categoryNo") or ["0"])[0]
        return int(raw) if raw.isdigit() else 0

    if host.endswith(".blog.me"):  # 예전 주소 아이디.blog.me
        blog_id = host[: -len(".blog.me")]
        path = parsed.path.strip("/")
        return BlogTarget(blog_id, cat()) if ID_RE.match(blog_id) and not path.isdigit() else None
    if host not in BLOG_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        blog_id = (qs.get("blogId") or [""])[0]
        return BlogTarget(blog_id, cat()) if ID_RE.match(blog_id) else None
    first = parts[0]
    if first.lower().startswith(("postlist.", "prologue.", "blogmain.")) or first.lower() == "postlist":
        blog_id = (qs.get("blogId") or [""])[0]
        return BlogTarget(blog_id, cat()) if ID_RE.match(blog_id) else None
    if "." in first or not ID_RE.match(first):
        return None  # PostView.naver 등
    if len(parts) >= 2 and parts[1].isdigit():
        return None  # 글 한 편
    return BlogTarget(first, cat())


def is_blog_list(url: str) -> bool:
    return blog_target(url) is not None


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=20,
                        headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _korean_date(text: str, now: float) -> float | None:
    """PC 목록의 날짜 표기: '2024. 1. 5.' / '3시간 전' / '25분 전' / '2일 전'."""
    text = (text or "").strip()
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", text)
    if m:
        from datetime import datetime
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()
    m = re.search(r"(\d+)\s*(분|시간|일)\s*전", text)
    if m:
        unit = {"분": 60, "시간": 3600, "일": 86400}[m.group(2)]
        return now - int(m.group(1)) * unit
    if "방금" in text:
        return now
    return None


def _mobile_pages(client: httpx.Client, t: BlogTarget):
    """m.blog.naver.com 목록 API (JSON). 날짜는 밀리초."""
    for page in range(1, MAX_PAGES + 1):
        r = client.get(f"https://m.blog.naver.com/api/blogs/{t.blog_id}/post-list",
                       params={"categoryNo": t.category, "itemCount": PAGE_SIZE, "page": page},
                       headers={"Referer": f"https://m.blog.naver.com/{t.blog_id}"})
        r.raise_for_status()
        data = r.json()
        result = data.get("result") or {}
        items = result.get("items") or []
        posts = []
        for it in items:
            log_no = str(it.get("logNo") or "")
            if not log_no.isdigit():
                continue
            ts = it.get("addDate")
            ts = float(ts) / 1000 if isinstance(ts, (int, float)) and ts > 1e11 else (float(ts) if ts else None)
            posts.append(BlogPostRef(t.blog_id, log_no, _plain(it.get("titleWithInnerHtml") or it.get("title") or ""), ts))
        if not posts:
            return
        yield posts
        if len(items) < PAGE_SIZE:
            return


def _pc_pages(client: httpx.Client, t: BlogTarget):
    """blog.naver.com PostTitleListAsync (JSON 비슷한 글, 제목은 URL 인코딩)."""
    now = time.time()
    for page in range(1, MAX_PAGES + 1):
        r = client.get("https://blog.naver.com/PostTitleListAsync.naver",
                       params={"blogId": t.blog_id, "viewdate": "", "currentPage": page,
                               "categoryNo": t.category, "parentCategoryNo": "", "countPerPage": PAGE_SIZE},
                       headers={"Referer": f"https://blog.naver.com/{t.blog_id}"})
        r.raise_for_status()
        data = json.loads(r.text.replace("\\'", "'"))
        items = data.get("postList") or []
        posts = [BlogPostRef(t.blog_id, str(it.get("logNo")), _plain(unquote_plus(it.get("title") or "")),
                             _korean_date(it.get("addDate") or "", now))
                 for it in items if str(it.get("logNo") or "").isdigit()]
        if not posts:
            return
        yield posts
        if len(items) < PAGE_SIZE:
            return


def _rss(client: httpx.Client, t: BlogTarget) -> tuple[str, list[BlogPostRef]]:
    """RSS: 최근 글 몇십 편만 나오고 카테고리 구분은 안 된다. 블로그 이름도 여기서 얻는다."""
    import xml.etree.ElementTree as ET

    r = client.get(f"https://rss.blog.naver.com/{t.blog_id}.xml")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.find("channel")
    name = _plain(channel.findtext("title", default="")) if channel is not None else ""
    out = []
    for item in root.iter("item"):
        link = item.findtext("link", default="")
        m = re.search(r"/(\d{6,})", link)
        if not m:
            continue
        ts = None
        try:
            ts = parsedate_to_datetime(item.findtext("pubDate", default="")).timestamp()
        except (TypeError, ValueError):
            pass
        out.append(BlogPostRef(t.blog_id, m.group(1), _plain(item.findtext("title", default="")), ts))
    return name, out


def list_blog_posts(url: str, months: int | None = 6, limit: int | None = None,
                    client: httpx.Client | None = None) -> tuple[str, list[BlogPostRef], str]:
    """(블로그 이름, 글 목록 - 최신순, 참고 메모). months=None 이면 기간 제한 없이 전부."""
    t = blog_target(url)
    if t is None:
        raise ValueError("네이버 블로그 주소가 아닙니다")
    own = client is None
    client = client or _client()
    cutoff = time.time() - months * 30.44 * 86400 if months else None
    note = ""
    try:
        name = ""
        try:
            name, rss_posts = _rss(client, t)
        except Exception as exc:
            log.info("블로그 RSS 실패: %s", exc)
            rss_posts = []

        posts: list[BlogPostRef] = []
        for source in (_mobile_pages, _pc_pages):
            posts = []
            try:
                for batch in source(client, t):
                    posts += batch
                    if limit and len(posts) >= limit:
                        break
                    # 최신순이라 이번 묶음의 가장 오래된 글이 기간 밖이면 더 볼 필요가 없다
                    olds = [p.timestamp for p in batch if p.timestamp]
                    if cutoff and olds and min(olds) < cutoff:
                        break
            except Exception as exc:
                log.info("블로그 목록(%s) 실패: %s", source.__name__, exc)
            if posts:
                break
        if not posts:
            posts = rss_posts
            if posts:
                note = "전체 목록을 못 가져와 RSS 로 최근 글만 가져왔습니다"
                if t.category:
                    note += " (카테고리 구분 없이)"
        if not posts:
            raise RuntimeError("블로그 글 목록을 가져오지 못했습니다 (비공개 블로그이거나 주소가 잘못됐을 수 있음)")
    finally:
        if own:
            client.close()

    seen, unique = set(), []
    for p in posts:
        if p.log_no not in seen:
            seen.add(p.log_no)
            unique.append(p)
    unique.sort(key=lambda p: -(p.timestamp or 0))
    if cutoff:
        # 날짜를 모르는 글은 넣는다 (빼면 몰래 사라지는 글이 생김)
        unique = [p for p in unique if p.timestamp is None or p.timestamp >= cutoff]
    if limit:
        unique = unique[:limit]
    return name or t.blog_id, unique, note
