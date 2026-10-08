"""유튜브 링크 → 영상 제목·채널·자막(대사)을 가져온다.

자막은 한국어 → 영어 → 그 밖의 언어 순서로 찾고, 사람이 단 자막이 없으면 자동 생성 자막을 쓴다.
(자막이 꺼져 있는 영상은 대사를 가져올 수 없어서 제목·설명만 쓴다)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
PREFERRED = ["ko", "ko-KR", "en", "en-US", "en-GB"]
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(url: str) -> str | None:
    """유튜브 주소에서 영상 ID 를 꺼낸다 (watch, youtu.be, shorts, live, embed 모두)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if host not in YOUTUBE_HOSTS:
        return None
    if host.endswith("youtu.be"):
        vid = parsed.path.strip("/").split("/")[0]
    else:
        vid = parse_qs(parsed.query).get("v", [""])[0]
        if not vid:
            m = re.match(r"^/(?:shorts|live|embed|v)/([^/?#]+)", parsed.path)
            vid = m.group(1) if m else ""
    return vid if ID_RE.match(vid or "") else None


def is_youtube(url: str) -> bool:
    return video_id(url) is not None


@dataclass
class Video:
    id: str
    title: str = ""
    channel: str = ""
    description: str = ""
    transcript: str = ""
    language: str = ""
    auto_generated: bool = False
    error: str = ""
    blocked: bool = False  # 유튜브가 잠시 막은 경우 (시간이 지나면 풀림)


class TranscriptUnavailable(RuntimeError):
    """대사(자막)를 못 가져옴 — 추측해서 쓰지 않도록 글쓰기를 멈춘다."""

    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


BLOCK_ERRORS = ("RequestBlocked", "IpBlocked", "TooManyRequests", "YouTubeRequestFailed", "PoTokenRequired")
MIN_GAP_SECONDS = 20  # 자막 요청 사이 최소 간격 (연달아 요청하면 막힘)
_last_request = 0.0
_gap_lock = __import__("threading").Lock()


def _wait_turn() -> None:
    """채널 영상처럼 연달아 요청할 때 간격을 둔다."""
    import time

    global _last_request
    with _gap_lock:
        wait = _last_request + MIN_GAP_SECONDS - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()


def _meta(vid: str) -> tuple[str, str, str]:
    """(제목, 채널, 설명). oEmbed 는 로그인 없이 되는 공개 주소."""
    title = channel = description = ""
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers={"Accept-Language": "ko-KR,ko;q=0.9"}) as c:
            r = c.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
            if r.status_code == 200:
                data = r.json()
                title, channel = data.get("title", ""), data.get("author_name", "")
            page = c.get(f"https://www.youtube.com/watch?v={vid}")
            m = re.search(r'"shortDescription":("(?:[^"\\]|\\.)*")', page.text)
            if m:
                description = json.loads(m.group(1))  # 자바스크립트 문자열 그대로라 JSON 으로 풀면 한글도 정확
    except Exception as exc:
        log.info("유튜브 정보 가져오기 실패: %s", exc)
    return title, channel, description[:3000]


