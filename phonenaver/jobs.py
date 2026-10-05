"""글쓰기 작업 대기열 — 휴대폰(텔레그램)과 대시보드(PC)가 같이 쓴다.

작업은 넣은 순서대로 하나씩 처리한다 (브라우저·네이버 세션이 하나뿐이므로).
작업 기록은 data/jobs.json 에 남아 대시보드를 껐다 켜도 보인다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .browser import BrowserSession
from .config import DATA, Config
from .html_utils import html_to_text
from .images import strip_markers

log = logging.getLogger(__name__)

JOBS_FILE = DATA / "jobs.json"
YOUTUBE_DONE_FILE = DATA / "youtube-done.json"  # 이미 글로 쓴 유튜브 영상 (채널 요청 때 중복 방지)
# 대기열(대기·진행 중)은 개수 제한 없음. 끝난 작업 기록만 최근 이만큼 보관 (글은 이미 네이버에 저장돼 있음)
KEEP_FINISHED = 2000
YOUTUBE_RETRIES = 3
Progress = Callable[[str], Awaitable[None]]


class Events:
    """대시보드로 실시간 전달할 소식 (로그 한 줄, 작업 상태 변경 등)."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.recent: list[dict] = []  # 새로 연 화면에 보여줄 최근 로그

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data) -> None:
        msg = {"type": kind, "data": data}
        if kind == "log":
            self.recent = (self.recent + [msg])[-300:]
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


