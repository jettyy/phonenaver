"""유튜브 링크 → 영상 제목·채널·자막(대사)을 가져온다.

자막은 한국어 → 영어 → 그 밖의 언어 순서로 찾고, 사람이 단 자막이 없으면 자동 생성 자막을 쓴다.
(자막이 꺼져 있는 영상은 대사를 가져올 수 없어서 제목·설명만 쓴다)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
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


def _transcript(vid: str) -> tuple[str, str, bool, str]:
    """(자막 전체, 언어, 자동 생성 여부, 오류)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return "", "", False, "youtube-transcript-api 가 설치되지 않았습니다 (npm install 을 다시 실행하세요)"

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
            return "", "", False, "이 영상에는 자막이 없습니다"
        fetched = chosen.fetch()
        lines = [s.text.replace("\n", " ").strip() for s in fetched]
        text = "\n".join(t for t in lines if t and t not in ("[음악]", "[Music]", "[박수]", "[Applause]"))
        return text, chosen.language_code, bool(chosen.is_generated), ""
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
        return "", "", False, reason


def fetch_video(url: str) -> Video:
    vid = video_id(url)
    if not vid:
        return Video(id="", error="유튜브 주소가 아닙니다")
    video = Video(id=vid)
    video.title, video.channel, video.description = _meta(vid)
    video.transcript, video.language, video.auto_generated, video.error = _transcript(vid)
    return video
