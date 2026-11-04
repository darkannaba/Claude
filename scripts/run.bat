@echo off
REM Lancement du serveur sous Windows, puis ouverture du navigateur.
setlocal
cd /d "%~dp0\.."
if not exist ".venv\Scripts\activate.bat" (
  echo L'application n'est pas installee : lancez d'abord scripts\install.bat
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
start "" "http://localhost:8000"
python main.py
pause