def _transcript(vid: str) -> tuple[str, str, bool, str, bool]:
    """(자막 전체, 언어, 자동 생성 여부, 오류, 막힘 여부) — 빠른 방법(자막 API)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return "", "", False, "youtube-transcript-api 가 설치되지 않았습니다 (npm install 을 다시 실행하세요)", False

    _wait_turn()
    api = YouTubeTranscriptApi()
    try:
        listing = api.list(vid)
        # 한국어(사람) → 한국어(자동) → 영어(사람) → 영어(자동) 순서. 한국어 블로그라 한국어 자막을 먼저 쓴다
        chosen = None
        for lang in PREFERRED:
            for finder in (listing.find_manually_created_transcript, listing.find_generated_transcript):
                try:
                    chosen = finder([lang])
                    break
                except Exception:
                    continue
            if chosen is not None:
                break
        if chosen is None:  # 한국어·영어가 없으면 있는 자막 아무거나
            chosen = next(iter(listing), None)
        if chosen is None:
            return "", "", False, "이 영상에는 자막이 없습니다", False
        fetched = chosen.fetch()
        lines = [s.text.replace("\n", " ").strip() for s in fetched]
        text = "\n".join(t for t in lines if t and t not in ("[음악]", "[Music]", "[박수]", "[Applause]"))
        return text, chosen.language_code, bool(chosen.is_generated), "", False
    except Exception as exc:  # 자막 꺼짐, 비공개, 연령 제한, 요청 차단 등
        name = type(exc).__name__
        reason = {
            "TranscriptsDisabled": "이 영상은 자막이 꺼져 있습니다",
            "NoTranscriptFound": "이 영상에는 자막이 없습니다",
            "VideoUnavailable": "영상을 볼 수 없습니다 (삭제·비공개)",
            "AgeRestricted": "연령 제한 영상이라 자막을 가져올 수 없습니다",
            "RequestBlocked": "유튜브가 요청을 막았습니다. 잠시 뒤 다시 시도하세요",
            "IpBlocked": "유튜브가 이 인터넷 주소의 요청을 막았습니다. 잠시 뒤 다시 시도하세요",
        }.get(name, f"자막을 가져오지 못했습니다 ({name})")
        return "", "", False, reason, name in BLOCK_ERRORS or "Proxy" in name or "Connect" in name


def fetch_video(url: str) -> Video:
    vid = video_id(url)
    if not vid:
        return Video(id="", error="유튜브 주소가 아닙니다")
    video = Video(id=vid)
    video.title, video.channel, video.description = _meta(vid)
    video.transcript, video.language, video.auto_generated, video.error, video.blocked = _transcript(vid)
    return video


# ── 실제 브라우저로 '스크립트 표시' 열어서 대사 읽기 ──────────────────

def _segments_from_json(data) -> list[str]:
    """youtubei get_transcript 응답에서 자막 줄을 모두 찾는다 (응답 모양이 바뀌어도 버티도록 재귀 탐색)."""
    out: list[str] = []

    def text_of(node) -> str:
        if isinstance(node, dict):
            if "simpleText" in node:
                return node["simpleText"]
            if "runs" in node:
                return "".join(r.get("text", "") for r in node["runs"])
            if "content" in node and isinstance(node["content"], str):
                return node["content"]
        return ""

    def walk(node) -> None:
        if isinstance(node, dict):
            seg = node.get("transcriptSegmentRenderer")
            if isinstance(seg, dict):
                t = text_of(seg.get("snippet", {})).strip()
                if t:
                    out.append(t)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


SHOW_TRANSCRIPT = re.compile(r"스크립트 표시|스크립트 보기|Show transcript", re.I)
NOISE = {"[음악]", "[Music]", "[박수]", "[Applause]", "[웃음]", "[Laughter]"}
TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
PANEL_UI_WORDS = {"스크립트", "Transcript", "검색", "Search", "챕터", "Chapters", "더보기", "닫기", "Close", "한국어", "English",
                  "한국어 (자동 생성됨)", "English (auto-generated)", "타임스탬프 전환", "Toggle timestamps"}


def _segments_from_timedtext(body: str) -> list[str]:
    """플레이어가 받아 오는 자막 파일 (json3 또는 XML) → 줄 목록."""
    import html as htmlmod

    body = body.strip()
    if not body:
        return []
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            return []
        lines = []
        for ev in data.get("events", []):
            t = "".join(seg.get("utf8", "") for seg in ev.get("segs", []) or []).replace("\n", " ").strip()
            if t:
                lines.append(t)
        return lines
    texts = re.findall(r"<(?:text|p)\b[^>]*>(.*?)</(?:text|p)>", body, re.S)
    return [htmlmod.unescape(re.sub(r"<[^>]+>", "", t)).replace("\n", " ").strip() for t in texts if t.strip()]


def _clean_lines(lines: list[str]) -> list[str]:
    out = []
    for t in lines:
        t = t.strip()
        if not t or t in NOISE or TIMESTAMP_RE.match(t) or t in PANEL_UI_WORDS:
            continue
        if out and out[-1] == t:  # 자동 자막의 겹친 줄
            continue
        out.append(t)
    return out


async def _panel_text(page) -> list[str]:
    """열린 '스크립트' 패널의 글자를 통째로 읽어 자막 줄만 남긴다 (화면 구조가 바뀌어도 버티도록)."""
    js = r"""() => {
      const panels = [...document.querySelectorAll('ytd-engagement-panel-section-list-renderer')]
        .filter(p => /transcript|script/i.test(p.getAttribute('target-id') || '') ||
                     /스크립트|transcript/i.test((p.querySelector('#title-text, #header') || {}).innerText || ''));
      const segs = [...document.querySelectorAll(
        'ytd-transcript-segment-renderer, transcript-segment-view-model, [class*="segment-text"]')];
      if (segs.length) return segs.map(s => s.innerText);
      return panels.map(p => p.innerText);
    }"""
    try:
        raw = await page.evaluate(js)
    except Exception:
        return []
    lines: list[str] = []
    for block in raw or []:
        lines += [ln for ln in str(block).splitlines()]
    return _clean_lines(lines)


async def transcript_via_browser(session, vid: str, timeout_s: int = 40) -> tuple[str, str]:
    """(대사 전체, 오류). 사람이 영상 페이지를 보는 것과 똑같이 한다.

    ① [스크립트 표시] 패널을 열어 유튜브가 받아 오는 대사 데이터를 가로채거나 패널 글자를 읽고
    ② 안 되면 영상을 소리 끈 채 재생하고 자막(CC)을 켜서, 플레이어가 받아 오는 자막 파일을 가로챈다.
    못 읽으면 data/debug/ 에 화면 캡처와 기록을 남긴다.
    """
    import asyncio

    await asyncio.to_thread(_wait_turn)
    from_api: list[str] = []
    from_player: list[str] = []
    seen_urls: list[str] = []
    async with session.lock():
        ctx = await session.context()
        page = await ctx.new_page()

        async def on_response(resp) -> None:
            url = resp.url
            if "get_transcript" in url or "timedtext" in url:
                seen_urls.append(f"{resp.status} {url[:160]}")
            try:
                if "get_transcript" in url:
                    from_api.extend(_segments_from_json(await resp.json()))
                elif "/api/timedtext" in url and resp.status == 200:
                    lines = _segments_from_timedtext(await resp.text())
                    if len(lines) > len(from_player):
                        from_player[:] = lines
            except Exception:
                pass

        page.on("response", on_response)
        loop = asyncio.get_running_loop()

        async def wait_for(check, seconds: float) -> bool:
            end = loop.time() + seconds
            while loop.time() < end:
                if check():
                    return True
                await page.wait_for_timeout(700)
            return check()

        try:
            await page.goto(f"https://www.youtube.com/watch?v={vid}", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3500)
            for sel in ("button[aria-label*='동의'], button[aria-label*='Accept']",):  # 쿠키 동의 창이 뜨는 지역 대비
                try:
                    btn = page.locator(sel).first
                    if await btn.count() and await btn.is_visible():
                        await btn.click()
                except Exception:
                    pass

            # ① 스크립트 패널
            for sel in ("#description-inline-expander #expand", "tp-yt-paper-button#expand", "#expand"):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click()
                        break
                except Exception:
                    continue
            await page.wait_for_timeout(1000)
            opened = False
            for loc in (
                page.locator("ytd-video-description-transcript-section-renderer button").first,
                page.get_by_role("button", name=SHOW_TRANSCRIPT).first,
            ):
                try:
                    if await loc.count():
                        await loc.click()
                        opened = True
                        break
                except Exception:
                    continue
            panel: list[str] = []
            if opened:
                async def panel_ready() -> None:
                    panel[:] = await _panel_text(page)

                end = loop.time() + min(timeout_s, 20)
                while loop.time() < end and not from_api:
                    await page.wait_for_timeout(1000)
                    await panel_ready()
                    if len(panel) >= 5:
                        await page.wait_for_timeout(1500)  # 나머지 줄이 다 그려질 때까지
                        await panel_ready()
                        break
            lines = _clean_lines(from_api) or panel
            if len(lines) >= 3:
                return "\n".join(lines), ""

            # ② 영상을 소리 끄고 재생 + 자막(CC) 켜기 → 플레이어가 받는 자막 파일
            try:
                await page.evaluate("""() => { const v = document.querySelector('video');
                                         if (v) { v.muted = true; v.play().catch(() => {}); } }""")
                player = page.locator("#movie_player, .html5-video-player").first
                if await player.count():
                    await player.hover()
                cc = page.locator(".ytp-subtitles-button").first
                if await cc.count() and (await cc.get_attribute("aria-pressed")) != "true":
                    await cc.click()
                else:
                    await page.keyboard.press("c")
            except Exception:
                pass
            await wait_for(lambda: len(from_player) >= 3, min(timeout_s, 25))
            lines = _clean_lines(from_player)
            if len(lines) >= 3:
                return "\n".join(lines), ""

            debug = await _save_debug(session, page, vid, opened, seen_urls)
            if not opened and not seen_urls:
                return "", f"이 영상에는 스크립트(자막)가 없는 것 같습니다 (확인용 화면: {debug})"
            return "", f"스크립트를 열었지만 대사를 읽지 못했습니다 (확인용 화면: {debug})"
        except Exception as exc:
            return "", f"브라우저로 대사를 읽지 못했습니다 ({str(exc).splitlines()[0][:120]})"
        finally:
            await page.close()


async def _save_debug(session, page, vid: str, opened: bool, seen_urls: list[str]) -> str:
    """못 읽었을 때 원인을 찾을 수 있게 화면과 기록을 남긴다."""
    import time

    folder = session.data_dir / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"youtube-{vid}-{time.strftime('%m%d-%H%M%S')}"
    try:
        await page.screenshot(path=str(stem) + ".png", full_page=False)
    except Exception:
        pass
    try:
        panels = await page.evaluate("""() => [...document.querySelectorAll('ytd-engagement-panel-section-list-renderer')]
            .map(p => (p.getAttribute('target-id') || '') + ' | ' + (p.getAttribute('visibility') || '') + '\\n' +
                      p.innerText.slice(0, 1500)).join('\\n----\\n')""")
    except Exception:
        panels = ""
    info = [f"video: {vid}", f"url: {page.url}", f"transcript button clicked: {opened}",
            "caption responses:", *seen_urls, "", "panels:", panels or "(none)"]
    Path(str(stem) + ".txt").write_text("\n".join(info), encoding="utf-8")
    return str(stem) + ".png"


# ── 채널: 최근 영상 목록 ─────────────────────────────────────

CHANNEL_PATH_RE = re.compile(r"^/(@[^/?#]+|channel/UC[\w-]{20,}|c/[^/?#]+|user/[^/?#]+)")


def channel_url(url: str) -> str | None:
    """유튜브 채널 주소면 그 채널의 '동영상' 탭 주소를, 아니면 None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if host not in YOUTUBE_HOSTS or host.endswith("youtu.be") or video_id(url):
        return None
    m = CHANNEL_PATH_RE.match(parsed.path)
    return f"https://www.youtube.com/{m.group(1)}/videos" if m else None


