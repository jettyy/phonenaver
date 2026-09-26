import asyncio

from phonenaver import slrwatch
from phonenaver.config import Config

ROW = """<tr>
  <td class="list_num">{no}</td>
  <td class="sbj"><a href="/bbs/vx2.php?id=used_market&page=1&divpage=1000&category=1&no={no}">{title}</a> <span>[1]</span></td>
  <td class="list_name"><span>{author}</span></td>
  <td class="list_date">10:24:20</td>
  <td class="list_vote">0</td>
  <td class="list_click">15</td>
</tr>"""
NOTICE = """<tr><td class="list_num">공지</td>
  <td class="sbj"><a href="/bbs/vx2.php?id=help&no=31">회원장터 이용 유의사항</a></td>
  <td class="list_name">SLR</td><td class="list_date">2013/09/03</td></tr>"""


def page(*rows):
    body = NOTICE + "".join(ROW.format(no=no, title=t, author="작성자") for no, t in rows)
    return f"<html><body><table id='bbs_list'><thead><tr><th>번호</th></tr></thead><tbody>{body}</tbody></table></body></html>"


def test_parse_list_skips_notice_and_builds_link():
    posts = slrwatch.parse_list(page((10028144, "후지 gfx100rf 팝니다"), (10028145, "라이카 니켈엘마")))
    assert [p.no for p in posts] == [10028145, 10028144]
    assert posts[0].title == "라이카 니켈엘마"
    assert posts[0].url == "https://www.slrclub.com/bbs/vx2.php?id=used_market&no=10028145"
    assert posts[0].author == "작성자"
    assert posts[0].date == "10:24:20"


def test_parse_list_fallback_without_table():
    html = '<div><a href="/bbs/vx2.php?id=used_market&no=77">소니 a7c2 팝니다</a><a href="/bbs/vx2.php?id=help&no=31">공지</a></div>'
    posts = slrwatch.parse_list(html)
    assert [(p.no, p.title) for p in posts] == [(77, "소니 a7c2 팝니다")]


def test_parse_list_euc_kr_bytes():
    html = page((5, "니콘 z5 24-50")).replace("<html>", "<html><head><meta charset='euc-kr'></head>")
    posts = slrwatch.parse_list(html.encode("euc-kr"))
    assert posts[0].title == "니콘 z5 24-50"


def test_matches_rules():
    title = "[아산]소니 A7M5 + 2870gm , DJI rs4mini combo"
    assert slrwatch.matches(title, "a7m5")
    assert slrwatch.matches(title, "소니 A7 M5")  # 띄어쓰기·대소문자 무시
    assert slrwatch.matches(title, "a7v|a7m5")
    assert not slrwatch.matches(title, "소니 a7c2")
    assert not slrwatch.matches(title, "a7m5 -dji")
    assert slrwatch.matches("니콘 z2470 24-70s f2.8", "24-70")
    assert not slrwatch.matches(title, "-dji")


def test_split_keywords():
    assert slrwatch.split_keywords(" 소니  a7m5, 라이카 q2\nx100v ,") == ["소니 a7m5", "라이카 q2", "x100v"]


def test_page_url_sets_page():
    url = slrwatch.page_url(slrwatch.DEFAULT_BOARD_URL, 2)
    assert "id=used_market" in url and "category=1" in url and "page=2" in url
    assert "page=" not in slrwatch.page_url(url, 1)


def test_store_add_remove(tmp_path):
    store = slrwatch.WatchStore(tmp_path / "w.json")
    assert store.add(["소니 a7m5", "라이카"]) == ["소니 a7m5", "라이카"]
    assert store.add(["소니A7M5"]) == []  # 같은 키워드
    assert store.remove("2") == "라이카"
    assert store.remove("없는거") is None
    assert store.remove("소니 a7m5") == "소니 a7m5"
    assert store.keywords == []


class FakeWatcher(slrwatch.SlrWatcher):
    def __init__(self, store, pages):
        super().__init__(Config(), store)
        self.pages = pages
        self.fetched = []

    async def fetch_page(self, page=1):
        self.fetched.append(page)
        return self.pages.get(page, [])


def post(no, title):
    return slrwatch.Post(no=no, title=title, url=slrwatch.post_url("used_market", no))


def test_poll_first_run_sets_baseline_then_notifies_new(tmp_path):
    store = slrwatch.WatchStore(tmp_path / "w.json")
    store.add(["a7m5"])
    w = FakeWatcher(store, {1: [post(10, "소니 a7m5"), post(9, "캐논")]})
    assert asyncio.run(w.poll()) == []  # 처음엔 기준만
    assert store.load()["last_no"] == 10

    w.pages = {1: [post(12, "캐논 r5"), post(11, "A7M5 미개봉"), post(10, "소니 a7m5")]}
    hits = asyncio.run(w.poll())
    assert [(p.no, kws) for p, kws in hits] == [(11, ["a7m5"])]
    assert asyncio.run(w.poll()) == []  # 같은 글은 다시 안 보냄


def test_poll_reads_next_pages_when_many_new(tmp_path):
    store = slrwatch.WatchStore(tmp_path / "w.json")
    store.add(["라이카"])
    data = store.load()
    data["last_no"] = 5
    store.save(data)
    w = FakeWatcher(store, {1: [post(9, "니콘"), post(8, "캐논")], 2: [post(7, "라이카 q2"), post(6, "소니")], 3: [post(5, "라이카 옛글")]})
    hits = asyncio.run(w.poll())
    assert w.fetched == [1, 2, 3]
    assert [p.no for p, _ in hits] == [7]
    assert store.load()["last_no"] == 9


def test_snapshot_sets_baseline_only_once(tmp_path):
    store = slrwatch.WatchStore(tmp_path / "w.json")
    w = FakeWatcher(store, {1: [post(20, "a")]})
    asyncio.run(w.snapshot())
    assert store.load()["last_no"] == 20
    w.pages = {1: [post(25, "b")]}
    asyncio.run(w.snapshot())
    assert store.load()["last_no"] == 20


def test_format_hit():
    text = slrwatch.format_hit(slrwatch.Post(1, "소니 A7M5", "https://x/1", "200F8", "09:55:53"), ["a7m5"])
    assert text == "🔔 SLR 장터 새 글  [a7m5]\n소니 A7M5\n200F8 · 09:55:53\nhttps://x/1"
