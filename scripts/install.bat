@echo off
REM Installation automatique sous Windows (Python 3.10 ou plus recent requis).
setlocal
cd /d "%~dp0\.."

where python >nul 2>nul
if errorlevel 1 (
  echo [ERREUR] Python est introuvable. Installez Python 3.10+ depuis https://www.python.org
  echo          en cochant "Add python.exe to PATH", puis relancez ce script.
  pause
  exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo [ERREUR] Python 3.10 ou plus recent est requis.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Creation de l'environnement virtuel...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error

echo Installation des dependances...
python -m pip install --upgrade pip || goto :error
pip install -r requirements.txt || goto :error

if not exist ".env" copy ".env.example" ".env" >nul

echo Initialisation de la base de donnees...
python scripts\seed_prod.py || goto :error

echo.
echo Installation terminee. NOTEZ les mots de passe affiches ci-dessus.
echo Lancez l'application avec scripts\run.bat
pause
exit /b 0

:error
echo [ERREUR] L'installation a echoue. Consultez les messages ci-dessus.
pause
exit /b 1
