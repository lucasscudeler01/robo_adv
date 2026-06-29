@echo off
REM ============================================================================
REM SETUP DO ROBO DE MANIFESTACAO — Execute este arquivo UMA VEZ apos instalar
REM o Python. Ele cria um ambiente virtual e instala todas as dependencias.
REM ============================================================================

echo.
echo === SETUP DO ROBO DE MANIFESTACAO ===
echo.

REM Vai pra pasta onde este .bat esta
cd /d "%~dp0"

REM Verifica se o Python esta instalado
python --version >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado.
    echo.
    echo Instale o Python 3.11 de https://www.python.org/downloads/
    echo IMPORTANTE: marque a opcao "Add Python to PATH" durante a instalacao.
    pause
    exit /b 1
)

echo [1/4] Criando ambiente virtual (.venv)...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo ERRO ao criar ambiente virtual.
        pause
        exit /b 1
    )
)

echo [2/4] Ativando ambiente virtual...
call .venv\Scripts\activate.bat

echo [3/4] Instalando dependencias (isso pode levar alguns minutos)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO ao instalar dependencias.
    pause
    exit /b 1
)

echo [4/4] Baixando o navegador do Playwright (isso pode levar 2-5 minutos)...
python -m playwright install chromium
if errorlevel 1 (
    echo ERRO ao baixar o navegador.
    pause
    exit /b 1
)

echo.
echo ============================================================================
echo  SETUP CONCLUIDO COM SUCESSO!
echo ============================================================================
echo.
echo  Proximos passos:
echo    1. Coloque sua planilha em: dados\planilha_entrada.xlsx
echo    2. Execute: run.bat
echo.
pause
