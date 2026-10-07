@echo off
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
if not exist "bin\erpflex_v78_bridge_windows.exe" (
echo ERRO: motor ERPFlex V7.8 nao encontrado em bin\erpflex_v78_bridge_windows.exe
pause
exit /b 1
)
set ERPFLEX_ENGINE=go_v78
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
start "" http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
