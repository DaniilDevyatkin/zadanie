@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Кабинет заданий:   http://127.0.0.1:8000
echo Панель админа:     http://127.0.0.1:8000/admin   (логин admin)
echo.
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
