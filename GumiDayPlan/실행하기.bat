@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 구미에서 뭐하지 - 로컬 서버
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 파이썬 가상환경을 만들고 있어요...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 (echo 파이썬 3.10 이상 설치 후 다시 실행해 주세요. & pause & exit /b 1)
)
call ".venv\Scripts\activate.bat"
echo [2/3] 의존성 확인 중... 최초 실행 시 패키지 설치가 필요합니다.
python -m pip install -r requirements.txt
if errorlevel 1 (echo 설치 실패: 인터넷 연결과 파이썬 버전을 확인해 주세요. & pause & exit /b 1)
echo.
echo [3/3] 사이트 실행: http://localhost:8000
echo 웹 브라우저를 닫아도 서버는 계속 실행됩니다. 종료하려면 이 창에서 Ctrl+C.
start "" "http://localhost:8000"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
if errorlevel 1 (echo 서버 실행 오류입니다. 포트 8000 사용 여부를 확인하세요. & pause)
