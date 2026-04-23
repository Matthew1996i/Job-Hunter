#!/usr/bin/env python3
"""
Setup inteligente para Job Hunter
- Detecta o SO (Linux, macOS, Windows)
- Cria/valida .env automaticamente
- Instala dependências do sistema
- Configura Python venv
- Instala dependências Python
- Sobe docker-compose
- Executa o projeto
"""

import os
import sys
import platform
import subprocess
import shutil
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Cores para output
# ─────────────────────────────────────────────────────────────────────────────

class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def log_success(msg: str):
    print(f"{Colors.GREEN}✓ {msg}{Colors.RESET}")

def log_info(msg: str):
    print(f"{Colors.BLUE}→ {msg}{Colors.RESET}")

def log_warn(msg: str):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.RESET}")

def log_error(msg: str):
    print(f"{Colors.RED}✗ {msg}{Colors.RESET}")

def log_section(msg: str):
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'─' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{msg}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'─' * 80}{Colors.RESET}\n")

# ─────────────────────────────────────────────────────────────────────────────
# Detecção do SO
# ─────────────────────────────────────────────────────────────────────────────

def get_os() -> str:
    """Detecta o sistema operacional"""
    system = platform.system()
    if system == "Darwin":
        return "macos"
    elif system == "Linux":
        return "linux"
    elif system == "Windows":
        return "windows"
    else:
        log_error(f"Sistema operacional não suportado: {system}")
        sys.exit(1)

def get_distro() -> Optional[str]:
    """Detecta a distribuição Linux (se aplicável)"""
    if platform.system() != "Linux":
        return None

    # Tenta detectar via /etc/os-release
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.split("=")[1].strip().strip('"')
    except:
        pass

    return None

# ─────────────────────────────────────────────────────────────────────────────
# Validação de dependências do sistema
# ─────────────────────────────────────────────────────────────────────────────

def check_command(cmd: str) -> bool:
    """Verifica se um comando está disponível"""
    return shutil.which(cmd) is not None

def install_system_deps(os_type: str):
    """Instala dependências do sistema baseado no SO"""
    log_section("Instalando dependências do sistema")

    required = {
        "docker": "Docker",
        "docker-compose": "Docker Compose",
        "python3": "Python 3",
    }

    missing = [cmd for cmd in required if not check_command(cmd)]

    if not missing:
        log_success("Todas as dependências do sistema já estão instaladas")
        return

    log_warn(f"Dependências faltando: {', '.join([required[m] for m in missing])}")

    if os_type == "macos":
        _install_macos_deps(missing)
    elif os_type == "linux":
        _install_linux_deps(missing)
    elif os_type == "windows":
        _install_windows_deps(missing)

def _install_macos_deps(missing: list):
    """Instala dependências no macOS via Homebrew"""
    if not check_command("brew"):
        log_error("Homebrew não encontrado. Instale em https://brew.sh")
        sys.exit(1)

    log_info("Instalando via Homebrew...")

    if "docker" in missing or "docker-compose" in missing:
        log_info("Instalando Docker Desktop (pode precisar de senha)...")
        subprocess.run(["brew", "install", "--cask", "docker"], check=False)
        log_warn("Inicie o Docker Desktop manualmente e execute este script novamente")
        sys.exit(0)

    if "python3" in missing:
        subprocess.run(["brew", "install", "python3"], check=True)

