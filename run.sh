#!/bin/bash

# Script wrapper para Job Hunter
# Uso: ./run.sh [opções]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Detecta Python3
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "✗ Erro: Python 3 não encontrado"
    echo "  Instale Python 3 ou execute: python3 run.py"
    exit 1
fi

# Se tem venv, ativa
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✓ Virtual environment ativado"
fi

# Executa
$PYTHON job_hunter.py "$@"
