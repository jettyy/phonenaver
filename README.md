# phonenaver — 휴대폰으로 지시하면 네이버 블로그에 글을 써서 임시저장

워드프레스 자동 정보글 생성기(키워드 → 조사 → AI 작성 → 자동 등록)의 흐름을
**네이버 블로그 + 휴대폰 지시**용으로 바꾼 자동화 프로그램입니다.

```
휴대폰(텔레그램) ──메시지──▶ 봇 ──▶ ① 링크 읽기 / 최신 정보 웹 검색
                                  ② Claude 가 블로그 글 작성 (제목·본문·태그)
                                  ③ 네이버 스마트에디터에 붙여넣고 [저장] = 임시저장
                 ◀── 완료 알림 + 에디터 화면 캡처 ──
```

발행은 자동으로 하지 않습니다. 네이버 앱 → 글쓰기 → **임시저장 글**에서 확인한 뒤 직접 발행하세요.

## 할 수 있는 것

| 휴대폰에서 보내는 메시지 | 동작 |
|---|---|
| `2026 청년도약계좌 조건 정리해줘` | 키워드/문장 → **최신 정보 웹 검색** 후 글 작성 → 임시저장 |
| `https://... 이거 분석해서 후기 글로 써줘` | 링크에 **들어가서 내용 분석** 후 글 작성 (네이버 블로그·카페 링크도 지원) |
| `제주 한달살기 준비물 글 써줘. 이 링크 넣어줘 https://...` | 글을 쓰면서 그 **링크를 본문에 하이퍼링크로 삽입** |
| `이 기사 분석해줘 https://A`<br>`마지막에 이 링크 걸어줘 https://B` | 여러 줄이면 `넣어/삽입/걸어/첨부`가 적힌 줄의 링크만 삽입, 나머지는 분석용 |
| `그대로: 제목`<br>`본문...` | AI 없이 **내가 쓴 글 그대로** 임시저장 (본문 안 URL은 자동 링크) |
| `/test ...` | 저장하지 않고 미리보기만 받기 |

말투·분량·대상은 메시지에 같이 쓰면 됩니다. (예: `1500자로`, `초보자용으로`, `리스트 위주로`)
링크만 주면 링크 내용만으로 쓰고, `최신`/`검색`/`찾아` 같은 말을 넣으면 웹 검색도 함께 합니다.

## 설치 (PC 또는 항상 켜둘 컴퓨터/서버)

Python 3.10 이상이 필요합니다.

```bash
git clone <이 저장소> && cd phonenaver
python -m venv .venv
source .venv/bin/activate          # 윈도우: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env               # 그리고 .env 를 열어 값 채우기
```

### 1) Claude API 키
[Claude Console](https://platform.claude.com)에서 API 키를 발급받아 `.env` 의 `ANTHROPIC_API_KEY` 에 넣습니다.
기본 모델은 `claude-opus-5` 이며 `CLAUDE_MODEL` 로 바꿀 수 있습니다.
(최신 정보 검색은 Claude 의 웹 검색 기능을 사용하므로 별도 검색 API 키가 필요 없습니다.)

### 2) 텔레그램 봇 만들기 (휴대폰 지시용)
1. 텔레그램에서 `@BotFather` → `/newbot` → 받은 토큰을 `TELEGRAM_BOT_TOKEN` 에 입력
2. `python -m phonenaver bot` 실행 후, 내 봇에게 `/id` 전송 → 나온 숫자를 `ALLOWED_CHAT_IDS` 에 입력
3. 봇 재시작. 이제 **허용된 채팅에서 온 메시지만** 처리합니다.

### 3) 네이버 로그인 (최초 1회)
`NAVER_BLOG_ID` 에 블로그 주소(`blog.naver.com/<아이디>`)의 아이디를 넣고:

```bash
python -m phonenaver login     # 브라우저 창이 뜨면 직접 로그인 ('로그인 상태 유지' 체크)
python -m phonenaver check     # 로그인 세션 확인
```

로그인 세션은 `data/browser` 폴더에 저장되어 계속 재사용됩니다.
아이디/비밀번호 자동 입력은 네이버 캡차·보안 차단 때문에 일부러 넣지 않았습니다.

**화면 없는 서버에서 돌릴 때**: PC 크롬에서 네이버 로그인 → 확장 프로그램 *Cookie-Editor* 로 naver.com 쿠키를
JSON 으로 내보내기 → 서버에서 `python -m phonenaver import-cookies cookies.json`.
쿠키 파일은 비밀번호와 같으니 가져온 뒤 바로 지우세요.

### 4) 실행

```bash
python -m phonenaver bot                                  # 휴대폰 지시 대기
python -m phonenaver write "캠핑 초보 준비물 정리" --dry   # 터미널에서 미리보기 테스트
python -m phonenaver write "캠핑 초보 준비물 정리"         # 터미널에서 바로 임시저장
```

처음에는 `.env` 에서 `HEADLESS=false` 로 두고 브라우저가 에디터를 조작하는 모습을 확인해 보는 것을 추천합니다.

## 구조

| 파일 | 역할 |
|---|---|
| `phonenaver/command.py` | 메시지 해석 (링크 추출, 삽입용/분석용 구분, 검색 여부, `그대로:`·`/test`) |
| `phonenaver/fetcher.py` | 링크 본문 추출 (네이버 블로그 PC 주소 → 모바일 주소 변환, naver.me 단축주소) |
| `phonenaver/ai.py` | Claude: ① 웹 검색으로 최신 정보 조사 ② 구조화된 출력(제목·HTML 본문·태그)으로 글 작성 |
| `phonenaver/html_utils.py` | 본문 HTML 정리(허용 태그만), 빠진 삽입 링크 보정 |
| `phonenaver/naver.py` | Playwright 로 스마트에디터 조작: 제목 입력 → 서식 있는 HTML 붙여넣기 → **저장** |
| `phonenaver/pipeline.py` | 전체 흐름 |
| `phonenaver/bot.py` | 텔레그램 봇 |

## 알아둘 점

- 네이버 블로그는 **임시저장용 공식 API 가 없어서** 실제 브라우저로 에디터를 조작합니다.
  네이버가 에디터 화면을 바꾸면 동작이 깨질 수 있으며, 이때는 `phonenaver/naver.py` 맨 위 `SELECTORS` 만 고치면 됩니다.
  실패하면 봇이 **오류 당시 화면 캡처**를 보내 줍니다.
- 현재 버전은 이미지 업로드, 카테고리 선택, 태그 입력칸 입력은 하지 않습니다. 태그는 본문 끝에 `#해시태그`로 붙습니다
  (`APPEND_HASHTAGS=false` 로 끌 수 있음).
- AI 가 쓴 글은 발행 전에 꼭 사실관계를 확인하세요. 짧은 시간에 많은 글을 자동으로 올리면 네이버 저품질/어뷰징 판정 위험이 있습니다.
- 다른 사람 글을 분석할 때는 그대로 베끼지 않고 재구성하도록 되어 있지만, 저작권은 사용자가 최종 확인해야 합니다.

## 테스트

```bash
pip install pytest && python -m pytest -q
```
