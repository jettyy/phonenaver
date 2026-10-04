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


def test_check_tables():
    from phonenaver.html_utils import check_tables, table_rows

    no_table = "<p>표 없음</p>"
    assert "표" in check_tables(no_table, None)
    rows = "".join(f"<tr><td>{i}위</td><td>x</td></tr>" for i in range(1, 8))
    t7 = f"<table><tr><th>순위</th><th>이름</th></tr>{rows}</table>"
    assert table_rows(t7) == [7]
    assert check_tables(t7, None) == ""            # 일반 글: 표만 있으면 됨
    assert "10위" in check_tables(t7, 0)           # 순위 글인데 몇 위까지인지 안 정함 → 최소 10줄
    assert "30위" in check_tables(t7, 30)
    assert check_tables(t7, 5) == ""


def test_check_post_rank_and_length():
    from phonenaver.html_utils import body_chars, check_post, rank_rows

    rows = "".join(f"<tr><td>{i}위</td><td>대학{i}</td></tr>" for i in range(1, 12))
    table = f"<table><tr><th>순위</th><th>대학</th></tr>{rows}</table>"
    assert rank_rows(table) == 11
    assert rank_rows("<table><tr><th>구분</th></tr><tr><td>A</td></tr></table>") == 0  # 순위표 아님
    long_text = "<p>" + "가나다라마바사 " * 300 + "</p>"
    assert check_post(table + long_text, 10, 1500) == []
    probs = check_post("<p>짧은 글</p>", 10, 1500)
    assert len(probs) == 3 and "표" in probs[0] and "순위표가 없습니다" in probs[1] and "짧습니다" in probs[2]
    assert "5위까지만" in check_post("<table>" + "".join(f"<tr><td>{i}</td><td>x</td></tr>" for i in range(1, 6)) + "</table>", 10, 0)[0]
    assert body_chars("<p>가 나 [[IMAGE1]]</p>") == 2