class EventLogHandler(logging.Handler):
    """파이썬 로그를 대시보드 '진행 로그' 로 보낸다."""

    def __init__(self, events: Events, loop: asyncio.AbstractEventLoop):
        super().__init__(logging.INFO)
        self.events, self.loop = events, loop

    def emit(self, record: logging.LogRecord) -> None:
        item = {
            "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        try:
            self.loop.call_soon_threadsafe(self.events.publish, "log", item)
        except RuntimeError:
            pass


@dataclass
class Job:
    id: str
    text: str
    source: str = "pc"  # "pc" | "phone"
    photos: list[str] = field(default_factory=list)
    photo_mode: Optional[str] = None
    dry_run: bool = False
    status: str = "queued"  # queued | running | done | failed | canceled
    message: str = ""
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    title: str = ""
    category: str = ""
    chars: int = 0
    images: list[str] = field(default_factory=list)
    image_sources: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    preview: str = ""
    error: str = ""
    screenshot: str = ""
    needs_login: bool = False
    children: list[str] = field(default_factory=list)  # 유튜브 채널 요청이 만든 영상별 작업
    retries: int = 0  # 유튜브가 잠시 막아서 자동으로 다시 시도한 횟수
    publish_mode: Optional[str] = None  # "draft" / "publish" / None(설정대로)
    publish_time: Optional[str] = None  # 메시지·화면에서 정한 발행 시각 "HH:MM"
    scheduled_at: Optional[float] = None  # 발행 예정 시각
    published: bool = False
    post_url: str = ""
    # 발행 예약: "" | scheduled | publishing | published | failed | canceled
    publish_state: str = ""
    publish_error: str = ""
    body_html: str = ""  # 발행할 때 임시저장 목록에서 못 찾으면 다시 쓰기 위한 본문 (발행 후 비움)
    category_name: str = ""
    category_no: Optional[int] = None
    chat_id: Optional[int] = None  # 휴대폰 요청이면 발행됐을 때 알릴 채팅

    def public(self) -> dict:
        data = asdict(self)
        data["photos"] = len(self.photos)
        data.pop("body_html", None)
        return data


@dataclass
class ChannelResult:
    """유튜브 채널 요청의 결과: 영상마다 글쓰기 작업을 대기열에 넣었다."""
    channel: str
    months: int
    found: int
    skipped: int
    children: list[Job]


def load_done_videos() -> set[str]:
    try:
        return set(json.loads(YOUTUBE_DONE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def mark_video_done(video_id: str) -> None:
    done = load_done_videos() | {video_id}
    YOUTUBE_DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    YOUTUBE_DONE_FILE.write_text(json.dumps(sorted(done)), encoding="utf-8")


class JobRunner:
    def __init__(self, get_cfg: Callable[[], Config], session: BrowserSession, events: Events):
        self.get_cfg = get_cfg
        self.session = session
        self.events = events
        self.jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._results: dict[str, asyncio.Future] = {}
        self._callbacks: dict[str, Progress] = {}
        from .schedule import PublishScheduler

        self.scheduler = PublishScheduler(get_cfg)
        self._publish_tasks: dict[str, asyncio.Task] = {}
        self.on_published: list[Callable[[Job], Awaitable[None]]] = []  # 발행 소식을 받을 곳 (휴대폰 봇)
        self._load()

    # ── 기록 ──
    def _load(self) -> None:
        try:
            for raw in json.loads(JOBS_FILE.read_text(encoding="utf-8")):
                job = Job(**{k: v for k, v in raw.items() if k in Job.__dataclass_fields__})
                if job.status in ("queued", "running"):  # 프로그램이 꺼지면서 끊긴 작업
                    job.status, job.message = "failed", "프로그램이 종료되어 중단됨 (다시 시도를 누르세요)"
                self.jobs[job.id] = job
        except Exception:
            pass

    def _save(self) -> None:
        active = [j for j in self.jobs.values() if j.status in ("queued", "running")]
        finished = [j for j in self.jobs.values() if j.status not in ("queued", "running")]
        finished = sorted(finished, key=lambda j: j.created)[-KEEP_FINISHED:]
        items = sorted(active + finished, key=lambda j: j.created)  # 대기 중인 작업은 절대 지우지 않는다
        self.jobs = {j.id: j for j in items}
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        JOBS_FILE.write_text(json.dumps([asdict(j) for j in items], ensure_ascii=False, indent=1), encoding="utf-8")

    def _update(self, job: Job, **changes) -> None:
        for k, v in changes.items():
            setattr(job, k, v)
        self._save()
        self.events.publish("job", job.public())

    def list(self) -> list[dict]:
        return [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)]

    # ── 실행 ──
    def start(self) -> None:
        if self._task is None:
            self._queue = asyncio.Queue()
            self._task = asyncio.create_task(self._worker())
            self._restore_publishes()

    # ── 발행 예약 ──
    def _restore_publishes(self) -> None:
        """프로그램을 다시 켰을 때 남아 있는 발행 예약을 이어서 잡는다 (시각이 지난 것은 간격을 두고 차례로)."""
        pending = sorted((j for j in self.jobs.values() if j.publish_state in ("scheduled", "publishing")),
                         key=lambda j: j.scheduled_at or 0)
        now = time.time()
        for job in pending:
            when = job.scheduled_at if job.scheduled_at and job.scheduled_at > now else None
            self.schedule_publish(job, when)

    def schedule_publish(self, job: Job, when: float | None = None) -> None:
        from .schedule import fmt

        when = when or self.scheduler.plan(job.publish_time)
        self.scheduler.last = max(self.scheduler.last, when)
        self._update(job, publish_state="scheduled", scheduled_at=when, publish_error="",
                     message=f"임시저장 완료 · ⏰ {fmt(when)} 발행 예정")
        log.info("⏰ %s 발행 예정: %s", fmt(when), job.title)
        old = self._publish_tasks.pop(job.id, None)
        if old:
            old.cancel()
        self._publish_tasks[job.id] = asyncio.get_running_loop().create_task(self._publish_later(job.id))

    async def _publish_later(self, job_id: str) -> None:
        while True:
            job = self.jobs.get(job_id)
            if job is None or job.publish_state != "scheduled":
                return
            left = (job.scheduled_at or 0) - time.time()
            if left <= 0:
                break
            await asyncio.sleep(min(30, left))
        await self.publish_now(job_id)

    async def publish_now(self, job_id: str) -> None:
        from .naver import Category, NaverBlog, NotLoggedIn

        job = self.jobs.get(job_id)
        if job is None or job.publish_state in ("publishing", "published"):
            return
        self._publish_tasks.pop(job_id, None)
        self._update(job, publish_state="publishing", message="🚀 발행 중...")
        cfg = self.get_cfg()
        category = Category(name=job.category_name, no=job.category_no) if job.category_name else None
        try:
            result = await NaverBlog(cfg, self.session).publish_saved(
                job.title, job.body_html, [Path(p) for p in job.images], category, job.tags
            )
        except NotLoggedIn as exc:
            self._update(job, publish_state="failed", needs_login=True, publish_error=str(exc),
                         message="발행 실패 — 네이버 로그인 필요 (로그인 후 [지금 발행])")
            log.warning("발행 실패 (로그인 필요): %s", job.title)
            await self._notify(job)
            return
        except Exception as exc:
            self._update(job, publish_state="failed", publish_error=str(exc),
                         message="발행 실패 (임시저장 글은 그대로 있어요. [지금 발행] 으로 다시)",
                         screenshot=str(getattr(exc, "screenshot", "") or job.screenshot))
            log.error("발행 실패: %s — %s", job.title, exc)
            await self._notify(job)
            return
        self._update(job, publish_state="published", published=True, post_url=result.post_url, body_html="",
                     warnings=list(dict.fromkeys(job.warnings + result.warnings)),
                     message="✅ 발행 완료", screenshot=str(result.screenshot or job.screenshot))
        log.info("✅ 발행 완료: %s %s", job.title, result.post_url)
        await self._notify(job)

    def cancel_publish(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.publish_state not in ("scheduled", "failed"):
            return False
        task = self._publish_tasks.pop(job_id, None)
        if task:
            task.cancel()
        self._update(job, publish_state="canceled", message="발행 취소 (임시저장 글은 그대로 있어요)")
        return True

    async def _notify(self, job: Job) -> None:
        for listener in list(self.on_published):
            try:
                await listener(job)
            except Exception as exc:
                log.info("발행 알림 실패: %s", exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def submit(
        self,
        text: str,
        photos: list[Path] | None = None,
        source: str = "pc",
        photo_mode: str | None = None,
        progress: Progress | None = None,
        want_result: bool = False,
        publish_mode: str | None = None,
        publish_time: str | None = None,
        chat_id: int | None = None,
    ) -> Job:
        from .command import DRY_PREFIXES, publish_options

        dry = any(text.strip().lower().startswith(p) for p in DRY_PREFIXES)
        said_mode, said_time = publish_options(text)  # "발행해줘", "21시에 발행", "임시저장"
        job = Job(id=uuid.uuid4().hex[:10], text=text, source=source, photos=[str(p) for p in photos or []],
                  photo_mode=photo_mode, dry_run=dry, message="대기 중",
                  publish_mode=publish_mode or said_mode, publish_time=publish_time or said_time, chat_id=chat_id)
        self.jobs[job.id] = job
        if want_result:  # 결과를 기다릴 사람이 있을 때만 (휴대폰 답장용)
            self._results[job.id] = asyncio.get_running_loop().create_future()
        if progress:
            self._callbacks[job.id] = progress
        self._update(job)
        self.start()
        assert self._queue is not None
        self._queue.put_nowait(job.id)
        first = text.strip().splitlines()[0][:60] if text.strip() else ""
        extra = f"  (요청 전체 {len(text):,}자, {len(text.strip().splitlines())}줄 — 전부 분석합니다)" if "\n" in text.strip() or len(text) > 60 else ""
        log.info("작업 추가 (%s): %s%s", "휴대폰" if source == "phone" else "PC", first, extra)
        return job

    async def wait(self, job: Job):
        """submit(..., want_result=True) 로 넣은 작업의 결과를 기다린다."""
        fut = self._results.get(job.id)
        if fut is None:
            raise RuntimeError("결과를 기다릴 수 없는 작업입니다")
        try:
            return await fut  # 이미 끝난 작업이면 바로 결과가 나온다
        finally:
            self._results.pop(job.id, None)

    def retry(self, job_id: str) -> Job:
        old = self.jobs[job_id]
        return self.submit(old.text, [Path(p) for p in old.photos], old.source, old.photo_mode)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status == "queued":
            self._update(job, status="canceled", message="취소됨")
            fut = self._results.get(job_id)  # 기다리는 쪽이 '취소됨' 을 받을 수 있게 남겨 둔다
            if fut and not fut.done():
                fut.cancel()
            return True
        if job.status in ("done", "failed", "canceled"):
            self.cancel_publish(job_id)  # 발행 예약이 걸려 있으면 같이 취소
            del self.jobs[job_id]
            self._save()
            self.events.publish("jobs", self.list())
            return True
        return False  # 진행 중인 작업은 끝날 때까지 둔다

    def clear_finished(self) -> None:
        self.jobs = {k: j for k, j in self.jobs.items() if j.status in ("queued", "running")}
        self._save()
        self.events.publish("jobs", self.list())

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued":
                continue
            try:
                await self._run(job)
            except Exception:  # 작업 하나가 실패해도 대기열은 계속
                log.exception("작업 처리 중 예상 못한 오류")
            await self._pause_between_posts(job)

    async def _pause_between_posts(self, job: Job) -> None:
        """네이버에 연달아 저장하면 어뷰징으로 보일 수 있어서, 다음 글 전에 잠깐 쉰다 (인포러시와 같은 방식)."""
        import random

        cfg = self.get_cfg()
        waiting = self._queue is not None and not self._queue.empty()
        if job.status != "done" or job.dry_run or not waiting or cfg.post_delay_max <= 0:
            return
        delay = random.uniform(max(0, cfg.post_delay_min), max(cfg.post_delay_min, cfg.post_delay_max))
        log.info("⏳ 다음 글까지 %d초 쉽니다 (네이버 연속 저장 방지)", delay)
        await asyncio.sleep(delay)

    async def _run(self, job: Job) -> None:
        from .naver import NaverError, NotLoggedIn
        from .pipeline import Pipeline
        from .youtube import TranscriptUnavailable

        cfg = self.get_cfg()
        self._update(job, status="running", message="시작")
        callback = self._callbacks.pop(job.id, None)

        from .command import parse
        from .youtube import is_channel

        if any(is_channel(u) for u in parse(job.text).urls):
            await self._expand_channel(job, callback)
            return

        async def progress(msg: str) -> None:
            self._update(job, message=msg)
            log.info(msg)
            if callback:
                try:
                    await callback(msg)
                except Exception:
                    pass

        fut = self._results.get(job.id)  # 결과는 기다리는 쪽이 가져갈 때까지 둔다
        try:
            pipe = Pipeline(cfg, self.session)
            result = await pipe.run(job.text, progress, photos=[Path(p) for p in job.photos], photo_mode=job.photo_mode)
        except TranscriptUnavailable as exc:
            # 유튜브가 잠시 막은 경우: 시간이 지나면 풀리므로 10분 → 20분 → 40분 뒤 자동으로 다시
            if exc.blocked and job.retries < YOUTUBE_RETRIES:
                minutes = 10 * (2 ** job.retries)
                self._update(job, status="queued", retries=job.retries + 1, error=str(exc),
                             message=f"유튜브가 잠시 막음 — {minutes}분 뒤 자동으로 다시 시도 ({job.retries + 1}/{YOUTUBE_RETRIES})")
                log.warning("유튜브가 잠시 막아서 %d분 뒤 다시 시도합니다: %s", minutes, job.text.splitlines()[0][:60])
                if callback:  # 다시 시도할 때도 휴대폰에 진행 상황을 보낸다
                    self._callbacks[job.id] = callback
                asyncio.get_running_loop().call_later(minutes * 60, self._requeue, job.id)
                if callback:
                    try:
                        await callback(job.message)
                    except Exception:
                        pass
                return
            self._update(job, status="failed", message="유튜브 대사 없음 — 글을 쓰지 않음", error=str(exc),
                         finished=time.time())
            log.warning("%s", exc)
            if fut and not fut.done():
                fut.set_exception(exc)
            return
        except NotLoggedIn as exc:
            self._update(job, status="failed", message="네이버 로그인 필요", error=str(exc), needs_login=True,
                         finished=time.time(), screenshot=str(exc.screenshot or ""))
            self.events.publish("session", self.session.read_session())
            log.warning("네이버 로그인이 필요합니다. [네이버 로그인 창 열기] 를 눌러 주세요.")
            if fut and not fut.done():
                fut.set_exception(exc)
            return
        except NaverError as exc:
            self._update(job, status="failed", message="임시저장 실패", error=str(exc), finished=time.time(),
                         screenshot=str(exc.screenshot or ""))
            log.error("임시저장 실패: %s", exc)
            if fut and not fut.done():
                fut.set_exception(exc)
            return
        except Exception as exc:
            self._update(job, status="failed", message="실패", error=str(exc), finished=time.time())
            log.error("작업 실패: %s", exc)
            if fut and not fut.done():
                fut.set_exception(exc)
            return

        plain = strip_markers(html_to_text(result.body_html))
        category = (result.draft.category if result.draft else None) or (result.category.label if result.category else "")
        self._update(
            job,
            status="done",
            message="미리보기 완료" if result.command.dry_run else "임시저장 완료",
            finished=time.time(),
            title=result.post.title,
            category=category or "",
            chars=len(plain),
            images=[str(i.path) for i in result.images],
            image_sources=[i.source for i in result.images],
            tags=list(result.post.tags),
            warnings=list(dict.fromkeys(result.warnings)),
            preview=plain[:20000],
            screenshot=str(result.draft.screenshot) if result.draft and result.draft.screenshot else "",
        )
        log.info("✅ %s: %s", job.message, result.post.title)
        # 발행 예약: 기본은 임시저장만. '정해진 시간에 발행' 이 켜져 있거나 메시지로 발행을 요청하면 그 시각에 발행
        if not result.command.dry_run and result.draft is not None:
            mode = job.publish_mode or cfg.publish_mode
            if mode in ("publish", "schedule"):
                self._update(job, body_html=result.body_html,
                             category_name=result.category.name if result.category else "",
                             category_no=result.category.no if result.category else None)
                self.schedule_publish(job)
        if not result.command.dry_run:  # 유튜브 영상으로 쓴 글은 기록해서 채널 요청 때 다시 안 쓴다
            from .youtube import video_id

            for url in result.command.urls:
                vid = video_id(url)
                if vid:
                    mark_video_done(vid)
        if fut and not fut.done():
            fut.set_result(result)

    def _requeue(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is not None and job.status == "queued" and self._queue is not None:
            self._queue.put_nowait(job_id)

    async def _expand_channel(self, job: Job, callback: Progress | None) -> None:
        """유튜브 채널 요청 → 최근 영상마다 글쓰기 작업 하나씩 대기열에 넣는다."""
        from .command import DRY_PREFIXES, channel_options, parse
        from .youtube import is_channel, list_channel_videos

        fut = self._results.get(job.id)  # 결과는 기다리는 쪽이 가져갈 때까지 둔다
        cmd = parse(job.text)
        months, limit, rest = channel_options(cmd.instruction)
        extra = rest if len(rest) >= 6 else "이 영상 내용으로 블로그 글 써줘"  # '글로 써줘' 같은 짧은 말은 기본 지시로
        prefix = "/test " if job.dry_run else ""

        async def say(msg: str) -> None:
            self._update(job, message=msg)
            log.info(msg)
            if callback:
                try:
                    await callback(msg)
                except Exception:
                    pass

        children: list[Job] = []
        found = skipped = 0
        names = []
        try:
            for url in [u for u in cmd.urls if is_channel(u)]:
                await say(f"📺 채널 영상 목록 확인 중 (최근 {months}개월{f', 최대 {limit}개' if limit else ''})...")
                name, videos = await asyncio.to_thread(list_channel_videos, url, months, limit)
                names.append(name or url)
                found += len(videos)
                for v in videos:
                    # 이미 쓴 영상도 다시 쓴다 (같은 채널을 몇 번이든 반복해서 보낼 수 있음)
                    children.append(self.submit(f"{prefix}{v.url}\n{extra}", source=job.source,
                                                want_result=fut is not None))
        except Exception as exc:
            self._update(job, status="failed", message="채널 영상 목록을 가져오지 못함", error=str(exc), finished=time.time())
            log.error("채널 영상 목록 실패: %s", exc)
            if fut and not fut.done():
                fut.set_exception(exc)
            return

        channel = ", ".join(names)
        summary = f"📺 {channel}: 최근 {months}개월 영상 {found}개 → 글 {len(children)}개 대기열에 추가"
        if skipped:
            summary += f" (이미 쓴 {skipped}개 건너뜀)"
        self._update(job, status="done", message=summary, title=f"📺 {channel} 최근 {months}개월",
                     children=[c.id for c in children], finished=time.time())
        log.info(summary)
        if fut and not fut.done():
            fut.set_result(ChannelResult(channel, months, found, skipped, children))
