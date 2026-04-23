@echo off
REM Script wrapper para Job Hunter no Windows
REM Uso: run.bat [opcoes]

setlocal enabledelayedexpansion

REM Detecta Python
where python3 >nul 2>&1
if !errorlevel! equ 0 (
    set PYTHON=python3
) else (
    where python >nul 2>&1
    if !errorlevel! equ 0 (
        set PYTHON=python
    ) else (
        echo.
        echo X Erro: Python 3 nao encontrado
        echo   Instale Python 3 de: https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
)

REM Se tem venv, ativa
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
    echo o Virtual environment ativado
)

REM Executa
%PYTHON% job_hunter.py %*