def _install_linux_deps(missing: list):
    """Instala dependências no Linux"""
    distro = get_distro()

    # Atualiza package manager
    log_info("Atualizando package manager...")
    if distro in ("ubuntu", "debian"):
        subprocess.run(["sudo", "apt-get", "update"], check=False)
    elif distro in ("fedora", "centos", "rhel"):
        subprocess.run(["sudo", "dnf", "update", "-y"], check=False)
    elif distro == "arch":
        subprocess.run(["sudo", "pacman", "-Sy"], check=False)

    # Instala dependências
    if "docker" in missing or "docker-compose" in missing:
        log_info("Instalando Docker (pode precisar de senha)...")
        if distro in ("ubuntu", "debian"):
            subprocess.run([
                "sudo", "apt-get", "install", "-y",
                "docker.io", "docker-compose"
            ], check=True)
        elif distro in ("fedora", "centos", "rhel"):
            subprocess.run([
                "sudo", "dnf", "install", "-y",
                "docker", "docker-compose"
            ], check=True)
        elif distro == "arch":
            subprocess.run([
                "sudo", "pacman", "-S", "--noconfirm",
                "docker", "docker-compose"
            ], check=True)

        log_info("Iniciando Docker daemon...")
        subprocess.run(["sudo", "systemctl", "start", "docker"], check=False)
        subprocess.run(["sudo", "systemctl", "enable", "docker"], check=False)

    if "python3" in missing:
        log_info("Instalando Python 3...")
        if distro in ("ubuntu", "debian"):
            subprocess.run(["sudo", "apt-get", "install", "-y", "python3", "python3-pip", "python3-venv"], check=True)
        elif distro in ("fedora", "centos", "rhel"):
            subprocess.run(["sudo", "dnf", "install", "-y", "python3", "python3-pip"], check=True)
        elif distro == "arch":
            subprocess.run(["sudo", "pacman", "-S", "--noconfirm", "python", "python-pip"], check=True)

def _install_windows_deps(missing: list):
    """Instala dependências no Windows"""
    log_warn("Windows detectado")
    log_error("Instale manualmente:")
    if "docker" in missing:
        print("  1. Docker Desktop: https://www.docker.com/products/docker-desktop")
    if "python3" in missing:
        print("  2. Python 3: https://www.python.org/downloads/")

    log_info("Após instalar, execute este script novamente")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Setup de .env
# ─────────────────────────────────────────────────────────────────────────────

def setup_env():
    """Cria ou valida .env com variáveis necessárias"""
    log_section("Configurando variáveis de ambiente")

    env_file = Path(".env")
    env_example = Path(".env.example")

    required_vars = {
        "GROQ_API_KEY": "API Key do Groq (https://console.groq.com)",
        "MONGO_HOST": "Host do MongoDB (padrão: localhost)",
        "MONGO_PORT": "Porta do MongoDB (padrão: 27017)",
        "MONGO_DB": "Nome do banco (padrão: job_hunter)",
    }

    # Se .env não existe, cria do .env.example ou vazio
    if not env_file.exists():
        log_info("Criando arquivo .env...")

        with open(env_file, "w") as f:
            for var, desc in required_vars.items():
                if var == "GROQ_API_KEY":
                    f.write(f"# {desc}\n{var}=sk_live_\n\n")
                else:
                    default = {
                        "MONGO_HOST": "localhost",
                        "MONGO_PORT": "27017",
                        "MONGO_DB": "job_hunter",
                    }.get(var, "")
                    f.write(f"# {desc}\n{var}={default}\n\n")

    # Valida variáveis
    env_vars = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env_vars[key] = val

    log_success(".env configurado")

    # Avisa se falta GROQ_API_KEY
    if not env_vars.get("GROQ_API_KEY") or env_vars.get("GROQ_API_KEY") == "sk_live_":
        log_warn("GROQ_API_KEY não configurada no .env")
        log_info("Obtenha em: https://console.groq.com")
        api_key = input("Cole sua GROQ_API_KEY (ou pressione ENTER para pular): ").strip()
        if api_key:
            with open(env_file, "a") as f:
                f.write(f"\nGROQ_API_KEY={api_key}\n")
            log_success("GROQ_API_KEY salva no .env")

# ─────────────────────────────────────────────────────────────────────────────
# Setup Python
# ─────────────────────────────────────────────────────────────────────────────

