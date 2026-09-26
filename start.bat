@echo off
rem Windows: double-click this file. First run installs everything, then the menu opens.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

py -3 --version >nul 2>&1
if not errorlevel 1 (
  py -3 launcher.py
  goto done
)
python --version >nul 2>&1
if not errorlevel 1 (
  python launcher.py
  goto done
)

echo Python is not installed. Installing Python 3.12 ...
echo 파이썬을 설치합니다. 끝나면 이 창을 닫고 start.bat 을 다시 더블클릭하세요.
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 echo winget 설치 실패: https://www.python.org/downloads/ 에서 설치하세요 (Add python.exe to PATH 체크).
pause
exit /b 1

:done
if errorlevel 1 pause
