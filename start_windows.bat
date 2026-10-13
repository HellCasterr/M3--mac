@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Add your Upstox credentials to .env, then run this file again.
  pause
  exit /b 1
)

call .venv\Scripts\python.exe run.py
if errorlevel 1 pause