def setup_python():
    """Configura virtual environment e instala dependências Python"""
    log_section("Configurando Python")

    venv_path = Path("venv")

    # Cria venv se não existir
    if not venv_path.exists():
        log_info("Criando virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
        log_success("Virtual environment criado")
    else:
        log_success("Virtual environment já existe")

    # Detecta ativação de venv
    if platform.system() == "Windows":
        python_exe = venv_path / "Scripts" / "python.exe"
        pip_exe = venv_path / "Scripts" / "pip.exe"
    else:
        python_exe = venv_path / "bin" / "python"
        pip_exe = venv_path / "bin" / "pip"

    # Instala requirements
    requirements_file = Path("requirements.txt")
    if requirements_file.exists():
        log_info("Instalando dependências Python...")
        subprocess.run([str(pip_exe), "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(pip_exe), "install", "-r", str(requirements_file)], check=True)
        log_success("Dependências Python instaladas")
    else:
        log_warn("requirements.txt não encontrado")

    return python_exe

# ─────────────────────────────────────────────────────────────────────────────
# Docker Compose
# ─────────────────────────────────────────────────────────────────────────────

def setup_docker():
    """Sobe o docker-compose"""
    log_section("Iniciando Docker Compose")

    compose_file = Path("docker-compose.yml")
    if not compose_file.exists():
        log_warn("docker-compose.yml não encontrado")
        return

    log_info("Subindo containers...")
    subprocess.run(["docker-compose", "up", "-d"], check=True)
    log_success("Docker Compose iniciado")

    log_info("Aguardando MongoDB estar pronto...")
    import time
    for i in range(30):
        try:
            subprocess.run(
                ["docker-compose", "exec", "-T", "mongodb", "mongosh", "--eval", "db.adminCommand('ping')"],
                capture_output=True,
                check=True,
                timeout=5
            )
            log_success("MongoDB está pronto")
            break
        except:
            time.sleep(1)
            if i == 29:
                log_warn("MongoDB pode ainda estar iniciando, continuando mesmo assim...")

# ─────────────────────────────────────────────────────────────────────────────
# Execução do projeto
# ─────────────────────────────────────────────────────────────────────────────

def run_project(python_exe: Path):
    """Executa o projeto"""
    log_section("Iniciando Job Hunter")

    resume = input("Caminho do currículo (PDF): ").strip()
    if not resume:
        log_error("Currículo não fornecido")
        sys.exit(1)

    if not Path(resume).exists():
        log_error(f"Arquivo não encontrado: {resume}")
        sys.exit(1)

    log_info(f"Executando com currículo: {resume}")
    subprocess.run([str(python_exe), "job_hunter.py", "--resume", resume])

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    """Executa o setup completo (apenas instalação — não lança o projeto)"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}")
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 78 + "║")
    print("║" + "  🎯  JOB HUNTER - Setup Inteligente".center(78) + "║")
    print("║" + " " * 78 + "║")
    print("╚" + "═" * 78 + "╝")
    print(f"{Colors.RESET}\n")

    try:
        os_type = get_os()
        log_info(f"Sistema operacional: {platform.system()}")

        print(f"{Colors.BOLD}O que deseja instalar?{Colors.RESET}\n")
        print("1. Tudo (dependências do sistema + Python + Docker)")
        print("2. Apenas dependências Python (venv + pip)")
        print("3. Apenas subir Docker Compose")
        print("4. Sair")
        choice = input("\nEscolha (1-4): ").strip()

        if choice == "1":
            install_system_deps(os_type)
            setup_env()
            setup_python()
            setup_docker()

        elif choice == "2":
            setup_env()
            setup_python()

        elif choice == "3":
            setup_docker()

        elif choice == "4":
            sys.exit(0)

        else:
            log_warn("Opção inválida")
            sys.exit(1)

        log_success("\n✓ Instalação concluída! Execute './run.sh' ou 'python3 run.py' para iniciar.")

    except KeyboardInterrupt:
        log_warn("\nSetup cancelado pelo usuário")
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        log_error(f"Comando falhou: {e}")
        sys.exit(1)
    except Exception as e:
        log_error(f"Erro: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
