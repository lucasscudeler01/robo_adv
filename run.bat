@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM --- 1. Auto-update (so se git estiver instalado e a pasta for um repo) ---
echo Buscando atualizacoes...
where git >nul 2>&1
if errorlevel 1 goto pula_update
if not exist ".git" goto pula_update
git pull --ff-only
:pula_update

REM --- 2. Ambiente ---
if not exist ".venv\Scripts\activate.bat" goto sem_ambiente
call .venv\Scripts\activate.bat

REM garante dependencias novas que um update possa ter trazido (rapido se ja ok)
python -m pip install -q -r requirements.txt

REM --- 3. Roda o robo (a senha do mes e pedida aqui dentro) ---
python main.py
goto fim

:sem_ambiente
echo ERRO: ambiente nao instalado. Rode instalar.bat primeiro.

:fim
pause