def is_channel(url: str) -> bool:
    return channel_url(url) is not None


@dataclass
class ChannelVideo:
    id: str
    title: str
    timestamp: float | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


def _rss_videos(channel_id: str) -> list[ChannelVideo]:
    """채널 RSS (최근 15개, 날짜 정확). yt-dlp 가 날짜를 못 줄 때 쓴다."""
    import xml.etree.ElementTree as ET
    from datetime import datetime

    r = httpx.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, timeout=20)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for entry in ET.fromstring(r.content).findall("a:entry", ns):
        vid = entry.findtext("yt:videoId", default="", namespaces=ns)
        published = entry.findtext("a:published", default="", namespaces=ns)
        ts = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp() if published else None
        out.append(ChannelVideo(vid, entry.findtext("a:title", default="", namespaces=ns), ts))
    return out


def _upload_time(vid: str) -> float | None:
    """영상 페이지의 올린 날짜 (uploadDate / publishDate)."""
    from datetime import datetime

    try:
        r = httpx.get(f"https://www.youtube.com/watch?v={vid}", timeout=15, follow_redirects=True,
                      headers={"Accept-Language": "ko-KR,ko;q=0.9"})
        m = re.search(r'"(?:uploadDate|publishDate)"\s*:\s*"(\d{4}-\d{2}-\d{2}[^"]*)"', r.text) or \
            re.search(r'itemprop="(?:uploadDate|datePublished)"\s+content="(\d{4}-\d{2}-\d{2}[^"]*)"', r.text)
        if not m:
            return None
        raw = m.group(1)
        return (datetime.fromisoformat(raw) if "T" in raw else datetime.fromisoformat(raw[:10])).timestamp()
    except Exception:
        return None


