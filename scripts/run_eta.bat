@echo off
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m eta_automations %*
) else (
  python -m eta_automations %*
)
