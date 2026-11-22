@echo off
rem wfk - issue-driven workflow CLI entry (zero deps; runs wfk.py)
rem `python` on this box is the Microsoft Store stub (exit 9009), so we resolve a
rem real 3.11+ interpreter ourselves. Override with: set WF_PYTHON=C:\path\python.exe
setlocal
set "SELF=%~dp0wfk.py"
set "PY=%WF_PYTHON%"

if not defined PY set "PY=%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
if not exist "%PY%" set "PY="

if not defined PY (
  uv run --no-project --python 3.11 python "%SELF%" %*
  exit /b %ERRORLEVEL%
)

"%PY%" "%SELF%" %*
exit /b %ERRORLEVEL%
