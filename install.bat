@echo off
chcp 65001 >nul
title Gemini 3.8 Flash 실시간 번역기 - 패키지 설치

echo ========================================================
echo  Gemini 3.8 Flash 실시간 화면/오디오 번역기 환경 설치
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [오류] Python이 설치되어 있지 않거나 PATH에 등록되지 않았습니다.
    echo https://www.python.org/downloads/ 에서 Python 3.10 이상을 설치해 주세요.
    echo 설치 시 하단의 "Add Python to PATH"를 반드시 체크해 주세요.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/3] 가상환경(.venv)을 생성합니다...
    python -m venv .venv
) else (
    echo [1/3] 기존 가상환경(.venv)을 사용합니다.
)

echo [2/3] pip 업그레이드 및 필수 패키지를 설치합니다...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo [오류] 패키지 설치 중 문제가 발생했습니다. 인터넷 연결을 확인해 주세요.
    pause
    exit /b 1
)

echo.
echo [3/3] 자체 동작 검증(Self-Test)을 실행합니다...
python screen_translator.py --test

echo.
echo ========================================================
echo  설치가 완료되었습니다!
echo  이제 'run.bat' 파일을 더블클릭하여 번역기를 실행하세요.
echo ========================================================
pause
