"""대시보드 서버: 버튼(프리셋)·설정 저장·작업 추가가 실제로 동작하는지 (가짜 claude 로)."""
import asyncio
import os
import stat
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from phonenaver import config, jobs, web
from tests.test_ai_cli import FAKE


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = dict(os.environ)
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE.replace("#!PY", "#!" + sys.executable), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"CLAUDE_BIN={fake}\nBROWSER_PROFILE_DIR={tmp_path / 'browser'}\nNAVER_BLOG_ID=\nIMAGE_COUNT=3\n"
        "TELEGRAM_BOT_TOKEN=\nALLOWED_CHAT_IDS=\n# 주석\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setattr(web, "PRESETS_FILE", tmp_path / "presets.json")
    monkeypatch.setattr(web, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log.jsonl"))
    yield env_file
    os.environ.clear()
    os.environ.update(saved)


def test_dashboard_api(env, tmp_path):
    async def scenario():
        state = web.State()
        state.cfg.image_dir = tmp_path / "images"
        async with TestClient(TestServer(web.build(state))) as client:
            async def post(path, **kw):
                resp = await client.post(path, **kw)
                return await resp.json()

            # 자주 쓰는 요청 버튼 저장 → 목록에 남음
            r = await post("/api/presets", json={"name": "지원금", "text": "청년 지원금 정리"})
            assert r["ok"] and r["data"][0]["name"] == "지원금"
            state_json = await (await client.get("/api/state")).json()
            assert state_json["data"]["presets"][0]["text"] == "청년 지원금 정리"

            # 설정 저장 → .env 에 반영, 주석 유지, 비밀값은 빈 칸이면 유지
            r = await post("/api/settings", json={"IMAGE_COUNT": 1, "HEADLESS": False, "PEXELS_API_KEY": ""})
            assert r["ok"] and r["data"]["IMAGE_COUNT"] == 1
            text = env.read_text(encoding="utf-8")
            assert "IMAGE_COUNT=1" in text and "HEADLESS=false" in text and "# 주석" in text
            assert "PEXELS_API_KEY=" not in text

            # 미리보기 작업 → 완료까지
            r = await post("/api/jobs", data={"text": "캠핑 준비물", "dry": "1"})
            assert r["ok"]
            job_id = r["data"]["id"]
            for _ in range(100):
                await asyncio.sleep(0.1)
                job = state.runner.jobs[job_id]
                if job.status in ("done", "failed"):
                    break
            assert job.status == "done", job.error
            assert job.title == "제목" and len(job.images) == 3 and job.dry_run  # 소제목마다 사진

            # 기록 파일에 남아서 다시 켜도 보임
            again = jobs.JobRunner(lambda: state.cfg, state.session, state.events)
            assert job_id in again.jobs

            # 빈 요청은 거절
            r = await post("/api/jobs", data={"text": ""})
            assert not r["ok"]
            await state.session.close()

    asyncio.run(scenario())
