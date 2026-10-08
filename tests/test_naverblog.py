"""네이버 블로그 주소 → 글 목록 → 글마다 새 글. 실제 네이버 대신 가짜 응답으로 검사."""
import asyncio
import json
import time

import httpx
import pytest

from phonenaver import html_utils, naverblog
from phonenaver.command import parse, wants_all
from phonenaver.naverblog import BlogTarget, blog_target


@pytest.mark.parametrize("url, want", [
    ("https://blog.naver.com/myblog", BlogTarget("myblog", 0)),
    ("https://blog.naver.com/myblog/", BlogTarget("myblog", 0)),
    ("https://m.blog.naver.com/my_blog-1", BlogTarget("my_blog-1", 0)),
    ("https://blog.naver.com/myblog?categoryNo=12", BlogTarget("myblog", 12)),
    ("https://blog.naver.com/PostList.naver?blogId=myblog&categoryNo=7", BlogTarget("myblog", 7)),
    ("https://m.blog.naver.com/PostList.naver?blogId=myblog", BlogTarget("myblog", 0)),
    ("https://myblog.blog.me", BlogTarget("myblog", 0)),
    ("https://blog.naver.com/myblog/223456789012", None),  # 글 한 편
    ("https://blog.naver.com/PostView.naver?blogId=myblog&logNo=223456789012", None),
    ("https://m.blog.naver.com/myblog/223456789012", None),
    ("https://cafe.naver.com/somecafe", None),
    ("https://www.youtube.com/@ch", None),
])
def test_blog_target(url, want):
    assert blog_target(url) == want


DAY = 86400


def transport(mobile=None, pc=None, rss=None):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url)
        if req.url.host == "m.blog.naver.com" and req.url.path.endswith("/post-list"):
            return mobile(req) if mobile else httpx.Response(500)
        if req.url.path == "/PostTitleListAsync.naver":
            return pc(req) if pc else httpx.Response(500)
        if req.url.host == "rss.blog.naver.com":
            return httpx.Response(200, content=rss.encode()) if rss else httpx.Response(404)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_mobile_list_pages_until_cutoff():
    now = time.time()
    # 총 70편, 하루에 하나씩 (최신순)
    all_items = [{"logNo": 223000000000 + i, "titleWithInnerHtml": f"글 &amp; {i}", "addDate": int((now - i * 5 * DAY) * 1000)}
                 for i in range(70)]

    def mobile(req):
        page, n = int(req.url.params["page"]), int(req.url.params["itemCount"])
        assert req.url.params["categoryNo"] == "3"
        return httpx.Response(200, json={"isSuccess": True, "result": {"items": all_items[(page - 1) * n: page * n]}})

    client, seen = transport(mobile=mobile)
    name, posts, note = naverblog.list_blog_posts("https://blog.naver.com/abc?categoryNo=3", months=6, client=client)
    assert name == "abc" and note == ""
    assert len(posts) == 37  # 0~36번 (5일 간격, 약 183일)
    assert posts[0].title == "글 & 0" and posts[0].url == "https://blog.naver.com/abc/223000000000"
    pages = [u for u in seen if u.path.endswith("/post-list")]
    assert len(pages) == 2  # 두 번째 묶음에서 기간 밖 글을 보고 멈춤


def test_all_posts_and_limit():
    items = [{"logNo": 100000 + i, "titleWithInnerHtml": f"t{i}", "addDate": int((time.time() - i * 400 * DAY) * 1000)}
             for i in range(5)]
    mobile = lambda req: httpx.Response(200, json={"result": {"items": items if req.url.params["page"] == "1" else []}})
    client, _ = transport(mobile=mobile)
    _, posts, _ = naverblog.list_blog_posts("https://blog.naver.com/abc", months=None, client=client)
    assert len(posts) == 5  # 기간 제한 없음
    _, posts, _ = naverblog.list_blog_posts("https://blog.naver.com/abc", months=None, limit=2, client=client)
    assert [p.log_no for p in posts] == ["100000", "100001"]


def test_pc_list_when_mobile_fails():
    body = {"resultCode": "S", "totalCount": "2", "postList": [
        {"logNo": "223111", "title": "%EC%B2%AB+%EA%B8%80", "addDate": "3시간 전"},
        {"logNo": "223110", "title": "it\\'s", "addDate": "2020. 1. 5."},
    ]}
    pc = lambda req: httpx.Response(200, text=json.dumps(body).replace("it\\\\'s", "it\\'s"))
    client, _ = transport(pc=pc)
    _, posts, _ = naverblog.list_blog_posts("https://blog.naver.com/abc", months=6, client=client)
    assert [(p.log_no, p.title) for p in posts] == [("223111", "첫 글")]  # 2020년 글은 기간 밖


def test_rss_when_lists_fail():
    rss = """<?xml version="1.0" encoding="UTF-8"?><rss><channel><title>내 블로그</title>
    <item><title>RSS 글</title><link>https://blog.naver.com/abc/223999?fromRss=true</link>
    <pubDate>{}</pubDate></item></channel></rss>""".format(time.strftime("%a, %d %b %Y %H:%M:%S +0900"))
    client, _ = transport(rss=rss)
    name, posts, note = naverblog.list_blog_posts("https://blog.naver.com/abc?categoryNo=2", client=client)
    assert name == "내 블로그" and [p.log_no for p in posts] == ["223999"]
    assert "RSS" in note and "카테고리" in note


def test_list_fails_clearly():
    client, _ = transport()
    with pytest.raises(RuntimeError, match="목록을 가져오지 못했습니다"):
        naverblog.list_blog_posts("https://blog.naver.com/abc", client=client)


