from phonenaver.fetcher import extract, normalize_url
from phonenaver.html_utils import append_links_section, html_to_text, sanitize, text_to_html


def test_normalize_naver_blog():
    assert normalize_url("https://blog.naver.com/abc/223344") == "https://m.blog.naver.com/abc/223344"
    assert (
        normalize_url("https://blog.naver.com/PostView.naver?blogId=abc&logNo=99")
        == "https://m.blog.naver.com/abc/99"
    )
    assert normalize_url("https://example.com/x") == "https://example.com/x"


def test_sanitize():
    out = sanitize(
        '<h1>t</h1><div><p style="x" onclick="y">hi <a href="javascript:alert(1)">bad</a>'
        '<a href="https://ok.com" class="c">ok</a></p></div><script>x()</script><h4>s</h4>'
    )
    assert "<h2>t</h2>" in out and "<h3>s</h3>" in out
    assert "script" not in out and "onclick" not in out and "javascript" not in out and "div" not in out
    assert '<a href="https://ok.com" target="_blank">ok</a>' in out


def test_append_missing_links():
    body = '<p><a href="https://a.com">a</a></p>'
    out = append_links_section(body, ["https://a.com", "https://b.com"])
    assert out.count("https://a.com") == 1 and "https://b.com" in out
    assert append_links_section(body, ["https://a.com"]) == body


def test_text_to_html_and_back():
    h = text_to_html("첫 문단 https://x.com\n둘째줄\n\n두번째 <문단>")
    assert '<a href="https://x.com">' in h and "&lt;문단&gt;" in h
    assert "두번째 <문단>" in html_to_text(h)


def test_extract_naver_mobile_fallback():
    html = (
        '<html><head><meta property="og:title" content="제목"></head><body>'
        '<div class="se-main-container"><p>' + "본문 내용입니다. " * 30 + "</p></div></body></html>"
    )
    title, text = extract(html)
    assert title == "제목" and "본문 내용입니다" in text
