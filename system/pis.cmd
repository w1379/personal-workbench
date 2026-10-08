@echo off
if not exist "%~dp0runtime\python\python.exe" (
  echo Project runtime missing. Read system/docs/SETUP.md and run system/setup_runtime.py. 1>&2
  exit /b 1
)
"%~dp0runtime\python\python.exe" -X utf8 "%~dp0pis.py" %*
exit /b %errorlevel%