def test_wants_all():
    assert wants_all("이 블로그 글 전부 다시 써줘")
    assert not wants_all("최근 3개월 전부 써줘")  # 기간을 정했으면 그 기간
    assert not wants_all("이 글들 하나씩 써줘")


def test_copied_sentences():
    src = "제주 공항에서 렌터카 하우스까지는 셔틀버스로 10분 정도 걸립니다.\n짧은 문장."
    body = "<p>제주 공항에서 렌터카 하우스까지는   셔틀버스로 10분 정도 걸립니다.</p><p>짧은 문장.</p>"
    assert html_utils.copied_sentences(body, src) == ["제주 공항에서 렌터카 하우스까지는 셔틀버스로 10분 정도 걸립니다."]
    assert html_utils.copied_sentences("<p>공항에서 셔틀로 약 10분이면 도착해요.</p>", src) == []


def test_blog_expands_to_one_job_per_post(tmp_path, monkeypatch):
    from phonenaver import jobs
    from phonenaver.config import Config

    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")
    asked = {}

    def fake_list(url, months, limit):
        asked.update(url=url, months=months, limit=limit)
        return "남의블로그", [naverblog.BlogPostRef("abc", str(223000 + i), f"글 {i}") for i in range(3)], ""

    monkeypatch.setattr(naverblog, "list_blog_posts", fake_list)

    async def scenario(text):
        runner = jobs.JobRunner(lambda: Config(), None, jobs.Events())
        runner._queue, runner._task = asyncio.Queue(), object()  # 실제 글쓰기는 돌리지 않음
        job = runner.submit(text, source="phone", want_result=True)
        await runner._run(job)
        return job, await runner.wait(job)

    job, result = asyncio.run(scenario("https://blog.naver.com/abc 전부"))
    assert asked["months"] is None and asked["limit"] is None
    assert isinstance(result, jobs.ChannelResult) and result.kind == "blog" and result.found == 3
    texts = [c.text for c in result.children]
    assert texts[0] == "https://blog.naver.com/abc/223000\n이 글 내용으로 블로그 글 새로 써줘"
    assert job.status == "done" and "전체 기간 글 3개" in job.message

    job, result = asyncio.run(scenario("https://blog.naver.com/abc 최근 2개월 10개만 초보자도 알기 쉽게 써줘"))
    assert asked["months"] == 2 and asked["limit"] == 10
    assert "초보자도 알기 쉽게" in result.children[0].text

    # '이 링크 넣어줘' 로 보낸 블로그 주소는 목록으로 펼치지 않는다
    cmd = parse("제주 여행 글 써줘. 이 링크 넣어줘 https://blog.naver.com/abc")
    assert cmd.insert_urls == ["https://blog.naver.com/abc"]


def test_unread_blog_post_stops(monkeypatch):
    from phonenaver import pipeline
    from phonenaver.config import Config
    from phonenaver.fetcher import Page

    monkeypatch.setattr(pipeline, "fetch", lambda u: Page(url=u, kind="naver_blog", error="비공개 글"))
    pipe = pipeline.Pipeline(Config())
    with pytest.raises(RuntimeError, match="블로그 글을 읽지 못해"):
        asyncio.run(pipe.generate(parse("https://blog.naver.com/abc/223000\n이 글 내용으로 써줘")))


@pytest.mark.parametrize("text, months, label", [
    ("https://m.blog.naver.com/dbgurwnzz", 6, "최근 6개월"),
    ("https://m.blog.naver.com/dbgurwnzz 최근 3개월", 3, "최근 3개월"),
    ("https://m.blog.naver.com/dbgurwnzz 1년치 써줘", 12, "최근 1년"),
    ("https://m.blog.naver.com/dbgurwnzz 2주", 14 / 30.44, "최근 2주"),
    ("https://m.blog.naver.com/dbgurwnzz 10일 동안 쓴 글", 10 / 30.44, "최근 10일"),
    ("https://m.blog.naver.com/dbgurwnzz 한 달", 1, "최근 1개월"),
    ("https://m.blog.naver.com/dbgurwnzz 일주일치", 7 / 30.44, "최근 1주"),
    ("https://m.blog.naver.com/dbgurwnzz 2026년 정책 글들 써줘", 6, "최근 6개월"),  # '2026년' 은 기간이 아님
])
def test_period(text, months, label):
    from phonenaver.command import channel_options, period_label

    got, _, _ = channel_options(parse(text).instruction)
    assert got == pytest.approx(months) and period_label(got) == label


@pytest.mark.parametrize("text, url", [
    ("m.blog.naver.com/dbgurwnzz 최근 3개월", "https://m.blog.naver.com/dbgurwnzz"),
    ("blog.naver.com/dbgurwnzz/223456789012 이 글로 써줘", "https://blog.naver.com/dbgurwnzz/223456789012"),
    ("https://m.blog.naver.com/dbgurwnzz", "https://m.blog.naver.com/dbgurwnzz"),
    ("youtu.be/AbCdEfGhIjK", "https://youtu.be/AbCdEfGhIjK"),
])
def test_url_without_scheme(text, url):
    assert parse(text).urls == [url]


def test_main_vs_detail_address():
    from phonenaver.naverblog import is_blog_list

    assert is_blog_list("https://m.blog.naver.com/dbgurwnzz")  # 메인 → 기간 안의 글 전부
    assert not is_blog_list("https://m.blog.naver.com/dbgurwnzz/223456789012")  # 세부 → 그 글만
    assert not is_blog_list("https://m.blog.naver.com/PostView.naver?blogId=dbgurwnzz&logNo=223456789012")
