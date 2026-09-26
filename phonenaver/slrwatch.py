"""SLR클럽 중고장터(팝니다) 새 글 감시: 제목에 내 키워드가 나오면 텔레그램으로 링크를 보낸다.

- 목록 페이지(기본: 회원장터 > 팝니다)를 주기적으로 읽어서 지난번 확인 이후 올라온 글만 본다.
- 키워드 규칙 (대소문자·띄어쓰기 무시)
    소니 a7m5        → 두 단어가 모두 제목에 있을 때
    a7m5|a7v         → 둘 중 하나라도 있을 때
    라이카 -배터리     → '라이카'가 있고 '배터리'는 없을 때
- 키워드와 마지막으로 확인한 글 번호는 data/slr_watch.json 에 저장된다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from .config import Config

log = logging.getLogger(__name__)

SITE = "https://www.slrclub.com"
DEFAULT_BOARD_URL = f"{SITE}/bbs/zboard.php?id=used_market&category=1"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
MAX_PAGES = 3  # 확인 간격 사이에 글이 많이 올라왔으면 뒤 페이지까지 (최대)
KEEP_NOTIFIED = 500
NO_RE = re.compile(r"[?&]no=(\d+)")


@dataclass
class Post:
    no: int
    title: str
    url: str
    author: str = ""
    date: str = ""


# ── 키워드 ────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def matches(title: str, keyword: str) -> bool:
    t = _norm(title)
    include, exclude = [], []
    for word in keyword.split():
        if word.startswith("-") and len(word) > 1:
            exclude.append(word[1:])
        else:
            include.append(word)
    if not include:
        return False

    def has(word: str) -> bool:
        return any(_norm(alt) in t for alt in word.split("|") if alt)

    return all(has(w) for w in include) and not any(has(w) for w in exclude)


def matched_keywords(title: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if matches(title, kw)]


def split_keywords(text: str) -> list[str]:
    """'/watch a7m5, 라이카 q2' 처럼 쉼표·줄바꿈으로 여러 개를 한 번에."""
    return [" ".join(part.split()) for part in re.split(r"[,\n]", text) if part.strip()]


# ── 목록 페이지 해석 ────────────────────────────────────────

def board_id(board_url: str) -> str:
    return parse_qs(urlparse(board_url).query).get("id", ["used_market"])[0]


def post_url(board: str, no: int) -> str:
    return f"{SITE}/bbs/vx2.php?id={board}&no={no}"


def page_url(board_url: str, page: int) -> str:
    parsed = urlparse(board_url)
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if page > 1:
        qs["page"] = str(page)
    else:
        qs.pop("page", None)
    return urlunparse(parsed._replace(query=urlencode(qs)))


def _text(tag) -> str:
    return tag.get_text(" ", strip=True) if tag else ""


def parse_list(html: str | bytes, board: str = "used_market") -> list[Post]:
    """목록 HTML 에서 글(공지 제외)을 뽑는다. 번호가 큰(최신) 글부터."""
    soup = BeautifulSoup(html, "html.parser")
    posts: dict[int, Post] = {}
    table = soup.find(id="bbs_list")
    for tr in table.find_all("tr") if table else []:
        sbj = tr.find("td", class_="sbj")
        link = sbj.find("a", href=True) if sbj else None
        if not link:
            continue
        num = _text(tr.find("td", class_="list_num"))
        m = NO_RE.search(link["href"])
        if num and not num.isdigit():  # 공지
            continue
        no = int(num) if num else (int(m.group(1)) if m else 0)
        if not no:
            continue
        posts[no] = Post(
            no=no,
            title=_text(link),
            url=post_url(board, no),
            author=_text(tr.find("td", class_=re.compile("name"))),
            date=_text(tr.find("td", class_="list_date")),
        )

    if not posts:  # 사이트 구조가 바뀌었을 때를 대비: 이 게시판 글 링크를 모두 찾는다
        for link in soup.find_all("a", href=NO_RE):
            href = link["href"]
            if "vx2.php" not in href or f"id={board}" not in href:
                continue
            title = _text(link)
            no = int(NO_RE.search(href).group(1))
            if title and no not in posts:
                posts[no] = Post(no=no, title=title, url=post_url(board, no))
    return sorted(posts.values(), key=lambda p: p.no, reverse=True)


# ── 저장 (키워드, 마지막 확인 번호) ───────────────────────────

class WatchStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("keywords", [])
        data.setdefault("last_no", 0)
        data.setdefault("notified", [])
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def keywords(self) -> list[str]:
        return list(self.load()["keywords"])

    def add(self, keywords: list[str]) -> list[str]:
        data = self.load()
        have = {_norm(k) for k in data["keywords"]}
        added = []
        for kw in keywords:
            if kw and _norm(kw) not in have:
                data["keywords"].append(kw)
                have.add(_norm(kw))
                added.append(kw)
        self.save(data)
        return added

    def remove(self, target: str) -> str | None:
        """키워드 문구 또는 목록 번호(1부터)로 삭제."""
        data = self.load()
        kws = data["keywords"]
        target = " ".join(target.split())
        idx = None
        if target.isdigit() and 1 <= int(target) <= len(kws):
            idx = int(target) - 1
        else:
            for i, kw in enumerate(kws):
                if _norm(kw) == _norm(target):
                    idx = i
                    break
        if idx is None:
            return None
        removed = kws.pop(idx)
        self.save(data)
        return removed

    def reset_baseline(self) -> None:
        """키워드가 없어 쉬는 동안은 기준을 지워서, 다시 등록했을 때 예전 글로 알림이 몰리지 않게."""
        data = self.load()
        if data["last_no"]:
            data["last_no"] = 0
            self.save(data)

    def clear(self) -> int:
        data = self.load()
        n = len(data["keywords"])
        data["keywords"] = []
        self.save(data)
        return n


# ── 감시 ──────────────────────────────────────────────────

class SlrError(RuntimeError):
    pass


class SlrWatcher:
    def __init__(self, cfg: Config, store: WatchStore | None = None):
        self.cfg = cfg
        self.board_url = cfg.slr_board_url or DEFAULT_BOARD_URL
        self.board = board_id(self.board_url)
        self.store = store or WatchStore(cfg.slr_watch_file)
        self._client: httpx.AsyncClient | None = None
        self._logged_in = False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9", "Referer": SITE + "/"},
                follow_redirects=True,
                timeout=20,
            )
        return self._client

    async def login(self) -> bool:
        """SLR_ID/SLR_PW 가 있으면 로그인 (목록이 안 보일 때만 사용)."""
        if not (self.cfg.slr_id and self.cfg.slr_pw):
            return False
        c = self.client()
        resp = await c.post(f"{SITE}/login/auth.php", data={"user_id": self.cfg.slr_id, "password": self.cfg.slr_pw})
        code = BeautifulSoup(resp.content, "html.parser").find("input", {"name": "code"})
        if not code or not code.get("value"):
            raise SlrError("SLR클럽 로그인 실패 (.env 의 SLR_ID / SLR_PW 확인)")
        await c.post(f"{SITE}/login/login_center.php", data={"code": code["value"]})
        self._logged_in = True
        return True

    async def fetch_page(self, page: int = 1) -> list[Post]:
        resp = await self.client().get(page_url(self.board_url, page))
        resp.raise_for_status()
        posts = parse_list(resp.content, self.board)
        if not posts and not self._logged_in and await self.login():
            resp = await self.client().get(page_url(self.board_url, page))
            resp.raise_for_status()
            posts = parse_list(resp.content, self.board)
        if not posts and page == 1:
            raise SlrError("SLR클럽 장터 목록에서 글을 찾지 못했습니다 (로그인이 필요하거나 사이트 화면이 바뀜)")
        return posts

    async def snapshot(self) -> list[Post]:
        """지금 첫 페이지 글. 처음이면 여기까지를 '이미 본 글'로 표시한다 (키워드 추가 직후 확인용)."""
        posts = await self.fetch_page(1)
        data = self.store.load()
        if not data["last_no"] and posts:
            data["last_no"] = posts[0].no
            self.store.save(data)
        return posts

    async def poll(self) -> list[tuple[Post, list[str]]]:
        """지난 확인 이후 새로 올라온 글 중 키워드에 맞는 것 (오래된 글부터)."""
        last = self.store.load()["last_no"]
        posts = await self.fetch_page(1)
        page = 1
        while last and posts and posts[-1].no > last and page < MAX_PAGES:
            page += 1
            more = await self.fetch_page(page)
            if not more:
                break
            posts += more

        # 여기부터는 await 없이 한 번에 읽고 저장 (그사이 키워드가 바뀌어도 안전)
        data = self.store.load()
        last = data["last_no"]
        notified = set(data["notified"])
        hits = []
        if last:  # 처음 실행 때는 기준만 잡고 알림 없음 (예전 글 폭탄 방지)
            for post in sorted({p.no: p for p in posts}.values(), key=lambda p: p.no):
                if post.no <= last or post.no in notified:
                    continue
                kws = matched_keywords(post.title, data["keywords"])
                if kws:
                    hits.append((post, kws))
                    notified.add(post.no)
        if posts:
            data["last_no"] = max(last, max(p.no for p in posts))
        data["notified"] = sorted(notified)[-KEEP_NOTIFIED:]
        self.store.save(data)
        return hits


def format_hit(post: Post, keywords: list[str]) -> str:
    lines = [f"🔔 SLR 장터 새 글  [{', '.join(keywords)}]", post.title]
    meta = " · ".join(x for x in (post.author, post.date) if x)
    if meta:
        lines.append(meta)
    lines.append(post.url)
    return "\n".join(lines)
