"""claude CLI 를 가짜 스크립트로 바꿔서, 구독 계정 호출 방식(인자·환경변수·결과 파싱)을 검사한다."""
import json
import stat
import sys
from pathlib import Path

import pytest
from PIL import Image

from phonenaver.ai import AIError, Writer
from phonenaver.config import Config

FAKE = r'''#!PY
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ["FAKE_LOG"]
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "prompt": prompt, "api_key": os.environ.get("ANTHROPIC_API_KEY"),
                        "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}, ensure_ascii=False) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "login":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login"}))
    sys.exit(1)
schema = json.loads(args[args.index("--json-schema") + 1])
props = schema["properties"]
if "body_html" in props:
    out = {"title": "제목", "body_html": "<p>도입</p><h2>A</h2><p>a</p><h2>B</h2><p>b</p><h2>C</h2><p>c</p>", "tags": ["a"],
           "images": [{"query": "q", "card_text": "c"}], "category": "여행"}
elif "search_topic" in props:
    out = {"photos": ["카페 라떼 사진"], "overall": "카페 방문", "search_topic": "성수 카페"}
else:
    out = {"notes": "최신 정보", "sources": [{"title": "t", "url": "https://x.com"}]}
if mode == "text":  # structured_output 없이 result 에 코드블록으로 준 경우
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": out}))
'''


@pytest.fixture
def writer(tmp_path, monkeypatch):
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE.replace("#!PY", "#!" + sys.executable), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    cfg = Config(claude_bin=str(fake), claude_oauth_token="oauth-tok", image_dir=tmp_path / "images")
    w = Writer(cfg)
    w.log = tmp_path / "log.jsonl"
    return w


def _calls(w):
    return [json.loads(line) for line in w.log.read_text(encoding="utf-8").splitlines()]


def test_research_uses_subscription_and_web_tools(writer):
    r = writer.research("성수동 카페")
    assert r.notes == "최신 정보" and r.sources == [("t", "https://x.com")]
    call = _calls(writer)[0]
    args = call["args"]
    assert call["api_key"] is None  # API 키는 넘기지 않음 → 구독 계정 사용
    assert call["token"] == "oauth-tok"
    assert args[0] == "-p" and "--json-schema" in args
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
    assert "성수동 카페" in call["prompt"]


def test_write_with_photos_reads_files(writer, tmp_path):
    photo = tmp_path / "p.png"
    Image.new("RGB", (3000, 2000), "red").save(photo)
    post = writer.write("카페 후기", None, [], [], image_count=1, categories=["여행"], user_photo_count=1,
                        photos=[photo])
    assert post.title == "제목" and post.category == "여행"
    call = _calls(writer)[0]
    assert call["args"][call["args"].index("--tools") + 1] == "Read"
    assert "--system-prompt" in call["args"]
    resized = writer.cfg.image_dir / "ai" / "p.jpg"
    assert str(resized.resolve()) in call["prompt"]
    assert max(Image.open(resized).size) == 1568


def test_write_without_photos_has_no_tools(writer):
    writer.write("글", None, [], [])
    args = _calls(writer)[0]["args"]
    assert args[args.index("--tools") + 1] == ""


def test_result_text_fallback(writer, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "text")
    assert writer.analyze_photos([], "x").search_topic == "성수 카페"


def test_login_error_message(writer, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "login")
    with pytest.raises(AIError, match="로그인"):
        writer.research("x")


@pytest.mark.parametrize("text, attached", [
    ("사진도 첨부해서 카페 후기 써줘", 1),
    ("이 사진으로 카페 후기 써줘", 0),  # 기본은 첨부 안 함
])
def test_pipeline_photo_modes(writer, tmp_path, text, attached):
    import asyncio

    from phonenaver.command import parse
    from phonenaver.pipeline import Pipeline

    photo = tmp_path / "me.png"
    Image.new("RGB", (100, 100), "blue").save(photo)
    cfg = writer.cfg
    cfg.naver_blog_id = ""  # 네이버 없이 생성만
    pipe = Pipeline(cfg)
    result = asyncio.run(pipe.generate(parse(text), [photo]))

    assert result.photo_analysis is not None  # 두 경우 모두 사진은 분석
    assert result.photos_attached == attached
    assert len(result.images) == 3  # 도입부 뒤 + 소제목 앞마다 (소제목 3개)
    assert result.body_html.count("[[IMAGE") == 3
    assert "<p><br/></p><p><br/></p><h2>" in result.body_html  # 문단 사이 빈 줄 2개
    assert [i.source for i in result.images].count("내 사진") == attached
    write_call = [c for c in _calls(writer) if "body_html" in c["args"][c["args"].index("--json-schema") + 1]][0]
    assert "사진 분석" in write_call["prompt"]
    assert ("사진은 글에 첨부되지 않으니" in write_call["prompt"]) == (attached == 0)


def test_long_text_is_fully_analyzed(writer):
    long_text = "\n".join(f"{i}. 2024년 기준 청년 월세 지원은 월 20만원입니다." for i in range(1, 40)) + "\n마지막줄표시"
    writer.research(long_text)
    writer.write(long_text, None, [], [])
    research_call, write_call = _calls(writer)
    assert "[사용자가 보낸 글 전체]" in research_call["prompt"] and "마지막줄표시" in research_call["prompt"]
    assert "[긴 글 처리 규칙]" in write_call["prompt"] and "마지막줄표시" in write_call["prompt"]
