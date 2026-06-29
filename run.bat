@echo off
chcp 65001 >nul
REM ============================================================================
REM RODA O ROBO — uso no dia a dia (depois de instalar.bat uma vez)
REM 1) Busca atualizacoes no GitHub  2) garante dependencias  3) roda o robo
REM ============================================================================

cd /d "%~dp0"

REM --- 1. Auto-update (so se git estiver instalado e a pasta for um repo) ---
where git >nul 2>&1
if %errorlevel%==0 (
    if exist ".git" (
        echo Buscando atualizacoes...
        git pull --ff-only
        if errorlevel 1 (
            echo.
            echo AVISO: nao consegui atualizar automaticamente. Seguindo com a
            echo versao atual. Se o robo der erro, fale com quem te passou.
            echo.
        )
    )
)

REM --- 2. Ambiente ---
if not exist ".venv\Scripts\activate.bat" (
    echo ERRO: ambiente nao instalado. Rode instalar.bat primeiro.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

REM garante dependencias novas que um update possa ter trazido (rapido se ja ok)
python -m pip install -q -r requirements.txt

REM --- 3. Roda o robo (a senha do mes e pedida aqui dentro) ---
python main.py

pause
