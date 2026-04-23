#!/usr/bin/env python3
"""
Script wrapper universal para executar Job Hunter
Funciona em Linux, macOS e Windows
"""

import sys
import subprocess
from pathlib import Path


def find_python():
    """Retorna o executável Python do venv se existir, senão usa o atual."""
    base = Path(__file__).parent
    candidates = [
        base / "venv" / "bin" / "python",        # Linux/macOS
        base / "venv" / "Scripts" / "python.exe", # Windows
        base / ".venv" / "bin" / "python",
        base / ".venv" / "Scripts" / "python.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return sys.executable


def main():
    job_hunter_script = Path(__file__).parent / "job_hunter.py"

    if not job_hunter_script.exists():
        print(f"✗ Erro: job_hunter.py não encontrado em {job_hunter_script}")
        sys.exit(1)

    python = find_python()

    try:
        result = subprocess.run(
            [python, str(job_hunter_script)] + sys.argv[1:],
            cwd=Path(__file__).parent
        )
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        print("\n\n  ✓ Até logo!\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
