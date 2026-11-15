@echo off
REM Resolved. - one-click start for Windows
cd /d "%~dp0"
REM Rebuilds the data only if it was seeded on another day: the demo lines say "yesterday".
python seed.py --if-stale
echo.
echo Starting Resolved. on http://127.0.0.1:8000
echo Console: http://127.0.0.1:8000/console
echo.
start "" http://127.0.0.1:8000
python server.py
pause
