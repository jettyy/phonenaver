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
KEEP_JOBS = 200
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

    def public(self) -> dict:
        data = asdict(self)
        data["photos"] = len(self.photos)
        return data


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
        items = sorted(self.jobs.values(), key=lambda j: j.created)[-KEEP_JOBS:]
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
    ) -> Job:
        from .command import DRY_PREFIXES

        dry = any(text.strip().lower().startswith(p) for p in DRY_PREFIXES)
        job = Job(id=uuid.uuid4().hex[:10], text=text, source=source, photos=[str(p) for p in photos or []],
                  photo_mode=photo_mode, dry_run=dry, message="대기 중")
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
        return await fut

    def retry(self, job_id: str) -> Job:
        old = self.jobs[job_id]
        return self.submit(old.text, [Path(p) for p in old.photos], old.source, old.photo_mode)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status == "queued":
            self._update(job, status="canceled", message="취소됨")
            fut = self._results.pop(job_id, None)
            if fut and not fut.done():
                fut.cancel()
            return True
        if job.status in ("done", "failed", "canceled"):
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

    async def _run(self, job: Job) -> None:
        from .naver import NaverError, NotLoggedIn
        from .pipeline import Pipeline

        cfg = self.get_cfg()
        self._update(job, status="running", message="시작")
        callback = self._callbacks.pop(job.id, None)

        async def progress(msg: str) -> None:
            self._update(job, message=msg)
            log.info(msg)
            if callback:
                try:
                    await callback(msg)
                except Exception:
                    pass

        fut = self._results.pop(job.id, None)
        try:
            pipe = Pipeline(cfg, self.session)
            result = await pipe.run(job.text, progress, photos=[Path(p) for p in job.photos], photo_mode=job.photo_mode)
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
        if fut and not fut.done():
            fut.set_result(result)
