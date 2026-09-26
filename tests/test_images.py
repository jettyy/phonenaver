from pathlib import Path

from PIL import Image

from phonenaver import images
from phonenaver.config import Config
from phonenaver.naver import Category, match_category, parse_category_json


def _markers(html: str) -> list[str]:
    return images.MARKER_RE.findall(html)


def test_ensure_markers_adds_missing_evenly():
    body = "<p>intro</p><h2>A</h2><p>a</p><h2>B</h2><p>b</p><h2>C</h2><p>c</p>"
    out = images.ensure_markers(body, 3)
    assert sorted(_markers(out)) == ["1", "2", "3"]
    assert out.startswith("<p>intro</p><p>[[IMAGE1]]</p>")


def test_ensure_markers_keeps_ai_positions_and_drops_extras():
    body = "<p>intro</p><p>[[IMAGE1]]</p><h2>A</h2><p>a [[IMAGE2]] 계속</p><p>[[IMAGE9]]</p><p>[[IMAGE1]]</p>"
    out = images.ensure_markers(body, 3)
    assert sorted(_markers(out)) == ["1", "2", "3"]
    assert "<p>[[IMAGE2]]</p>" in out  # 문장 속 표시는 독립 문단으로 분리
    assert "a  계속" in out or "a 계속" in out
    assert "IMAGE9" not in out


def test_prepare_uses_user_photos_then_cards(tmp_path: Path):
    photo = tmp_path / "me.jpg"
    Image.new("RGB", (10, 10)).save(photo)
    cfg = Config(image_dir=tmp_path / "img", pexels_api_key="")
    out = images.prepare(cfg, "제주 한달살기 준비물 총정리", [("a", "짐 싸기"), ("b", "숙소"), ("c", "교통")], [photo], 3)
    assert [i.source for i in out] == ["내 사진", "카드", "카드"]
    assert all(i.path.exists() for i in out)
    out = images.prepare(cfg, "제목", [], [], 3)
    assert len(out) == 3 and Image.open(out[0].path).size == (1080, 1080)


def test_parse_category_json_and_match():
    data = {"isSuccess": True, "result": {"mylogCategoryList": [
        {"categoryNo": 0, "categoryName": "전체보기"},
        {"categoryNo": 3, "categoryName": "여행", "parentCategoryNo": None},
        {"categoryNo": 7, "categoryName": "제주", "parentCategoryNo": 3},
        {"categoryNo": 8, "categoryName": "", "divisionLine": True},
        {"categoryNo": 5, "categoryName": "IT 리뷰"},
    ]}}
    cats = parse_category_json(data)
    assert [(c.no, c.label) for c in cats] == [(3, "여행"), (7, "여행 > 제주"), (5, "IT 리뷰")]
    assert match_category("여행 > 제주", cats).no == 7
    assert match_category("it리뷰", cats).no == 5
    assert match_category("요리", cats) is None
    assert match_category("", [Category("a")]) is None
