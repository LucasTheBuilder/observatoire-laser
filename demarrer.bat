@echo off
cd /d "%~dp0"
docker compose up -d --build
if errorlevel 1 goto erreur
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8765"
exit /b 0
:erreur
echo.
echo Le demarrage a echoue. Verifiez que Docker Desktop est lance.
pause
