"""유튜브 링크 → 자막(대사) 읽기. 실제 유튜브 대신 가짜 응답으로 검사."""
from types import SimpleNamespace

import pytest

from phonenaver import fetcher, youtube
from phonenaver.command import parse


@pytest.mark.parametrize("url, vid", [
    ("https://www.youtube.com/watch?v=AbCdEfGhIjK&t=10s", "AbCdEfGhIjK"),
    ("https://youtu.be/AbCdEfGhIjK?si=xyz", "AbCdEfGhIjK"),
    ("https://m.youtube.com/shorts/AbCdEfGhIjK", "AbCdEfGhIjK"),
    ("https://www.youtube.com/live/AbCdEfGhIjK?feature=share", "AbCdEfGhIjK"),
    ("https://www.youtube.com/@somechannel", None),
    ("https://example.com/watch?v=AbCdEfGhIjK", None),
])
def test_video_id(url, vid):
    assert youtube.video_id(url) == vid


class FakeTranscript:
    def __init__(self, lang, generated, lines):
        self.language_code, self.is_generated, self._lines = lang, generated, lines

    def fetch(self):
        return [SimpleNamespace(text=t) for t in self._lines]


class FakeListing:
    def __init__(self, items):
        self.items = items

    def __iter__(self):
        return iter(self.items)

    # 실제 라이브러리처럼: 요청한 언어 순서대로 찾는다
    def _find(self, langs, generated):
        for lang in langs:
            for t in self.items:
                if t.is_generated == generated and t.language_code == lang:
                    return t
        raise LookupError

    def find_manually_created_transcript(self, langs):
        return self._find(langs, False)

    def find_generated_transcript(self, langs):
        return self._find(langs, True)


def _fake_api(monkeypatch, listing=None, error=None):
    import youtube_transcript_api

    class Api:
        def list(self, vid):
            if error:
                raise error
            return listing

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", Api)
    monkeypatch.setattr(youtube, "_meta", lambda vid: ("전세 사기 피하는 법 5가지", "부동산채널", "계약 전 확인할 것 정리"))


def test_youtube_link_reads_whole_transcript(monkeypatch):
    lines = ["안녕하세요 오늘은", "[음악]", "전세 계약 전에 등기부등본을", "꼭 확인하셔야 합니다", "마지막으로 보증보험 가입"]
    _fake_api(monkeypatch, FakeListing([FakeTranscript("ko", True, lines)]))
    page = fetcher.fetch("https://youtu.be/AbCdEfGhIjK")
    assert page.ok and page.kind == "youtube" and page.title == "전세 사기 피하는 법 5가지"
    assert "채널: 부동산채널" in page.text and "자동 생성 자막" in page.text
    assert "등기부등본" in page.text and "마지막으로 보증보험 가입" in page.text  # 끝까지 전부
    assert "[음악]" not in page.text


def test_prefers_korean(monkeypatch):
    items = [FakeTranscript("en", False, ["english line"]), FakeTranscript("ko", True, ["자동 자막"]),
             FakeTranscript("ko", False, ["사람이 단 자막"])]
    _fake_api(monkeypatch, FakeListing(items))
    video = youtube.fetch_video("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    assert video.transcript == "사람이 단 자막" and not video.auto_generated
    # 사람이 단 한국어 자막이 없으면: 영어(사람)보다 한국어 자동 자막
    _fake_api(monkeypatch, FakeListing(items[:2]))
    video = youtube.fetch_video("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    assert video.transcript == "자동 자막" and video.auto_generated
    # 한국어·영어가 없으면 있는 것 아무거나
    _fake_api(monkeypatch, FakeListing([FakeTranscript("ja", True, ["日本語"])]))
    assert youtube.fetch_video("https://youtu.be/AbCdEfGhIjK").language == "ja"


def test_no_transcript_uses_title_and_warns(monkeypatch):
    class TranscriptsDisabled(Exception):
        pass

    _fake_api(monkeypatch, error=TranscriptsDisabled())
    page = fetcher.fetch("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    assert page.ok and "전세 사기 피하는 법" in page.text
    assert "자막이 꺼져" in page.warning


def test_youtube_link_is_analyzed_not_inserted():
    cmd = parse("https://youtu.be/AbCdEfGhIjK 이 영상 내용으로 블로그 글 써줘")
    assert cmd.analyze_urls == ["https://youtu.be/AbCdEfGhIjK"] and not cmd.insert_urls
    assert not cmd.search  # '최신/검색' 이라고 하면 추가로 검색
    assert parse("https://youtu.be/AbCdEfGhIjK 최신 정보도 찾아서 써줘").search
