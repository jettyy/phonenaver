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


def test_no_transcript_means_no_guessing(monkeypatch):
    """대사를 못 가져오면 제목·설명으로 추측해서 쓰지 않는다 (예전에는 제목만으로 썼음)."""
    class IpBlocked(Exception):
        pass

    _fake_api(monkeypatch, error=IpBlocked())
    page = fetcher.fetch("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    assert not page.ok and "막았습니다" in page.error
    assert page.video.blocked and page.video.title == "전세 사기 피하는 법 5가지"


def _pipeline(tmp_path):
    from phonenaver.config import Config
    from phonenaver.pipeline import Pipeline

    return Pipeline(Config(naver_blog_id="", image_dir=tmp_path / "img", browser_profile_dir=tmp_path / "b"), session=object())


def test_browser_fallback_reads_transcript(monkeypatch, tmp_path):
    """빠른 방법이 막히면 실제 브라우저로 [스크립트 표시] 를 열어 대사를 읽는다."""
    import asyncio

    class IpBlocked(Exception):
        pass

    _fake_api(monkeypatch, error=IpBlocked())

    async def fake_browser(session, vid):
        return "브라우저로 읽은 대사 첫 줄\n마지막 줄", ""

    monkeypatch.setattr(youtube, "transcript_via_browser", fake_browser)
    pages = [fetcher.fetch("https://youtu.be/AbCdEfGhIjK")]

    async def noop(_):
        return None

    out = asyncio.run(_pipeline(tmp_path)._youtube_fallback(pages, noop))
    assert out[0].ok and "브라우저로 읽은 대사" in out[0].text and "마지막 줄" in out[0].text


def test_no_transcript_anywhere_stops_the_post(monkeypatch, tmp_path):
    import asyncio

    class IpBlocked(Exception):
        pass

    _fake_api(monkeypatch, error=IpBlocked())

    async def fake_browser(session, vid):
        return "", "스크립트를 열었지만 대사를 읽지 못했습니다"

    monkeypatch.setattr(youtube, "transcript_via_browser", fake_browser)
    pages = [fetcher.fetch("https://youtu.be/AbCdEfGhIjK")]

    async def noop(_):
        return None

    with pytest.raises(youtube.TranscriptUnavailable) as err:
        asyncio.run(_pipeline(tmp_path)._youtube_fallback(pages, noop))
    assert err.value.blocked and "글을 쓰지 않았습니다" in str(err.value)


def test_blocked_job_is_retried_later(tmp_path, monkeypatch):
    """유튜브가 잠시 막았으면 실패로 끝내지 않고 10분 뒤 자동으로 다시 시도하도록 대기열에 둔다."""
    import asyncio

    from phonenaver import jobs, pipeline
    from phonenaver.config import Config

    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")

    async def blocked_run(self, *a, **k):
        raise youtube.TranscriptUnavailable("유튜브가 막음", blocked=True)

    monkeypatch.setattr(pipeline.Pipeline, "run", blocked_run)
    later = []

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "call_later", lambda delay, fn, *args: later.append((delay, args)))
        runner = jobs.JobRunner(lambda: Config(), None, jobs.Events())
        runner._queue, runner._task = asyncio.Queue(), object()
        job = runner.submit("https://youtu.be/AbCdEfGhIjK 글 써줘")
        await runner._run(job)
        return job

    job = asyncio.run(scenario())
    assert job.status == "queued" and job.retries == 1 and "10분 뒤" in job.message
    assert later and later[0][0] == 600


def test_youtube_link_is_analyzed_not_inserted():
    cmd = parse("https://youtu.be/AbCdEfGhIjK 이 영상 내용으로 블로그 글 써줘")
    assert cmd.analyze_urls == ["https://youtu.be/AbCdEfGhIjK"] and not cmd.insert_urls
    assert not cmd.search  # '최신/검색' 이라고 하면 추가로 검색
    assert parse("https://youtu.be/AbCdEfGhIjK 최신 정보도 찾아서 써줘").search


