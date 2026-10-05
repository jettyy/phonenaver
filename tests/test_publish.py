"""임시저장 후 정해진 시각(시작 시각 + 간격 + 랜덤)에 발행."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from phonenaver import jobs, naver
from phonenaver.command import publish_options
from phonenaver.config import Config
from phonenaver.schedule import PublishScheduler, next_clock


def test_publish_options_from_message():
    assert publish_options("캠핑 글 써서 바로 발행해줘") == ("publish", None)
    assert publish_options("21시 30분에 발행") == ("publish", "21:30")
    assert publish_options("오후 9시 발행") == ("publish", "21:00")
    assert publish_options("저녁 8시반에 발행해줘") == ("publish", "20:30")
    assert publish_options("임시저장만 해줘") == ("draft", None)
    assert publish_options("발행하지 말고 써줘") == ("draft", None)
    assert publish_options("캠핑 준비물") == (None, None)  # 설정대로


def test_scheduler_start_interval_random():
    now = datetime(2026, 10, 5, 20, 0).timestamp()
    cfg = Config(publish_at="21:00", publish_interval=60, publish_random=30)
    s = PublishScheduler(lambda: cfg, rand=lambda a, b: b)  # 랜덤을 최대값으로 고정
    times = [datetime.fromtimestamp(s.plan(now=now)).strftime("%H:%M") for _ in range(3)]
    assert times == ["21:30", "23:00", "00:30"]  # 21:00+30분, 그 뒤 60분+30분씩
    s2 = PublishScheduler(lambda: Config(publish_at="", publish_interval=60, publish_random=0))
    gaps = [round((s2.plan(now=now) - now) / 60) for _ in range(3)]
    assert gaps == [0, 60, 120]  # 시작 시각을 비우면 바로부터
    assert datetime.fromtimestamp(next_clock("19:00", now)).day == 6  # 지난 시각이면 다음 날


class FakeBlog:
    calls = []
    fail = None

    def __init__(self, cfg, session):
        pass

    async def publish_saved(self, title, body_html, images, category, tags):
        FakeBlog.calls.append((title, body_html, category.name if category else None, tags))
        if FakeBlog.fail:
            raise FakeBlog.fail
        return naver.DraftResult(title=title, screenshot=None, editor_url="", published=True,
                                 post_url="https://blog.naver.com/me/224000000001")


def _runner(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(naver, "NaverBlog", FakeBlog)
    FakeBlog.calls, FakeBlog.fail = [], None
    runner = jobs.JobRunner(lambda: cfg, None, jobs.Events())
    runner._queue, runner._task = asyncio.Queue(), object()
    return runner


def _saved_job(runner, **kw):
    job = jobs.Job(id="j1", text="캠핑 준비물", status="done", title="캠핑 준비물 TOP 10", tags=["캠핑"],
                   body_html="<p>본문</p>", category_name="여행", chat_id=7, **kw)
    runner.jobs[job.id] = job
    return job


def test_scheduled_publish_runs_at_time_and_notifies(tmp_path, monkeypatch):
    cfg = Config(publish_mode="schedule", publish_at="", publish_interval=0, publish_random=0)
    runner = _runner(tmp_path, monkeypatch, cfg)
    told = []

    async def listener(job):
        told.append((job.publish_state, job.post_url))

    runner.on_published.append(listener)

    async def scenario():
        job = _saved_job(runner)
        runner.schedule_publish(job)  # 시작 시각 비움 + 랜덤 0 → 곧바로
        assert job.publish_state == "scheduled" and "발행 예정" in job.message
        await asyncio.sleep(0.2)
        return job

    job = asyncio.run(scenario())
    assert FakeBlog.calls == [("캠핑 준비물 TOP 10", "<p>본문</p>", "여행", ["캠핑"])]
    assert job.publish_state == "published" and job.published and job.post_url.endswith("224000000001")
    assert job.body_html == ""  # 발행 후 본문 사본은 지움
    assert told == [("published", job.post_url)]


def test_cancel_and_failure(tmp_path, monkeypatch):
    cfg = Config(publish_mode="schedule", publish_at="", publish_interval=0, publish_random=0)
    runner = _runner(tmp_path, monkeypatch, cfg)

    async def scenario():
        job = _saved_job(runner)
        runner.schedule_publish(job, when=10**10)  # 아주 먼 미래
        assert runner.cancel_publish(job.id) and job.publish_state == "canceled"
        await asyncio.sleep(0.05)
        assert FakeBlog.calls == []
        FakeBlog.fail = naver.NotLoggedIn("로그인 필요")
        await runner.publish_now(job.id)  # [지금 발행]
        return job

    job = asyncio.run(scenario())
    assert job.publish_state == "failed" and job.needs_login and "로그인" in job.message


def test_reservations_survive_restart(tmp_path, monkeypatch):
    cfg = Config(publish_mode="schedule", publish_at="", publish_interval=0, publish_random=0)
    runner = _runner(tmp_path, monkeypatch, cfg)

    async def first():
        job = _saved_job(runner)
        runner.schedule_publish(job, when=10**10)

    asyncio.run(first())

    async def second():
        again = jobs.JobRunner(lambda: cfg, None, jobs.Events())
        again._queue = None
        again._task = None
        again.start()  # 다시 켜면 예약을 이어서 잡는다
        await asyncio.sleep(0.05)
        job = again.jobs["j1"]
        state = (job.publish_state, job.scheduled_at)
        await again.stop()
        for t in again._publish_tasks.values():
            t.cancel()
        return state

    state, when = asyncio.run(second())
    assert state == "scheduled" and when == 10**10


def test_draft_mode_does_not_publish(tmp_path, monkeypatch):
    """기본(임시저장만)에서는 발행 예약을 걸지 않는다. 메시지로 '발행해줘' 하면 그 글만 예약."""
    runner = _runner(tmp_path, monkeypatch, Config(publish_mode="draft"))

    async def scenario():
        a = runner.submit("캠핑 준비물 정리")
        b = runner.submit("전세 월세 정리해서 21시에 발행해줘")
        return a, b

    a, b = asyncio.run(scenario())
    assert a.publish_mode is None and b.publish_mode == "publish" and b.publish_time == "21:00"
