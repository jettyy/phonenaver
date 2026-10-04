"""Claude 로 사진 분석 + 최신 정보 조사 + 네이버 블로그 글 작성.

API 키 대신 **내가 구독 중인 Claude 계정(Pro/Max)** 으로 동작한다.
Claude Code CLI(`claude -p`)를 실행해서 결과를 받아오며, CLI 는 `claude` 로그인 정보나
`claude setup-token` 으로 만든 CLAUDE_CODE_OAUTH_TOKEN 을 사용한다. (구독 사용량 한도가 적용됨)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from .config import CLAUDE_MODEL, Config
from .fetcher import Page
from .command import LONG_TEXT, rank_target
from .images import resize_for_ai

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger(__name__)


class AIError(RuntimeError):
    pass


class ImagePlan(BaseModel):
    query: str = Field(description="이 자리에 어울리는 사진을 찾을 영어 검색어 2~4단어 (예: 'jeju beach sunset')")
    card_text: str = Field(description="사진이 없을 때 만들 카드 이미지 문구. 한국어 20자 이내")


class BlogPost(BaseModel):
    title: str = Field(description="네이버 블로그 글 제목 (검색 키워드를 앞쪽에, 40자 이내)")
    body_html: str = Field(description="본문 HTML. h2, h3, p, strong, ul, ol, li, a, blockquote, hr, table 만 사용")
    tags: list[str] = Field(description="네이버 태그용 키워드 5~10개, # 없이")
    images: list[ImagePlan] = Field(
        description="본문의 [[IMAGE1]], [[IMAGE2]] ... 순서대로 각 자리의 이미지 계획"
    )
    category: str = Field(description="카테고리 목록 중 가장 어울리는 이름 그대로. 목록이 없으면 빈 문자열")


class PhotoAnalysis(BaseModel):
    photos: list[str] = Field(
        description="사진마다 한 항목씩 순서대로: 무엇이 찍혔는지, 장소·제품·브랜드·메뉴 추정, 사진 속 글자(가격표·간판 등), 분위기, 블로그에 쓸 만한 포인트"
    )
    overall: str = Field(description="사진 전체로 알 수 있는 상황·주제 요약 (2~4문장)")
    search_topic: str = Field(description="최신 정보를 검색할 한국어 주제 한 줄. 검색할 것이 없으면 빈 문자열")

    def as_text(self) -> str:
        lines = [f"사진 {i}: {d}" for i, d in enumerate(self.photos, 1)]
        return "\n".join(lines + [f"전체: {self.overall}"])


class Source(BaseModel):
    title: str
    url: str


class ResearchOut(BaseModel):
    notes: str = Field(description="조사 결과 정리 (항목마다 기준 날짜와 출처 URL 포함)")
    sources: list[Source] = Field(description="참고한 출처 목록")


@dataclass
class Research:
    notes: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url)


def today() -> str:
    now = datetime.now(KST)
    return f"{now:%Y년 %m월 %d일} ({'월화수목금토일'[now.weekday()]})"


def _extract_json(text: str):
    """응답 텍스트에서 JSON 객체를 꺼낸다 (```json 코드블록 허용)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


def find_claude(name: str = "claude") -> str:
    """claude 실행 파일 찾기. PATH 에 없어도 설치 프로그램이 쓰는 기본 위치를 확인한다."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    for cand in (
        home / ".local/bin/claude",           # 공식 설치 스크립트 (맥)
        home / ".local/bin/claude.exe",       # 공식 설치 스크립트 (윈도우)
        home / ".claude/local/claude",
        Path("/opt/homebrew/bin/claude"),     # 맥 Homebrew
        Path("/usr/local/bin/claude"),
        home / ".npm-global/bin/claude",
        home / "AppData/Roaming/npm/claude.cmd",  # 윈도우 npm
    ):
        if cand.exists():
            return str(cand)
    return name


class Writer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bin = find_claude(cfg.claude_bin)
        self.workdir = cfg.image_dir.parent.resolve() / "claude"
        self.workdir.mkdir(parents=True, exist_ok=True)

    def _env(self) -> dict:
        env = dict(os.environ)
        # API 키가 있으면 CLI 가 구독 대신 API 과금으로 동작하므로 제거
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if self.cfg.claude_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.cfg.claude_oauth_token
        return env

    def _run(self, prompt: str, schema: type[BaseModel], tools: list[str], system: str | None = None) -> dict:
        """claude -p 로 한 번 실행하고, 스키마에 맞는 JSON 을 돌려받는다."""
        cmd = [
            self.bin, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema.model_json_schema(), ensure_ascii=False),
            "--no-session-persistence",
            "--max-turns", str(self.cfg.claude_max_turns),
            "--tools", ",".join(tools),
        ]
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        cmd += ["--model", CLAUDE_MODEL]  # 모델 고정 (sonnet)
        if system:
            cmd += ["--system-prompt", system]
        cmd += ["--add-dir", str(self.cfg.image_dir.resolve())]
        prompt += "\n\n결과는 지정된 JSON 스키마에 맞춰 반환하세요."
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                cwd=self.workdir, env=self._env(), timeout=self.cfg.claude_timeout,
            )
        except FileNotFoundError as exc:
            raise AIError(
                "Claude Code(claude) 가 설치되어 있지 않습니다. 맥은 터미널에서 "
                "`curl -fsSL https://claude.ai/install.sh | bash` 로 설치한 뒤 "
                "`claude` 를 실행해 구독 계정으로 로그인하세요."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AIError(f"Claude 응답이 {self.cfg.claude_timeout}초 안에 오지 않았습니다") from exc

        out = proc.stdout.strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            detail = (proc.stderr or out)[-500:]
            raise AIError(self._explain(detail)) from None
        if data.get("is_error") or data.get("subtype", "success") != "success":
            raise AIError(self._explain(str(data.get("result") or data.get("subtype") or data)))

        result = data.get("structured_output")
        if result is None:
            try:
                result = _extract_json(data.get("result", ""))
            except (json.JSONDecodeError, ValueError) as exc:
                raise AIError("Claude 응답에서 결과(JSON)를 찾지 못했습니다") from exc
        try:
            schema.model_validate(result)
        except ValidationError as exc:
            raise AIError(f"Claude 응답 형식이 맞지 않습니다: {exc.errors()[:2]}") from exc
        return result

    @staticmethod
    def _explain(detail: str) -> str:
        low = detail.lower()
        if any(k in low for k in ("login", "log in", "authenticat", "oauth", "credential", "401")):
            return (
                "Claude 구독 계정 로그인이 필요합니다. 이 컴퓨터에서 `claude` 를 실행해 로그인하거나, "
                "`claude setup-token` 으로 받은 토큰을 .env 의 CLAUDE_CODE_OAUTH_TOKEN 에 넣으세요."
            )
        if any(k in low for k in ("usage limit", "rate limit", "limit reached", "429")):
            return "Claude 구독 사용량 한도에 도달했습니다. 한도가 초기화된 뒤 다시 시도하세요."
        return f"Claude 실행 실패: {detail.strip()[:300]}"

    def _photo_prompt(self, photos: list[Path]) -> str:
        paths = [resize_for_ai(p, self.cfg.image_dir / "ai") for p in photos]
        listing = "\n".join(f"- 사진 {i}: {p.resolve()}" for i, p in enumerate(paths, 1))
        return f"[사용자가 보낸 사진] Read 도구로 아래 파일을 모두 열어 직접 보고 참고하세요.\n{listing}"

    # ── 0단계: 보낸 사진 분석 ─────────────────────────────────
    def analyze_photos(self, photos: list[Path], instruction: str) -> PhotoAnalysis:
        prompt = (
            f"오늘은 {today()}입니다. 아래 사진 {len(photos)}장은 네이버 블로그 글에 쓸 자료입니다.\n"
            f"{self._photo_prompt(photos)}\n\n"
            "사진을 꼼꼼히 분석해 주세요. 확실하지 않은 추정은 '~로 보임'이라고 표시하고, "
            "사진 속 글자(메뉴판, 가격, 간판, 제품명)는 정확히 옮겨 적으세요.\n\n"
            f"사용자 지시: {instruction or '(없음)'}"
        )
        return PhotoAnalysis.model_validate(self._run(prompt, PhotoAnalysis, tools=["Read"]))

    # ── 1단계: 최신 정보 조사 (웹 검색) ─────────────────────────
    def research(self, topic: str) -> Research:
        prompt = (
            f"오늘은 {today()}입니다.\n"
            "아래 주제로 네이버 블로그 글을 쓰려고 합니다. 웹 검색으로 가장 최신 정보를 조사해서 "
            "글 작성에 필요한 사실, 수치, 날짜, 가격, 일정, 변경 사항 등을 한국어로 정리해 주세요.\n"
            f"- 검색은 {self.cfg.max_searches}번 이내로 하세요.\n"
            "- 오래된 정보와 최신 정보가 다르면 최신 기준으로, 기준 날짜를 함께 적어 주세요.\n"
            "- 확인되지 않은 내용은 '미확인'으로 표시하세요.\n"
            "- 각 항목 뒤에 근거 출처 URL을 적어 주세요.\n\n"
            f"주제/지시: {topic}"
        )
        rank = rank_target(topic)
        if rank is not None:
            upto = f"1위부터 {rank}위까지" if rank else "1위부터 확인되는 마지막 순위까지 (최소 10위 이상)"
            prompt += (
                f"\n\n[순위 조사] 이 글은 순위 글입니다. {upto} 순위 목록을 빠짐없이 확보하세요. "
                "순위마다 이름과 핵심 수치(점수·금액·지표 등)를 적고, 순위의 기준(어느 기관·조사·연도 기준인지)과 출처를 밝히세요. "
                "자료마다 순위가 다르면 가장 최신·공신력 있는 자료 하나를 기준으로 삼으세요."
            )
        if len(topic) >= LONG_TEXT:
            prompt = (
                f"오늘은 {today()}입니다.\n"
                "사용자가 네이버 블로그 글의 재료로 아래 글을 통째로 보냈습니다. 이 글을 새로 고쳐 쓰려고 합니다.\n"
                "1) 아래 글을 **처음부터 끝까지 전부** 읽고, 다루는 주제와 핵심 주장·수치·가격·날짜·제도·일정을 모두 뽑으세요.\n"
                "2) 뽑은 항목마다 웹 검색으로 **지금도 맞는지** 확인하세요. 바뀌었거나 오래된 내용은 최신 값과 기준 날짜로,\n"
                "   원문에 없지만 독자에게 필요한 최신 소식(새 제도, 변경 예정, 신청 기간 등)도 찾아 더하세요.\n"
                f"3) 검색은 {self.cfg.max_searches}번 이내로 하고, 확인하지 못한 내용은 '미확인'으로 표시하세요.\n"
                "4) notes 에는 '원문 내용 → 최신 확인 결과(기준 날짜, 출처 URL)' 형태로 정리하세요.\n\n"
                f"[사용자가 보낸 글 전체]\n{topic}"
            )
        data = ResearchOut.model_validate(self._run(prompt, ResearchOut, tools=["WebSearch", "WebFetch"]))
        return Research(notes=data.notes, sources=[(s.title or s.url, s.url) for s in data.sources])

    # ── 2단계: 블로그 글 작성 ─────────────────────────────────
    def write(
        self,
        instruction: str,
        research: Research | None,
        pages: list[Page],
        insert_urls: list[str],
        image_count: int = 0,
        categories: list[str] | None = None,
        user_photo_count: int = 0,
        photos: list[Path] | None = None,
        photo_analysis: PhotoAnalysis | None = None,
        image_layout: str = "fixed",
        fix_note: str = "",
    ) -> BlogPost:
        parts = [f"오늘 날짜: {today()}", f"[사용자 지시]\n{instruction or '(지시 없음 - 링크 내용으로 글 작성)'}"]
        rank = rank_target(instruction)
        if rank is not None:
            upto = f"1위부터 {rank}위까지" if rank else "1위부터 자료에서 확인되는 마지막 순위까지 (최소 10위 이상)"
            parts.append(
                "[순위 글 규칙] 이 글은 순위 글입니다. 반드시 지키세요.\n"
                f"- 본문 앞쪽(도입부 바로 다음 소제목)에 순위표 <table> 을 넣고, {upto} 한 줄도 빠짐없이 순서대로 넣으세요.\n"
                "- 표 첫 줄은 <th> 제목 줄, 첫 열은 '순위'(1위, 2위 …), 이어서 이름과 핵심 수치·특징 열을 둡니다.\n"
                "- '…', '이하 생략', '나머지는' 처럼 중간을 줄이지 마세요. 표 아래에 순위 기준(기관·연도)을 한 줄로 밝히세요.\n"
                "- 표 다음에는 상위 순위부터 h3 소제목('1위 ○○')으로 하나씩 설명합니다 (항목이 많으면 상위 10개는 자세히, 나머지는 짧게)."
            )
        if len(instruction) >= LONG_TEXT:
            parts.append(
                "[긴 글 처리 규칙] 위 [사용자 지시] 는 사용자가 통째로 보낸 글(원문 자료)입니다.\n"
                "- 원문 전체를 빠짐없이 분석해서 다루는 주제와 핵심 내용을 모두 반영하세요. 앞부분만 보고 쓰지 마세요.\n"
                "- [최신 정보 조사 결과] 와 다르거나 오래된 내용은 최신 정보로 고치고, 새로 확인된 소식을 더하세요.\n"
                "- 원문 문장을 그대로 옮기지 말고, 구성과 문장을 새로 써서 독자가 읽기 좋은 블로그 글로 만드세요.\n"
                "- 원문 안에 사용자가 따로 적은 요청(말투, 분량, 카테고리 등)이 있으면 그것을 따르세요."
            )
        if photo_analysis:
            parts.append(
                "[사용자가 보낸 사진 분석]\n" + photo_analysis.as_text() + "\n"
                "사진에서 확인한 내용(장소, 메뉴, 가격, 제품 특징, 분위기 등)을 글에 구체적으로 반영하세요."
            )
        if research and research.notes:
            parts.append(f"[최신 정보 조사 결과]\n{research.notes}")
        for i, page in enumerate(pages, 1):
            if page.ok:
                note = " (길어서 앞부분만)" if page.truncated else ""
                kind = " (유튜브 영상)" if page.kind == "youtube" else ""
                parts.append(
                    f"[참고 링크 {i}{kind}{note}] {page.title}\nURL: {page.url}\n---\n{page.text}\n---"
                )
            else:
                parts.append(f"[참고 링크 {i}] {page.url} - 읽지 못함: {page.error}")
        if any(p.ok and p.kind == "youtube" for p in pages):
            parts.append(
                "[유튜브 영상 처리 규칙] 유튜브 참고 링크의 '영상 대사' 는 말로 한 내용을 받아 적은 자막입니다.\n"
                "- 영상 전체 대사를 처음부터 끝까지 분석해서, 영상이 전하는 핵심 정보·주장·수치·팁·순서를 빠짐없이 뽑으세요.\n"
                "- 블로그 작성자가 직접 정리한 정보글처럼 쓰세요. 유튜브·영상·채널·유튜버·'영상에서'·'이 영상은' 같은 출처 언급은 하지 마세요.\n"
                "- 자막 문장이나 영상의 말하는 순서를 그대로 따라 쓰지 말고, 뽑은 정보를 바탕으로 제목·도입·소제목 구성과 문장을 "
                "완전히 새로 쓰세요. 필요하면 정보를 표·목록·요약으로 재정리하고, 독자에게 도움이 될 설명을 덧붙이세요.\n"
                "- 영상 속 사람의 개인 경험(직접 가 봤다, 써 봤다 등)을 작성자의 경험처럼 1인칭으로 쓰지 마세요. 정보·방법·팁으로 바꿔 전달하세요.\n"
                "- 구어체·반복·추임새는 정리하고, 자동 생성 자막의 오타·잘못 들은 단어는 문맥으로 바로잡고, 확실하지 않은 고유명사·수치는 단정하지 마세요.\n"
                "- 자막을 가져오지 못한 영상은 제목·설명에 있는 정보만 쓰고, 영상 내용을 지어내지 마세요."
            )
        parts.append(
            "[표 규칙] 본문에 반드시 <table> 을 1개 이상 넣으세요. 비교·정리·일정·가격·장단점·체크리스트처럼 "
            "표로 보면 편한 내용을 골라, 첫 줄은 <th> 제목 줄로 만드세요. 표가 없는 글은 다시 쓰게 됩니다."
        )
        if fix_note:
            parts.append(f"[다시 쓰기 요청] 앞서 쓴 글에 문제가 있었습니다. 이번에는 꼭 고치세요:\n{fix_note}")
        if insert_urls:
            links = "\n".join(f"- {u}" for u in insert_urls)
            parts.append(
                "[본문에 반드시 넣을 링크]\n" + links + "\n"
                "위 링크는 모두 <a href=\"URL 그대로\">자연스러운 안내 문구</a> 형태로 본문의 알맞은 위치에 넣으세요. "
                "URL은 한 글자도 바꾸지 마세요."
            )
        else:
            parts.append("[링크 규칙] 사용자가 넣으라고 한 링크가 없으므로 본문에 외부 링크를 넣지 마세요.")
        if image_count:
            markers = ", ".join(f"[[IMAGE{i}]]" for i in range(1, image_count + 1))
            photo_note = (
                f"\n[[IMAGE1]]~[[IMAGE{user_photo_count}]] 에는 사용자가 보낸 사진 1~{user_photo_count} 이 "
                "순서대로 들어갑니다. 각 표시는 그 사진 내용을 설명하는 문단 바로 앞이나 뒤에 두세요. "
                "나머지 자리는 글 내용에 맞는 이미지 계획을 세우세요."
                if user_photo_count else ""
            )
            if image_layout == "section":
                parts.append(
                    "[이미지] 사진은 프로그램이 도입부 바로 뒤 1장, 그리고 소제목(h2) 앞마다 1장씩 자동으로 넣습니다. "
                    "본문에 [[IMAGE]] 표시는 넣지 마세요. images 에는 순서대로 '도입부용 1개 + 소제목 순서대로 1개씩' "
                    f"(최대 {image_count}개) 계획을 적어, 각 사진이 바로 뒤에 오는 섹션 내용과 어울리게 하세요."
                    + photo_note.replace("각 표시는 그 사진 내용을 설명하는 문단 바로 앞이나 뒤에 두세요. ", "")
                )
            else:
                parts.append(
                    f"[이미지 {image_count}장]\n본문에 {markers} 를 각각 독립된 <p> 문단으로 한 번씩 넣어 "
                    "이미지가 들어갈 자리를 표시하세요. 첫 이미지는 도입부 바로 뒤, 나머지는 내용이 바뀌는 소제목 근처에 "
                    f"고르게 배치합니다. images 에는 순서대로 {image_count}개의 계획을 적으세요." + photo_note
                )
        if categories:
            listing = "\n".join(f"- {c}" for c in categories)
            parts.append(
                "[내 블로그 카테고리]\n" + listing + "\n"
                "글 내용에 가장 잘 어울리는 카테고리 하나를 골라 category 에 목록의 이름 그대로 적으세요. "
                "사용자가 카테고리를 지정했다면 그것을 따릅니다."
            )

        if photos and not user_photo_count:
            parts.append(
                "[사진 사용 규칙] 사용자가 보낸 사진의 내용이 이 글의 핵심 소재입니다. 사진에서 확인한 장소·음식·제품·"
                "가격·글자·분위기를 바탕으로 구체적으로 쓰세요. 단, 사진은 글에 첨부되지 않으니 '아래 사진처럼', "
                "'사진을 보시면' 같은 표현은 쓰지 마세요. 사진에서 확인할 수 없는 내용은 지어내지 마세요."
            )
        if photos:
            parts.insert(0, self._photo_prompt(photos))
        data = self._run("\n\n".join(parts), BlogPost, tools=["Read"] if photos else [], system=WRITER_SYSTEM)
        return BlogPost.model_validate(data)


WRITER_SYSTEM = """당신은 네이버 블로그 상위노출 경험이 많은 한국어 블로그 작가입니다.
사용자의 지시와 제공된 자료(최신 조사 결과, 참고 링크 본문)만 근거로 네이버 블로그 글을 씁니다.

글쓰기 원칙
- 친근하고 읽기 쉬운 존댓말(~요, ~습니다 혼용). 광고 티 나는 과장 표현은 피합니다.
- 도입부 2~3문장에서 독자가 얻을 내용을 먼저 알려 줍니다.
- 소제목(h2) 3~6개로 구성하고, 필요하면 h3 를 씁니다. 문단은 2~3문장으로 짧게 끊고, 문단마다 <p> 로 나누세요 (빈 줄은 프로그램이 넣습니다).
- 핵심 수치·날짜·주의 사항은 <strong> 으로 강조하고, 나열은 ul/ol, 비교·정리는 table 을 씁니다. 모든 글에 표가 최소 1개 들어갑니다.
- 마지막에 요약 또는 한 줄 정리로 마무리합니다.
- 분량은 사용자가 따로 말하지 않으면 공백 포함 2,000~3,000자.
- 자료에 없는 사실, 수치, 후기를 지어내지 않습니다. 날짜가 중요한 정보는 기준일을 밝힙니다.
- 참고 링크를 분석해 쓸 때는 원문을 그대로 베끼지 말고 내 말로 재구성하고, 필요한 경우 내 의견·정리를 덧붙입니다.
- 사용자가 말투, 분량, 구성, 대상 독자 등을 지정하면 그것을 우선합니다.

형식 규칙
- body_html 에는 제목(h1)을 넣지 않습니다. 허용 태그: h2, h3, p, br, strong, em, u, ul, ol, li, a, blockquote, hr, table, tr, th, td.
- <img> 태그, 이모지 남용, 마크다운 문법(**, ##)은 쓰지 않습니다. 이미지 자리는 [[IMAGEn]] 표시로만 나타냅니다.
- tags 는 검색에 쓰일 핵심 키워드 5~10개 (# 없이)."""