def list_channel_videos(url: str, months: int = 6, limit: int | None = None) -> tuple[str, list[ChannelVideo]]:
    """(채널 이름, 최근 months 개월 영상 목록 - 최신순). 쇼츠·예정된 라이브는 뺀다."""
    import time

    import yt_dlp

    tab = channel_url(url)
    if not tab:
        raise ValueError("유튜브 채널 주소가 아닙니다")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": max(limit or 0, 400),
        # 목록에서 '3개월 전' 같은 표시로 대략의 날짜를 계산하게 한다
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(tab, download=False) or {}
    name = info.get("channel") or info.get("uploader") or info.get("title") or ""
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    entries = [e for e in entries if e.get("live_status") not in ("is_upcoming", "is_live")]

    cutoff = time.time() - months * 30.44 * 86400
    videos = [ChannelVideo(e["id"], e.get("title") or "", e.get("timestamp")) for e in entries]
    if videos and not any(v.timestamp for v in videos):
        # 목록에 날짜가 안 나오면 영상마다 올린 날짜를 직접 확인 (RSS 는 최근 15개뿐이라 6개월치가 안 됨)
        log.info("영상 날짜를 하나씩 확인합니다 (최근 %.1f개월치)", months)
        for v in videos:
            v.timestamp = _upload_time(v.id)
            if v.timestamp and v.timestamp < cutoff:
                break
        if not any(v.timestamp for v in videos) and info.get("channel_id"):
            log.info("영상 날짜를 알 수 없어 RSS(최근 15개)로 대신합니다")
            videos = _rss_videos(info["channel_id"])
    recent = []
    for v in videos:  # 최신순이라 기준보다 오래된 영상이 나오면 멈춘다
        if v.timestamp and v.timestamp < cutoff:
            break
        recent.append(v)
    if limit:
        recent = recent[:limit]
    return name, recent
