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


def test_category_and_no_images():
    cmd = parse("카테고리: 여행 이야기\n제주 한달살기 후기 써줘, 사진 없이")
    assert cmd.category == "여행 이야기"
    assert "카테고리" not in cmd.instruction
    assert cmd.no_images
    assert not parse("제주 여행 글").no_images


def test_photo_modes():
    # 기본: 사진은 분석해서 글 내용으로만 쓰고 첨부하지 않는다
    for text in ("이 사진으로 카페 후기 써줘", "사진 분석해서 글 써줘", "사진은 분석만 하고 글 써줘",
                 "사진은 첨부하지 마", "사진 넣지 말고 써줘", "이 링크 넣어줘 https://a.com", ""):
        assert parse(text).photo_mode == "analyze", text
    # 사진을 넣어 달라고 분명히 말하면 첨부
    for text in ("사진도 첨부해줘", "사진 그대로 넣어서 후기 써줘", "이미지 같이 첨부해서 써줘"):
        assert parse(text).photo_mode == "attach", text


def test_long_text_keeps_everything():
    body = "\n".join(f"{i}번째 줄: 전세 월세 차이와 대출 금리 이야기입니다." for i in range(1, 31))
    cmd = parse(body)
    assert cmd.instruction.count("\n") == 29  # 줄바꿈 유지
    assert "30번째 줄" in cmd.instruction  # 마지막 줄까지 전부
    assert cmd.is_long and cmd.search
    assert not parse("캠핑 준비물").is_long
