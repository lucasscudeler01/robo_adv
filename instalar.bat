@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
REM ============================================================================
REM INSTALADOR DO ROBO NPJUR — o amigo roda ISTO uma unica vez.
REM Instala Git e Python (se faltarem), baixa o robo do GitHub, monta o
REM ambiente e cria um atalho na area de trabalho.
REM
REM >>> ADMIN: troque a linha REPO abaixo pela URL do SEU repositorio. <<<
REM ============================================================================

set "REPO=https://github.com/lucasscudeler01/robo_adv"
set "DESTINO=%USERPROFILE%\Documents\robo_npjur"

echo.
echo === INSTALADOR DO ROBO NPJUR ===
echo Pasta de instalacao: %DESTINO%
echo.

REM --- 1. Git ---
where git >nul 2>&1
if errorlevel 1 (
    echo [1/5] Instalando o Git...
    winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
) else (
    echo [1/5] Git ja instalado.
)

REM --- 2. Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [2/5] Instalando o Python...
    winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
) else (
    echo [2/5] Python ja instalado.
)

REM Confere de novo: o winget as vezes so coloca no PATH apos reabrir o terminal
where git >nul 2>&1
if errorlevel 1 goto :precisa_reabrir
where python >nul 2>&1
if errorlevel 1 goto :precisa_reabrir

REM --- 3. Baixar / atualizar o robo ---
if exist "%DESTINO%\.git" (
    echo [3/5] Atualizando o robo existente...
    cd /d "%DESTINO%"
    git pull --ff-only
) else (
    echo [3/5] Baixando o robo do GitHub (vai pedir login do GitHub na 1a vez)...
    git clone "%REPO%" "%DESTINO%"
    if errorlevel 1 (
        echo ERRO ao baixar do GitHub. Confira sua conexao e o acesso ao repositorio.
        pause
        exit /b 1
    )
    cd /d "%DESTINO%"
)

REM --- 4. Ambiente Python + navegador ---
echo [4/5] Montando o ambiente (pode levar alguns minutos)...
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO ao instalar dependencias.
    pause
    exit /b 1
)
python -m playwright install chromium
if errorlevel 1 (
    echo ERRO ao baixar o navegador do Playwright.
    pause
    exit /b 1
)

REM --- 5. Atalho na area de trabalho ---
echo [5/5] Criando atalho na area de trabalho...
powershell -NoProfile -Command ^
  "$ws=New-Object -ComObject WScript.Shell; $lnk=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Robo NPJUR.lnk'); $lnk.TargetPath='%DESTINO%\run.bat'; $lnk.WorkingDirectory='%DESTINO%'; $lnk.Save()"

echo.
echo ============================================================================
echo  INSTALACAO CONCLUIDA!
echo  Use o atalho "Robo NPJUR" na area de trabalho para abrir o robo.
echo  (Coloque sua planilha na pasta:  %DESTINO%\dados )
echo ============================================================================
echo.
pause
exit /b 0

:precisa_reabrir
echo.
echo ============================================================================
echo  O Git e/ou o Python foram instalados agora, mas o Windows so reconhece
echo  eles depois de REABRIR. Feche esta janela e rode o instalar.bat de novo.
echo ============================================================================
echo.
pause
exit /b 0
