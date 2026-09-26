from phonenaver.command import parse


def test_keyword_only_searches():
    cmd = parse("2026 청년도약계좌 조건 정리해줘")
    assert cmd.urls == [] and cmd.insert_urls == []
    assert cmd.search and not cmd.raw and not cmd.dry_run


def test_link_only_is_analyzed_without_search():
    cmd = parse("https://example.com/a 이거 분석해서 후기 글로 써줘")
    assert cmd.urls == ["https://example.com/a"]
    assert cmd.insert_urls == []
    assert cmd.analyze_urls == ["https://example.com/a"]
    assert not cmd.search


def test_link_with_search_word_also_searches():
    cmd = parse("https://example.com/a 참고해서 최신 정보도 찾아서 써줘")
    assert cmd.search


def test_insert_link_single_line():
    cmd = parse("제주 한달살기 준비물 글 써줘. 이 링크 넣어줘 https://example.com/shop.")
    assert cmd.insert_urls == ["https://example.com/shop"]
    assert cmd.search  # 주제는 검색으로 조사


def test_insert_mixed_multiline():
    cmd = parse(
        "이 기사 분석해서 써줘 https://news.example.com/1\n"
        "마지막에 이 링크 걸어줘 https://example.com/buy"
    )
    assert cmd.urls == ["https://news.example.com/1", "https://example.com/buy"]
    assert cmd.insert_urls == ["https://example.com/buy"]
    assert cmd.analyze_urls == ["https://news.example.com/1"]
    assert not cmd.search


def test_insert_keyword_on_separate_line_applies_to_all():
    cmd = parse("아래 링크들 글에 넣어서 캠핑 글 써줘\nhttps://a.com\nhttps://b.com")
    assert cmd.insert_urls == ["https://a.com", "https://b.com"]


def test_raw_and_dry():
    cmd = parse("/test 그대로: 제목입니다\n본문 첫줄")
    assert cmd.raw and cmd.dry_run
    assert cmd.text.splitlines()[0] == "제목입니다"
