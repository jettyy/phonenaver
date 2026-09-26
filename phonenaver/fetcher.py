"""링크를 열어서 본문 텍스트를 뽑아낸다. (네이버 블로그 iframe 대응 포함)"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

MAX_CHARS = 20000
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SM-S928N) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"
)


@dataclass
class Page:
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    error: str = ""
    truncated: bool = False
    kind: str = "web"  # "web" | "youtube"
    warning: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text)


def normalize_url(url: str) -> str:
    """PC 네이버 블로그 주소는 본문이 iframe 안에 있어서 모바일 주소로 바꾼다."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in ("blog.naver.com", "www.blog.naver.com"):
        qs = parse_qs(parsed.query)
        if "blogId" in qs and "logNo" in qs:
            return f"https://m.blog.naver.com/{qs['blogId'][0]}/{qs['logNo'][0]}"
        m = re.match(r"^/([A-Za-z0-9_-]+)/(\d+)", parsed.path)
        if m:
            return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    if host == "cafe.naver.com":
        return url.replace("://cafe.naver.com", "://m.cafe.naver.com", 1)
    return url


def _title(soup: BeautifulSoup) -> str:
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return tag["content"].strip()
    return soup.title.get_text(strip=True) if soup.title else ""


def extract(html: str, url: str = "") -> tuple[str, str]:
    """(제목, 본문) 반환."""
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    text = trafilatura.extract(
        html, url=url or None, include_comments=False, include_tables=True, favor_recall=True
    ) or ""
    if len(text) < 200:
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()
        main = soup.select_one(".se-main-container, #postViewArea, article, main") or soup.body or soup
        fallback = re.sub(r"\n\s*\n+", "\n\n", main.get_text("\n", strip=True))
        if len(fallback) > len(text):
            text = fallback
    return title, text.strip()


YOUTUBE_MAX_CHARS = 60000  # 긴 영상 자막도 최대한 전부


def fetch_youtube(url: str) -> Page:
    """유튜브: 영상 제목·채널·설명과 자막(대사) 전체."""
    from .youtube import fetch_video

    video = fetch_video(url)
    page = Page(url=url, final_url=f"https://www.youtube.com/watch?v={video.id}", title=video.title, kind="youtube")
    head = [f"[유튜브 영상] {video.title}".strip()]
    if video.channel:
        head.append(f"채널: {video.channel}")
    if video.description:
        head.append(f"영상 설명:\n{video.description}")
    if video.transcript:
        auto = " (자동 생성 자막이라 틀린 글자가 있을 수 있음)" if video.auto_generated else ""
        body = video.transcript
        if len(body) > YOUTUBE_MAX_CHARS:
            body, page.truncated = body[:YOUTUBE_MAX_CHARS], True
        head.append(f"영상 대사(자막, 언어 {video.language}){auto}:\n{body}")
        page.text = "\n\n".join(head)
    elif video.title or video.description:
        # 자막이 없으면 제목·설명만이라도 (오류 문구는 경고로 남긴다)
        page.text = "\n\n".join(head + [f"(대사를 가져오지 못함: {video.error})"])
        page.error = ""
        page.warning = f"유튜브 자막 없음 — {video.error}. 제목·설명만으로 썼습니다"
    else:
        page.error = video.error or "유튜브 영상 정보를 가져오지 못했습니다"
    return page


def fetch(url: str, timeout: float = 20.0) -> Page:
    from .youtube import is_youtube

    if is_youtube(url):
        return fetch_youtube(url)
    page = Page(url=url)
    target = normalize_url(url)
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
        ) as client:
            resp = client.get(target)
            # naver.me 같은 단축 주소는 리다이렉트 후에 다시 정규화
            final = str(resp.url)
            if normalize_url(final) != final:
                resp = client.get(normalize_url(final))
                final = str(resp.url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        page.error = f"접속 실패: {exc}"
        return page

    page.final_url = final
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        page.error = f"HTML 페이지가 아님 ({ctype or '알 수 없음'})"
        return page

    page.title, text = extract(resp.text, final)
    if len(text) > MAX_CHARS:
        text, page.truncated = text[:MAX_CHARS], True
    page.text = text
    if not text:
        page.error = "본문을 추출하지 못함 (로그인이 필요하거나 스크립트로만 그려지는 페이지일 수 있음)"
    return page
