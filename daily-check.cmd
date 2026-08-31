@echo off
rem Daily check launcher (ASCII only - cmd.exe reads this file in the
rem system codepage, so non-ASCII paths here would be mangled).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" daily_check.py
exit /b %ERRORLEVEL%