def test_channel_url_detection():
    assert youtube.channel_url("https://www.youtube.com/@mychannel") == "https://www.youtube.com/@mychannel/videos"
    assert youtube.channel_url("https://m.youtube.com/@mychannel/featured") == "https://www.youtube.com/@mychannel/videos"
    assert youtube.is_channel("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")
    assert not youtube.is_channel("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    assert not youtube.is_channel("https://youtu.be/AbCdEfGhIjK")


def test_channel_request_becomes_one_job_per_video(tmp_path, monkeypatch):
    """채널 링크 → 최근 영상마다 글 작업 하나씩. 이미 쓴 영상도 다시 쓴다 (몇 번이든 반복 가능)."""
    import asyncio

    from phonenaver import jobs
    from phonenaver.config import Config

    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(jobs, "YOUTUBE_DONE_FILE", tmp_path / "done.json")
    asked = {}

    def fake_list(url, months, limit):
        asked.update(url=url, months=months, limit=limit)
        return "테스트채널", [youtube.ChannelVideo(f"Vid{i:08d}", f"영상 {i}") for i in range(1, 5)]

    monkeypatch.setattr(youtube, "list_channel_videos", fake_list)
    jobs.mark_video_done("Vid00000002")  # 예전에 쓴 영상이어도

    async def scenario():
        runner = jobs.JobRunner(lambda: Config(), None, jobs.Events())
        runner._queue, runner._task = asyncio.Queue(), object()  # 실제 글쓰기는 돌리지 않음
        job = runner.submit("https://www.youtube.com/@testch 최근 3개월 초보자용으로 쉽게 써줘", source="phone", want_result=True)
        await runner._run(job)
        result = await runner.wait(job)
        return runner, job, result

    runner, job, result = asyncio.run(scenario())
    assert asked["months"] == 3 and asked["limit"] is None
    assert isinstance(result, jobs.ChannelResult) and result.found == 4 and result.skipped == 0
    texts = [c.text for c in result.children]
    assert len(texts) == 4 and "Vid00000002" in " ".join(texts)  # 건너뛰지 않음
    assert texts[0].startswith("https://www.youtube.com/watch?v=Vid00000001") and "초보자용으로 쉽게" in texts[0]
    assert all(c.status == "queued" and c.source == "phone" for c in result.children)
    assert job.status == "done" and "글 4개" in job.message and len(job.children) == 4


def test_waiting_after_job_finished_or_canceled(tmp_path, monkeypatch):
    """채널 글들을 차례로 기다릴 때, 먼저 끝난(또는 취소된) 작업의 결과도 받을 수 있어야 한다."""
    import asyncio

    from phonenaver import jobs
    from phonenaver.config import Config

    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")

    async def scenario():
        runner = jobs.JobRunner(lambda: Config(), None, jobs.Events())
        runner._queue, runner._task = asyncio.Queue(), object()
        a = runner.submit("글 A", want_result=True)
        b = runner.submit("글 B", want_result=True)
        runner._results[a.id].set_result("결과 A")  # A 는 이미 끝남
        runner.cancel(b.id)                        # B 는 취소됨
        assert await runner.wait(a) == "결과 A"
        with pytest.raises(asyncio.CancelledError):
            await runner.wait(b)
        assert not runner._results  # 받아 간 결과는 정리

    asyncio.run(scenario())


def test_timedtext_and_panel_parsing():
    data = '{"events": [{"segs": [{"utf8": "첫 줄"}]}, {"segs": [{"utf8": "[음악]"}]}, {"segs": [{"utf8": "둘째"}, {"utf8": " 줄"}]}]}'
    assert youtube._clean_lines(youtube._segments_from_timedtext(data)) == ["첫 줄", "둘째 줄"]
    xml = '<transcript><text start="0">가 &amp; 나</text><text start="1">다</text></transcript>'
    assert youtube._segments_from_timedtext(xml) == ["가 & 나", "다"]
    assert youtube._clean_lines(["스크립트", "0:00", "대사 하나", "1:02:03", "대사 하나", "대사 둘"]) == ["대사 하나", "대사 둘"]


def test_channel_dates_looked_up_when_list_has_none(monkeypatch):
    """목록에 날짜가 없으면 영상마다 날짜를 확인해서 6개월치를 다 가져온다 (RSS 15개 제한에 걸리지 않게)."""
    import sys
    import time
    import types

    now = time.time()
    entries = [{"id": f"Vid{i:08d}", "title": f"영상 {i}"} for i in range(30)]

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"channel": "채널", "channel_id": "UCx", "entries": entries}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    looked = []

    def fake_time(vid):
        looked.append(vid)
        return now - int(vid[3:]) * 7 * 86400  # 일주일에 하나씩

    monkeypatch.setattr(youtube, "_upload_time", fake_time)
    monkeypatch.setattr(youtube, "_rss_videos", lambda cid: pytest.fail("RSS 를 쓰면 안 됨"))
    name, videos = youtube.list_channel_videos("https://www.youtube.com/@ch", months=6)
    assert 25 <= len(videos) <= 27  # 약 26주
    assert len(looked) == len(videos) + 1  # 기준보다 오래된 영상 하나 보고 멈춤
