"""phonenaver 시작 프로그램 (맥·윈도우 공용).

`npm install` → `python launcher.py setup`  : 필요한 것을 모두 설치 (처음 한 번, 업데이트 후)
`npm start`   → `python launcher.py start`  : 대시보드 + 텔레그램 봇 실행
(start.command / start.bat 더블클릭도 `start` 와 같다)

- 파이썬 가상환경(.venv) 만들기 + 패키지 설치 (requirements.txt 가 바뀌면 자동 재설치)
- 글쓰기용 브라우저(Chromium) 설치
- Claude Code 설치 + 구독 계정 로그인 확인

표준 라이브러리만 사용한다 (가상환경이 만들어지기 전에 실행되므로).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"
VPY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
STAMP = VENV / ".phonenaver-installed"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def run(cmd: list[str], **kw) -> int:
    return subprocess.call(cmd, cwd=ROOT, **kw)


def pause_exit(code: int) -> None:
    # 창을 바로 닫지 않는 것은 start.command / start.bat 이 처리
    sys.exit(code)


def ensure_python_version() -> None:
    if sys.version_info < (3, 9):
        say(f"❌ 파이썬 {sys.version.split()[0]} 은 너무 오래되었습니다. 3.9 이상이 필요합니다.")
        say("   https://www.python.org/downloads/ 에서 최신 파이썬을 설치한 뒤 다시 실행하세요.")
        pause_exit(1)


def ensure_venv() -> None:
    req = (ROOT / "requirements.txt").read_bytes()
    want = hashlib.sha256(req + sys.version.encode()).hexdigest()
    if VPY.exists() and STAMP.exists() and STAMP.read_text().strip() == want:
        return

    if not VPY.exists():
        say("📦 [1/3] 프로그램 전용 파이썬 환경을 만드는 중...")
        venv.create(VENV, with_pip=True)
    say("📦 [2/3] 필요한 패키지를 설치하는 중... (처음 한 번, 몇 분 걸려요)")
    # 오래된 pip 는 일부 패키지를 제대로 못 깔아서 먼저 최신으로 올린다
    run([str(VPY), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    if run([str(VPY), "-m", "pip", "install", "--quiet", "-r", "requirements.txt"]) != 0:
        say("❌ 패키지 설치에 실패했습니다. 인터넷 연결을 확인하고 다시 실행하세요.")
        pause_exit(1)
    say("🌐 [3/3] 글쓰기용 브라우저를 설치하는 중...")
    if run([str(VPY), "-m", "playwright", "install", "chromium"]) != 0:
        say("❌ 브라우저 설치에 실패했습니다. 다시 실행해 보세요.")
        pause_exit(1)
    STAMP.write_text(want)
    say("✅ 설치 완료\n")


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (
        home / ".local/bin/claude",
        home / ".local/bin/claude.exe",
        home / ".claude/local/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global/bin/claude",
        home / "AppData/Roaming/npm/claude.cmd",
    ):
        if cand.exists():
            return str(cand)
    return None


def install_claude() -> str | None:
    say("🤖 Claude Code 가 없어서 설치합니다. (내 Claude 구독 계정으로 글을 쓰는 데 필요)")
    if IS_WIN:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               "irm https://claude.ai/install.ps1 | iex"]
    else:
        cmd = ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]
    run(cmd)
    return find_claude()


def claude_logged_in(claude: str) -> bool:
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or _env_value("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    try:
        out = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30).stdout
        return bool(json.loads(out).get("loggedIn"))
    except Exception:
        return False


def _env_value(key: str) -> str:
    env = ROOT / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def ensure_claude() -> None:
    claude = find_claude() or install_claude()
    if not claude:
        say("⚠️ Claude Code 설치를 확인하지 못했습니다. 창을 닫고 다시 실행해 보세요.")
        say("   (계속 안 되면 https://claude.ai/download 안내를 따라 설치)")
        return
    if claude_logged_in(claude):
        return
    say("\n🔑 Claude 구독 계정 로그인이 필요합니다. 브라우저가 열리면 로그인하세요.")
    run([claude, "auth", "login"])
    if claude_logged_in(claude):
        say("✅ Claude 로그인 완료\n")
    else:
        say("⚠️ Claude 로그인이 확인되지 않았습니다. 나중에 메뉴에서 다시 시도할 수 있습니다.\n")


def main() -> None:
    os.chdir(ROOT)
    mode = sys.argv[1] if len(sys.argv) > 1 else "start"
    say("=" * 50)
    say("  📝 폰네이버 — 네이버 블로그 자동 글쓰기")
    say("=" * 50)
    ensure_python_version()
    ensure_venv()
    if mode == "setup":
        ensure_claude()
        say("✅ 준비 완료! 이제 `npm start` 로 실행하세요.")
        return
    if not find_claude():  # 실행할 때는 설치만 확인 (로그인은 대시보드에서도 할 수 있음)
        ensure_claude()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    code = subprocess.call([str(VPY), "-m", "phonenaver", "app"], cwd=ROOT, env=env)
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\n종료합니다.")
