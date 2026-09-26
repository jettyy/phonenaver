"""사용법: python -m phonenaver <명령>

  bot                 텔레그램 봇 실행 (휴대폰 지시 대기)
  login               브라우저 창을 띄워 네이버에 직접 로그인 (최초 1회, PC)
  import-cookies F    PC에서 내보낸 쿠키 JSON 을 서버 프로필에 넣기
  check               로그인 세션 확인
  categories          내 블로그 카테고리 목록 새로고침
  write "지시"         터미널에서 바로 글쓰기+임시저장 (--dry 미리보기, --photo 사진 첨부)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phonenaver", description="네이버 블로그 자동 글쓰기")
    parser.add_argument("--env", default=".env", help=".env 파일 경로")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bot")
    sub.add_parser("login")
    sub.add_parser("check")
    sub.add_parser("categories")
    p_cookie = sub.add_parser("import-cookies")
    p_cookie.add_argument("file", type=Path)
    p_write = sub.add_parser("write")
    p_write.add_argument("text")
    p_write.add_argument("--dry", action="store_true", help="임시저장하지 않고 결과만 출력")
    p_write.add_argument("--photo", type=Path, action="append", default=[], help="글에 넣을 내 사진 (여러 번 가능)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.load(args.env)

    if args.cmd == "bot":
        from .bot import run_bot

        run_bot(cfg)
        return 0

    from . import naver

    if args.cmd == "login":
        return 0 if asyncio.run(naver.interactive_login(cfg)) else 1
    if args.cmd == "check":
        ok = asyncio.run(naver.check_login(cfg))
        print("로그인 되어 있음 ✅" if ok else "로그인 필요 ❌  (python -m phonenaver login)")
        return 0 if ok else 1
    if args.cmd == "import-cookies":
        n = asyncio.run(naver.import_cookies(cfg, args.file))
        print(f"쿠키 {n}개를 저장했습니다.")
        return 0
    if args.cmd == "categories":
        cats = asyncio.run(naver.NaverBlog(cfg).categories(refresh=True))
        print("\n".join(f"- {c.label} (번호 {c.no})" for c in cats) or "카테고리를 찾지 못했습니다.")
        return 0 if cats else 1
    if args.cmd == "write":
        from .html_utils import html_to_text
        from .pipeline import Pipeline

        async def progress(msg: str) -> None:
            print(msg)

        text = ("/test " + args.text) if args.dry else args.text
        result = asyncio.run(Pipeline(cfg).run(text, progress, photos=args.photo))
        print("\n제목:", result.post.title)
        print("카테고리:", result.category.label if result.category else "기본")
        for img in result.images:
            print(f"이미지({img.source}):", img.path)
        for w in result.warnings:
            print("⚠️", w)
        print(html_to_text(result.body_html))
        if result.draft:
            print("\n임시저장 완료. 스크린샷:", result.draft.screenshot)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
