"""휴대폰에서 온 메시지를 해석해 '무엇을 쓸지'를 정한다.

- 링크가 없으면: 키워드/문장 주제로 최신 정보를 검색해서 쓴다.
- 링크만 주면: 링크 내용을 분석해서 쓴다.
- "넣어/삽입/첨부/걸어" 같은 말과 함께 준 링크는 본문에 하이퍼링크로 넣는다.
- "그대로:" 로 시작하면 AI 없이 보낸 글을 그대로 임시저장한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

URL_RE = re.compile(
    r"(?:https?://|(?<![\w./@-])(?=(?:m\.|www\.)?(?:blog\.naver\.com|youtube\.com|youtu\.be|naver\.me)/))"
    r"[^\s<>\"'\]\)）」』]+"
)
# 링크를 '본문에 넣어 달라'는 뜻으로 보는 표현
INSERT_WORDS = ("넣어", "넣고", "삽입", "첨부", "걸어", "걸고", "달아", "포함", "링크추가", "링크 추가")
# 링크가 있어도 최신 정보를 추가로 검색하라는 표현
SEARCH_WORDS = ("검색", "최신", "찾아", "조사", "요즘", "최근")
RAW_PREFIXES = ("그대로:", "그대로 :", "원문:", "/raw")
DRY_PREFIXES = ("/test", "/dry", "미리보기:")
CATEGORY_RE = re.compile(r"카테고리\s*[:：]\s*([^\n,]+?)\s*(?:[,\n]|$)")
# 보낸 사진은 기본적으로 '분석해서 글 내용으로만' 쓰고 첨부하지 않는다.
# 아래처럼 사진을 글에 넣어 달라고 분명히 말한 경우에만 첨부한다.
ATTACH_RE = re.compile(
    r"(?:사진|이미지)(?:도|을|를|은|는)?\s*(?:같이\s*|함께\s*|그대로\s*|글에\s*|본문에\s*)*"
    r"(?:첨부|넣어|넣고|올려|붙여)(?!\s*(?:하지|는\s*하지|말|마|없이))",
)
NO_IMAGE_RE = re.compile(r"(이미지|사진)\s*(없이|빼고|넣지\s*마)")


@dataclass
class Command:
    text: str
    instruction: str
    urls: list[str] = field(default_factory=list)
    insert_urls: list[str] = field(default_factory=list)
    search: bool = True
    raw: bool = False
    dry_run: bool = False
    category: str | None = None  # "카테고리: 여행" 처럼 직접 지정한 경우
    no_images: bool = False  # "이미지 없이"
    # 보낸 사진 처리: "analyze" = 분석해서 글 내용으로만 씀(기본), "attach" = 분석 + 사진도 본문에 첨부
    photo_mode: str = "analyze"

    @property
    def rank_target(self) -> int | None:
        return rank_target(self.instruction)

    @property
    def is_long(self) -> bool:
        """긴 글을 붙여넣은 경우: 전체를 분석하고 최신 정보로 확인해서 새로 쓴다."""
        return len(self.instruction) >= LONG_TEXT

    @property
    def analyze_urls(self) -> list[str]:
        """본문 삽입용이 아닌, 분석 대상으로만 준 링크."""
        return [u for u in self.urls if u not in self.insert_urls]


LONG_TEXT = 300  # 이보다 긴 글은 '분석할 원문 자료' 로 본다

# '순위' 글: 순위표를 1위부터 끝까지 빠짐없이 넣는다
RANK_RE = re.compile(r"순위|랭킹|ranking|top\s*\d+|탑\s*\d+|best\s*\d+|베스트\s*\d+|\d+\s*위", re.IGNORECASE)
RANK_N_RE = re.compile(r"(?:top|탑|best|베스트)\s*(\d{1,3})|(\d{1,3})\s*위(?:까지)?|(\d{1,3})\s*선(?![가-힣])", re.IGNORECASE)
RANK_DEFAULT_MIN = 10  # 몇 위까지인지 안 정했을 때 최소

# 저장 방식을 메시지로 정하기: "발행해줘", "임시저장", "21시 30분에 발행", "오후 9시 발행"
DRAFT_RE = re.compile(r"임시\s*저장")
PUBLISH_RE = re.compile(r"(?:바로|즉시|지금|예약)?\s*발행(?!\s*(?:하지|은\s*하지|는\s*하지|말|마|없이|안))")
PUBLISH_TIME_RE = re.compile(
    r"(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*(?:시|:)\s*(?:(\d{1,2})\s*분?|반)?\s*(?:에|쯤)?\s*(?:예약\s*)?발행"
)


def publish_options(text: str) -> tuple[str | None, str | None]:
    """(저장 방식 'draft'/'publish' 또는 None=설정대로, 발행 시각 'HH:MM' 또는 None)."""
    m = PUBLISH_TIME_RE.search(text)
    if m:
        ampm, hour, minute = m.group(1), int(m.group(2)), m.group(3)
        mins = 30 if minute is None and "반" in m.group(0) else int(minute or 0)
        if ampm in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        if ampm in ("오전", "아침") and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= mins <= 59:
            return "publish", f"{hour:02d}:{mins:02d}"
    if DRAFT_RE.search(text) or re.search(r"발행\s*(?:하지|은\s*하지|는\s*하지|말|마|없이|안)", text):
        return "draft", None
    if PUBLISH_RE.search(text):
        return "publish", None
    return None, None


# 유튜브 채널·블로그 기간: "최근 3개월", "1년", "2주", "10일", "한 달", "반년", "10개만"
CHANNEL_MONTHS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:개월|달)")
CHANNEL_YEARS_RE = re.compile(r"(?<!\d)(\d)\s*년(?!도)")
CHANNEL_WEEKS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*주(?:일)?(?:간|치|동안)?")
CHANNEL_DAYS_RE = re.compile(r"(?<!\d)(\d{1,3})\s*일(?:간|치|동안)?(?!\s*(?:\d|전|째|차))")
WORD_PERIODS = {"일주일": 7 / 30.44, r"한\s*주": 7 / 30.44, "보름": 15 / 30.44, r"한\s*달": 1, r"두\s*달": 2,
                r"세\s*달": 3, "반년": 6, r"일\s*년": 12}
WORD_PERIOD_RE = re.compile("|".join(f"(?:{w})" for w in WORD_PERIODS))
CHANNEL_LIMIT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*개(?!월)(?:만|까지)?")
CHANNEL_DEFAULT_MONTHS = 6
DAYS_PER_MONTH = 30.44


def _period(text: str) -> float | None:
    """메시지에 적힌 기간(개월 단위, 2주면 약 0.46). 없으면 None."""
    found = []
    for rx, unit in ((CHANNEL_MONTHS_RE, 1), (CHANNEL_YEARS_RE, 12),
                     (CHANNEL_WEEKS_RE, 7 / DAYS_PER_MONTH), (CHANNEL_DAYS_RE, 1 / DAYS_PER_MONTH)):
        m = rx.search(text)
        if m and int(m.group(1)) > 0:
            found.append((m.start(), int(m.group(1)) * unit))
    m = WORD_PERIOD_RE.search(text)
    if m:
        for w, v in WORD_PERIODS.items():
            if re.fullmatch(w, m.group(0)):
                found.append((m.start(), v))
                break
    return min(found)[1] if found else None  # 여러 개면 먼저 적은 것


def period_label(months: float | None) -> str:
    if not months:
        return "전체 기간"
    days = round(months * DAYS_PER_MONTH)
    if days < 28:
        return f"최근 {days // 7}주" if days % 7 == 0 else f"최근 {days}일"
    if float(months).is_integer():
        return f"최근 {int(months) // 12}년" if months % 12 == 0 else f"최근 {int(months)}개월"
    return f"최근 {days}일"


def channel_options(text: str) -> tuple[float, int | None, str]:
    """(몇 개월 - 2주처럼 한 달보다 짧으면 소수, 최대 몇 개, 기간·개수 표현을 뺀 나머지 지시)."""
    months = _period(text) or CHANNEL_DEFAULT_MONTHS
    if float(months).is_integer():
        months = int(months)
    n = CHANNEL_LIMIT_RE.search(text)
    limit = int(n.group(1)) if n else None
    rest = text
    for rx in (CHANNEL_MONTHS_RE, CHANNEL_YEARS_RE, CHANNEL_WEEKS_RE, CHANNEL_DAYS_RE, WORD_PERIOD_RE, CHANNEL_LIMIT_RE):
        rest = rx.sub(" ", rest)
    rest = re.sub(r"(최근|채널|영상들?|의|치|간|동안|을|를|전부|전체|모두|모든|하나씩|각각|블로그에?|글들)\s*", " ", rest)
    return months, limit, clean_text(rest)


ALL_RE = re.compile(r"전부|전체|모든\s*글|글\s*모두|다\s*써")


def wants_all(text: str) -> bool:
    """블로그 글을 기간 제한 없이 전부 쓰라는 말인지 ('최근 3개월' 처럼 기간을 정했으면 그 기간)."""
    return bool(ALL_RE.search(text)) and _period(text) is None


def rank_target(text: str) -> int | None:
    """순위 글이면 몇 위까지 쓸지 (정하지 않았으면 0), 순위 글이 아니면 None."""
    head = text[:LONG_TEXT * 2]  # 긴 원문 안의 숫자에 휘둘리지 않게 앞부분 지시만 본다
    if not RANK_RE.search(head):
        return None
    nums = [int(next(g for g in m.groups() if g)) for m in RANK_N_RE.finditer(head)]
    nums = [n for n in nums if 3 <= n <= 300]
    return max(nums) if nums else 0


def clean_text(text: str) -> str:
    """줄바꿈은 살리고, 줄 안의 공백과 너무 많은 빈 줄만 정리."""
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:!?…~")
    return url if re.match(r"https?://", url) else "https://" + url  # 'm.blog.naver.com/아이디' 처럼 보낸 주소


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    compact = text.replace(" ", "")
    return any(w.replace(" ", "") in compact for w in words)


def parse(text: str) -> Command:
    text = text.strip()
    dry_run = False
    for prefix in DRY_PREFIXES:
        if text.lower().startswith(prefix):
            dry_run = True
            text = text[len(prefix):].strip()
            break

    category = None
    m = CATEGORY_RE.search(text)
    if m:
        category = m.group(1).strip()
        text = (text[:m.start()] + "\n" + text[m.end():]).strip()
    no_images = bool(NO_IMAGE_RE.search(text))
    photo_mode = "attach" if ATTACH_RE.search(text) else "analyze"

    for prefix in RAW_PREFIXES:
        if text.lower().startswith(prefix):
            body = text[len(prefix):].strip()
            return Command(
                text=body, instruction=body, raw=True, search=False, dry_run=dry_run,
                category=category, no_images=no_images, photo_mode=photo_mode,
            )

    urls: list[str] = []
    insert_urls: list[str] = []
    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    # 여러 줄 중 '링크 + 넣어' 가 같은 줄에 있으면 그 줄의 링크만 삽입용으로 본다.
    # 그렇지 않으면 메시지 어딘가에 '넣어' 가 있을 때 모든 링크를 삽입용으로 본다.
    per_line = len(lines) > 1 and any(
        URL_RE.search(ln) and _has_any(URL_RE.sub(" ", ln), INSERT_WORDS) for ln in lines
    )
    message_insert = _has_any(URL_RE.sub(" ", text), INSERT_WORDS)

    for line in lines:
        line_insert = _has_any(URL_RE.sub(" ", line), INSERT_WORDS) if per_line else message_insert
        for url in (_clean_url(u) for u in URL_RE.findall(line)):
            if url in urls:
                continue
            urls.append(url)
            if line_insert:
                insert_urls.append(url)

    instruction = clean_text(URL_RE.sub(" ", text))
    analyze_only = [u for u in urls if u not in insert_urls]
    # 분석할 링크를 줬으면 그 링크가 주 재료. 검색하라는 말이 있을 때만 추가 검색.
    search = not analyze_only or _has_any(instruction, SEARCH_WORDS)
    return Command(
        text=text,
        instruction=instruction,
        urls=urls,
        insert_urls=insert_urls,
        search=search,
        dry_run=dry_run,
        category=category,
        no_images=no_images,
        photo_mode=photo_mode,
    )
