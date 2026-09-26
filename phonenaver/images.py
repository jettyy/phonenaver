"""글에 넣을 이미지 준비.

우선순위
1. 텔레그램으로 보낸 내 사진
2. (THUMBNAIL_CARD) 첫 장은 제목이 들어간 썸네일 카드
3. Pexels 무료 사진 (PEXELS_API_KEY 가 있을 때, 상업적 이용 가능)
4. 자동 생성 카드 이미지 (키가 없거나 검색 실패 시)

본문 HTML 에는 [[IMAGE1]] 같은 표시를 넣어 두고, 에디터에서 그 자리에 사진을 업로드한다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

from .config import Config

log = logging.getLogger(__name__)

MARKER_RE = re.compile(r"\[\[IMAGE(\d+)\]\]")
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Bold.ttf"
FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
# (배경 위, 배경 아래, 글자색)
PALETTES = [
    ((33, 150, 83), (18, 99, 58), (255, 255, 255)),
    ((37, 99, 235), (30, 58, 138), (255, 255, 255)),
    ((250, 204, 21), (234, 179, 8), (30, 30, 30)),
    ((236, 72, 153), (157, 23, 77), (255, 255, 255)),
    ((15, 23, 42), (51, 65, 85), (255, 255, 255)),
]


def marker(i: int) -> str:
    return f"[[IMAGE{i}]]"


@dataclass
class PreparedImage:
    path: Path
    source: str  # "내 사진" / "Pexels" / "카드"
    credit: str = ""


# ── 본문 안 이미지 위치 ─────────────────────────────────────

def _new_marker(soup: BeautifulSoup, i: int):
    p = soup.new_tag("p")
    p.string = marker(i)
    return p


def ensure_markers(body_html: str, count: int) -> str:
    """[[IMAGE1..N]] 표시를 정확히 N개, 각각 독립된 문단으로 맞춘다.

    AI 가 넣은 위치는 그대로 살리고, 빠진 번호는 도입부 뒤·소제목 앞에 고르게 넣는다.
    범위를 벗어나거나 중복된 표시는 지운다.
    """
    soup = BeautifulSoup(body_html, "html.parser")
    placed: dict[int, object] = {}  # 번호 → 표시가 있던 최상위 블록
    for node in list(soup.find_all(string=MARKER_RE)):
        top = node
        while top.parent is not None and top.parent is not soup:
            top = top.parent
        for m in MARKER_RE.findall(str(node)):
            n = int(m)
            if 1 <= n <= count and n not in placed:
                placed[n] = top
        node.replace_with(MARKER_RE.sub("", str(node)))

    # 표시가 있던 블록 바로 뒤에 표시 전용 문단을 넣고, 비어버린 블록은 지운다
    by_block: dict[int, tuple[object, list[int]]] = {}
    for n, block in placed.items():
        by_block.setdefault(id(block), (block, []))[1].append(n)
    for block, nums in by_block.values():
        for n in sorted(nums, reverse=True):
            block.insert_after(_new_marker(soup, n))
        if getattr(block, "name", None) == "p" and not block.get_text(strip=True):
            block.decompose()

    missing = [i for i in range(1, count + 1) if i not in placed]
    if missing:
        blocks = [b for b in soup.contents if getattr(b, "name", None) and not MARKER_RE.fullmatch(b.get_text(strip=True))]
        slots: list = blocks[:1]  # 도입부 첫 문단 뒤
        slots += [h.find_previous_sibling() for h in blocks if h.name == "h2"][1:]  # 두 번째 소제목부터 그 앞
        step = max(1, len(blocks) // (count + 1))
        slots += blocks[step::step]
        slots = [s for s in slots if s is not None]
        used: set[int] = set()
        for i in missing:
            anchor = next((s for s in slots if id(s) not in used), None)
            if anchor is None:
                soup.append(_new_marker(soup, i))
                continue
            used.add(id(anchor))
            anchor.insert_after(_new_marker(soup, i))
    return str(soup)


def strip_markers(text: str) -> str:
    return re.sub(r"\n?\s*\[\[IMAGE\d+\]\]\s*\n?", "\n", text).strip()


# ── 이미지 소스 ────────────────────────────────────────────

def _out_path(cfg: Config, key: str, ext: str) -> Path:
    cfg.image_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(key.encode()).hexdigest()[:8]
    return cfg.image_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{digest}.{ext}"


def search_pexels(cfg: Config, query: str, exclude: set[str]) -> PreparedImage | None:
    if not cfg.pexels_api_key or not query.strip():
        return None
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(
                "https://api.pexels.com/v1/search",
                params={"query": query, "per_page": 10, "orientation": "landscape"},
                headers={"Authorization": cfg.pexels_api_key},
            )
            resp.raise_for_status()
            for photo in resp.json().get("photos", []):
                url = photo.get("src", {}).get("large2x") or photo.get("src", {}).get("large")
                if not url or url in exclude:
                    continue
                img = client.get(url)
                img.raise_for_status()
                path = _out_path(cfg, url, "jpg")
                path.write_bytes(img.content)
                exclude.add(url)
                return PreparedImage(path, "Pexels", f"Photo by {photo.get('photographer', '')} on Pexels")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Pexels 검색 실패(%s): %s", query, exc)
    return None


def find_font(cfg: Config) -> str | None:
    if cfg.font_path and Path(cfg.font_path).exists():
        return cfg.font_path
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    cached = cfg.image_dir.parent / "fonts" / "NanumGothic-Bold.ttf"
    if cached.exists():
        return str(cached)
    try:
        resp = httpx.get(FONT_URL, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(resp.content)
        return str(cached)
    except httpx.HTTPError as exc:
        log.warning("한글 폰트를 찾지 못했습니다 (FONT_PATH 설정 필요): %s", exc)
        return None


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.splitlines() or [text]:
        line = ""
        for ch in para:
            if draw.textlength(line + ch, font=font) <= max_width:
                line += ch
                continue
            # 가능하면 띄어쓰기 단위로 줄바꿈
            cut = line.rfind(" ")
            if cut > len(line) // 2:
                lines.append(line[:cut])
                line = line[cut + 1:] + ch
            else:
                lines.append(line)
                line = ch
        lines.append(line)
    return [ln.strip() for ln in lines if ln.strip()]


def make_card(cfg: Config, headline: str, sub: str = "", index: int = 0, size=(1080, 1080)) -> PreparedImage:
    """제목/소제목 문구가 들어간 카드 이미지를 만든다 (썸네일·섹션 이미지용)."""
    top, bottom, fg = PALETTES[index % len(PALETTES)]
    w, h = size
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    for y in range(h):  # 세로 그라데이션
        t = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(top[k] * (1 - t) + bottom[k] * t) for k in range(3)))
    pad = 90
    draw.rounded_rectangle([pad // 2, pad // 2, w - pad // 2, h - pad // 2], radius=40, outline=fg, width=4)

    font_file = find_font(cfg)

    def font(px: int):
        return ImageFont.truetype(font_file, px) if font_file else ImageFont.load_default()

    size_px = 96
    head_font = font(size_px)
    lines = _wrap(draw, headline, head_font, w - pad * 2)
    while (len(lines) > 4 or size_px * 1.3 * len(lines) > h * 0.55) and size_px > 48:
        size_px -= 8
        head_font = font(size_px)
        lines = _wrap(draw, headline, head_font, w - pad * 2)
    sub_font = font(max(36, size_px // 2))
    sub_lines = _wrap(draw, sub, sub_font, w - pad * 2)[:3] if sub else []

    line_h = int(size_px * 1.3)
    sub_h = int(sub_font.size * 1.4) if sub_lines else 0
    total = line_h * len(lines) + (40 + sub_h * len(sub_lines) if sub_lines else 0)
    y = (h - total) // 2
    for ln in lines:
        draw.text(((w - draw.textlength(ln, font=head_font)) / 2, y), ln, font=head_font, fill=fg)
        y += line_h
    if sub_lines:
        y += 40
        for ln in sub_lines:
            draw.text(((w - draw.textlength(ln, font=sub_font)) / 2, y), ln, font=sub_font, fill=fg)
            y += sub_h

    path = _out_path(cfg, f"{headline}|{sub}|{index}", "png")
    img.save(path)
    return PreparedImage(path, "카드")


def prepare(
    cfg: Config,
    title: str,
    plans: list[tuple[str, str]],
    user_photos: list[Path],
    count: int,
) -> list[PreparedImage]:
    """plans: [(영문 검색어, 카드 문구)] - AI 가 이미지 자리마다 제안한 내용."""
    result: list[PreparedImage] = [PreparedImage(Path(p), "내 사진") for p in user_photos[:count]]
    used: set[str] = set()
    while len(result) < count:
        i = len(result)
        query, card_text = plans[i] if i < len(plans) else ("", "")
        if i == 0 and cfg.thumbnail_card:
            result.append(make_card(cfg, title, "", index=0))
            continue
        image = search_pexels(cfg, query, used)
        if image is None:
            image = make_card(cfg, card_text or title, title if card_text else "", index=i)
        result.append(image)
    return result


def resize_for_ai(path: Path, out_dir: Path, max_side: int = 1568) -> Path:
    """AI 가 읽을 사진 사본 (긴 변 1568px 이하 JPEG, 휴대폰 회전 정보 반영)."""
    from PIL import ImageOps

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{Path(path).stem}.jpg"
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_side, max_side))
        img.save(out, "JPEG", quality=85)
    return out
