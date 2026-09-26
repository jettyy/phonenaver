#!/bin/bash
# 맥: 이 파일을 더블클릭하면 설치(처음 한 번) → 메뉴가 열립니다.
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo "파이썬 3.9 이상이 필요합니다."
  echo "설치 안내 창이 뜨면 [설치]를 누르고, 끝나면 이 파일을 다시 더블클릭하세요."
  xcode-select --install 2>/dev/null
  read -r -p "엔터를 누르면 닫힙니다..."
  exit 1
fi

"$PY" launcher.py
status=$?
if [ $status -ne 0 ]; then
  read -r -p "오류가 있었습니다. 위 내용을 확인하고 엔터를 누르세요..."
fi
exit $status
