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
REM Ouvre le navigateur seulement quand le serveur repond (le premier demarrage met a jour la base).
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 90;$i++){try{Invoke-WebRequest -UseBasicParsing http://localhost:8000/health -TimeoutSec 2 | Out-Null; Start-Process http://localhost:8000; break}catch{Start-Sleep -Seconds 1}}"
echo Demarrage du serveur : NE FERMEZ PAS cette fenetre (le navigateur s ouvrira automatiquement).
python main.py
pause
