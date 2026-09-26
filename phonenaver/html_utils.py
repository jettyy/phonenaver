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


