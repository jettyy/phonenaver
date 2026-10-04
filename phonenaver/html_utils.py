"""AI가 만든 HTML을 네이버 에디터에 붙여넣기 안전한 형태로 정리한다."""
from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

ALLOWED_TAGS = {
    "h2", "h3", "p", "br", "strong", "b", "em", "i", "u", "s",
    "ul", "ol", "li", "a", "blockquote", "hr", "table", "thead", "tbody", "tr", "th", "td",
}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "form", "input", "button"}


def sanitize(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup.find_all(DROP_WITH_CONTENT):
        tag.decompose()
    # h1 은 제목과 겹치므로 h2, h4 이하는 h3 으로
    for tag in soup.find_all("h1"):
        tag.name = "h2"
    for tag in soup.find_all(["h4", "h5", "h6"]):
        tag.name = "h3"
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        href = tag.get("href") if tag.name == "a" else None
        tag.attrs = {}
        if tag.name == "a":
            if href and re.match(r"^https?://", href.strip(), re.I):
                tag["href"] = href.strip()
                tag["target"] = "_blank"
            else:
                tag.unwrap()
    return str(soup).strip()


def text_to_html(text: str) -> str:
    """일반 텍스트를 문단 HTML로. 줄 안의 URL은 링크로 만든다."""
    url_re = re.compile(r"(https?://[^\s<]+)")
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    out = []
    for block in blocks:
        lines = [url_re.sub(r'<a href="\1">\1</a>', html.escape(ln, quote=False)) for ln in block.splitlines()]
        out.append("<p>" + "<br>".join(lines) + "</p>")
    return "\n".join(out)


def html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))


def links_in(raw_html: str) -> list[str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    return [a["href"] for a in soup.find_all("a", href=True)]


def append_links_section(raw_html: str, urls: list[str], heading: str = "관련 링크") -> str:
    """본문에 빠진 링크가 있으면 끝에 목록으로 붙인다."""
    present = set(links_in(raw_html))
    missing = [u for u in urls if u not in present]
    if not missing:
        return raw_html
    items = "".join(f'<li><a href="{html.escape(u)}">{html.escape(u)}</a></li>' for u in missing)
    return f"{raw_html}\n<h3>{html.escape(heading)}</h3>\n<ul>{items}</ul>"




def table_rows(raw_html: str) -> list[int]:
    """본문의 표마다 내용 줄 수 (제목 줄 제외)."""
    soup = BeautifulSoup(raw_html, "html.parser")
    counts = []
    for table in soup.find_all("table"):
        rows = [tr for tr in table.find_all("tr") if tr.find("td")]
        counts.append(len(rows))
    return counts


def check_tables(raw_html: str, rank: int | None, rank_min: int = 10) -> str:
    """표·순위표 규칙을 어겼으면 고칠 내용을, 지켰으면 빈 문자열을 돌려준다."""
    rows = table_rows(raw_html)
    if not rows or max(rows) == 0:
        return "본문에 표(<table>)가 없습니다. 내용에 맞는 정리표를 반드시 1개 이상 넣으세요."
    if rank is not None:
        need = rank or rank_min
        if max(rows) < need:
            return (f"순위표가 {max(rows)}줄뿐입니다. 1위부터 {need}위까지 한 줄도 빠짐없이 순위표에 넣으세요. "
                    "중간을 줄이거나 '이하 생략' 하지 마세요.")
    return ""


BLOCK_TAGS = {"p", "h2", "h3", "ul", "ol", "table", "blockquote", "hr"}
# 인포러시에서 실제 네이버 에디터로 검증된 빈 줄 형태
BLANK = "<p><br></p>"


def add_paragraph_gaps(raw_html: str, blank_lines: int = 2) -> str:
    """문단·소제목·표·사진 사이마다 빈 줄을 넣어 읽기 편하게 한다."""
    if blank_lines <= 0:
        return raw_html
    soup = BeautifulSoup(raw_html, "html.parser")
    blocks = [b for b in soup.contents if getattr(b, "name", None) in BLOCK_TAGS]
    # 이미 빈 문단이 있으면 지우고 일정하게 다시 넣는다
    for b in blocks:
        if b.name == "p" and not b.get_text(strip=True) and not b.find(["a", "img"]):
            b.decompose()
    blocks = [b for b in soup.contents if getattr(b, "name", None) in BLOCK_TAGS]
    marker = re.compile(r"^\[\[IMAGE\d+\]\]$")
    for b, nxt in zip(blocks, blocks[1:]):
        # 사진 자리 표시 문단은 사진을 넣으면서 빈 줄로 남으므로, 그 앞에는 하나 덜 넣어 위아래를 똑같이 맞춘다
        n = blank_lines - 1 if marker.match(nxt.get_text(strip=True)) else blank_lines
        for _ in range(n):
            b.insert_after(BeautifulSoup(BLANK, "html.parser"))
    return str(soup)
