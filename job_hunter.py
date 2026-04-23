#!/usr/bin/env python3
"""
job_hunter.py
Lê seu currículo em PDF, busca vagas em múltiplas plataformas e usa
a API do Groq (llama-3.3-70b) para avaliar aderência (mínimo 80%).

Plataformas suportadas:
  Brasil:        Indeed BR, Gupy, Vagas.com.br
  Internacional: Indeed USA, RemoteOK, Himalayas, We Work Remotely
  LinkedIn:      LinkedIn Jobs, LinkedIn Feed Posts (requer LINKEDIN_LI_AT)

Motor de scraping: curl_cffi (TLS fingerprint Chrome 124) — bypassa Cloudflare.
Fila e persistência: MongoDB via Docker.
Query de busca: gerada automaticamente pela IA com base no perfil e plataformas.

Pré-requisitos:
    docker-compose up -d
    pip install -r requirements.txt

Variáveis de ambiente:
    GROQ_API_KEY       — obrigatório (https://console.groq.com)
    LINKEDIN_LI_AT     — opcional, para busca no feed do LinkedIn
    MONGO_HOST / MONGO_PORT / MONGO_DB — opcionais (padrão: localhost:27017/job_hunter)

Uso:
    python job_hunter.py --resume curriculo.pdf
    python job_hunter.py --resume curriculo.pdf --max-pages 3
    python job_hunter.py --resume curriculo.pdf --source remoteok --query "react developer"
"""

import argparse
import hashlib
import json
import os
import random
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union, Any

from pymongo import MongoClient, UpdateOne
import pymongo
from groq import Groq
import pdfplumber
from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup
import questionary
from questionary import Style as QStyle

# ──────────────────────────────────────────────────────────────────────────────
# Controle de fluxo
# ──────────────────────────────────────────────────────────────────────────────

class UserAbort(Exception):
    """
    Levantada quando o usuário pressiona ESC / Ctrl+C dentro de um sub-menu.
    Faz o loop principal voltar ao menu inicial sem encerrar o processo.
    """


# ──────────────────────────────────────────────────────────────────────────────
# Controle de parada do scraping (thread-safe)
# ──────────────────────────────────────────────────────────────────────────────

# Setado pela thread listener quando o usuário pressiona Q/ESC durante o scraping.
_STOP_SCRAPE:   threading.Event = threading.Event()
# Mantido em True enquanto scrape_sources() estiver em execução.
_SCRAPE_ACTIVE: threading.Event = threading.Event()


def _scrape_key_listener() -> None:
    """
    Thread auxiliar iniciada por scrape_sources().
    Fica em cbreak mode (lê tecla a tecla sem Enter) e seta _STOP_SCRAPE
    quando o usuário pressiona Q, ESC ou barra de espaço.
    Encerra automaticamente quando _SCRAPE_ACTIVE for limpo.
    """
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return                         # stdin não é um tty (pipe/redirecionamento)
    try:
        tty.setcbreak(fd)              # sem eco, sem canonical — output não afetado
        while _SCRAPE_ACTIVE.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.15)
            if r:
                ch = os.read(fd, 1)
                if ch in (b"q", b"Q", b"\x1b", b" "):
                    _STOP_SCRAPE.set()
                    break
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


def _scrape_aborted() -> bool:
    """Retorna True se o usuário pediu para parar o scraping."""
    return _STOP_SCRAPE.is_set()


# ──────────────────────────────────────────────────────────────────────────────
# Filtro de relevância por título
# ──────────────────────────────────────────────────────────────────────────────

# Palavras que NÃO devem ser usadas para filtrar títulos (localização, modalidade, etc.)
_RELEVANCE_STOPWORDS: frozenset[str] = frozenset({
    # Localização / modalidade
    "remote", "remoto", "remota", "presencial", "hibrido", "híbrido",
    "brasil", "brazil", "usa", "uk", "us", "international", "internacional",
    "home", "office", "anywhere",
    # Preposições / conjunções PT e EN
    "and", "or", "for", "the", "in", "at", "with", "to",
    "e", "ou", "de", "para", "com", "em", "na", "no", "por", "dos", "das",
    # Palavras muito genéricas de vaga
    "vaga", "vagas", "job", "jobs", "position", "opportunity", "oportunidade",
    "opening", "role",
})

# Keywords ativas para a busca corrente (preenchido por scrape_sources antes de iniciar)
_ACTIVE_KEYWORDS: list[str] = []


def _set_active_keywords(query: str) -> None:
    """Extrai keywords relevantes da query e as armazena para o filtro de títulos."""
    global _ACTIVE_KEYWORDS
    words = re.findall(r"[a-záéíóúàâêôãõç#\+\.]{2,}", query.lower())
    _ACTIVE_KEYWORDS = [w for w in words if w not in _RELEVANCE_STOPWORDS]


def _title_is_relevant(title: str) -> bool:
    """
    Retorna True se o título da vaga contém pelo menos uma keyword da busca.
    Normaliza hífens (front-end → frontend) antes de comparar.
    Se não houver keywords ativas, aceita tudo.
    """
    if not _ACTIVE_KEYWORDS:
        return True
    # Normaliza: lower + remove hífen/barra para fundir "front-end" → "frontend"
    normalized = re.sub(r"[-/]", "", title.lower())
    for kw in _ACTIVE_KEYWORDS:
        kw_norm = re.sub(r"[-/]", "", kw)
        # Boundary check: evita "front" matchando "frontend" erroneamente
        if re.search(r"\b" + re.escape(kw_norm) + r"\b", normalized):
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Visual feedback
# ──────────────────────────────────────────────────────────────────────────────

_G   = "\033[92m"    # verde
_R   = "\033[91m"    # vermelho
_Y   = "\033[93m"    # amarelo
_B   = "\033[94m"    # azul
_CY  = "\033[96m"    # ciano
_BD  = "\033[1m"     # negrito
_DIM = "\033[2m"     # fraco
_RST = "\033[0m"     # reset

# Símbolos — (✔) verde com círculo  |  (✗) vermelho
CHECK = f"{_G}{_BD}(✔){_RST}"
CROSS = f"{_R}{_BD}(✗){_RST}"
ARROW = f"{_B}{_BD} → {_RST}"
WARN  = f"{_Y}{_BD} ⚠ {_RST}"
DOTS  = f"{_DIM}...{_RST}"

# ── Modo verbose ──────────────────────────────────────────────────────────────
_VERBOSE: bool = False

def set_verbose(v: bool) -> None:
    global _VERBOSE
    _VERBOSE = v

# ── API status — único ponto de verdade ───────────────────────────────────────
_api_ok: bool = False   # True somente quando GROQ_API_KEY foi validada com sucesso

def set_api_ok(ok: bool) -> None:
    global _api_ok
    _api_ok = ok

def get_api_ok() -> bool:
    return _api_ok

# ── Referência global ao MongoManager (definida em main()) ───────────────────
_global_rdb: Optional[Any] = None

# ── Spinner animado ───────────────────────────────────────────────────────────
_active_spinner:   Optional[Any] = None   # spinner global (modo discreto)
_verbose_spinner:  Optional[Any] = None   # spinner da operação atual (verbose)

class Spinner:
    """Spinner de braille para processos demorados. Thread-safe."""
    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, msg: str = ""):
        self._msg    = msg
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._lock   = threading.Lock()
        self._width  = 0  # chars da última linha escrita (para apagar)

    # ── internos ──────────────────────────────────────────────────────────────
    def _render(self, frame: str) -> str:
        return f"\r  {_CY}{frame}{_RST}  {self._msg}"

    def _erase(self) -> None:
        sys.stdout.write(f"\r{' ' * (self._width + 4)}\r")
        sys.stdout.flush()

    def _spin(self) -> None:
        i = 0
        while self._active:
            with self._lock:
                line = self._render(self._FRAMES[i % len(self._FRAMES)])
                self._width = len(self._msg) + 6
                sys.stdout.write(line)
                sys.stdout.flush()
            time.sleep(0.08)
            i += 1

    # ── API pública ───────────────────────────────────────────────────────────
    def clear_line(self) -> None:
        """Limpa a linha do spinner sem pará-lo (chamado por log_* antes de print)."""
        with self._lock:
            self._erase()

    def update(self, msg: str) -> None:
        """Troca a mensagem do spinner em tempo real."""
        self._msg = msg

    def start(self, msg: str = "") -> "Spinner":
        global _active_spinner
        if msg:
            self._msg = msg
        self._active  = True
        _active_spinner = self
        self._thread  = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def stop(self, ok_msg: str = "") -> None:
        """Para o spinner; ok_msg é impresso como log_ok se fornecido."""
        global _active_spinner
        self._active    = False
        _active_spinner = None
        if self._thread:
            self._thread.join(timeout=0.3)
        with self._lock:
            self._erase()
        if ok_msg:
            print(f"  {CHECK} {ok_msg}")

    def done(self) -> None:
        """Verbose mode: congela a linha atual como '→ msg' (operação concluída)."""
        global _verbose_spinner
        self._active   = False
        _verbose_spinner = None
        if self._thread:
            self._thread.join(timeout=0.3)
        with self._lock:
            self._erase()
        # Imprime a linha estática com a seta (operação concluída, sem ✓/✗ próprio)
        print(f"  {ARROW}{_DIM}{self._msg}{_RST}")

    def fail(self, err_msg: str = "") -> None:
        """Para o spinner com mensagem de erro."""
        global _active_spinner
        self._active    = False
        _active_spinner = None
        if self._thread:
            self._thread.join(timeout=0.3)
        with self._lock:
            self._erase()
        if err_msg:
            print(f"  {CROSS} {err_msg}")

    # ── context manager ───────────────────────────────────────────────────────
    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type:
            self.fail()
        else:
            self.stop()


def _spin_clear() -> None:
    """Apaga a linha do spinner ativo (discreto ou verbose) antes de imprimir."""
    if _verbose_spinner:
        _verbose_spinner._erase()
    elif _active_spinner:
        _active_spinner.clear_line()


def _verbose_done() -> None:
    """Finaliza o verbose spinner da operação atual (congela como linha estática)."""
    global _verbose_spinner
    if _verbose_spinner:
        _verbose_spinner.done()
        _verbose_spinner = None


def spinner(msg: str) -> Spinner:
    """Atalho: retorna um Spinner para usar como context manager."""
    return Spinner(msg)


# ── Funções de log ────────────────────────────────────────────────────────────

def log_ok(msg: str) -> None:
    """Sucesso. Suprimido durante spinner em modo discreto (evita histórico de linhas)."""
    if _active_spinner and not _VERBOSE:
        return
    _verbose_done()   # finaliza operação verbose pendente
    _spin_clear()
    print(f"  {CHECK} {msg}")

def log_err(msg: str) -> None:
    """Erro crítico — sempre visível, mesmo durante spinner."""
    global _verbose_spinner
    if _verbose_spinner:
        _verbose_spinner._erase()
        _verbose_spinner = None
    _spin_clear()
    print(f"  {CROSS} {msg}")

def log_info(msg: str) -> None:
    """Detalhe técnico. Em verbose: mostra spinner na linha da operação atual."""
    global _verbose_spinner
    if not _VERBOSE:
        return
    # Finaliza a operação anterior (congela linha estática) e inicia nova
    _verbose_done()
    _verbose_spinner = Spinner(msg).start()

def log_warn(msg: str) -> None:
    """Aviso — suprimido durante spinner em modo discreto."""
    if _active_spinner and not _VERBOSE:
        return
    _verbose_done()
    _spin_clear()
    print(f"  {WARN}{msg}")

def log_scrape(msg: str) -> None:
    """Resultado de scraping (vaga aceita/ignorada) — só visível em verbose.
    Para o verbose spinner da operação atual e imprime o resultado."""
    if not _VERBOSE:
        return
    _verbose_done()   # congela linha da operação
    print(msg)        # imprime resultado (✓, ↷, etc.)

def section(title: str) -> None:
    _spin_clear()
    print(f"\n{_BD}{_CY}{'─' * 60}{_RST}")
    print(f"  {_BD}{title}{_RST}")
    print(f"{_BD}{_CY}{'─' * 60}{_RST}")


def clr() -> None:
    """Limpa o terminal — usa sequência ANSI para compatibilidade."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


# ──────────────────────────────────────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────────────────────────────────────

# Diretório base para perfis persistentes de browser por plataforma
BROWSER_PROFILES_DIR: Path = Path.home() / ".job_hunter" / "browser_profiles"

INDEED_DOMAINS = {
    "br": "https://br.indeed.com",
    "us": "https://www.indeed.com",
}
LINKEDIN_DOMAIN = "https://www.linkedin.com"

# Mapa de fontes disponíveis
# region: "brasil" | "internacional" | "ambos"
# URLs de login por plataforma (usadas pelo passo de autenticação)
LOGIN_URLS: dict[str, str] = {
    "linkedin":       "https://www.linkedin.com/login",
    "linkedin-feed":  "https://www.linkedin.com/login",
    "indeed-br":      "https://secure.br.indeed.com/account/login",
    "indeed-us":      "https://secure.indeed.com/account/login",
    "gupy":           "https://portal.gupy.io/login",
    "catho":          "https://seguro.catho.com.br/candidato/login/",
    "infojobs":       "https://www.infojobs.com.br/candidato/login",
    "vagas":          "https://www.vagas.com.br/login-candidatos",
    "programathor":   "https://programathor.com.br/users/sign_in",
    "geekHunter":     "https://www.geekHunter.com.br/login",
    "revelo":         "https://www.revelo.com.br/candidato/login",
    "impulso":        "https://impulso.network/login",
    "remotar":        "https://remotar.com.br/login",
    "workana":        "https://www.workana.com/login",
    "99freelas":      "https://www.99freelas.com.br/user/login",
    "upwork":         "https://www.upwork.com/ab/account-security/login",
    "turing":         "https://www.turing.com/auth/login",
    "toptal":         "https://www.toptal.com/login",
    "glassdoor":      "https://www.glassdoor.com.br/profile/login_input.htm",
}

# Seções de perfil por plataforma — (URL, descrição da seção)
PROFILE_EDIT_SECTIONS: dict[str, list[tuple[str, str]]] = {
    "linkedin": [
        ("https://www.linkedin.com/in/me/edit/intro/",  "Informações básicas / Headline"),
        ("https://www.linkedin.com/in/me/edit/about/",  "Sobre / Resumo profissional"),
    ],
    "linkedin-feed": [
        ("https://www.linkedin.com/in/me/edit/intro/",  "Informações básicas / Headline"),
        ("https://www.linkedin.com/in/me/edit/about/",  "Sobre / Resumo profissional"),
    ],
    "indeed-br": [
        ("https://profile.indeed.com/",                 "Perfil completo"),
    ],
    "indeed-us": [
        ("https://profile.indeed.com/",                 "Full profile"),
    ],
    "gupy": [
        ("https://portal.gupy.io/profile",              "Perfil"),
    ],
    "catho": [
        ("https://seguro.catho.com.br/candidato/curriculo/dados-pessoais/", "Dados pessoais"),
        ("https://seguro.catho.com.br/candidato/curriculo/experiencia/",    "Experiência"),
        ("https://seguro.catho.com.br/candidato/curriculo/escolaridade/",   "Escolaridade"),
    ],
    "infojobs": [
        ("https://curriculoonline.infojobs.com.br/",    "Currículo online"),
    ],
    "vagas": [
        ("https://www.vagas.com.br/meu-curriculo",      "Meu currículo"),
    ],
    "programathor": [
        ("__auto__:https://programathor.com.br",         "Perfil"),
    ],
    "geekHunter": [
        ("https://www.geekHunter.com.br/profile",       "Perfil"),
    ],
    "revelo": [
        ("https://www.revelo.com.br/candidato/perfil",  "Perfil profissional"),
    ],
    "impulso": [
        ("https://impulso.network/profile",             "Perfil"),
    ],
    "remotar": [
        ("https://remotar.com.br/profile",              "Perfil"),
    ],
    "workana": [
        ("https://www.workana.com/settings/profile",    "Perfil freelancer"),
    ],
    "99freelas": [
        ("https://www.99freelas.com.br/user/profile",   "Perfil freelancer"),
    ],
    "upwork": [
        ("https://www.upwork.com/freelancers/settings/contactInfo", "Informações de contato"),
        ("https://www.upwork.com/freelancers/settings/profile",     "Perfil profissional"),
    ],
    "turing": [
        ("https://www.turing.com/developer/profile",    "Perfil"),
    ],
    "toptal": [
        ("https://www.toptal.com/profile",              "Perfil"),
    ],
}

SOURCES = {
    # login_required=True → plataforma bloqueia vagas sem autenticação
    "indeed-br":      {"type": "indeed",         "domain": INDEED_DOMAINS["br"],           "label": "Indeed BR",        "region": "brasil",        "login_required": False},
    "indeed-us":      {"type": "indeed",         "domain": INDEED_DOMAINS["us"],           "label": "Indeed USA",       "region": "internacional", "login_required": False},
    "linkedin-br":    {"type": "linkedin-br",    "domain": LINKEDIN_DOMAIN,                "label": "LinkedIn Brasil",  "region": "brasil",        "login_required": False},
    "linkedin-intl":  {"type": "linkedin-intl",  "domain": LINKEDIN_DOMAIN,                "label": "LinkedIn Global",  "region": "internacional",  "login_required": False},
    "linkedin":       {"type": "linkedin",       "domain": LINKEDIN_DOMAIN,                "label": "LinkedIn Jobs",    "region": "ambos",          "login_required": False},
    "linkedin-feed":  {"type": "linkedin-feed",  "domain": LINKEDIN_DOMAIN,                "label": "LinkedIn Feed",    "region": "ambos",          "login_required": True},
    "gupy":           {"type": "gupy",           "domain": "https://portal.gupy.io",       "label": "Gupy",             "region": "brasil",        "login_required": False},
    "vagas":          {"type": "vagas",          "domain": "https://www.vagas.com.br",     "label": "Vagas.com.br",     "region": "brasil",        "login_required": False},
    "remoteok":       {"type": "remoteok",       "domain": "https://remoteok.com",         "label": "RemoteOK",         "region": "internacional", "login_required": False},
    "himalayas":      {"type": "himalayas",      "domain": "https://himalayas.app",        "label": "Himalayas",        "region": "internacional", "login_required": False},
    "weworkremotely": {"type": "weworkremotely", "domain": "https://weworkremotely.com",   "label": "We Work Remotely", "region": "internacional", "login_required": False},
    "programathor":   {"type": "programathor",  "domain": "https://programathor.com.br",  "label": "ProgramaThor",     "region": "brasil",        "login_required": True},
    "geekHunter":     {"type": "geekHunter",    "domain": "https://www.geekhunter.com.br","label": "GeekHunter",       "region": "brasil",        "login_required": False},
    "catho":          {"type": "catho",         "domain": "https://www.catho.com.br",     "label": "Catho",            "region": "brasil",        "login_required": False},
    "infojobs":       {"type": "infojobs",      "domain": "https://www.infojobs.com.br",  "label": "InfoJobs",         "region": "brasil",        "login_required": False},
    "impulso":        {"type": "impulso",       "domain": "https://impulso.network",      "label": "Impulso",          "region": "brasil",        "login_required": True},
    "remotar":        {"type": "remotar",       "domain": "https://remotar.com.br",       "label": "Remotar",          "region": "brasil",        "login_required": False},
    "revelo":         {"type": "revelo",        "domain": "https://www.revelo.com.br",    "label": "Revelo",           "region": "brasil",        "login_required": True},
    "workana":        {"type": "workana",       "domain": "https://www.workana.com",      "label": "Workana",          "region": "ambos",         "login_required": False},
    "99freelas":      {"type": "99freelas",     "domain": "https://www.99freelas.com.br", "label": "99Freelas",        "region": "brasil",        "login_required": False},
    "turing":         {"type": "turing",        "domain": "https://www.turing.com",       "label": "Turing",           "region": "internacional", "login_required": True},
    "toptal":         {"type": "toptal",        "domain": "https://www.toptal.com",       "label": "Toptal",           "region": "internacional", "login_required": True},
    "upwork":         {"type": "upwork",        "domain": "https://www.upwork.com",       "label": "Upwork",           "region": "ambos",         "login_required": True},
    "glassdoor":      {"type": "glassdoor",     "domain": "https://www.glassdoor.com.br", "label": "Glassdoor",        "region": "ambos",         "login_required": True},
    "ziprecruiter":   {"type": "ziprecruiter",  "domain": "https://www.ziprecruiter.com", "label": "ZipRecruiter",     "region": "internacional", "login_required": False},
    "careerjet":      {"type": "careerjet",     "domain": "https://www.careerjet.com.br", "label": "Careerjet BR",     "region": "brasil",        "login_required": False},
    "jora":           {"type": "jora",          "domain": "https://www.jora.com",         "label": "Jora",             "region": "internacional", "login_required": False},
}
SOURCE_CHOICES = list(SOURCES.keys()) + ["all"]

# Opções de recência — aplicadas como filtros nas plataformas que suportam
RECENCY_OPTIONS = [
    ("1d",  "Últimas 24 horas"),
    ("3d",  "Últimos 3 dias"),
    ("7d",  "Última semana   ← recomendado"),
    ("14d", "Últimas 2 semanas"),
    ("any", "Qualquer data"),
]
# Parâmetros HTTP de recência por plataforma
_RECENCY_INDEED   = {"1d": 1,  "3d": 3,  "7d": 7,  "14d": 14, "any": None}
_RECENCY_LINKEDIN = {"1d": "r86400", "3d": "r259200", "7d": "r604800", "14d": "r1209600", "any": ""}
_RECENCY_DAYS     = {"1d": 1,  "3d": 3,  "7d": 7,  "14d": 14}   # genérico: dias → timestamp

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MIN_MATCH_SCORE  = 80
DELAY_MIN        = 2.0   # segundos entre requisições (mínimo)
DELAY_MAX        = 4.5   # segundos entre requisições (máximo)
PAGE_TIMEOUT_MS  = 30_000

MONGO_HOST = os.environ.get("MONGO_HOST", "localhost")
MONGO_PORT = int(os.environ.get("MONGO_PORT", "27017"))
MONGO_DB   = os.environ.get("MONGO_DB",   "job_hunter")
MONGO_USER = os.environ.get("MONGO_USER", "")
MONGO_PASS = os.environ.get("MONGO_PASS", "")

# ──────────────────────────────────────────────────────────────────────────────
# Stacks, tecnologias e roles para o menu de query
# ──────────────────────────────────────────────────────────────────────────────

STACKS: dict[str, dict] = {
    "🖥  Frontend": {
        "role": "frontend developer",
        "techs": [
            # Frameworks & libs principais
            "React", "Vue.js", "Angular", "Next.js", "Nuxt.js",
            "Svelte", "SvelteKit", "Astro", "Remix", "Qwik",
            "Solid.js", "Lit", "Alpine.js", "Preact",
            # Linguagens
            "TypeScript", "JavaScript", "HTML5", "CSS3", "WebAssembly",
            # Estilo & UI
            "Tailwind CSS", "Sass/SCSS", "Styled Components", "Emotion",
            "MUI", "Ant Design", "Chakra UI", "Shadcn/ui", "Radix UI",
            "Bootstrap", "Storybook", "CSS Modules",
            # Estado & dados
            "Redux", "Zustand", "Jotai", "Recoil", "MobX",
            "React Query", "SWR", "Apollo Client", "GraphQL",
            # Build & tooling
            "Vite", "Webpack", "Rollup", "esbuild", "Turbopack",
            "Babel", "ESLint", "Prettier", "Vitest", "Jest",
            "Cypress", "Playwright", "Testing Library",
            # Performance & padrões
            "PWA", "Web Components", "Micro-frontends",
            "Server-Side Rendering", "Static Site Generation",
            "Core Web Vitals", "Accessibility (a11y)", "i18n",
        ],
    },
    "⚙  Backend": {
        "role": "backend developer",
        "techs": [
            # Linguagens
            "Node.js", "Python", "Java", "Go", "PHP", "Ruby",
            "C#", "Rust", "Elixir", "Scala", "Kotlin", "C++",
            # Frameworks
            "FastAPI", "Django", "Flask", "Express", "NestJS",
            "Spring Boot", "Spring Framework", "Quarkus", "Micronaut",
            "Laravel", "Symfony", "Ruby on Rails", "Gin", "Echo",
            "Fiber", "Actix-web", "Phoenix", "Hapi.js", "Koa",
            "AdonisJS", ".NET Core", "ASP.NET",
            # Bancos de dados relacionais
            "PostgreSQL", "MySQL", "SQL Server", "Oracle", "SQLite",
            # Bancos NoSQL
            "MongoDB", "Redis", "Elasticsearch", "Cassandra",
            "DynamoDB", "CouchDB", "RavenDB", "InfluxDB",
            # Mensageria & streaming
            "Kafka", "RabbitMQ", "NATS", "SQS", "Pub/Sub",
            "gRPC", "WebSockets", "REST API", "GraphQL", "tRPC",
            # ORM & migrations
            "Prisma", "TypeORM", "Sequelize", "SQLAlchemy",
            "Hibernate", "Drizzle", "Knex.js",
            # Auth & segurança
            "JWT", "OAuth2", "OpenID Connect", "Keycloak",
            # Infra de apoio
            "Docker", "Nginx", "Redis Cache", "CDN",
            "Microservices", "Clean Architecture", "DDD", "CQRS",
        ],
    },
    "🔀 Full Stack": {
        "role": "full stack developer",
        "techs": [
            # Frontend
            "React", "Vue.js", "Angular", "Next.js", "Nuxt.js",
            "TypeScript", "JavaScript", "Tailwind CSS", "HTML5", "CSS3",
            # Backend
            "Node.js", "Python", "Go", "Java", "C#", "PHP",
            "FastAPI", "Django", "Express", "NestJS", "Laravel",
            "Spring Boot", ".NET Core",
            # Banco de dados
            "PostgreSQL", "MySQL", "MongoDB", "Redis",
            "Prisma", "TypeORM", "SQLAlchemy",
            # APIs
            "REST API", "GraphQL", "tRPC", "WebSockets", "gRPC",
            # Infra & deploy
            "Docker", "Kubernetes", "AWS", "GCP", "Azure",
            "Vercel", "Netlify", "Railway", "Heroku",
            "CI/CD", "GitHub Actions", "Linux",
            # Padrões
            "Microservices", "Monorepo", "Clean Architecture",
            "DDD", "TDD", "BDD",
        ],
    },
    "📱 Mobile": {
        "role": "mobile developer",
        "techs": [
            # Cross-platform
            "React Native", "Flutter", "Expo", "Ionic", "Capacitor",
            "Xamarin", ".NET MAUI", "NativeScript", "Kotlin Multiplatform",
            # iOS nativo
            "Swift", "SwiftUI", "UIKit", "Objective-C",
            "Xcode", "Core Data", "Combine", "ARKit", "MapKit",
            # Android nativo
            "Kotlin", "Java", "Jetpack Compose", "Android SDK",
            "Android Jetpack", "Room", "Hilt", "Coroutines", "Flow",
            # Navegação & estado
            "React Navigation", "Expo Router", "Redux", "Zustand",
            "Bloc / Cubit", "Riverpod", "GetX",
            # Serviços & integração
            "Firebase", "Supabase", "Push Notifications",
            "In-App Purchase", "Deep Linking", "Biometrics",
            "Maps API", "Camera / Media", "Bluetooth / BLE",
            # Testes & distribuição
            "Detox", "Maestro", "Appium", "XCTest",
            "TestFlight", "Google Play Console", "App Store Connect",
            "Fastlane", "CodePush", "EAS Build",
        ],
    },
    "🧪 QA / Testes": {
        "role": "QA engineer",
        "techs": [
            # Automação Web
            "Cypress", "Playwright", "Selenium", "WebdriverIO",
            "Puppeteer", "TestCafe", "Nightwatch.js",
            # Automação Mobile
            "Appium", "Detox", "XCTest", "Espresso", "Maestro",
            # Testes unitários & integração
            "Jest", "Vitest", "Mocha", "Jasmine", "Testing Library",
            "Pytest", "JUnit", "TestNG", "NUnit", "xUnit",
            "RSpec", "Go Test",
            # Performance & carga
            "K6", "JMeter", "Gatling", "Locust", "Artillery",
            # API Testing
            "Postman", "Insomnia", "REST Assured", "Karate",
            "Pact (Contract Testing)",
            # BDD & organização
            "Cucumber", "BDD", "TDD", "Gherkin",
            "Robot Framework", "Behave", "SpecFlow",
            # Gestão & qualidade
            "JIRA", "TestRail", "Allure", "Zephyr",
            "SonarQube", "Code Coverage",
            # CI & monitoramento
            "GitHub Actions", "Jenkins", "CircleCI",
            "Sentry", "Datadog", "Grafana",
        ],
    },
    "🚀 DevOps / SRE": {
        "role": "DevOps engineer",
        "techs": [
            # Containers & orquestração
            "Docker", "Kubernetes", "Helm", "Kustomize",
            "Docker Compose", "Podman", "Containerd",
            "OpenShift", "Rancher", "K3s",
            # IaC
            "Terraform", "Ansible", "Pulumi", "CloudFormation",
            "Chef", "Puppet", "CDK",
            # Cloud
            "AWS", "GCP", "Azure", "DigitalOcean", "Cloudflare",
            "OCI (Oracle Cloud)", "IBM Cloud",
            # CI/CD
            "GitHub Actions", "GitLab CI", "Jenkins", "CircleCI",
            "ArgoCD", "Flux", "Spinnaker", "Tekton",
            "Drone CI", "Concourse",
            # Observabilidade
            "Prometheus", "Grafana", "Datadog", "New Relic",
            "Elasticsearch / ELK Stack", "Loki", "Jaeger",
            "OpenTelemetry", "Sentry", "PagerDuty",
            # Mensageria & service mesh
            "Kafka", "RabbitMQ", "Istio", "Linkerd", "Envoy",
            # Segurança & rede
            "Vault (HashiCorp)", "Trivy", "Falco",
            "Nginx", "HAProxy", "Traefik", "Cert-Manager",
            # Sistemas
            "Linux", "Bash / Shell Script", "Python", "Go",
            "Git", "SRE", "SLA/SLO/SLI", "Chaos Engineering",
        ],
    },
    "☁  Cloud / Infra": {
        "role": "cloud engineer",
        "techs": [
            # AWS
            "AWS", "EC2", "S3", "Lambda", "ECS", "EKS",
            "RDS", "DynamoDB", "CloudFront", "Route 53",
            "IAM", "VPC", "CloudWatch", "SNS", "SQS",
            "API Gateway", "Step Functions", "Glue", "Athena",
            # GCP
            "GCP", "GKE", "Cloud Run", "Cloud Functions",
            "BigQuery", "Pub/Sub", "Cloud Storage", "Firestore",
            # Azure
            "Azure", "AKS", "Azure Functions", "Azure DevOps",
            "Cosmos DB", "Blob Storage", "Azure AD",
            # IaC & automação
            "Terraform", "Pulumi", "CloudFormation", "CDK",
            "Ansible", "Packer",
            # Redes & segurança
            "VPN", "VPC / VNET", "Load Balancer", "DNS",
            "Zero Trust", "IAM / RBAC", "WAF", "DDoS Protection",
            # Containers
            "Kubernetes", "Docker", "Helm", "Service Mesh",
            # FinOps & observabilidade
            "FinOps", "Cost Optimization", "CloudWatch",
            "Datadog", "Prometheus", "Grafana",
            "Linux", "Bash", "Python", "Go",
        ],
    },
    "📊 Data / BI": {
        "role": "data engineer",
        "techs": [
            # Linguagens & libs
            "Python", "SQL", "R", "Scala", "Java",
            "Pandas", "NumPy", "Polars", "PySpark",
            # Processamento & pipelines
            "Apache Spark", "Apache Flink", "Apache Beam",
            "Airflow", "Prefect", "Dagster", "Luigi",
            "dbt", "Great Expectations", "Delta Lake",
            # Data Warehouses & Lakes
            "Snowflake", "BigQuery", "Redshift", "Databricks",
            "Apache Hive", "Apache Hudi", "Apache Iceberg",
            "Delta Lake", "ClickHouse", "DuckDB",
            # Bancos & armazenamento
            "PostgreSQL", "MySQL", "MongoDB", "Cassandra",
            "Elasticsearch", "Redis", "S3", "GCS", "ADLS",
            # Streaming & mensageria
            "Kafka", "Kinesis", "Pub/Sub", "Flink",
            # BI & visualização
            "Power BI", "Tableau", "Looker", "Metabase",
            "Apache Superset", "Grafana", "QlikSense",
            # MLOps (Data Science)
            "Scikit-learn", "TensorFlow", "PyTorch",
            "MLflow", "Jupyter", "Feature Store",
            # Cloud data
            "AWS Glue", "EMR", "Athena", "GCP Dataflow",
            "Azure Data Factory", "Synapse Analytics",
        ],
    },
    "🤖 Machine Learning / AI": {
        "role": "machine learning engineer",
        "techs": [
            # Linguagens & libs
            "Python", "R", "Julia", "Scala",
            "NumPy", "Pandas", "Polars", "SciPy",
            # Frameworks de ML/DL
            "TensorFlow", "PyTorch", "Keras", "JAX",
            "Scikit-learn", "XGBoost", "LightGBM", "CatBoost",
            # LLMs & GenAI
            "LangChain", "LlamaIndex", "OpenAI API", "Anthropic API",
            "HuggingFace", "Transformers", "PEFT / LoRA",
            "LangGraph", "Semantic Kernel", "DSPy",
            "Ollama", "vLLM", "LiteLLM",
            # Visão computacional
            "OpenCV", "YOLO", "Detectron2", "SAM",
            "Stable Diffusion", "CLIP",
            # NLP
            "NLTK", "spaCy", "Gensim", "BERT", "GPT",
            # MLOps & plataformas
            "MLflow", "Weights & Biases", "DVC",
            "Kubeflow", "SageMaker", "Vertex AI",
            "BentoML", "Triton Inference Server", "Ray",
            "Feature Store", "Evidently AI",
            # Infraestrutura
            "CUDA", "ROCm", "TensorRT", "ONNX",
            "Spark MLlib", "Dask",
            # Dados
            "Jupyter", "Colab", "Databricks",
            "Airflow", "dbt",
        ],
    },
    "🔒 Segurança / SecOps": {
        "role": "security engineer",
        "techs": [
            # Pentest & ofensivo
            "Pentest", "Burp Suite", "Metasploit", "Cobalt Strike",
            "Nmap", "Nessus", "Nuclei", "SQLMap",
            "Wireshark", "Aircrack-ng", "Kali Linux",
            "OWASP Top 10", "CTF", "Red Team",
            # Defensivo & SOC
            "SIEM", "SOC", "Splunk", "IBM QRadar",
            "Microsoft Sentinel", "Elastic SIEM",
            "EDR", "XDR", "IDS / IPS", "SOAR",
            # AppSec & DevSecOps
            "SAST", "DAST", "SCA", "Snyk", "SonarQube",
            "Trivy", "Falco", "OWASP ZAP", "Checkmarx",
            "GitLab Security", "GitHub Advanced Security",
            # Cloud Security
            "AWS Security Hub", "AWS GuardDuty", "AWS IAM",
            "Azure Defender", "GCP Security Command Center",
            "Zero Trust", "CSPM", "CWPP",
            # Criptografia & identidade
            "PKI", "TLS/SSL", "JWT", "OAuth2", "SAML",
            "MFA", "Vault (HashiCorp)", "KMS",
            "Keycloak", "Active Directory",
            # Frameworks & compliance
            "ISO 27001", "NIST", "SOC 2", "PCI-DSS",
            "LGPD", "GDPR", "CIS Benchmarks",
            "Threat Modeling", "STRIDE", "Blue Team",
        ],
    },
    "🗄  DBA / Banco de Dados": {
        "role": "database administrator DBA",
        "techs": [
            # Relacionais
            "PostgreSQL", "MySQL", "MariaDB", "SQL Server",
            "Oracle Database", "SQLite", "CockroachDB",
            "Percona", "TimescaleDB",
            # NoSQL — Documento
            "MongoDB", "CouchDB", "RavenDB", "Firestore",
            # NoSQL — Chave-valor
            "Redis", "DynamoDB", "Valkey", "Memcached",
            # NoSQL — Coluna larga
            "Cassandra", "HBase", "ScyllaDB", "Bigtable",
            # Busca & analítico
            "Elasticsearch", "OpenSearch", "Solr",
            "ClickHouse", "DuckDB", "Apache Druid",
            # Grafos
            "Neo4j", "Amazon Neptune", "ArangoDB", "TigerGraph",
            # Vetores (AI)
            "Pinecone", "Weaviate", "Qdrant", "Milvus",
            "pgvector", "Chroma",
            # ORM & acesso
            "Prisma", "SQLAlchemy", "Hibernate", "TypeORM",
            "Sequelize", "Drizzle",
            # Habilidades DBA
            "Query Optimization", "Indexing", "Partitioning",
            "Replication", "Sharding", "Backup & Recovery",
            "Migration", "Schema Design", "ACID",
            "High Availability", "CDC (Change Data Capture)",
        ],
    },
    "🔌 Embedded / Hardware": {
        "role": "embedded systems engineer",
        "techs": [
            # Linguagens
            "C", "C++", "Rust", "Assembly", "MicroPython",
            "Ada", "MATLAB", "Simulink",
            # Plataformas & MCUs
            "Arduino", "Raspberry Pi", "ESP32", "ESP8266",
            "STM32", "nRF52", "PIC", "AVR",
            "Zephyr RTOS", "FreeRTOS", "ThreadX", "ChibiOS",
            # Protocolos de comunicação
            "I2C", "SPI", "UART", "CAN Bus", "Modbus",
            "MQTT", "CoAP", "Bluetooth / BLE", "LoRa / LoRaWAN",
            "Zigbee", "USB", "Ethernet", "Wi-Fi",
            # FPGA & HDL
            "FPGA", "Verilog", "VHDL", "SystemVerilog",
            "Xilinx", "Intel FPGA", "Lattice",
            # Linux embarcado
            "Linux Embarcado", "Yocto", "Buildroot",
            "Device Tree", "Kernel Modules", "Uboot",
            # IoT & edge
            "IoT", "Edge Computing", "AWS IoT", "Azure IoT",
            "MQTT Broker", "Node-RED", "Home Assistant",
            # Ferramentas
            "GDB", "JTAG", "Oscilloscope", "Logic Analyzer",
            "CMake", "Makefile", "Cross-compilation",
            "CI/CD Embedded", "Unit Testing (Unity/CMock)",
        ],
    },
    "🎮 Game Development": {
        "role": "game developer",
        "techs": [
            # Engines
            "Unity", "Unreal Engine", "Godot", "Bevy",
            "CryEngine", "GameMaker Studio",
            # Linguagens
            "C#", "C++", "GDScript", "Python", "Lua", "Rust",
            "Blueprint (Unreal)",
            # Gráficos & shaders
            "OpenGL", "DirectX", "Vulkan", "Metal",
            "HLSL", "GLSL", "ShaderLab",
            "Ray Tracing", "Physically Based Rendering",
            # Física & IA
            "PhysX", "Havok", "Bullet Physics",
            "Behavior Trees", "Pathfinding", "NavMesh",
            "State Machines", "Finite Automata",
            # Multiplayer & rede
            "Photon", "Mirror", "Unity Netcode",
            "WebSockets", "UDP", "ENet",
            "Dedicated Servers", "P2P",
            # 2D / 3D / áudio
            "Blender", "Maya", "3ds Max", "Spine (2D)",
            "FMOD", "Wwise", "OpenAL",
            # Plataformas & distribuição
            "PC", "PlayStation", "Xbox", "Nintendo Switch",
            "Android", "iOS", "WebGL", "VR / AR",
            "Steam SDK", "Epic Games Store",
        ],
    },
    "🌐 Blockchain / Web3": {
        "role": "blockchain developer",
        "techs": [
            # Redes
            "Ethereum", "Solana", "Polygon", "Avalanche",
            "BNB Chain", "Arbitrum", "Optimism", "Base",
            "Bitcoin", "Cosmos", "Polkadot", "NEAR",
            # Smart Contracts
            "Solidity", "Rust (Solana)", "Vyper", "Move",
            "Hardhat", "Foundry", "Truffle", "Anchor",
            "OpenZeppelin", "ERC-20", "ERC-721", "ERC-1155",
            # Frontend Web3
            "ethers.js", "web3.js", "wagmi", "viem",
            "RainbowKit", "WalletConnect", "MetaMask SDK",
            "Next.js", "React", "TypeScript",
            # Infraestrutura
            "IPFS", "Arweave", "The Graph",
            "Chainlink", "Alchemy", "Infura", "Moralis",
            "QuickNode",
            # DeFi & NFT
            "DeFi", "AMM / DEX", "Lending Protocols",
            "NFT Marketplace", "DAO", "Tokenomics",
            "Flash Loans", "Yield Farming",
            # Segurança
            "Smart Contract Auditing", "Slither", "MythX",
            "Formal Verification", "Reentrancy",
        ],
    },
}

# Estilo visual do questionary alinhado ao tema do projeto
Q_STYLE = QStyle([
    ("qmark",       "fg:#00d7ff bold"),
    ("question",    "bold"),
    ("answer",      "fg:#00ff87 bold"),
    ("pointer",     "fg:#00d7ff bold"),
    ("highlighted", "fg:#00d7ff bold"),
    ("selected",    "fg:#00ff87"),
    ("separator",   "fg:#444444"),
    ("instruction", "fg:#555555 italic"),
    ("text",        ""),
    ("disabled",    "fg:#555555 italic"),
])

# Mapeamento nome limpo → chave do STACKS (para respostas da IA)
STACK_KEYS: dict[str, str] = {
    "Frontend":   "🖥  Frontend",
    "Backend":    "⚙  Backend",
    "Full Stack": "🔀 Full Stack",
    "Mobile":     "📱 Mobile",
    "QA":         "🧪 QA / Testes",
    "DevOps":     "🚀 DevOps / SRE",
    "Cloud":      "☁  Cloud / Infra",
    "Data":       "📊 Data / BI",
    "ML":         "🤖 Machine Learning / AI",
    "Security":   "🔒 Segurança / SecOps",
    "DBA":        "🗄  DBA / Banco de Dados",
    "Embedded":   "🔌 Embedded / Hardware",
    "Game":       "🎮 Game Development",
    "Blockchain": "🌐 Blockchain / Web3",
}

ENGLISH_LEVELS = [
    ("A1",     "A1 — Iniciante (sem conhecimento prático)"),
    ("A2",     "A2 — Básico (palavras e frases simples)"),
    ("B1",     "B1 — Intermediário (conversas cotidianas)"),
    ("B2",     "B2 — Intermediário Avançado (fluência parcial)"),
    ("C1",     "C1 — Avançado (domínio profissional)"),
    ("C2",     "C2 — Proficiente / Fluente"),
    ("Nativo", "Nativo"),
]

SEEN_FILE    = Path.home() / ".job_hunter_seen.json"    # mantido apenas para restore_seen_from_file (migração)

# ── Modelos Groq disponíveis ──────────────────────────────────────────────────
GROQ_MODELS: dict[str, dict] = {
    "llama-3.3-70b-versatile": {
        "label": "Llama 3.3 70B  ← recomendado",
        "desc":  "Melhor qualidade geral. Limite: 100k tokens/dia.",
    },
    "llama-3.1-8b-instant": {
        "label": "Llama 3.1 8B  (muito rápido, limite alto)",
        "desc":  "Ótimo quando o 70B atinge rate limit. Limite: 800k tokens/dia.",
    },
    "mixtral-8x7b-32768": {
        "label": "Mixtral 8x7B  (contexto longo)",
        "desc":  "Janela de 32k tokens. Limite: 500k tokens/dia.",
    },
    "gemma2-9b-it": {
        "label": "Gemma 2 9B  (Google, eficiente)",
        "desc":  "Rápido e eficiente. Limite: 800k tokens/dia.",
    },
    "deepseek-r1-distill-llama-70b": {
        "label": "DeepSeek R1 70B  (raciocínio avançado)",
        "desc":  "Forte em análise complexa. Limite: 100k tokens/dia.",
    },
    "meta-llama/llama-4-scout-17b-16e-instruct": {
        "label": "Llama 4 Scout 17B  (multimodal, novo)",
        "desc":  "Modelo mais recente da Meta. Limite variável.",
    },
}
_DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Modelo ativo em runtime — alterado por set_active_model()
_ACTIVE_MODEL: str = _DEFAULT_MODEL

JOB_STATUS = {
    "accepted": ("✅", "Aceitas"),
    "rejected": ("❌", "Recusadas"),
    "applied":  ("📤", "Candidatei"),
    "seen":     ("👁 ", "Vistas"),
}


# ──────────────────────────────────────────────────────────────────────────────
# Configurações persistidas (modelo de IA, etc.)
# ──────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Carrega configurações do .env (via os.environ). Fonte única de verdade para config."""
    return {
        "model":       os.environ.get("MODEL", _DEFAULT_MODEL),
        "last_resume": os.environ.get("RESUME_PATH", ""),
    }


def save_config(cfg: dict) -> None:
    """Persiste configurações no .env. Fonte única de verdade para config."""
    if "model" in cfg and cfg["model"]:
        _save_to_dotenv("MODEL", cfg["model"])
    if "last_resume" in cfg and cfg["last_resume"]:
        _save_to_dotenv("RESUME_PATH", cfg["last_resume"])


def set_active_model(model_id: str) -> None:
    """Troca o modelo Groq em runtime e persiste no .env (fonte única)."""
    global _ACTIVE_MODEL
    if model_id not in GROQ_MODELS:
        log_warn(f"Modelo desconhecido: {model_id} — mantendo {_ACTIVE_MODEL}")
        return
    _ACTIVE_MODEL = model_id
    _save_to_dotenv("MODEL", model_id)
    log_ok(f"Modelo alterado para: {_BD}{GROQ_MODELS[model_id]['label']}{_RST}")


# ──────────────────────────────────────────────────────────────────────────────
# Preset — salvar e carregar configurações
# ──────────────────────────────────────────────────────────────────────────────

def load_presets() -> list[dict]:
    """Carrega presets do MongoDB (fonte única de verdade)."""
    if _global_rdb:
        return _global_rdb.load_presets_from_db()
    return []


def save_preset(preset: dict) -> None:
    """Persiste preset no MongoDB."""
    if _global_rdb:
        _global_rdb.save_preset_to_db(preset)
        log_ok(f"Preset salvo: {_BD}{preset['name']}{_RST}")


def _preset_summary(p: dict) -> str:
    """Linha curta descrevendo o preset para o menu (sem ANSI — questionary não processa)."""
    prefs  = p.get("prefs", {})
    mod    = {"remoto": "🏠", "presencial": "🏢", "hibrido": "🔄", "todos": "✅"}.get(prefs.get("modality", "todos"), "")
    cont   = {"pj": "PJ", "clt": "CLT", "autonomo": "Aut.", "todos": "todos"}.get(prefs.get("contract", "todos"), "")
    eng    = prefs.get("english_level", "")
    rec    = prefs.get("recency", "any")
    criado = p.get("created_at", "")[:10]
    src_n  = len(p.get("sources", []))
    name   = p["name"][:34]
    return f"{name:<35} {criado}  {mod} {cont}  EN:{eng}  {rec}  {src_n}f"


# ──────────────────────────────────────────────────────────────────────────────
# Redis Manager — filas e persistência
# ──────────────────────────────────────────────────────────────────────────────

class MongoManager:
    """
    Persistência via MongoDB.

    Collections:
      sessions   — metadados de cada execução (query, sources, prefs, stats embutidas)
      queue      — fila FIFO de vagas pendentes de avaliação por sessão
      jobs       — vagas avaliadas com score, indexadas por session_id
      seen       — hashes globais de vagas já mapeadas (deduplicação cross-sessão)
      decisions  — decisões do usuário (aceita / recusada / candidatou)
      errors     — erros de avaliação por sessão
      meta       — documentos singleton (resume_hash, api_status)
    """

    # Conexão compartilhada entre todas as instâncias — criada apenas uma vez
    _shared_client: "Optional[MongoClient]" = None

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._connect()

    def _connect(self) -> None:
        if MongoManager._shared_client is None:
            try:
                if MONGO_USER and MONGO_PASS:
                    uri = (
                        f"mongodb://{MONGO_USER}:{MONGO_PASS}"
                        f"@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}"
                    )
                else:
                    uri = f"mongodb://{MONGO_HOST}:{MONGO_PORT}/"
                client = MongoClient(uri, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")   # força conexão agora
                MongoManager._shared_client = client

                # Índices criados uma única vez na primeira conexão
                db = client[MONGO_DB]
                db.jobs.create_index([("session_id", 1), ("score", -1)])
                db.queue.create_index([("session_id", 1), ("created_at", 1)])
                db.decisions.create_index([("status", 1)])
            except Exception as exc:
                log_err(f"MongoDB inacessível: {exc}")
                log_err("Suba o container primeiro:  docker-compose up -d")
                sys.exit(1)

        self.db = MongoManager._shared_client[MONGO_DB]

    # ── Sessão ────────────────────────────────────────────────────────────────

    def save_run_info(self, info: dict) -> None:
        doc = {**info, "_id": self.session_id}
        self.db.sessions.replace_one({"_id": self.session_id}, doc, upsert=True)

    # ── Fila de vagas ─────────────────────────────────────────────────────────

    def push_job(self, job: dict) -> str:
        """Enfileira vaga; marca como vista globalmente; retorna job_id."""
        job_id      = hashlib.md5(job["link"].encode()).hexdigest()[:10]
        job["job_id"] = job_id
        self.db.queue.insert_one({
            "session_id": self.session_id,
            "job_id":     job_id,
            "data":       job,
            "created_at": datetime.now(),
        })
        # Marca como vista globalmente para deduplicação cross-sessão
        link_hash = self._job_hash(job["link"])
        self.db.seen.update_one(
            {"_id": link_hash},
            {"$setOnInsert": {"_id": link_hash, "ts": datetime.now().isoformat()}},
            upsert=True,
        )
        return job_id

    def pop_job(self) -> Optional[dict]:
        """Remove e retorna a próxima vaga da fila (FIFO)."""
        doc = self.db.queue.find_one_and_delete(
            {"session_id": self.session_id},
            sort=[("created_at", pymongo.ASCENDING)],
        )
        return doc["data"] if doc else None

    def queue_size(self) -> int:
        return self.db.queue.count_documents({"session_id": self.session_id})

    # ── Resultados ────────────────────────────────────────────────────────────

    def save_result(self, job: dict) -> None:
        """Persiste vaga avaliada e incrementa stats da sessão."""
        score  = job.get("score", 0)
        job_id = job["job_id"]
        doc    = {**job, "_id": job_id, "session_id": self.session_id}
        self.db.jobs.replace_one({"_id": job_id}, doc, upsert=True)
        inc: dict = {"stats.evaluated": 1}
        if score >= MIN_MATCH_SCORE:
            inc["stats.matched"] = 1
        self.db.sessions.update_one(
            {"_id": self.session_id},
            {"$inc": inc},
            upsert=True,
        )

    def save_error(self, job: dict, error: str) -> None:
        self.db.errors.insert_one({
            "session_id": self.session_id,
            "job_id":     job.get("job_id", "?"),
            "title":      job.get("title", "?"),
            "error":      error,
            "ts":         datetime.now().isoformat(),
        })
        self.db.sessions.update_one(
            {"_id": self.session_id},
            {"$inc": {"stats.errors": 1}},
            upsert=True,
        )

    def get_matched_jobs(self) -> list[dict]:
        """Retorna vagas com score ≥ MIN, em ordem decrescente."""
        return list(self.db.jobs.find(
            {"session_id": self.session_id, "score": {"$gte": MIN_MATCH_SCORE}},
            sort=[("score", pymongo.DESCENDING)],
            projection={"_id": 0},
        ))

    def get_all_evaluated_jobs(self) -> list[dict]:
        """Retorna TODAS as vagas avaliadas (qualquer score), em ordem decrescente."""
        return list(self.db.jobs.find(
            {"session_id": self.session_id, "score": {"$exists": True}},
            sort=[("score", pymongo.DESCENDING)],
            projection={"_id": 0},
        ))

    def get_stats(self) -> dict:
        doc = self.db.sessions.find_one({"_id": self.session_id}, {"stats": 1})
        raw = (doc or {}).get("stats", {})
        return {k: int(v) for k, v in raw.items()}

    # ── API status ────────────────────────────────────────────────────────────

    def set_api_status(self, status: str, detail: str = "") -> None:
        self.db.meta.replace_one(
            {"_id": "api_status"},
            {"_id": "api_status", "status": status,
             "detail": detail, "ts": datetime.now().isoformat()},
            upsert=True,
        )

    def get_api_status(self) -> dict:
        doc = self.db.meta.find_one({"_id": "api_status"}, {"_id": 0})
        return doc or {}

    # ── Configurações persistidas — único ponto de verdade ────────────────────

    def save_setting(self, key: str, value) -> None:
        """Persiste configuração no MongoDB (único ponto de verdade para config)."""
        try:
            self.db.meta.replace_one(
                {"_id": f"cfg_{key}"},
                {"_id": f"cfg_{key}", "value": value, "ts": datetime.now().isoformat()},
                upsert=True,
            )
        except Exception:
            pass

    def load_setting(self, key: str, default=None):
        """Carrega configuração do MongoDB; retorna default se não existir."""
        try:
            doc = self.db.meta.find_one({"_id": f"cfg_{key}"})
            return doc["value"] if doc else default
        except Exception:
            return default

    # ── Presets — fonte primária ──────────────────────────────────────────────────

    def save_preset_to_db(self, preset: dict) -> None:
        """Persiste preset no MongoDB (único ponto de verdade)."""
        try:
            doc = {**preset, "_id": preset["id"]}
            self.db.presets.replace_one({"_id": preset["id"]}, doc, upsert=True)
        except Exception:
            pass

    def load_presets_from_db(self) -> list[dict]:
        """Carrega todos os presets do MongoDB."""
        try:
            docs = list(self.db.presets.find({}, {"_id": 0}))
            return docs
        except Exception:
            return []

    def delete_preset_from_db(self, pid: str) -> None:
        """Remove preset do MongoDB pelo id."""
        try:
            self.db.presets.delete_one({"_id": pid})
        except Exception:
            pass

    def delete_presets_bulk(self, pids: list) -> None:
        """Remove múltiplos presets do MongoDB."""
        try:
            self.db.presets.delete_many({"_id": {"$in": pids}})
        except Exception:
            pass

    # ── Histórico de vagas (global, cross-sessão) ──────────────────────────────

    @staticmethod
    def _job_hash(link: str) -> str:
        return hashlib.md5(link.encode()).hexdigest()[:14]

    def record_job_decision(self, job: dict, status: str) -> None:
        """Registra decisão do usuário: 'accepted' | 'rejected' | 'applied' | 'seen'."""
        jh = self._job_hash(job["link"])
        self.db.decisions.replace_one(
            {"_id": jh},
            {
                "_id":     jh,
                "title":   job.get("title", ""),
                "company": job.get("company", ""),
                "link":    job.get("link", ""),
                "region":  job.get("region", ""),
                "score":   str(job.get("score", 0)),
                "status":  status,
                "ts":      datetime.now().isoformat(),
            },
            upsert=True,
        )

    def is_rejected(self, link: str) -> bool:
        return self.db.decisions.find_one(
            {"_id": self._job_hash(link), "status": "rejected"},
            {"_id": 1},
        ) is not None

    # ── Deduplicação global cross-sessão ──────────────────────────────────────

    def is_seen(self, link: str) -> bool:
        """True se a vaga já foi mapeada em qualquer sessão anterior."""
        return self.db.seen.find_one(
            {"_id": self._job_hash(link)}, {"_id": 1}
        ) is not None

    def count_seen(self) -> int:
        """Total de vagas já mapeadas globalmente."""
        return self.db.seen.count_documents({})

    def clear_seen_jobs(self) -> int:
        """Apaga o cache global de vagas mapeadas. Retorna quantas foram removidas."""
        count = self.db.seen.count_documents({})
        self.db.seen.drop()
        return count

    # ── Hash do currículo para detecção de mudanças ───────────────────────────

    def get_resume_hash(self) -> str:
        doc = self.db.meta.find_one({"_id": "resume_hash"})
        return (doc or {}).get("value", "")

    def set_resume_hash(self, h: str) -> None:
        self.db.meta.replace_one(
            {"_id": "resume_hash"},
            {"_id": "resume_hash", "value": h},
            upsert=True,
        )

    # ── Backup local (migração) ───────────────────────────────────────────────

    def save_seen_to_file(self) -> None:
        """No-op: todos os dados de seen estão no MongoDB. Mantido por compatibilidade."""
        pass

    def restore_seen_from_file(self) -> int:
        """
        Importa hashes do arquivo local para o MongoDB.
        No-op se o MongoDB já possui dados (evita duplicatas após reinício).
        Útil apenas na primeira execução após migração do Redis.
        """
        if not SEEN_FILE.exists():
            return 0
        if self.db.seen.count_documents({}) > 0:
            return 0   # MongoDB já tem dados — não sobrescreve
        try:
            data   = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            hashes = data.get("seen", [])
            if not hashes:
                return 0
            ops = [
                UpdateOne(
                    {"_id": h},
                    {"$setOnInsert": {"_id": h, "ts": datetime.now().isoformat()}},
                    upsert=True,
                )
                for h in hashes
            ]
            self.db.seen.bulk_write(ops, ordered=False)
            return len(hashes)
        except Exception as exc:
            log_warn(f"Não foi possível restaurar backup local de vagas: {exc}")
            return 0

    # ── Histórico por status ───────────────────────────────────────────────────

    def get_jobs_by_status(self, status: str) -> list[dict]:
        return list(self.db.decisions.find(
            {"status": status},
            {"_id": 0},
            sort=[("ts", pymongo.DESCENDING)],
        ))

    def count_by_status(self) -> dict:
        return {s: self.db.decisions.count_documents({"status": s}) for s in JOB_STATUS}

    # ── Sessões com vagas avaliadas (para retomada de revisão) ────────────────

    def get_all_sessions_with_jobs(self) -> list[dict]:
        """
        Retorna metadados de todas as sessões com vagas avaliadas,
        ordenadas da mais recente para a mais antiga.
        """
        pipeline = [
            {"$match": {"session_id": {"$ne": "admin"}}},
            {"$group": {
                "_id":     "$session_id",
                "total":   {"$sum": 1},
                "matched": {
                    "$sum": {
                        "$cond": [{"$gte": ["$score", MIN_MATCH_SCORE]}, 1, 0]
                    }
                },
            }},
            {"$match": {"total": {"$gt": 0}}},
        ]
        grouped = {doc["_id"]: doc for doc in self.db.jobs.aggregate(pipeline)}

        result = []
        for sid, counts in grouped.items():
            info = self.db.sessions.find_one({"_id": sid}) or {}
            result.append({
                "session_id": sid,
                "query":      info.get("query", "?"),
                "sources":    info.get("sources", ""),
                "started_at": info.get("started_at", sid),
                "matched":    counts["matched"],
                "total":      counts["total"],
            })
        return sorted(result, key=lambda x: x["started_at"], reverse=True)

    def get_matched_jobs_for_session(self, session_id: str) -> list[dict]:
        """Retorna vagas com score ≥ MIN de uma sessão específica."""
        return list(self.db.jobs.find(
            {"session_id": session_id, "score": {"$gte": MIN_MATCH_SCORE}},
            sort=[("score", pymongo.DESCENDING)],
            projection={"_id": 0},
        ))

    def get_all_jobs_for_session(self, session_id: str) -> list[dict]:
        """Retorna TODAS as vagas avaliadas de uma sessão (qualquer score), por score desc."""
        return list(self.db.jobs.find(
            {"session_id": session_id, "score": {"$exists": True}},
            sort=[("score", pymongo.DESCENDING)],
            projection={"_id": 0},
        ))

    def get_prior_decision(self, link: str) -> str:
        """Retorna a decisão prévia sobre uma vaga ('accepted','rejected','applied') ou ''."""
        doc = self.db.decisions.find_one(
            {"_id": self._job_hash(link)}, {"status": 1}
        )
        return (doc or {}).get("status", "")

    # ── Perfil do candidato ───────────────────────────────────────────────────

    def save_profile(self, profile: dict) -> None:
        """Persiste perfil extraído do currículo. Chave = resume_hash."""
        doc = {**profile, "_id": profile["resume_hash"]}
        self.db.profiles.replace_one({"_id": profile["resume_hash"]}, doc, upsert=True)

    def load_profile(self, resume_hash: str) -> Optional[dict]:
        """Carrega perfil do banco se existir para este currículo."""
        doc = self.db.profiles.find_one({"_id": resume_hash}, {"_id": 0})
        return doc if doc else None

    def save_auth_cookies(self, source_key: str, cookies: list[dict]) -> None:
        """Persiste cookies de autenticação capturados pelo browser para uma fonte."""
        self.db.auth_cookies.replace_one(
            {"_id": source_key},
            {"_id": source_key, "cookies": cookies, "saved_at": datetime.now().isoformat()},
            upsert=True,
        )

    def load_auth_cookies(self, source_key: str) -> Optional[dict]:
        """Carrega cookies de autenticação salvos para uma fonte. Retorna o doc completo ou None."""
        return self.db.auth_cookies.find_one({"_id": source_key})

    def save_application_result(self, job: dict, status: str, notes: str = "") -> None:
        """Persiste resultado de uma candidatura automática."""
        doc_id = job.get("job_id") or job.get("jk") or hashlib.md5(job.get("link","").encode()).hexdigest()[:10]
        self.db.applications.replace_one(
            {"_id": doc_id},
            {
                "_id":        doc_id,
                "job":        job,
                "status":     status,   # "success" | "failed" | "manual_needed"
                "notes":      notes,
                "applied_at": datetime.now().isoformat(),
            },
            upsert=True,
        )

    def update_profile_extra(self, key: str, value: str) -> None:
        """Adiciona/atualiza campo extra no perfil salvo (informação coletada durante auto-apply)."""
        meta = self.db.meta.find_one({"_id": "resume_hash"})
        resume_hash = (meta or {}).get("value", "")
        if not resume_hash:
            # Tenta pegar o hash do primeiro perfil salvo
            first = self.db.profiles.find_one({})
            resume_hash = (first or {}).get("resume_hash", "")
        if resume_hash:
            self.db.profiles.update_one(
                {"_id": resume_hash},
                {"$set": {f"extra_info.{key}": value}},
            )

    def load_all_auth_cookies(self) -> "dict[str, list[dict]]":
        """Carrega todos os cookies de autenticação salvos no banco."""
        result: dict = {}
        for doc in self.db.auth_cookies.find():
            result[doc["_id"]] = doc.get("cookies", [])
        return result

    def save_platform_meta(self, platform_key: str, data: dict) -> None:
        """Salva metadados de plataforma (ex: URL do perfil descoberta dinamicamente)."""
        self.db.platform_meta.update_one(
            {"_id": platform_key},
            {"$set": data},
            upsert=True,
        )

    def load_platform_meta(self, platform_key: str) -> dict:
        """Carrega metadados de plataforma salvos anteriormente."""
        doc = self.db.platform_meta.find_one({"_id": platform_key})
        return {k: v for k, v in (doc or {}).items() if k != "_id"}

    def save_storage_state(self, source_key: str, storage_state: dict) -> None:
        """Salva storage_state completo (cookies + localStorage + sessionStorage) de uma plataforma."""
        self.db.auth_cookies.update_one(
            {"_id": source_key},
            {"$set": {
                "storage_state": storage_state,
                "storage_saved_at": datetime.now().isoformat(),
            }},
            upsert=True,
        )

    def load_storage_state(self, source_key: str) -> Optional[dict]:
        """Carrega storage_state completo salvo para uma plataforma."""
        doc = self.db.auth_cookies.find_one({"_id": source_key})
        return (doc or {}).get("storage_state")

    def save_profile_fields(self, platform_key: str, section_url: str, fields: dict) -> None:
        """Salva os campos de formulário descobertos para uma seção de perfil."""
        self.db.platform_meta.update_one(
            {"_id": platform_key},
            {
                "$set": {
                    f"profile_fields.{section_url.replace('/', '_').replace(':', '_')}": {
                        "fields": fields,
                        "discovered_at": datetime.now().isoformat(),
                        "section_url": section_url,
                    }
                }
            },
            upsert=True,
        )

    def load_profile_fields(self, platform_key: str, section_url: str) -> Optional[dict]:
        """Carrega os campos salvos para uma seção específica."""
        doc = self.db.platform_meta.find_one({"_id": platform_key})
        if not doc:
            return None
        key = f"profile_fields.{section_url.replace('/', '_').replace(':', '_')}"
        field_doc = doc.get("profile_fields", {}).get(section_url.replace('/', '_').replace(':', '_'))
        return (field_doc or {}).get("fields") if field_doc else None


# ──────────────────────────────────────────────────────────────────────────────
# 1. Health-check da API do Groq
# ──────────────────────────────────────────────────────────────────────────────

def check_groq_api(client: Groq, rdb: MongoManager, quiet: bool = False) -> bool:
    """
    Faz uma chamada mínima ao Groq para validar autenticação, rede e modelo.
    Persiste o status no MongoDB.
    quiet=True suprime toda a saída (usado na inicialização silenciosa).

    Classificação de erros:
      - 429 / rate limit → chave válida, apenas throttled → retorna True
      - 401 / auth error → chave inválida → retorna False
      - rede / timeout   → tenta 1x extra antes de retornar False
    """
    if not quiet:
        section("Health-check da API de IA (Groq)")
        log_info("Verificando chave, rede e disponibilidade do modelo...")

    sp = Spinner("Verificando API Groq...").start() if (not _VERBOSE and not quiet) else None

    def _ping() -> object:
        return client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )

    for attempt in range(2):   # tenta 2x para absorver falhas transitórias de rede
        try:
            t0   = time.time()
            resp = _ping()
            elapsed = time.time() - t0

            if resp.choices and resp.choices[0].message.content is not None:
                if sp: sp.stop()
                set_api_ok(True)
                if not quiet:
                    log_ok(f"API funcionando  |  latência: {elapsed:.2f}s  |  {_ACTIVE_MODEL}")
                if rdb: rdb.set_api_status("ok", f"latência={elapsed:.2f}s modelo={_ACTIVE_MODEL}")
                return True
            else:
                if sp: sp.fail()
                set_api_ok(False)
                if not quiet:
                    log_err("API respondeu com conteúdo vazio")
                if rdb: rdb.set_api_status("error", "resposta vazia")
                return False

        except Exception as exc:
            err_msg = str(exc)

            is_rate_limit = "rate" in err_msg.lower() or "429" in err_msg
            is_auth_error = (
                "401" in err_msg
                or "auth" in err_msg.lower()
                or "api_key" in err_msg.lower()
                or "invalid_api_key" in err_msg.lower()
            )
            is_net_error  = "connection" in err_msg.lower() or "timeout" in err_msg.lower()

            # Rate limit = chave válida, só throttled — marca online
            if is_rate_limit:
                if sp: sp.stop()
                set_api_ok(True)
                if not quiet:
                    log_warn("Rate limit atingido — API válida, aguarde antes de iniciar nova busca.")
                if rdb: rdb.set_api_status("ok", "rate_limit - chave válida")
                return True

            # Auth error = chave definitivamente inválida — não retenta
            if is_auth_error:
                if sp: sp.fail()
                set_api_ok(False)
                if not quiet:
                    log_err("Chave de API inválida ou expirada")
                if rdb: rdb.set_api_status("error", err_msg[:200])
                return False

            # Erro de rede transitório: espera 1s e tenta de novo (apenas na 1ª tentativa)
            if is_net_error and attempt == 0:
                if not quiet:
                    pass  # silencioso no startup; spinner continua girando
                time.sleep(1)
                continue   # segunda tentativa

            # Segunda tentativa também falhou ou outro erro desconhecido
            if sp: sp.fail()
            set_api_ok(False)
            if not quiet:
                if is_net_error:
                    log_err("Falha de rede ao alcançar a API do Groq")
                else:
                    log_err(f"Erro ao verificar API: {exc}")
            if rdb: rdb.set_api_status("error", err_msg[:200])
            return False

    # Nunca deve chegar aqui
    set_api_ok(False)
    return False


# ──────────────────────────────────────────────────────────────────────────────
# 2. Extração do currículo
# ──────────────────────────────────────────────────────────────────────────────

def extract_resume_text(pdf_path: str, quiet: bool = False) -> str:
    if not quiet:
        section("Lendo currículo")
    if not os.path.exists(pdf_path):
        log_err(f"Arquivo não encontrado: {pdf_path}")
        sys.exit(1)

    if not quiet:
        log_info(f"Arquivo: {pdf_path}")
    sp = Spinner(f"Lendo PDF: {Path(pdf_path).name}...").start() if (not _VERBOSE and not quiet) else None
    text = ""
    n_pages = 0
    try:
        with pdfplumber.open(pdf_path) as pdf:
            n_pages = len(pdf.pages)
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as exc:
        if sp: sp.fail()
        log_err(f"Falha ao ler o PDF: {exc}")
        sys.exit(1)

    if not text.strip():
        if sp: sp.fail()
        log_err("Nenhum texto extraído. Verifique se o PDF não é escaneado.")
        sys.exit(1)

    if sp: sp.stop()
    if not quiet:
        log_ok(f"{len(text)} caracteres extraídos  |  {n_pages} página(s)")
    return text.strip()


def analyze_resume_for_selection(client: Groq, resume_text: str) -> dict:
    """
    Pede à IA para inferir do currículo:
      - stack principal (uma das chaves de STACK_KEYS)
      - tecnologias relevantes (subset das listas de STACKS)
      - nível de inglês inferido (A1–C2 ou Nativo)
    Retorna dict com essas sugestões para pré-preencher os menus.
    """
    section("Analisando currículo para pré-seleção dos menus")
    log_info("A IA vai sugerir stack, tecnologias e nível de inglês...")

    stack_options   = list(STACK_KEYS.keys())
    all_techs_flat  = sorted({t for s in STACKS.values() for t in s["techs"]})
    english_options = [lv for lv, _ in ENGLISH_LEVELS]

    prompt = (
        "Analise o currículo abaixo e retorne APENAS um JSON com:\n"
        f'  "stack": uma das opções exatas: {stack_options}\n'
        f'  "technologies": lista de tecnologias do currículo que existam em: {all_techs_flat}\n'
        f'  "english_level": um de: {english_options} (infira pelo currículo)\n\n'
        f"CURRÍCULO:\n{resume_text[:3500]}\n\n"
        "Responda APENAS com JSON válido, sem markdown."
    )

    try:
        resp = client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=400,
            messages=[
                {"role": "system", "content": "Responda APENAS com JSON válido."},
                {"role": "user",   "content": prompt},
            ],
        )
        raw  = resp.choices[0].message.content.strip()
        raw  = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)

        # Valida stack
        stack_clean = data.get("stack", "")
        stack_key   = STACK_KEYS.get(stack_clean)
        if not stack_key:
            # Fuzzy: pega o primeiro que contenha a palavra
            stack_key = next(
                (v for k, v in STACK_KEYS.items() if k.lower() in stack_clean.lower()),
                list(STACK_KEYS.values())[0],
            )

        # Valida tecnologias (apenas as que existem na stack sugerida)
        valid_techs    = set(STACKS.get(stack_key, {}).get("techs", []))
        suggested_techs = [t for t in data.get("technologies", []) if t in valid_techs]

        # Valida inglês
        english = data.get("english_level", "B1")
        if english not in english_options:
            english = "B1"

        log_ok(f"Stack sugerida:    {_BD}{stack_key}{_RST}")
        log_ok(f"Tecnologias ({len(suggested_techs)}): {', '.join(suggested_techs) or 'nenhuma'}")
        log_ok(f"Inglês inferido:   {_BD}{english}{_RST}")

        return {"stack": stack_key, "technologies": suggested_techs, "english_level": english}

    except Exception as exc:
        log_warn(f"Análise automática falhou ({exc}) — menus sem pré-seleção")
        return {"stack": None, "technologies": [], "english_level": "B1"}


# ──────────────────────────────────────────────────────────────────────────────
# 3. Scraping com curl_cffi  (TLS fingerprint real do Chrome → bypassa Cloudflare)
# ──────────────────────────────────────────────────────────────────────────────

def _build_listing_url(domain: str, query: str, location: str, page: int, recency: str = "any") -> str:
    params: dict = {"q": query, "l": location, "start": page * 10}
    fromage = _RECENCY_INDEED.get(recency)
    if fromage:
        params["fromage"] = fromage
    return f"{domain}/jobs?{urllib.parse.urlencode(params)}"


def _build_detail_url(domain: str, jk: str) -> str:
    return f"{domain}/viewjob?jk={jk}"


def _build_linkedin_url(query: str, location: str, page: int, recency: str = "any") -> str:
    params: dict = {"keywords": query, "location": location, "start": page * 25}
    tpr = _RECENCY_LINKEDIN.get(recency, "")
    if tpr:
        params["f_TPR"] = tpr
    return f"{LINKEDIN_DOMAIN}/jobs/search/?{urllib.parse.urlencode(params)}"


def _slugify(text: str) -> str:
    """Converte texto em slug para URLs (ex: 'React Developer' → 'react-developer')."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text


def _random_delay() -> None:
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def _new_session() -> cf_requests.Session:
    """
    Sessão curl_cffi com fingerprint idêntico ao Chrome 124.
    O Cloudflare verifica JA3/JA4 (TLS) — curl_cffi replica exatamente,
    o que browsers headless não conseguem.
    """
    s = cf_requests.Session(impersonate="chrome124")
    s.headers.update({
        "Accept-Language":          "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding":          "gzip, deflate, br",
        "DNT":                      "1",
        "Upgrade-Insecure-Requests":"1",
        "Sec-Fetch-Dest":           "document",
        "Sec-Fetch-Mode":           "navigate",
        "Sec-Fetch-Site":           "none",
    })
    return s


def _get_soup(session: cf_requests.Session, url: str, retries: int = 3) -> "Optional[BeautifulSoup]":
    """GET com retry — retorna BeautifulSoup ou None."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                log_ok("Página carregada")
                return BeautifulSoup(resp.text, "html.parser")
            if resp.status_code == 429:
                wait = 30 * attempt
                log_warn(f"Rate limit (429) — aguardando {wait}s...")
                time.sleep(wait)
            elif resp.status_code in (403, 503):
                log_warn(f"Bloqueado ({resp.status_code}) — tentativa {attempt}/{retries}  aguardando {10 * attempt}s")
                time.sleep(10 * attempt)
            else:
                log_warn(f"HTTP {resp.status_code} para {url}")
                return None
        except Exception as exc:
            log_warn(f"Erro na requisição (tentativa {attempt}/{retries}): {exc}")
            time.sleep(5 * attempt)
    log_err(f"Falha após {retries} tentativas: {url}")
    return None


def _soup_text(soup: "BeautifulSoup", *selectors: str) -> str:
    """Tenta cada seletor em ordem, retorna o primeiro texto encontrado."""
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return el.get_text(" ", strip=True)
    return ""


def _extract_jsonld(soup: "BeautifulSoup") -> dict:
    """Extrai o primeiro bloco JSON-LD da página (dados estruturados)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            return json.loads(tag.string or "")
        except Exception:
            continue
    return {}


# ── Indeed ────────────────────────────────────────────────────────────────────

def _collect_jk_ids(session: cf_requests.Session, listing_url: str, label: str) -> list[str]:
    log_info(f"Listagem: {listing_url}")
    soup = _get_soup(session, listing_url)
    if not soup:
        log_warn(f"Sem resposta para listagem {label}")
        return []
    links  = soup.select("a[data-jk]")
    jk_ids = list(dict.fromkeys(a["data-jk"] for a in links if a.get("data-jk")))
    return jk_ids


def _fetch_job_detail(session: cf_requests.Session, domain: str, jk: str, region_label: str) -> Optional[dict]:
    detail_url = _build_detail_url(domain, jk)
    log_info(f"Acessando: {detail_url}")
    soup = _get_soup(session, detail_url)
    if not soup:
        return None

    # 1. JSON-LD (mais confiável — dados estruturados server-side)
    ld = _extract_jsonld(soup)
    if ld.get("title"):
        desc_html   = ld.get("description", "")
        description = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)[:3000]
        loc         = ld.get("jobLocation", {})
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        location = loc.get("address", {}).get("addressLocality", "")
        return {
            "jk":          jk,
            "title":       ld["title"],
            "company":     ld.get("hiringOrganization", {}).get("name", "Empresa não informada"),
            "location":    location,
            "description": description,
            "benefits":    "",
            "link":        detail_url,
            "region":      region_label,
        }

    # 2. Fallback: seletores CSS no HTML estático
    title       = _soup_text(soup, '[data-testid="jobsearch-JobInfoHeader-title"]', "h1")
    company     = _soup_text(soup, '[data-testid="inlineHeader-companyName"]', '[data-testid="companyName"]')
    location    = _soup_text(soup, '[data-testid="jobsearch-JobInfoHeader-companyLocation"]')
    description = _soup_text(soup, "#jobDescriptionText", '[data-testid="jobsearch-JobComponent-description"]')
    benefits    = _soup_text(soup, '[data-testid="benefits-test"]')

    if not title and not description:
        log_warn(f"Sem conteúdo extraível: jk={jk}")
        return None

    return {
        "jk":          jk,
        "title":       title or "Título não encontrado",
        "company":     company or "Empresa não informada",
        "location":    location,
        "description": description[:3000],
        "benefits":    benefits,
        "link":        detail_url,
        "region":      region_label,
    }


# ── LinkedIn ──────────────────────────────────────────────────────────────────

def _collect_linkedin_job_ids(session: cf_requests.Session, url: str) -> list[str]:
    log_info(f"Listagem: {url}")
    soup = _get_soup(session, url)
    if not soup:
        return []
    job_ids = []
    for card in soup.select("[data-entity-urn]"):
        urn = card.get("data-entity-urn", "")
        if "jobPosting:" in urn:
            job_ids.append(urn.split("jobPosting:")[-1])
    return list(dict.fromkeys(job_ids))


def _fetch_linkedin_detail(session: cf_requests.Session, job_id: str) -> Optional[dict]:
    detail_url = f"{LINKEDIN_DOMAIN}/jobs/view/{job_id}/"
    log_info(f"Acessando: {detail_url}")
    soup = _get_soup(session, detail_url)
    if not soup:
        return None

    ld = _extract_jsonld(soup)
    if ld.get("title"):
        desc_html   = ld.get("description", "")
        description = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)[:3000]
        return {
            "jk":          job_id,
            "title":       ld["title"],
            "company":     ld.get("hiringOrganization", {}).get("name", "Empresa não informada"),
            "location":    ld.get("jobLocation", {}).get("address", {}).get("addressLocality", ""),
            "description": description,
            "benefits":    "",
            "link":        detail_url,
            "region":      "LinkedIn",
        }

    title       = _soup_text(soup, "h1.top-card-layout__title", "h1")
    company     = _soup_text(soup, ".topcard__org-name-link", ".top-card-layout__card .topcard__org-name-link")
    location    = _soup_text(soup, ".topcard__flavor--bullet")
    description = _soup_text(soup, ".show-more-less-html__markup", ".description__text")

    if not title and not description:
        log_warn(f"Sem conteúdo extraível: job_id={job_id}")
        return None

    return {
        "jk":          job_id,
        "title":       title or "Título não encontrado",
        "company":     company or "Empresa não informada",
        "location":    location,
        "description": description[:3000],
        "benefits":    "",
        "link":        detail_url,
        "region":      "LinkedIn",
    }


# ── Scrapers por fonte ────────────────────────────────────────────────────────

def _scrape_indeed_source(
    session:   cf_requests.Session,
    domain:    str,
    label:     str,
    query:     str,
    location:  str,
    max_pages: int,
    rdb:       "MongoManager",
    recency:   str = "any",
) -> int:
    total = 0; page_num = 0
    log_info(f"Fonte: {_BD}{label}{_RST}  query='{query}'  local='{location}'  recência='{recency}'")

    while True:
        if _scrape_aborted(): break
        listing_url = _build_listing_url(domain, query, location, page_num, recency)
        jk_ids      = _collect_jk_ids(session, listing_url, label)

        if not jk_ids:
            log_scrape(f"    {CROSS} Página {page_num + 1}  —  sem vagas (fim ou bloqueio)")
            break

        log_scrape(f"    {CHECK} Página {page_num + 1}  —  {len(jk_ids)} vagas encontradas")

        skipped_seen = 0
        for jk in jk_ids:
            if _scrape_aborted(): break
            detail_url = _build_detail_url(domain, jk)
            if rdb.is_rejected(detail_url):
                continue
            if rdb.is_seen(detail_url):
                skipped_seen += 1; continue
            _random_delay()
            job = _fetch_job_detail(session, domain, jk, label)
            if job:
                if not _title_is_relevant(job["title"]):
                    log_scrape(f"      {_DIM}↷ Irrelevante: {job['title'][:55]}{_RST}")
                    continue
                rdb.push_job(job)
                total += 1
                log_scrape(f"      {CHECK} {job['title'][:55]}  —  {job['company'][:30]}")
            else:
                log_scrape(f"      {CROSS} jk={jk}  —  sem dados")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")

        if _scrape_aborted(): break
        page_num += 1
        if max_pages > 0 and page_num >= max_pages:
            log_info(f"Limite de {max_pages} página(s) atingido"); break
        if len(jk_ids) < 10:
            log_info("Menos de 10 resultados — última página"); break
        _random_delay()

    return total


def _scrape_linkedin_source(
    session:   cf_requests.Session,
    query:     str,
    location:  str,
    max_pages: int,
    rdb:       "MongoManager",
    recency:   str = "any",
    geo_id:    str = "",
) -> int:
    """LinkedIn Jobs — guest API pública (sem login necessário).
    Endpoint: /jobs-guest/jobs/api/seeMoreJobPostings/search
    Retorna fragmentos HTML com cards de vaga, 25 por página.
    geo_id: quando fornecido, usa geoId ao invés de location (mais preciso).
            Brasil = "106057199"
    """
    total = 0
    loc_desc = f"geoId={geo_id}" if geo_id else location
    log_info(f"Fonte: {_BD}LinkedIn Jobs{_RST}  query='{query}'  local='{loc_desc}'  recência='{recency}'")

    recency_param = _RECENCY_LINKEDIN.get(recency, "")
    max_p = max_pages or 5

    # Palavras que indicam localização FORA do Brasil — usadas no filtro pós-fetch
    # quando geo_id == Brasil. Aceita vazia (remoto indefinido) e rejeita explícitas.
    _NON_BR_LOCS: tuple[str, ...] = (
        "india", "bengaluru", "mumbai", "delhi", "bangalore", "hyderabad",
        "pune", "chennai", "kolkata", "ahmedabad",
        "france", "paris", "lyon", "marseille",
        "london", "england", "united kingdom",
        "canada", "toronto", "montreal", "vancouver",
        "united states", "new york", "san francisco", "chicago", "los angeles",
        "germany", "berlin", "munich", "hamburg",
        "australia", "sydney", "melbourne",
        "spain", "madrid", "barcelona",
        "netherlands", "amsterdam",
        "singapore", "hong kong",
        "argentina", "buenos aires",
        "chile", "santiago",
        "colombia", "bogotá", "bogota",
        "mexico", "ciudad de méxico",
        "peru", "lima",
        "portugal", "lisbon", "lisboa",
        "italy", "rome", "milan",
        "poland", "warsaw",
        "czech republic", "prague",
        "sweden", "stockholm",
        "norway", "oslo",
        "denmark", "copenhagen",
        "switzerland", "zurich",
        "austria", "vienna",
        "belgium", "brussels",
        "japan", "tokyo",
        "south korea", "seoul",
        "china", "beijing", "shanghai",
        "indonesia", "jakarta",
        "philippines", "manila",
        "vietnam", "hanoi",
        "malaysia", "kuala lumpur",
        "israel", "tel aviv",
        "turkey", "istanbul",
    )

    for page_num in range(max_p):
        if _scrape_aborted(): break

        start = page_num * 25
        params: dict = {"keywords": query, "start": start}
        # Duplo sinal para reforçar filtragem no Brasil:
        # geoId (numérico) + location (texto) — o LinkedIn usa ambos quando presentes
        if geo_id:
            params["geoId"]    = geo_id
            params["location"] = "Brasil"   # reforça o sinal textual
        elif location:
            params["location"] = location
        if recency_param:
            params["f_TPR"] = recency_param

        # Guest API — não requer autenticação
        api_url = (
            f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
            f"{urllib.parse.urlencode(params)}"
        )
        log_info(f"LinkedIn guest API  start={start}: {api_url}")
        soup = _get_soup(session, api_url)
        if not soup: break

        cards = soup.select("li") or []
        if not cards:
            log_info("Sem cards — última página"); break

        log_scrape(f"    {CHECK} Página {page_num + 1}  —  {len(cards)} cards")
        found = 0
        skipped_seen = 0

        for card in cards:
            if _scrape_aborted(): break

            link_el = card.select_one(
                "a.base-card__full-link, "
                "a[href*='/jobs/view/'], "
                "a[href*='linkedin.com/jobs']"
            )
            if not link_el: continue
            link = link_el.get("href", "").split("?")[0]
            if not link: continue

            title_el = card.select_one(
                "h3.base-search-card__title, "
                ".job-search-card__title, "
                "h3, h4"
            )
            title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            company_el = card.select_one(
                "h4.base-search-card__subtitle, "
                ".job-search-card__company-name, "
                ".base-search-card__subtitle"
            )
            company = company_el.get_text(strip=True) if company_el else "Empresa não informada"

            # ── Localização: seletor específico evita misturar com horário ──────
            loc_el = card.select_one(".job-search-card__location")
            if not loc_el:
                # fallback: primeiro span/span dentro de metadata (sem o <time>)
                meta_el = card.select_one(".base-search-card__metadata")
                if meta_el:
                    # remove o elemento <time> do clone antes de extrair texto
                    import copy as _copy
                    meta_clone = _copy.copy(meta_el)
                    for t in meta_clone.find_all("time"):
                        t.decompose()
                    loc_el = meta_clone
            location_text = loc_el.get_text(strip=True) if loc_el else ""

            # ── Horário de publicação (elemento <time> separado) ─────────────
            time_el   = card.select_one("time, .job-search-card__listdate, .job-search-card__listdate--new")
            published = time_el.get_text(strip=True) if time_el else ""

            # ── Easy Apply flag ───────────────────────────────────────────────
            easy_apply_el = card.select_one(
                ".job-search-card__easy-apply-label, "
                "[aria-label*='Easy Apply'], "
                "[aria-label*='Candidatura simplificada']"
            )
            easy_apply = bool(easy_apply_el)

            # ── Filtro pós-fetch: rejeita localização explicitamente fora do Brasil ──
            if geo_id == "106057199" and location_text:
                loc_lower = location_text.lower()
                if any(kw in loc_lower for kw in _NON_BR_LOCS):
                    log_scrape(
                        f"      {_DIM}↷ Fora do Brasil ({location_text[:40]}) — ignorado{_RST}"
                    )
                    continue

            region_label = "LinkedIn Brasil" if geo_id == "106057199" else "LinkedIn"
            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location_text,
                "description": "",
                "benefits":    "",
                "link":        link,
                "region":      region_label,
                "published":   published,
                "easy_apply":  easy_apply,
                "applicants":  "",   # preenchido pelo _enrich_descriptions
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


# ── Geração de query com IA ───────────────────────────────────────────────────

def generate_ai_query(
    client:     Groq,
    stack_name: str,
    techs:      list[str],
    prefs:      dict,
    sources:    list[str],
) -> str:
    """
    IA gera uma query de busca otimizada para as plataformas selecionadas.
    Leva em conta área, tecnologias, modalidade e localização.
    """
    log_info("Gerando query otimizada com IA...")

    scope    = prefs.get("location_scope", "ambos")
    idioma   = "português (BR)" if scope == "brasil" else "inglês (mais resultados)"
    mod_map  = {"remoto": "remota", "presencial": "presencial", "hibrido": "híbrida", "todos": "qualquer"}
    cont_map = {"pj": "PJ", "clt": "CLT", "autonomo": "freelancer", "todos": "qualquer"}
    src_labels = [SOURCES[s]["label"] for s in sources if s in SOURCES]

    prompt = (
        f"Você é especialista em busca de empregos tech.\n\n"
        f"Perfil de busca:\n"
        f"  Área:       {stack_name}\n"
        f"  Tecnologias: {', '.join(techs) if techs else 'não especificadas'}\n"
        f"  Modalidade: {mod_map.get(prefs.get('modality','todos'),'qualquer')}\n"
        f"  Contrato:   {cont_map.get(prefs.get('contract','todos'),'qualquer')}\n"
        f"  Escopo:     {scope}\n"
        f"  Plataformas: {', '.join(src_labels)}\n\n"
        f"Gere uma query de busca OTIMIZADA com no máximo 60 caracteres:\n"
        f"- Cargo principal + 2 ou 3 tecnologias mais relevantes\n"
        f"- Idioma preferencial: {idioma}\n"
        f"- Use termos que aparecem nas descrições reais de vagas\n"
        f"- NÃO inclua modalidade nem tipo de contrato na query\n\n"
        f"Responda APENAS com a query, sem aspas nem explicações."
    )

    try:
        resp = client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=70,
            messages=[
                {"role": "system", "content": "Responda APENAS com a query de busca, nada mais."},
                {"role": "user",   "content": prompt},
            ],
        )
        q = resp.choices[0].message.content.strip().strip('"\'`')
        # Remove quebras de linha e limita tamanho
        q = q.splitlines()[0].strip()[:80]
        log_ok(f"Query IA: {_BD}{q}{_RST}")
        return q
    except Exception as exc:
        log_warn(f"Geração de query IA falhou ({exc}) — usando fallback")
        role = STACKS.get(stack_name, {}).get("role", stack_name)
        return (f"{' '.join(techs[:3])} {role}".strip() if techs else role)


# ── Plataformas adicionais ────────────────────────────────────────────────────

def _recency_cutoff(recency: str) -> Optional[float]:
    """Retorna timestamp Unix mínimo para o filtro de recência (ou None se 'any')."""
    days = _RECENCY_DAYS.get(recency)
    return (datetime.now() - timedelta(days=days)).timestamp() if days else None


def _scrape_gupy_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Gupy — API JSON do portal público (principal plataforma BR)."""
    total = 0; page_num = 1
    log_info(f"Fonte: {_BD}Gupy{_RST}  query='{query}'  recência='{recency}'")

    date_start = ""
    days = _RECENCY_DAYS.get(recency)
    if days:
        date_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")

    while True:
        if _scrape_aborted(): break
        params: dict = {"term": query, "page": page_num}
        if date_start:
            params["publishedAt[start]"] = date_start

        url = f"https://portal.gupy.io/api/job?{urllib.parse.urlencode(params)}"
        log_info(f"Gupy pág. {page_num}: {url}")

        try:
            resp = session.get(url, timeout=30, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                log_warn(f"Gupy HTTP {resp.status_code}"); break
            data      = resp.json()
            jobs_data = data.get("data", [])
        except Exception as exc:
            log_warn(f"Gupy erro: {exc}"); break

        if not jobs_data:
            log_info("Sem mais vagas — Gupy"); break

        skipped_seen = 0
        log_scrape(f"    {CHECK} Gupy pág. {page_num}  —  {len(jobs_data)} vagas")
        for jd in jobs_data:
            if _scrape_aborted(): break
            job_url = jd.get("jobUrl") or f"https://portal.gupy.io/job/{jd.get('id','')}"
            if rdb.is_rejected(job_url): continue
            if rdb.is_seen(job_url): skipped_seen += 1; continue

            raw_desc    = jd.get("description", "") or ""
            description = BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)[:3000]
            loc_parts   = [jd.get("city",""), jd.get("state","")]
            location    = ", ".join(p for p in loc_parts if p) or jd.get("workplaceType","")

            job = {
                "jk":          str(jd.get("id","")),
                "title":       jd.get("name","Título não informado"),
                "company":     (jd.get("company") or {}).get("name","Empresa não informada"),
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        job_url,
                "region":      "Gupy BR",
                "published":   jd.get("publishedAt",""),
            }
            if not _title_is_relevant(job["title"]):
                log_scrape(f"      {_DIM}↷ Irrelevante: {job['title'][:55]}{_RST}")
                continue
            rdb.push_job(job); total += 1
            log_scrape(f"      {CHECK} {job['title'][:55]}  —  {job['company'][:30]}")
            _random_delay()

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")

        if _scrape_aborted(): break
        page_num += 1
        if max_pages > 0 and page_num > max_pages:
            log_info(f"Limite de {max_pages} pág. atingido — Gupy"); break
        if len(jobs_data) < 10:
            log_info("Última página — Gupy"); break
        _random_delay()

    return total


def _scrape_vagas_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Vagas.com.br — scraping HTML ordenado por data."""
    total = 0; page_num = 0
    log_info(f"Fonte: {_BD}Vagas.com.br{_RST}  query='{query}'")

    slug = _slugify(query)

    while True:
        if _scrape_aborted(): break
        url = (
            f"https://www.vagas.com.br/vagas-de-{urllib.parse.quote(slug)}"
            f"?sort=date&page={page_num + 1}"
        )
        log_info(f"Vagas.com.br pág. {page_num + 1}: {url}")
        soup = _get_soup(session, url)
        if not soup:
            break

        cards = soup.select("li.vaga") or soup.select(".job-shortdesc") or []
        if not cards:
            log_info("Sem vagas — Vagas.com.br"); break

        skipped_seen = 0
        log_scrape(f"    {CHECK} Vagas.com.br pág. {page_num + 1}  —  {len(cards)} vagas")
        for card in cards:
            if _scrape_aborted(): break
            link_el = card.select_one("a.link-detalhes-vaga, a[href*='/vagas/']")
            if not link_el:
                continue
            href    = link_el.get("href","")
            job_url = href if href.startswith("http") else f"https://www.vagas.com.br{href}"
            if rdb.is_rejected(job_url): continue
            if rdb.is_seen(job_url): skipped_seen += 1; continue

            title   = _soup_text(card, "a.link-detalhes-vaga", ".title", "h2", "a")
            company = _soup_text(card, ".empr-name", ".company")
            loc     = _soup_text(card, ".vaga-local", ".location")
            desc    = _soup_text(card, ".vaga-desc", "p")

            if not title:
                continue

            # Filtra antes de buscar detalhe (economiza requisições)
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}")
                continue

            # Página de detalhe para descrição completa
            _random_delay()
            detail_soup = _get_soup(session, job_url)
            if detail_soup:
                full = _soup_text(
                    detail_soup,
                    "#job-description", ".job-description__text",
                    "#descricao-vaga", ".description__text",
                )
                if full:
                    desc = full[:3000]

            job = {
                "jk":          hashlib.md5(job_url.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company or "Empresa não informada",
                "location":    loc,
                "description": desc[:3000],
                "benefits":    "",
                "link":        job_url,
                "region":      "Vagas.com.br",
            }
            rdb.push_job(job); total += 1
            log_scrape(f"      {CHECK} {job['title'][:55]}  —  {job['company'][:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")

        if _scrape_aborted(): break
        page_num += 1
        if max_pages > 0 and page_num >= max_pages:
            log_info(f"Limite atingido — Vagas.com.br"); break
        if len(cards) < 10:
            log_info("Última página — Vagas.com.br"); break
        _random_delay()

    return total


def _scrape_remoteok_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """RemoteOK — API JSON pública (sem paginação, filtragem por tags + data)."""
    total = 0
    log_info(f"Fonte: {_BD}RemoteOK{_RST}  query='{query}'  recência='{recency}'")

    # RemoteOK usa tags separadas por vírgula
    tags = ",".join(t.strip() for t in query.split() if t.strip())
    url  = f"https://remoteok.com/api?tag={urllib.parse.quote(tags)}"
    log_info(f"RemoteOK API: {url}")

    try:
        resp = session.get(url, timeout=30, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        if resp.status_code != 200:
            log_warn(f"RemoteOK HTTP {resp.status_code}"); return 0
        jobs_raw = resp.json()
    except Exception as exc:
        log_warn(f"RemoteOK erro: {exc}"); return 0

    jobs_list = [j for j in jobs_raw if isinstance(j, dict) and j.get("position")]

    # Filtro de recência pelo campo epoch (Unix timestamp)
    cutoff = _recency_cutoff(recency)
    if cutoff:
        jobs_list = [j for j in jobs_list if (j.get("epoch") or 0) >= cutoff]

    skipped_seen = 0
    log_scrape(f"    {CHECK} RemoteOK  —  {len(jobs_list)} vagas (após filtro)")
    for jd in jobs_list:
        if _scrape_aborted(): break
        job_url = jd.get("url","")
        if not job_url:
            job_url = f"https://remoteok.com/remote-jobs/{jd.get('id','')}"
        if not job_url.startswith("http"):
            job_url = f"https://remoteok.com{job_url}"
        if rdb.is_rejected(job_url): continue
        if rdb.is_seen(job_url): skipped_seen += 1; continue

        desc_html   = jd.get("description","") or ""
        description = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)[:3000]

        job = {
            "jk":          str(jd.get("id","")),
            "title":       jd.get("position","Título não informado"),
            "company":     jd.get("company","Empresa não informada"),
            "location":    "Remote",
            "description": description,
            "benefits":    "",
            "link":        job_url,
            "region":      "RemoteOK",
            "published":   jd.get("date",""),
        }
        if not _title_is_relevant(job["title"]):
            log_scrape(f"      {_DIM}↷ Irrelevante: {job['title'][:55]}{_RST}")
            continue
        rdb.push_job(job); total += 1
        log_scrape(f"      {CHECK} {job['title'][:55]}  —  {job['company'][:30]}")

    if skipped_seen:
        log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
    return total


def _scrape_himalayas_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Himalayas — API JSON de vagas remotas internacionais."""
    total = 0; page_num = 1
    log_info(f"Fonte: {_BD}Himalayas{_RST}  query='{query}'  recência='{recency}'")

    cutoff = _recency_cutoff(recency)

    while True:
        if _scrape_aborted(): break
        params = {"q": query, "page": page_num, "limit": 20}
        url = f"https://himalayas.app/jobs/api?{urllib.parse.urlencode(params)}"
        log_info(f"Himalayas pág. {page_num}: {url}")

        try:
            resp = session.get(url, timeout=30, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                log_warn(f"Himalayas HTTP {resp.status_code}"); break
            data      = resp.json()
            jobs_data = data.get("jobs", [])
        except Exception as exc:
            log_warn(f"Himalayas erro: {exc}"); break

        if not jobs_data:
            log_info("Sem mais vagas — Himalayas"); break

        # Filtro de recência por publishedAt / createdAt
        if cutoff:
            filtered = []
            for jd in jobs_data:
                pub = jd.get("publishedAt") or jd.get("createdAt","")
                if pub:
                    try:
                        pub_ts = datetime.fromisoformat(pub.replace("Z","+00:00")).timestamp()
                        if pub_ts >= cutoff:
                            filtered.append(jd)
                    except Exception:
                        filtered.append(jd)   # data inválida → inclui por segurança
                else:
                    filtered.append(jd)
            jobs_data = filtered

        skipped_seen = 0
        log_scrape(f"    {CHECK} Himalayas pág. {page_num}  —  {len(jobs_data)} vagas")
        for jd in jobs_data:
            if _scrape_aborted(): break
            job_url = jd.get("url","") or f"https://himalayas.app/jobs/{jd.get('slug','')}"
            if not job_url.startswith("http"):
                job_url = f"https://himalayas.app{job_url}"
            if rdb.is_rejected(job_url): continue
            if rdb.is_seen(job_url): skipped_seen += 1; continue

            raw_desc = jd.get("description","") or jd.get("summary","") or ""
            desc     = BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)[:3000]
            company  = jd.get("company","Empresa não informada")
            if isinstance(company, dict):
                company = company.get("name","Empresa não informada")

            job = {
                "jk":          str(jd.get("id","") or jd.get("slug","")),
                "title":       jd.get("title","Título não informado"),
                "company":     company,
                "location":    jd.get("location","Remote"),
                "description": desc,
                "benefits":    "",
                "link":        job_url,
                "region":      "Himalayas",
                "published":   jd.get("publishedAt",""),
            }
            if not _title_is_relevant(job["title"]):
                log_scrape(f"      {_DIM}↷ Irrelevante: {job['title'][:55]}{_RST}")
                continue
            rdb.push_job(job); total += 1
            log_scrape(f"      {CHECK} {job['title'][:55]}  —  {job['company'][:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")

        if _scrape_aborted(): break
        page_num += 1
        if max_pages > 0 and page_num > max_pages:
            log_info(f"Limite atingido — Himalayas"); break
        if len(jobs_data) < 20:
            log_info("Última página — Himalayas"); break
        _random_delay()

    return total


def _scrape_weworkremotely_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """We Work Remotely — scraping HTML (busca pública, sem paginação)."""
    total = 0
    log_info(f"Fonte: {_BD}We Work Remotely{_RST}  query='{query}'")

    url = f"https://weworkremotely.com/remote-jobs/search?term={urllib.parse.quote(query)}"
    log_info(f"URL: {url}")
    soup = _get_soup(session, url)
    if not soup:
        return 0

    # Seletores para cards de vaga
    cards = (
        soup.select("ul.jobs li:not(.view-all)") or
        soup.select("article[data-id]") or
        soup.select("li[class*='job']")
    )

    skipped_seen = 0
    log_scrape(f"    {CHECK} We Work Remotely  —  {len(cards)} vagas encontradas")
    for card in cards:
        if _scrape_aborted(): break
        link_el = card.select_one("a[href]")
        if not link_el:
            continue
        href    = link_el.get("href","")
        job_url = href if href.startswith("http") else f"https://weworkremotely.com{href}"
        if "#" in job_url:
            job_url = job_url.split("#")[0]
        if rdb.is_rejected(job_url): continue
        if rdb.is_seen(job_url): skipped_seen += 1; continue

        title   = _soup_text(card, ".title", "span.title", "h2", "a")
        company = _soup_text(card, ".company", "span.company", "[class*='company']")

        # Filtra por relevância antes de buscar detalhe (economiza requisições)
        if title and not _title_is_relevant(title):
            log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}")
            continue

        # Busca descrição completa na página de detalhe
        _random_delay()
        detail_soup = _get_soup(session, job_url)
        desc = ""
        if detail_soup:
            desc = _soup_text(
                detail_soup,
                ".listing-container", "#job-listing-show-container",
                ".description", "article",
            )[:3000]

        if not title and not desc:
            continue

        job = {
            "jk":          hashlib.md5(job_url.encode()).hexdigest()[:10],
            "title":       title or "Título não informado",
            "company":     company or "Empresa não informada",
            "location":    "Remote",
            "description": desc,
            "benefits":    "",
            "link":        job_url,
            "region":      "We Work Remotely",
        }
        rdb.push_job(job); total += 1
        log_scrape(f"      {CHECK} {job['title'][:55]}  —  {job['company'][:30]}")

    if skipped_seen:
        log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
    return total


def _scrape_linkedin_feed_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
    scope:     str = "ambos",
) -> int:
    """
    LinkedIn Feed — busca posts públicos com menção a vagas/oportunidades.
    Requer cookie de autenticação: export LINKEDIN_LI_AT='AQEDATi...'
    scope: "brasil" adiciona contexto de localização BR na query do feed.
    """
    li_at = os.environ.get("LINKEDIN_LI_AT","").strip()
    if not li_at:
        log_warn("LinkedIn Feed: defina a variável LINKEDIN_LI_AT para ativar esta fonte.")
        log_warn(f"  {_DIM}Acesse linkedin.com → DevTools → Application → Cookies → li_at{_RST}")
        log_warn(f"  {_DIM}export LINKEDIN_LI_AT='AQEDATi...'{_RST}")
        return 0

    total = 0
    log_info(f"Fonte: {_BD}LinkedIn Feed{_RST}  query='{query}'  escopo='{scope}'  recência='{recency}'")

    # Cookie de autenticação
    session.cookies.set("li_at", li_at, domain=".linkedin.com")

    # Adiciona hashtags de vagas à query para cobrir posts de recrutadores
    if scope == "brasil":
        # Feed não suporta geoId — injeta termos de localização para filtrar BR
        location_ctx = "(Brasil OR \"São Paulo\" OR \"Rio de Janeiro\" OR Remoto)"
        hashtags = "vaga OR oportunidade OR contratando OR \"processo seletivo\""
        feed_query = f"({query}) {location_ctx} ({hashtags})"
    else:
        hashtags = "vaga OR hiring OR oportunidade OR contratando"
        feed_query = f"({query}) ({hashtags})"

    # Filtro de data para LinkedIn
    date_param = ""
    if recency in ("1d", "3d"):
        date_param = "past-day"
    elif recency in ("7d", "14d"):
        date_param = "past-week"

    # Palavras-chave para identificar posts de vagas
    VAG_KEYWORDS = [
        "vaga", "oportunidade", "hiring", "job opening", "we're hiring",
        "contratando", "processo seletivo", "aberta(s)", "open position",
        "looking for", "buscando profissional",
    ]

    n_pages = max_pages if max_pages > 0 else 3
    for page_num in range(n_pages):
        if _scrape_aborted(): break
        start  = page_num * 10
        params: dict = {
            "keywords": feed_query,
            "origin":   "FACETED_SEARCH",
            "sortBy":   "date_posted",
            "start":    start,
        }
        if date_param:
            params["datePosted"] = date_param

        url = f"https://www.linkedin.com/search/results/content/?{urllib.parse.urlencode(params)}"
        log_info(f"LinkedIn Feed pág. {page_num + 1}: {url}")
        soup = _get_soup(session, url)
        if not soup:
            break

        # Seletores de posts
        posts = (
            soup.select(".feed-shared-update-v2") or
            soup.select("[data-urn*='activity']") or
            soup.select(".update-components-text")
        )
        if not posts:
            log_info(f"Sem posts — LinkedIn Feed pág. {page_num + 1}"); break

        log_scrape(f"    {CHECK} LinkedIn Feed pág. {page_num + 1}  —  {len(posts)} posts analisados")
        for post in posts:
            if _scrape_aborted(): break
            text_el = post.select_one(
                ".feed-shared-text, .update-components-text, .break-words"
            )
            if not text_el:
                continue
            text  = text_el.get_text(" ", strip=True)
            lower = text.lower()

            # Filtra apenas posts com linguagem de vaga
            if not any(kw in lower for kw in VAG_KEYWORDS):
                continue

            # URL do post
            link_el  = post.select_one("a[href*='/posts/'], a[href*='/feed/update/']")
            post_url = ""
            if link_el:
                href     = link_el.get("href","")
                post_url = href if href.startswith("http") else f"https://www.linkedin.com{href}"
            if not post_url:
                urn      = post.get("data-urn","") or post.get("data-id","")
                post_url = f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""
            if not post_url:
                continue

            if rdb.is_rejected(post_url): continue
            if rdb.is_seen(post_url): continue   # posts feed — sem contagem (volume alto)

            author_el = post.select_one(
                ".update-components-actor__name, .feed-shared-actor__name"
            )
            author = author_el.get_text(strip=True) if author_el else "LinkedIn Post"

            job = {
                "jk":          hashlib.md5(post_url.encode()).hexdigest()[:10],
                "title":       f"[Post] {text[:80].strip()}",
                "company":     author,
                "location":    "LinkedIn Feed",
                "description": text[:3000],
                "benefits":    "",
                "link":        post_url,
                "region":      "LinkedIn Feed",
            }
            rdb.push_job(job); total += 1
            log_scrape(f"      {CHECK} Post: {author[:35]}  |  {text[:40]}...")

        if len(posts) < 5:
            log_info("Poucos resultados — provavelmente última página — LinkedIn Feed"); break
        _random_delay()

    log_ok(f"LinkedIn Feed: {total} posts com vagas enfileirados")
    return total


def _scrape_programathor_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """ProgramaThor — scraping HTML (plataforma tech brasileira)."""
    total = 0
    log_info(f"Fonte: {_BD}ProgramaThor{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        # ProgramaThor: paginação via /jobs/page/N, busca via ?search=
        page_path = f"/page/{page}" if page > 1 else ""
        url = f"https://programathor.com.br/jobs{page_path}?search={urllib.parse.quote(query)}"
        log_info(f"ProgramaThor p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select(".cell-list-developer") or
            soup.select(".job-list-item") or
            soup.select("a[href*='/jobs/']") or
            soup.select("article.job") or
            soup.select("[class*='JobCard']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://programathor.com.br{href}"

            title_el = card.select_one("h2, h3, h4, .title, .job-title")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .empresa, .companyName")
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(".location, .localizacao, .cidade, .local")
            location = loc_el.get_text(strip=True) if loc_el else "Brasil"

            desc_el = card.select_one(".description, .descricao, .summary, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "ProgramaThor",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_geekHunter_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """GeekHunter — scraping HTML (plataforma tech brasileira)."""
    total = 0
    log_info(f"Fonte: {_BD}GeekHunter{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        url = f"https://www.geekhunter.com.br/vagas?q={urllib.parse.quote(query)}&page={page}"
        log_info(f"GeekHunter p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("a[data-cy='job-item']") or
            soup.select(".sc-dkzDqf") or           # class dinâmica do React
            soup.select("[class*='JobCard']") or
            soup.select("[class*='job-card']") or
            soup.select("li[class*='item']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.geekhunter.com.br{href}"

            title_el = card.select_one(
                "h2, h3, h4, [class*='title'], [class*='Title'], "
                "[data-cy='job-title'], .job-title"
            )
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .empresa, .companyName")
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(".location, .localizacao, .cidade, .local")
            location = loc_el.get_text(strip=True) if loc_el else "Brasil"

            desc_el = card.select_one(".description, .descricao, .summary, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "GeekHunter",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_catho_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Catho — scraping HTML (maior plataforma geral brasileira)."""
    total = 0
    log_info(f"Fonte: {_BD}Catho{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        url = f"https://www.catho.com.br/vagas/?q={urllib.parse.quote(query)}&page={page}"
        log_info(f"Catho p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("[class*='JobCard']") or
            soup.select("[class*='job-card']") or
            soup.select("article[data-job]") or
            soup.select(".vacancy") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.catho.com.br{href}"

            title_el = card.select_one("h2, h3, h4, .title, .job-title, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .empresa, [class*='company']")
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(".location, .localizacao, .cidade, [class*='location']")
            location = loc_el.get_text(strip=True) if loc_el else "Brasil"

            desc_el = card.select_one(".description, .descricao, .summary, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Catho",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_infojobs_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """InfoJobs BR — scraping HTML (plataforma geral brasileira)."""
    total = 0
    log_info(f"Fonte: {_BD}InfoJobs{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        url = f"https://www.infojobs.com.br/empregos.aspx?palabra={urllib.parse.quote(query)}&pagina={page}"
        log_info(f"InfoJobs p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select(".ij-OfferCardContent") or
            soup.select(".offer-card") or
            soup.select("article[class*='offer']") or
            soup.select(".vacancy-item") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.infojobs.com.br{href}"

            title_el = card.select_one("h2, h3, h4, .title, .job-title, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .empresa, [class*='company']")
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(".location, .localizacao, .cidade, .local")
            location = loc_el.get_text(strip=True) if loc_el else "Brasil"

            desc_el = card.select_one(".description, .descricao, .summary, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "InfoJobs",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_impulso_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Impulso Network — scraping HTML (sem paginação)."""
    total = 0
    log_info(f"Fonte: {_BD}Impulso{_RST}  query='{query}'")

    url = f"https://impulso.network/jobs?q={urllib.parse.quote(query)}"
    log_info(f"Impulso: {url}")
    soup = _get_soup(session, url)
    if not soup:
        return 0

    cards = (
        soup.select(".job-card") or
        soup.select("article.job") or
        soup.select("[class*='vacancy']") or
        []
    )

    skipped_seen = 0
    log_scrape(f"    {CHECK} Impulso  —  {len(cards)} vagas encontradas")
    for card in cards:
        if _scrape_aborted(): break

        a = card.select_one("a[href]")
        if not a: continue
        href = a["href"]
        link = href if href.startswith("http") else f"https://impulso.network{href}"

        title_el = card.select_one("h2, h3, h4, .title, .job-title")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
        if not title: continue

        if rdb.is_rejected(link): continue
        if rdb.is_seen(link): skipped_seen += 1; continue
        if not _title_is_relevant(title):
            log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

        comp_el = card.select_one(".company, .employer, .empresa, .companyName")
        company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

        loc_el = card.select_one(".location, .localizacao, .cidade, .local")
        location = loc_el.get_text(strip=True) if loc_el else "Brasil"

        desc_el = card.select_one(".description, .descricao, .summary, p")
        description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

        rdb.push_job({
            "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
            "title":       title,
            "company":     company,
            "location":    location,
            "description": description,
            "benefits":    "",
            "link":        link,
            "region":      "Impulso",
            "published":   "",
        })
        total += 1
        log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

    if skipped_seen:
        log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
    return total


def _scrape_remotar_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Remotar — scraping HTML (sem paginação, vagas remotas BR)."""
    total = 0
    log_info(f"Fonte: {_BD}Remotar{_RST}  query='{query}'")

    # Remotar: busca via /jobs com parâmetro ?busca= ou ?search=
    url = f"https://remotar.com.br/jobs?busca={urllib.parse.quote(query)}"
    log_info(f"Remotar: {url}")
    soup = _get_soup(session, url)
    if not soup:
        return 0

    cards = (
        soup.select(".vagas-item") or
        soup.select(".job-card") or
        soup.select("article.vaga") or
        soup.select("[class*='vaga']") or
        soup.select("[class*='job']") or
        []
    )

    skipped_seen = 0
    log_scrape(f"    {CHECK} Remotar  —  {len(cards)} vagas encontradas")
    for card in cards:
        if _scrape_aborted(): break

        a = card.select_one("a[href]")
        if not a: continue
        href = a["href"]
        link = href if href.startswith("http") else f"https://remotar.com.br{href}"

        title_el = card.select_one("h2, h3, h4, .title, .job-title")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
        if not title: continue

        if rdb.is_rejected(link): continue
        if rdb.is_seen(link): skipped_seen += 1; continue
        if not _title_is_relevant(title):
            log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

        comp_el = card.select_one(".company, .employer, .empresa, .companyName")
        company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

        loc_el = card.select_one(".location, .localizacao, .cidade, .local")
        location = loc_el.get_text(strip=True) if loc_el else "Brasil"

        desc_el = card.select_one(".description, .descricao, .summary, p")
        description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

        rdb.push_job({
            "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
            "title":       title,
            "company":     company,
            "location":    location,
            "description": description,
            "benefits":    "",
            "link":        link,
            "region":      "Remotar",
            "published":   "",
        })
        total += 1
        log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

    if skipped_seen:
        log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
    return total


def _scrape_revelo_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Revelo — scraping HTML (plataforma tech brasileira)."""
    total = 0
    log_info(f"Fonte: {_BD}Revelo{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        url = f"https://www.revelo.com.br/jobs?q={urllib.parse.quote(query)}&page={page}"
        log_info(f"Revelo p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select(".job-card") or
            soup.select("article[class*='job']") or
            soup.select("[class*='JobCard']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.revelo.com.br{href}"

            title_el = card.select_one("h2, h3, h4, .title, .job-title")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .empresa, .companyName")
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(".location, .localizacao, .cidade, .local")
            location = loc_el.get_text(strip=True) if loc_el else "Brasil"

            desc_el = card.select_one(".description, .descricao, .summary, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Revelo",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_workana_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Workana — scraping HTML (plataforma freelance, foco em TI)."""
    total = 0
    log_info(f"Fonte: {_BD}Workana{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        url = (
            f"https://www.workana.com/jobs?language=pt-BR&category=it-programming"
            f"&query={urllib.parse.quote(query)}&page={page}"
        )
        log_info(f"Workana p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select(".project-item") or
            soup.select("article[class*='project']") or
            soup.select(".js-project") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.workana.com{href}"

            title_el = card.select_one("h2, h3, h4, .title, .project-title, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .client, [class*='client']")
            company = comp_el.get_text(strip=True) if comp_el else "Cliente não informado"

            desc_el = card.select_one(".description, .descricao, .project-description, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    "Freelance / Remoto",
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Workana",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_99freelas_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """99Freelas — scraping HTML (plataforma freelance brasileira)."""
    total = 0
    log_info(f"Fonte: {_BD}99Freelas{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        url = f"https://www.99freelas.com.br/projects?text={urllib.parse.quote(query)}&page={page}"
        log_info(f"99Freelas p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select(".project-item") or
            soup.select(".result-item") or
            soup.select("li[class*='project']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.99freelas.com.br{href}"

            title_el = card.select_one("h2, h3, h4, .title, .project-title")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, .employer, .client, .usuario")
            company = comp_el.get_text(strip=True) if comp_el else "Cliente não informado"

            desc_el = card.select_one(".description, .descricao, .summary, p")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    "Freelance / Remoto",
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "99Freelas",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_turing_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Turing — scraping HTML (plataforma internacional, sem paginação)."""
    total = 0
    log_info(f"Fonte: {_BD}Turing{_RST}  query='{query}'")

    url = f"https://www.turing.com/jobs?q={urllib.parse.quote(query)}"
    log_info(f"Turing: {url}")
    soup = _get_soup(session, url)
    if not soup:
        return 0

    cards = (
        soup.select(".job-card") or
        soup.select("article[class*='job']") or
        soup.select("[class*='JobCard']") or
        []
    )

    skipped_seen = 0
    log_scrape(f"    {CHECK} Turing  —  {len(cards)} vagas encontradas")
    for card in cards:
        if _scrape_aborted(): break

        a = card.select_one("a[href]")
        if not a: continue
        href = a["href"]
        link = href if href.startswith("http") else f"https://www.turing.com{href}"

        title_el = card.select_one("h2, h3, h4, .title, .job-title")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
        if not title: continue

        if rdb.is_rejected(link): continue
        if rdb.is_seen(link): skipped_seen += 1; continue
        if not _title_is_relevant(title):
            log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

        comp_el = card.select_one(".company, .employer, .empresa, .companyName")
        company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

        loc_el = card.select_one(".location, .localizacao, .cidade, .local")
        location = loc_el.get_text(strip=True) if loc_el else "Remote"

        desc_el = card.select_one(".description, .descricao, .summary, p")
        description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

        rdb.push_job({
            "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
            "title":       title,
            "company":     company,
            "location":    location,
            "description": description,
            "benefits":    "",
            "link":        link,
            "region":      "Turing",
            "published":   "",
        })
        total += 1
        log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

    if skipped_seen:
        log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
    return total


def _scrape_toptal_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Toptal — scraping HTML (plataforma internacional de elite, sem paginação)."""
    total = 0
    log_info(f"Fonte: {_BD}Toptal{_RST}  query='{query}'")

    url = "https://www.toptal.com/jobs#open-positions"
    log_info(f"Toptal: {url}")
    soup = _get_soup(session, url)
    if not soup:
        return 0

    cards = (
        soup.select(".job-listing") or
        soup.select("article[class*='job']") or
        soup.select("[class*='JobListing']") or
        soup.select("li[class*='position']") or
        []
    )

    skipped_seen = 0
    log_scrape(f"    {CHECK} Toptal  —  {len(cards)} vagas encontradas")
    for card in cards:
        if _scrape_aborted(): break

        a = card.select_one("a[href]")
        if not a: continue
        href = a["href"]
        link = href if href.startswith("http") else f"https://www.toptal.com{href}"

        title_el = card.select_one("h2, h3, h4, .title, .job-title, [class*='title']")
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
        if not title: continue

        if rdb.is_rejected(link): continue
        if rdb.is_seen(link): skipped_seen += 1; continue
        if not _title_is_relevant(title):
            log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

        comp_el = card.select_one(".company, .employer, .empresa, .companyName")
        company = comp_el.get_text(strip=True) if comp_el else "Toptal"

        loc_el = card.select_one(".location, .localizacao, .cidade, .local")
        location = loc_el.get_text(strip=True) if loc_el else "Remote"

        desc_el = card.select_one(".description, .descricao, .summary, p")
        description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

        rdb.push_job({
            "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
            "title":       title,
            "company":     company,
            "location":    location,
            "description": description,
            "benefits":    "",
            "link":        link,
            "region":      "Toptal",
            "published":   "",
        })
        total += 1
        log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

    if skipped_seen:
        log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
    return total


def _scrape_upwork_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Upwork — scraping HTML (plataforma freelance/emprego internacional)."""
    total = 0
    log_info(f"Fonte: {_BD}Upwork{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        offset = (page - 1) * 10
        url = (
            f"https://www.upwork.com/nx/jobs/search/?q={urllib.parse.quote(query)}"
            f"&sort=recency&paging={offset}%3B10"
        )
        log_info(f"Upwork p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("article[data-test='job-tile']") or
            soup.select(".job-tile") or
            soup.select("[class*='JobTile']") or
            soup.select("section[class*='up-card']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.upwork.com{href}"

            title_el = card.select_one(
                "h2, h3, h4, .job-title, [data-test='job-title'], "
                "[class*='title'], [class*='JobTitle']"
            )
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(
                ".company, .client-name, [data-test='client-name'], "
                "[class*='client'], [class*='Company']"
            )
            company = comp_el.get_text(strip=True) if comp_el else "Cliente não informado"

            loc_el = card.select_one(
                ".location, [data-test='client-country'], "
                "[class*='country'], [class*='Location']"
            )
            location = loc_el.get_text(strip=True) if loc_el else "Remote / Worldwide"

            desc_el = card.select_one(
                ".description, [data-test='job-description'], "
                "[class*='description'], [class*='Description'], p"
            )
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Upwork",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_glassdoor_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Glassdoor BR — scraping HTML com sessão autenticada."""
    total = 0
    log_info(f"Fonte: {_BD}Glassdoor{_RST}  query='{query}'")

    recency_map = {"1d": 1, "3d": 3, "7d": 7, "14d": 14}
    from_age = recency_map.get(recency, 0)

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        params = f"sc.keyword={urllib.parse.quote(query)}&p={page}"
        if from_age:
            params += f"&fromAge={from_age}"
        url = f"https://www.glassdoor.com.br/Vaga/vagas.htm?{params}"
        log_info(f"Glassdoor p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("li[data-test='jobListing']") or
            soup.select("li.react-job-listing") or
            soup.select("[class*='JobCard']") or
            soup.select("[class*='job-listing']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.glassdoor.com.br{href}"
            # Limpa parâmetros de tracking
            link = link.split("?")[0] if "?" in link else link

            title_el = card.select_one(
                "[data-test='job-title'], [class*='JobTitle'], "
                "h2, h3, h4, .title, .job-title"
            )
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(
                "[data-test='employer-name'], [class*='EmployerName'], "
                ".employer-name, .company, [class*='company']"
            )
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(
                "[data-test='emp-location'], [class*='Location'], "
                ".location, .localizacao"
            )
            location = loc_el.get_text(strip=True) if loc_el else "Brasil"

            desc_el = card.select_one(
                "[data-test='job-description'], [class*='description'], p"
            )
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Glassdoor",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_ziprecruiter_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """ZipRecruiter — scraping HTML (plataforma internacional)."""
    total = 0
    log_info(f"Fonte: {_BD}ZipRecruiter{_RST}  query='{query}'")

    recency_map = {"1d": 1, "3d": 3, "7d": 7, "14d": 14}
    days = recency_map.get(recency, 0)

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        params = f"search={urllib.parse.quote(query)}&location=Remote&page={page}"
        if days:
            params += f"&days={days}"
        url = f"https://www.ziprecruiter.com/jobs-search?{params}"
        log_info(f"ZipRecruiter p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("article.job_result") or
            soup.select("[data-testid='job-card']") or
            soup.select(".job_results li") or
            soup.select("[class*='JobCard']") or
            soup.select(".jobList-item") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.ziprecruiter.com{href}"

            title_el = card.select_one(
                "h2, h3, h4, [class*='title'], [class*='JobTitle'], "
                ".job_title, [data-testid='job-title']"
            )
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(
                ".company, [class*='company'], [class*='Company'], "
                ".hiring_company, [data-testid='company-name']"
            )
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(
                ".location, [class*='location'], [class*='Location'], "
                "[data-testid='location']"
            )
            location = loc_el.get_text(strip=True) if loc_el else "Remote"

            desc_el = card.select_one(
                ".job_description, [class*='description'], "
                "[data-testid='job-description'], p"
            )
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "ZipRecruiter",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_careerjet_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Careerjet — scraping HTML (agregador internacional de vagas)."""
    total = 0
    log_info(f"Fonte: {_BD}Careerjet{_RST}  query='{query}'")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        # careerjet.com.br para vagas brasileiras; parâmetro de busca é `s=`
        url = (
            f"https://www.careerjet.com.br/empregos?s={urllib.parse.quote(query)}"
            f"&l=Remoto&p={page}"
        )
        log_info(f"Careerjet p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("article.job") or
            soup.select("li.job") or
            soup.select(".jobs li") or
            soup.select("[class*='job-item']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.careerjet.com.br{href}"

            title_el = card.select_one("h2, h3, h4, .title, .job-title, a.job")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(".company, p.company, .employer, .recruiter")
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(".location, ul.tags li, .city")
            location = loc_el.get_text(strip=True) if loc_el else "Remote"

            desc_el = card.select_one(".desc, .description, p.description, .snippet")
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Careerjet",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _scrape_jora_source(
    session:   cf_requests.Session,
    query:     str,
    max_pages: int,
    recency:   str,
    rdb:       "MongoManager",
) -> int:
    """Jora — scraping HTML (agregador internacional de vagas)."""
    total = 0
    log_info(f"Fonte: {_BD}Jora{_RST}  query='{query}'")

    recency_map = {"1d": "1", "3d": "3", "7d": "7", "14d": "14"}
    date_filter = recency_map.get(recency, "")

    pages = range(1, (max_pages or 5) + 1)
    for page in pages:
        if _scrape_aborted(): break

        params = f"q={urllib.parse.quote(query)}&l=Remote&p={page}"
        if date_filter:
            params += f"&daterange={date_filter}"
        url = f"https://www.jora.com/j?{params}"
        log_info(f"Jora p{page}: {url}")
        soup = _get_soup(session, url)
        if not soup: break

        cards = (
            soup.select("article[class*='job-card']") or
            soup.select("[data-automation='jobCard']") or
            soup.select(".job-card") or
            soup.select("article.result") or
            soup.select("li[class*='result']") or
            []
        )
        if not cards: break

        found = 0
        skipped_seen = 0
        for card in cards:
            if _scrape_aborted(): break

            a = card.select_one("a[href]")
            if not a: continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.jora.com{href}"

            title_el = card.select_one(
                "h2, h3, h4, .title, .job-title, "
                "[class*='title'], [data-automation='jobTitle']"
            )
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title: continue

            if rdb.is_rejected(link): continue
            if rdb.is_seen(link): skipped_seen += 1; continue
            if not _title_is_relevant(title):
                log_scrape(f"      {_DIM}↷ Irrelevante: {title[:55]}{_RST}"); continue

            comp_el = card.select_one(
                ".company, .employer, [class*='company'], "
                "[data-automation='jobCompany']"
            )
            company = comp_el.get_text(strip=True) if comp_el else "Empresa não informada"

            loc_el = card.select_one(
                ".location, .city, [class*='location'], "
                "[data-automation='jobLocation']"
            )
            location = loc_el.get_text(strip=True) if loc_el else "Remote"

            desc_el = card.select_one(
                ".description, .snippet, [class*='description'], p"
            )
            description = desc_el.get_text(" ", strip=True)[:1500] if desc_el else ""

            rdb.push_job({
                "jk":          hashlib.md5(link.encode()).hexdigest()[:10],
                "title":       title,
                "company":     company,
                "location":    location,
                "description": description,
                "benefits":    "",
                "link":        link,
                "region":      "Jora",
                "published":   "",
            })
            total += 1; found += 1
            log_scrape(f"      {CHECK} {title[:55]}  —  {company[:30]}")

        if skipped_seen:
            log_scrape(f"      {_DIM}↷ {skipped_seen} vaga(s) já mapeadas — ignoradas{_RST}")
        if not found: break
        _random_delay()

    return total


def _browser_profile_dir(platform_key: str) -> Path:
    """Retorna (e cria) o diretório de perfil persistente do browser para a plataforma."""
    d = BROWSER_PROFILES_DIR / platform_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _has_browser_session(platform_key: str) -> bool:
    """Retorna True se já existe um perfil persistente salvo para a plataforma."""
    profile_dir = BROWSER_PROFILES_DIR / platform_key
    # Chromium cria a pasta "Default" dentro do user_data_dir após primeiro uso
    return (profile_dir / "Default").exists()


def _launch_persistent(pw_instance, platform_key: str, headless: bool = False):
    """
    Lança um contexto de browser persistente para a plataforma.
    Tenta channel='chrome' primeiro (menos detecção), cai em Chromium puro.
    Retorna o BrowserContext (NÃO um Browser — feche com context.close()).
    """
    profile_dir = str(_browser_profile_dir(platform_key))
    common_args = {
        "user_data_dir": profile_dir,
        "headless":      headless,
        "args": [
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        "viewport": None,
    }
    try:
        return pw_instance.chromium.launch_persistent_context(
            **common_args, channel="chrome"
        )
    except Exception:
        return pw_instance.chromium.launch_persistent_context(**common_args)


def _inject_storage_state(context, storage_state: Optional[dict]) -> None:
    """
    Injeta localStorage e sessionStorage salvos em um contexto.
    storage_state é o dict retornado por context.storage_state().
    """
    if not storage_state:
        return

    try:
        # Injeta cookies
        cookies = storage_state.get("cookies", [])
        if cookies:
            context.add_cookies(cookies)
    except Exception:
        pass

    try:
        # Injeta localStorage
        origins_storage = storage_state.get("origins", [])
        for origin_data in origins_storage:
            origin = origin_data.get("origin", "")
            local_storage = origin_data.get("localStorage", [])
            if origin and local_storage:
                # Cria um script que injeta cada chave/valor no localStorage
                script = "".join([
                    f"window.localStorage.setItem({json.dumps(item['name'])}, {json.dumps(item['value'])});"
                    for item in local_storage
                ])
                if script:
                    try:
                        context.add_init_script(script)
                    except Exception:
                        pass
    except Exception:
        pass


def _extract_profile_fields_from_page(page) -> dict:
    """
    Extrai todos os campos de formulário visíveis da página.
    Faz scroll para garantir que todos os elementos são carregados.
    Retorna dict com campo_name: {type, label, placeholder, required, options, ...}
    """
    try:
        # Faz scroll para o topo
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        # Faz vários scrolls para garantir carregamento de elementos lazy
        for _ in range(5):
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(300)

        # Volta ao topo
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        # Extrai campos via JavaScript
        fields_data = page.evaluate("""
        () => {
            const fields = {};
            const seen = new Set();

            // Inputs, textareas, selects
            document.querySelectorAll('input, textarea, select').forEach(el => {
                if (!el.offsetParent && el.offsetParent !== null) return; // skip hidden

                const name = el.name || el.id || '';
                if (!name || seen.has(name)) return;
                seen.add(name);

                // Procura label associada
                let label = '';
                const labelEl = document.querySelector(`label[for="${el.id}"], label[for="${el.name}"]`);
                if (labelEl) label = labelEl.textContent.trim();
                if (!label && el.placeholder) label = el.placeholder;
                if (!label && el.parentElement) {
                    const parent_label = el.parentElement.textContent.split('\\n')[0].trim();
                    if (parent_label && parent_label.length < 100) label = parent_label;
                }

                const field_info = {
                    name: name,
                    type: el.tagName.toLowerCase(),
                    input_type: el.type || '',
                    label: label,
                    placeholder: el.placeholder || '',
                    required: el.required || el.getAttribute('required') ? true : false,
                    readonly: el.readOnly || el.getAttribute('readonly') ? true : false,
                    value: el.value || '',
                };

                // Se é select, pega as opções
                if (el.tagName.toLowerCase() === 'select') {
                    field_info.options = Array.from(el.options || []).map(opt => ({
                        value: opt.value,
                        text: opt.text
                    }));
                }

                // Se é radio/checkbox, tenta pegar grupo
                if (el.type === 'radio' || el.type === 'checkbox') {
                    const group = document.querySelectorAll(`[name="${name}"]`);
                    if (group.length > 1) {
                        field_info.options = Array.from(group).map(g => ({
                            value: g.value,
                            text: g.parentElement?.textContent.trim() || g.value
                        }));
                    }
                }

                fields[name] = field_info;
            });

            return fields;
        }
        """)

        return fields_data if fields_data else {}

    except Exception as exc:
        log_warn(f"Erro ao extrair campos da página: {exc}")
        return {}


def authenticate_sources(sources: list[str], rdb: "MongoManager") -> dict[str, list[dict]]:
    """
    Passo opcional entre select_sources() e scrape_sources().
    Abre um browser Chromium real para o usuário logar em cada fonte selecionada.
    Cookies capturados são salvos no MongoDB e reusados em execuções futuras.
    Retorna {source_key: [lista de dicts de cookie]} para as fontes autenticadas.
    """
    clr()
    section("Autenticação nas plataformas")

    # Apenas fontes selecionadas que têm URL de login
    auth_candidates = [s for s in sources if s in LOGIN_URLS]

    if not auth_candidates:
        print(f"  {_DIM}Nenhuma fonte selecionada requer autenticação — pulando etapa.{_RST}")
        time.sleep(1)
        return {}

    print(f"  {_DIM}Autenticar melhora os resultados em plataformas que restringem conteúdo.{_RST}")
    print(f"  {_DIM}Para cada plataforma marcada, um navegador será aberto para fazer login.{_RST}")
    print(f"  {_DIM}Plataformas já autenticadas reaplicam os cookies salvos automaticamente.{_RST}\n")

    # Monta choices indicando quais já têm cookies no MongoDB
    choices = []
    for key in auth_candidates:
        src     = SOURCES[key]
        existing    = rdb.load_auth_cookies(key)
        has_session = _has_browser_session(key)
        if has_session or existing:
            saved_at = (existing or {}).get("saved_at", "")[:10]
            lbl = f"{src['label']:<20}  ✔ já autenticado ({saved_at})"
        else:
            lbl = f"{src['label']:<20}  ○ não autenticado"
        choices.append(questionary.Choice(title=lbl, value=key, checked=False))

    chosen = questionary.checkbox(
        "Fazer login em quais plataformas? (ESPAÇO = marcar  |  ENTER = confirmar):",
        choices=choices,
        style=Q_STYLE,
    ).ask()

    if chosen is None:
        chosen = []

    # Tenta importar playwright — só se o usuário marcou algo
    pw = None
    if chosen:
        try:
            import playwright.sync_api as _pw
            pw = _pw
        except ImportError:
            log_err("playwright não está instalado.")
            log_info("Execute:  pip install playwright && playwright install chromium")
            input(f"\n  {_DIM}ENTER para continuar sem autenticação...{_RST}")
            chosen = []

    result: dict[str, list[dict]] = {}

    # Carrega cookies existentes para fontes NÃO re-autenticadas
    for key in auth_candidates:
        if key not in chosen:
            existing = rdb.load_auth_cookies(key)
            if existing:
                result[key] = existing["cookies"]

    # Abre browser para cada fonte que o usuário marcou
    for key in chosen:
        src       = SOURCES[key]
        login_url = LOGIN_URLS[key]

        clr()
        section(f"Login — {src['label']}")
        print(f"\n  {_CY}Abrindo navegador em:{_RST}  {login_url}")
        print(f"  {_DIM}Faça o login normalmente no navegador.{_RST}")
        print(f"  {_DIM}A sessão será salva automaticamente no perfil do browser.{_RST}")
        print(f"  {_DIM}Quando terminar, volte aqui e pressione {_BD}ENTER{_RST}{_DIM}.{_RST}\n")

        try:
            with pw.sync_playwright() as p:
                context = _launch_persistent(p, key, headless=False)
                page    = context.new_page()
                page.goto(login_url, timeout=60_000)

                input(f"  Pressione ENTER após completar o login em {_BD}{src['label']}{_RST}... ")

                # Extrai storage_state completo (cookies + localStorage + sessionStorage)
                storage_state = context.storage_state()
                all_cookies = context.cookies()
                context.close()  # fecha o contexto (salva estado no disco)

            # Salva ambos: storage_state no banco e cookies também
            rdb.save_storage_state(key, storage_state)
            rdb.save_auth_cookies(key, all_cookies)
            result[key] = all_cookies
            log_ok(f"{src['label']}: sessão completa salva (storage_state + {len(all_cookies)} cookies)")

        except Exception as exc:
            log_err(f"Erro ao autenticar {src['label']}: {exc}")

    clr()
    return result


def scrape_sources(
    query:        str,
    location:     str,
    sources:      list[str],
    max_pages:    int,
    rdb:          "MongoManager",
    show_browser: bool = False,        # mantido por compatibilidade, não usado
    recency:      str  = "any",
    auth_cookies: "Optional[dict]" = None,
) -> int:
    clr()
    section("Coletando vagas")
    log_info(f"Fontes:    {_BD}{', '.join(sources)}{_RST}")
    log_info(f"Recência:  {_BD}{recency}{_RST}")
    log_info(f"Páginas:   {'todas' if max_pages == 0 else max_pages} por fonte")
    log_info("Motor:     curl_cffi (TLS fingerprint Chrome 124)")
    print(f"  {_DIM}Pressione {_BD}[Q]{_RST}{_DIM} ou {_BD}[ESC]{_RST}{_DIM} a qualquer momento para parar a busca{_RST}\n")

    # ── Filtro de relevância: extrai keywords da query ────────────────────────
    _set_active_keywords(query)
    if _ACTIVE_KEYWORDS:
        log_info(f"Filtro:    {_BD}{', '.join(_ACTIVE_KEYWORDS)}{_RST}  (título deve conter ao menos 1)")

    session = _new_session()

    # Aplica cookies de autenticação ao session (domain-scoped pelo cookiejar)
    if auth_cookies:
        for _src_key, _cookies in auth_cookies.items():
            if _src_key in sources:
                for _ck in _cookies:
                    try:
                        session.cookies.set(
                            _ck["name"],
                            _ck["value"],
                            domain=_ck.get("domain", ""),
                            path=_ck.get("path", "/"),
                        )
                    except Exception:
                        pass

    # ── Detecta fontes que exigem login mas não têm sessão ───────────────────
    _needs_login = [
        key for key in sources
        if SOURCES.get(key, {}).get("login_required")
        and not (auth_cookies or {}).get(key)
        and not _has_browser_session(key)
    ]

    if _needs_login:
        print(f"\n  {_Y}{_BD}⚠  Login necessário{_RST}")
        print(f"  As seguintes plataformas precisam de autenticação para mostrar vagas:")
        for _lk in _needs_login:
            _lsrc = SOURCES.get(_lk, {})
            print(f"    {_DIM}•  {_lsrc.get('label', _lk)}{_RST}")
        print(f"\n  {_DIM}Sem login, essas fontes serão puladas automaticamente.{_RST}\n")

        _do_login = questionary.confirm(
            "  Deseja fazer login agora?",
            default=True,
            style=Q_STYLE,
        ).ask()

        if _do_login:
            for _lk in list(_needs_login):
                _lsrc   = SOURCES.get(_lk, {})
                _llabel = _lsrc.get("label", _lk)
                _lurl   = LOGIN_URLS.get(_lk)
                if not _lurl:
                    log_warn(f"URL de login não encontrada para {_llabel} — pulando")
                    continue
                clr()
                print(f"  {_CY}{_BD}Login: {_llabel}{_RST}\n")
                print(f"  {_DIM}Abrindo navegador para login em {_llabel}...{_RST}")
                try:
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as _pw:
                        _ctx = _launch_persistent(_pw, _lk, headless=False)
                        _pg  = _ctx.new_page()
                        _pg.goto(_lurl, timeout=60_000)
                        print(f"\n  {_CY}Navegador aberto — faça login em {_llabel}{_RST}")
                        print(f"  {_DIM}Quando terminar o login, volte aqui e pressione ENTER.{_RST}\n")
                        input(f"  Pressione {_BD}ENTER{_RST} para continuar após o login...")
                        _new_ck  = _ctx.cookies()
                        _storage = _ctx.storage_state()
                        _ctx.close()
                    rdb.save_auth_cookies(_lk, _new_ck)
                    rdb.save_storage_state(_lk, _storage)
                    if auth_cookies is None:
                        auth_cookies = {}
                    auth_cookies[_lk] = _new_ck
                    # Injeta novas cookies na sessão HTTP
                    for _ck in _new_ck:
                        try:
                            session.cookies.set(
                                _ck["name"], _ck["value"],
                                domain=_ck.get("domain", ""),
                                path=_ck.get("path", "/"),
                            )
                        except Exception:
                            pass
                    log_ok(f"{_llabel}: sessão salva ({len(_new_ck)} cookies)")
                except ImportError:
                    log_warn("Playwright não instalado — instale com: pip install playwright && playwright install chromium")
                except Exception as _le:
                    log_warn(f"Erro ao fazer login em {_llabel}: {_le}")
        else:
            # Remove fontes que precisam de login da lista — scraping continua sem elas
            sources = [s for s in sources if s not in _needs_login]
            if not sources:
                log_warn("Nenhuma fonte disponível sem exigência de login")
                return 0

        clr()
        section("Coletando vagas")

    # ── Inicia thread listener de parada ─────────────────────────────────────
    _STOP_SCRAPE.clear()
    _SCRAPE_ACTIVE.set()
    listener = threading.Thread(target=_scrape_key_listener, daemon=True)
    listener.start()

    total   = 0

    _dispatch = {
        "indeed":         lambda src, sk: _scrape_indeed_source(
                            session, src["domain"], src["label"], query, location, max_pages, rdb, recency),
        "linkedin":       lambda src, sk: _scrape_linkedin_source(
                            session, query, location, max_pages, rdb, recency),
        "linkedin-br":    lambda src, sk: _scrape_linkedin_source(
                            session, query, "", max_pages, rdb, recency,
                            geo_id="106057199"),
        "linkedin-intl":  lambda src, sk: _scrape_linkedin_source(
                            session, query, "Remote", max_pages, rdb, recency),
        "linkedin-feed":  lambda src, sk: _scrape_linkedin_feed_source(
                            session, query, max_pages, recency, rdb,
                            scope="brasil" if "linkedin-br" in sources else "ambos"),
        "gupy":           lambda src, sk: _scrape_gupy_source(
                            session, query, max_pages, recency, rdb),
        "vagas":          lambda src, sk: _scrape_vagas_source(
                            session, query, max_pages, recency, rdb),
        "remoteok":       lambda src, sk: _scrape_remoteok_source(
                            session, query, max_pages, recency, rdb),
        "himalayas":      lambda src, sk: _scrape_himalayas_source(
                            session, query, max_pages, recency, rdb),
        "weworkremotely": lambda src, sk: _scrape_weworkremotely_source(
                            session, query, max_pages, recency, rdb),
        "programathor":   lambda src, sk: _scrape_programathor_source(session, query, max_pages, recency, rdb),
        "geekHunter":     lambda src, sk: _scrape_geekHunter_source(session, query, max_pages, recency, rdb),
        "catho":          lambda src, sk: _scrape_catho_source(session, query, max_pages, recency, rdb),
        "infojobs":       lambda src, sk: _scrape_infojobs_source(session, query, max_pages, recency, rdb),
        "impulso":        lambda src, sk: _scrape_impulso_source(session, query, max_pages, recency, rdb),
        "remotar":        lambda src, sk: _scrape_remotar_source(session, query, max_pages, recency, rdb),
        "revelo":         lambda src, sk: _scrape_revelo_source(session, query, max_pages, recency, rdb),
        "workana":        lambda src, sk: _scrape_workana_source(session, query, max_pages, recency, rdb),
        "99freelas":      lambda src, sk: _scrape_99freelas_source(session, query, max_pages, recency, rdb),
        "turing":         lambda src, sk: _scrape_turing_source(session, query, max_pages, recency, rdb),
        "toptal":         lambda src, sk: _scrape_toptal_source(session, query, max_pages, recency, rdb),
        "upwork":         lambda src, sk: _scrape_upwork_source(session, query, max_pages, recency, rdb),
        "glassdoor":      lambda src, sk: _scrape_glassdoor_source(session, query, max_pages, recency, rdb),
        "ziprecruiter":   lambda src, sk: _scrape_ziprecruiter_source(session, query, max_pages, recency, rdb),
        "careerjet":      lambda src, sk: _scrape_careerjet_source(session, query, max_pages, recency, rdb),
        "jora":           lambda src, sk: _scrape_jora_source(session, query, max_pages, recency, rdb),
    }

    try:
        for source_key in sources:
            if _scrape_aborted():
                log_warn(f"{_Y}⏹  Busca interrompida — fontes restantes ignoradas{_RST}")
                break
            src = SOURCES.get(source_key)
            if not src:
                log_warn(f"Fonte desconhecida: {source_key}"); continue
            fn = _dispatch.get(src["type"])
            if not fn:
                log_warn(f"Dispatcher não implementado: {src['type']}"); continue

            sp = Spinner(f"Buscando em {src['label']}...").start() if not _VERBOSE else None
            n  = fn(src, source_key)
            if sp: sp.stop()
            log_ok(f"{src['label']}: {n} vaga{'s' if n != 1 else ''} encontrada{'s' if n != 1 else ''}")
            total += n
    finally:
        # ── Encerra listener independente do que aconteceu ────────────────────
        _SCRAPE_ACTIVE.clear()
        listener.join(timeout=1.0)

    if _scrape_aborted():
        print(f"\n  {_Y}{_BD}⏹  Busca interrompida.{_RST}")
        if total:
            print(f"  {_DIM}{total} vaga(s) coletadas — seguindo para avaliação...{_RST}")
        else:
            print(f"  {_DIM}Nenhuma vaga coletada até o momento.{_RST}")
        time.sleep(1.5)
    elif total:
        log_ok(f"Total geral enfileirado: {_BD}{total}{_RST} vagas")
    else:
        log_err("Nenhuma vaga coletada em nenhuma fonte")

    return total


# ──────────────────────────────────────────────────────────────────────────────
# 4. Perfil do candidato com IA
# ──────────────────────────────────────────────────────────────────────────────

def _profile_to_ai_summary(profile: dict) -> dict:
    """
    Converte o perfil completo em um dict enxuto para uso no prompt de matching de vagas.
    Mantém compatibilidade com o formato esperado por evaluate_batch.
    """
    tech   = profile.get("technical", {})
    skills = list({
        t for lst in tech.values() if isinstance(lst, list) for t in lst
    })
    return {
        "skills":           profile.get("top_technologies", skills),
        "experience_years": profile.get("professional", {}).get("experience_years", 0),
        "seniority":        profile.get("professional", {}).get("seniority", ""),
        "roles":            [e.get("role", "") for e in profile.get("experience", [])[:3]],
        "languages":        [
            f"{l.get('language','')} ({l.get('level','')})"
            for l in profile.get("languages", [])
        ],
        "highlights":       profile.get("highlights", []),
    }


def _profile_to_menu_hints(profile: dict) -> dict:
    """
    Converte o perfil completo em hints validados para os menus de seleção
    (substitui a chamada a analyze_resume_for_selection).
    """
    # Valida main_stack contra STACK_KEYS
    raw_stack = profile.get("main_stack", "")
    stack_key = STACK_KEYS.get(raw_stack)
    if not stack_key:
        stack_key = next(
            (v for k, v in STACK_KEYS.items() if k.lower() in raw_stack.lower()),
            list(STACK_KEYS.values())[0] if STACK_KEYS else None,
        )

    # Filtra top_technologies para as que existem na stack escolhida
    valid_techs    = set(STACKS.get(stack_key, {}).get("techs", [])) if stack_key else set()
    top_techs      = profile.get("top_technologies", [])
    filtered_techs = [t for t in top_techs if t in valid_techs]

    # Valida inglês
    english_options = [lv for lv, _ in ENGLISH_LEVELS]
    english = profile.get("english_level", "B1")
    if english not in english_options:
        english = "B1"

    return {"stack": stack_key, "technologies": filtered_techs, "english_level": english}


def _display_profile(profile: dict) -> None:
    """Exibe o perfil completo do candidato de forma rica e legível."""
    personal     = profile.get("personal", {})
    professional = profile.get("professional", {})
    technical    = profile.get("technical", {})
    cols         = min(shutil.get_terminal_size((80, 24)).columns, 90)
    thin         = f"{_DIM}{'╌' * cols}{_RST}"

    # ── Cabeçalho pessoal ─────────────────────────────────────────────────────
    name = personal.get("name") or "Candidato"
    loc  = personal.get("location") or ""
    print(f"\n  {_BD}{_CY}{name}{_RST}" + (f"  {_DIM}—  {loc}{_RST}" if loc else ""))

    role = professional.get("current_role", "")
    exp  = professional.get("experience_years", "")
    sen  = professional.get("seniority", "")
    sub  = "  ·  ".join(p for p in [role, f"{exp} anos" if exp else "", sen] if p)
    if sub:
        print(f"  {_DIM}{sub}{_RST}")

    # Links de contato
    links = []
    if personal.get("email"):     links.append(personal["email"])
    if personal.get("linkedin"):  links.append(f"linkedin: {personal['linkedin']}")
    if personal.get("github"):    links.append(f"github: {personal['github']}")
    if personal.get("portfolio"): links.append(personal["portfolio"])
    if links:
        print(f"  {_DIM}{' │ '.join(links)}{_RST}")

    # Objetivo
    obj = professional.get("objective")
    if obj:
        print(f"\n  {_DIM}💡 {obj}{_RST}")

    print(f"\n{thin}")

    # ── Stack e tecnologias ───────────────────────────────────────────────────
    main_stack = profile.get("main_stack", "")
    top_techs  = profile.get("top_technologies", [])
    if main_stack:
        log_ok(f"Stack principal:    {_BD}{main_stack}{_RST}")
    if top_techs:
        log_ok(f"Top tecnologias:    {', '.join(top_techs)}")

    print()
    tech_labels = [
        ("programming_languages", "Linguagens"),
        ("frameworks_libs",       "Frameworks / Libs"),
        ("databases",             "Databases"),
        ("cloud_infra",           "Cloud / Infra"),
        ("devops_tools",          "DevOps"),
        ("testing",               "Testes"),
        ("other_tools",           "Outras ferramentas"),
    ]
    for key, label in tech_labels:
        items = technical.get(key, [])
        if items:
            log_info(f"{label+':':<22} {_DIM}{', '.join(items)}{_RST}")

    print(f"\n{thin}")

    # ── Idiomas ───────────────────────────────────────────────────────────────
    langs = profile.get("languages", [])
    if langs:
        print(f"  {_BD}🌐  Idiomas{_RST}")
        for l in langs:
            print(f"     {_DIM}•  {l.get('language','')}  —  {_BD}{l.get('level','')}{_RST}")
        print()

    # ── Formação ──────────────────────────────────────────────────────────────
    education = profile.get("education", [])
    if education:
        print(f"  {_BD}🎓  Formação{_RST}")
        for e in education:
            yr   = f" ({e['year']})" if e.get("year") else ""
            stat = f"  {_DIM}{e['status']}{_RST}" if e.get("status") else ""
            print(f"     {_DIM}•  {_BD}{e.get('degree','')}{_RST}{_DIM}  —  {e.get('institution','')}{yr}{_RST}{stat}")
        print()

    # ── Experiência ───────────────────────────────────────────────────────────
    experience = profile.get("experience", [])
    if experience:
        print(f"  {_BD}💼  Experiência{_RST}")
        for exp_item in experience:
            print(
                f"     {_DIM}•  {_BD}{exp_item.get('role','')}{_RST}"
                f"  @  {_BD}{exp_item.get('company','')}{_RST}"
                f"  {_DIM}{exp_item.get('period','')}{_RST}"
            )
            for h in exp_item.get("highlights", [])[:2]:
                print(f"          {_DIM}→  {h}{_RST}")
        print()

    # ── Certificações ─────────────────────────────────────────────────────────
    certs = profile.get("certifications", [])
    if certs:
        print(f"  {_BD}📜  Certificações{_RST}")
        for c in certs:
            yr = f" ({c['year']})" if c.get("year") else ""
            print(f"     {_DIM}•  {c.get('name','')}  —  {c.get('issuer','')}{yr}{_RST}")
        print()

    # ── Destaques + soft skills ───────────────────────────────────────────────
    print(f"{thin}")
    highlights = profile.get("highlights", [])
    if highlights:
        print(f"  {_BD}✨  Destaques{_RST}")
        for h in highlights:
            print(f"     {_G}•{_RST}  {h}")
        print()

    soft = profile.get("soft_skills", [])
    if soft:
        print(f"  {_DIM}Soft skills: {', '.join(soft)}{_RST}")

    print()


def extract_full_profile(
    client:      "Groq",
    resume_text: str,
    resume_hash: str,
    rdb:         "MongoManager",
    quiet:       bool = False,
) -> Optional[dict]:
    """
    Extrai perfil completo e estruturado do currículo via IA e persiste no MongoDB.
    Se já existe um perfil salvo para este hash de currículo, carrega do banco
    sem chamar a API (evita consumir tokens desnecessariamente).
    Retorna None apenas se a API estiver indisponível.
    quiet=True suprime cabeçalhos de seção e display do perfil (usado na inicialização).
    """
    # ── 1. Tenta carregar do banco ────────────────────────────────────────────
    cached = rdb.load_profile(resume_hash)
    if cached:
        if not quiet:
            clr()
            section("Perfil do candidato")
            log_ok(f"Perfil carregado do banco  {_DIM}(currículo inalterado){_RST}")
            _display_profile(cached)
        return cached

    # ── 2. Extrai com IA ──────────────────────────────────────────────────────
    if not quiet:
        clr()
        section("Analisando currículo com IA")
        log_info("Extraindo perfil estruturado completo — aguarde alguns segundos...")
    sp = Spinner("Extraindo perfil com IA — pode levar alguns segundos...").start() if (not _VERBOSE and not quiet) else None

    stack_options   = list(STACK_KEYS.keys())
    english_options = [lv for lv, _ in ENGLISH_LEVELS]

    prompt = (
        "Analise o currículo abaixo e extraia o perfil estruturado completo em JSON.\n"
        "Retorne APENAS JSON válido, sem markdown, sem explicações.\n\n"
        "Formato esperado:\n"
        "{\n"
        '  "personal": {\n'
        '    "name": "nome completo ou null",\n'
        '    "email": "email ou null",\n'
        '    "phone": "telefone ou null",\n'
        '    "location": "cidade, estado/país ou null",\n'
        '    "linkedin": "URL completa ou null",\n'
        '    "github": "URL completa ou null",\n'
        '    "portfolio": "URL ou null"\n'
        "  },\n"
        '  "professional": {\n'
        '    "seniority": "junior|mid|senior|lead|principal",\n'
        '    "experience_years": número inteiro,\n'
        '    "current_role": "cargo atual ou mais recente",\n'
        '    "objective": "objetivo profissional em 1 frase ou null"\n'
        "  },\n"
        '  "technical": {\n'
        '    "programming_languages": ["lista de linguagens"],\n'
        '    "frameworks_libs": ["lista de frameworks e bibliotecas"],\n'
        '    "databases": ["lista de bancos de dados"],\n'
        '    "cloud_infra": ["lista: AWS, GCP, Azure, Docker, K8s, etc."],\n'
        '    "devops_tools": ["lista: CI/CD, Git, Jenkins, etc."],\n'
        '    "testing": ["lista: Jest, Cypress, pytest, etc."],\n'
        '    "other_tools": ["lista: Figma, Jira, etc."]\n'
        "  },\n"
        '  "languages": [\n'
        '    {"language": "nome do idioma", "level": "Nativo|C2|C1|B2|B1|A2|A1"}\n'
        "  ],\n"
        '  "education": [\n'
        '    {"degree": "nome do curso", "institution": "nome", "year": ano_int_ou_null, "status": "concluído|em andamento|incompleto"}\n'
        "  ],\n"
        '  "experience": [\n'
        '    {"company": "nome", "role": "cargo", "period": "YYYY – YYYY ou YYYY – presente", "highlights": ["realização 1", "realização 2"]}\n'
        "  ],\n"
        '  "certifications": [\n'
        '    {"name": "nome", "issuer": "emissor", "year": ano_int_ou_null}\n'
        "  ],\n"
        '  "soft_skills": ["lista de soft skills identificadas"],\n'
        '  "highlights": ["5 pontos mais fortes do perfil como recrutador veria"],\n'
        f'  "main_stack": "uma destas opções exatas: {stack_options}",\n'
        '  "top_technologies": ["as 10 tecnologias mais relevantes do perfil"],\n'
        f'  "english_level": "um destes: {english_options}"\n'
        "}\n\n"
        f"CURRÍCULO:\n{resume_text[:5000]}"
    )

    try:
        resp = client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=1800,
            messages=[
                {"role": "system", "content": "Responda APENAS com JSON válido, sem markdown."},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        profile = json.loads(raw)
        profile["resume_hash"]  = resume_hash
        profile["extracted_at"] = datetime.now().isoformat()

        rdb.save_profile(profile)
        if sp: sp.stop()
        if not quiet:
            log_ok("Perfil extraído e salvo.")
            clr()
            section("Perfil do candidato")
            _display_profile(profile)
        return profile

    except json.JSONDecodeError as exc:
        if sp: sp.fail()
        log_warn(f"Resposta da IA não é JSON válido ({exc}) — modo limitado ativado.")
        return None

    except Exception as exc:
        err_str = str(exc)
        if sp: sp.fail()
        log_err(f"Falha ao extrair perfil: {exc}")
        wait_match = re.search(r"try again in\s+([\dm.s]+)", err_str, re.IGNORECASE)
        if wait_match:
            log_warn(f"Rate limit — tente novamente em: {_BD}{wait_match.group(1)}{_RST}")
        elif "429" in err_str or "rate_limit" in err_str.lower():
            log_warn("Rate limit da API Groq atingido — aguarde alguns minutos.")
        log_info("Modo limitado: revisão de vagas mapeadas ainda disponível.")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 5. Avaliação de aderência em lote
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_batch(
    client:       Groq,
    profile_json: str,
    jobs:         list[dict],
    prefs:        Optional[dict] = None,
) -> list[dict]:
    if not jobs:
        return []

    prefs = prefs or {}
    modality_map  = {"remoto": "remoto", "presencial": "presencial", "hibrido": "híbrido", "todos": "qualquer"}
    contract_map  = {"pj": "PJ", "clt": "CLT", "autonomo": "autônomo", "todos": "qualquer"}

    # Tecnologias que o candidato selecionou explicitamente para esta busca
    search_techs = prefs.get("search_techs", [])
    techs_context = (
        f"Tecnologias que o candidato selecionou para esta busca: {', '.join(search_techs)}. "
        "Priorize vagas que usam essas tecnologias. "
    ) if search_techs else ""

    prefs_context = (
        f"{techs_context}"
        f"Preferências: "
        f"modalidade={modality_map.get(prefs.get('modality','todos'), 'qualquer')} | "
        f"contrato={contract_map.get(prefs.get('contract','todos'), 'qualquer')} | "
        f"inglês={prefs.get('english_level','não informado')} | "
        f"localização={prefs.get('location_scope','ambos')}. "
        "Penalize vagas que não atendam às preferências de modalidade/contrato. "
        "Se a vaga exige inglês acima do nível informado, sinalize como gap."
    )

    jobs_text = ""
    for i, job in enumerate(jobs):
        desc = job.get("description") or job.get("snippet") or "Sem descrição"
        jobs_text += (
            f"\n--- VAGA {i + 1} ---\n"
            f"Título:  {job['title']}\n"
            f"Empresa: {job['company']}\n"
            f"Local:   {job.get('location', '')}\n"
            f"Região:  {job['region']}\n"
            f"Desc:    {desc[:1200]}\n"
        )

    try:
        completion = client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=2500,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é um especialista em recrutamento tech. "
                        "Avalie a compatibilidade real entre o perfil do candidato e cada vaga, "
                        "com base nas habilidades, experiências e tecnologias descritas. "
                        "Seja preciso: score 80+ significa boa compatibilidade real, não perfeição. "
                        "Responda APENAS com JSON array válido, sem texto adicional."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Avalie a compatibilidade entre o perfil e cada vaga abaixo.\n\n"
                        f"PERFIL DO CANDIDATO:\n{profile_json}\n\n"
                        f"CONTEXTO DA BUSCA:\n{prefs_context}\n\n"
                        f"VAGAS:\n{jobs_text}\n\n"
                        "Para cada vaga, retorne um JSON array com os campos:\n"
                        "  index: número da vaga (1-based)\n"
                        "  score: 0-100 (compatibilidade real — considere tecnologias, experiência, nível)\n"
                        "  match_reasons: lista com até 3 pontos fortes da compatibilidade\n"
                        "  gap_reasons: lista com até 2 pontos de atenção ou requisitos não atendidos\n"
                        "  summary: resumo em ≤80 chars do fit candidato×vaga\n"
                        "Retorne APENAS o JSON array."
                    ),
                },
            ],
        )
        raw = completion.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        evaluations = json.loads(raw)

        results = []
        for ev in evaluations:
            idx = ev.get("index", 0) - 1
            if 0 <= idx < len(jobs):
                job_copy = jobs[idx].copy()
                job_copy["score"]         = ev.get("score", 0)
                job_copy["match_reasons"] = ev.get("match_reasons", [])
                job_copy["gap_reasons"]   = ev.get("gap_reasons", [])
                job_copy["ai_summary"]    = ev.get("summary", "")
                results.append(job_copy)
        return results

    except Exception as exc:
        exc_str = str(exc)
        # Erros de autenticação (401) não têm sentido tentar de novo — propaga
        if (
            "401" in exc_str
            or "invalid_api_key" in exc_str
            or "Invalid API Key" in exc_str
            or "authentication" in exc_str.lower()
        ):
            raise
        log_err(f"Falha ao avaliar lote: {exc}")
        return []


# ──────────────────────────────────────────────────────────────────────────────
# 5b. Enriquecimento de descrições
# ──────────────────────────────────────────────────────────────────────────────

def _enrich_descriptions(jobs: list[dict], session: "cf_requests.Session") -> list[dict]:
    """
    Para vagas com description vazia, busca a página de detalhe e extrai:
      - description (texto completo da vaga)
      - applicants  (quantidade de candidatos, ex: "Mais de 200 candidatos")
      - easy_apply  (True se candidatura simplificada pelo LinkedIn)
    Suporta LinkedIn, Indeed e qualquer fonte que salve o link.
    """
    needs_fetch = [j for j in jobs if not (j.get("description") or "").strip()]
    if not needs_fetch:
        return jobs

    log_info(f"Buscando descrições e metadados: {len(needs_fetch)} vaga(s)...")

    for job in needs_fetch:
        link = job.get("link", "")
        if not link:
            continue

        try:
            soup = _get_soup(session, link)
            if not soup:
                continue

            def _to_plain(html_str: str) -> str:
                """Converte HTML para texto puro, lidando com dupla codificação."""
                text = BeautifulSoup(html_str, "html.parser").get_text(" ", strip=True)
                # Caso ainda restem tags (encoding duplo), remove com regex
                if "<" in text:
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s{2,}", " ", text).strip()
                return text

            desc = ""

            # 1. JSON-LD (mais confiável — dados estruturados)
            ld = _extract_jsonld(soup)
            if ld.get("description"):
                desc = _to_plain(ld["description"])

            # 2. Seletores HTML específicos por plataforma
            if not desc:
                desc = _soup_text(
                    soup,
                    # LinkedIn
                    ".show-more-less-html__markup",
                    ".description__text",
                    # Indeed
                    "#jobDescriptionText",
                    ".jobsearch-jobDescriptionText",
                    # Gupy / genérico
                    "[data-cy='job-description']",
                    ".job-description",
                    "section.description",
                    # Fallback amplo
                    "article",
                    "main",
                )

            if desc:
                job["description"] = desc[:3000]
                log_scrape(f"      {CHECK} Descrição obtida: {job.get('title','')[:50]}")
            else:
                log_info(f"Sem descrição extraível: {link[:70]}")

            # ── Metadados extras (LinkedIn) ───────────────────────────────────
            is_linkedin = "linkedin.com" in link

            if is_linkedin:
                # Contagem de candidatos
                if not job.get("applicants"):
                    appl_el = soup.select_one(
                        ".num-applicants__caption, "
                        ".jobs-unified-top-card__applicant-count, "
                        "[class*='applicant-count'], "
                        "[class*='num-applicants']"
                    )
                    if appl_el:
                        job["applicants"] = appl_el.get_text(strip=True)

                # Easy Apply (candidatura simplificada dentro do LinkedIn)
                if not job.get("easy_apply"):
                    easy_btn = soup.select_one(
                        ".jobs-apply-button--top-card, "
                        "[aria-label*='Easy Apply'], "
                        "[aria-label*='Candidatura simplificada'], "
                        "button.jobs-apply-button"
                    )
                    if easy_btn:
                        btn_text = easy_btn.get_text(strip=True).lower()
                        job["easy_apply"] = (
                            "easy apply" in btn_text
                            or "simplificada" in btn_text
                            or "candidatura fácil" in btn_text
                        )

            time.sleep(random.uniform(0.5, 1.5))   # intervalo curto — evita bloqueio

        except Exception as exc:
            log_warn(f"Erro ao buscar descrição ({link[:60]}): {exc}")

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# 6. Processamento da fila
# ──────────────────────────────────────────────────────────────────────────────

def process_queue(
    client:       Groq,
    profile_json: str,
    rdb:          "MongoManager",
    prefs:        Optional[dict] = None,
    batch_size:   int = 10,
) -> None:
    clr()
    section("Avaliando vagas")
    total = rdb.queue_size()

    if total == 0:
        log_warn("Fila vazia — nenhuma vaga para avaliar.")
        return

    log_info(f"{total} vagas na fila  |  lotes de {batch_size}")

    # Sessão HTTP reutilizada para buscar descrições faltantes
    enrich_session = _new_session()

    # Spinner global para modo discreto (atualizado a cada lote)
    sp = Spinner(f"Avaliando vagas: 0/{total}...").start() if not _VERBOSE else None

    lote_num  = 0
    done_count= 0
    while rdb.queue_size() > 0:
        batch: list[dict] = []
        for _ in range(batch_size):
            job = rdb.pop_job()
            if job is None:
                break
            batch.append(job)

        if not batch:
            break

        lote_num  += 1
        remaining  = rdb.queue_size()
        log_info(f"Lote {lote_num}  ({len(batch)} vagas)  —  {remaining} restantes na fila")
        if sp: sp.update(f"Buscando descrições e avaliando: {done_count}/{total} — lote {lote_num}...")

        # ── Enriquece vagas que chegaram sem descrição (ex: LinkedIn guest API) ──
        batch = _enrich_descriptions(batch, enrich_session)

        try:
            evaluated = evaluate_batch(client, profile_json, batch, prefs)
        except Exception as _auth_exc:
            if sp: sp.stop()
            log_err(f"Chave de API inválida ou expirada — avaliação cancelada")
            log_warn("Acesse Configurações → IA para atualizar a GROQ_API_KEY")
            log_warn(f"Detalhe: {_auth_exc}")
            # Descarta o restante da fila para não travar o fluxo
            while rdb.queue_size() > 0:
                rdb.pop_job()
            return

        # Salva resultados e exibe score em tempo real (verbose) ou acumula (discreto)
        for job in batch:
            jid        = job["job_id"]
            matched_ev = next((e for e in evaluated if e["job_id"] == jid), None)
            if matched_ev:
                rdb.save_result(matched_ev)
                if _VERBOSE:
                    score     = matched_ev.get("score", 0)
                    sym       = CHECK if score >= MIN_MATCH_SCORE else CROSS
                    score_col = _G if score >= MIN_MATCH_SCORE else (_Y if score >= 60 else _R)
                    bar       = f"{_G}" + "█" * (score // 10) + f"{_DIM}" + "░" * (10 - score // 10) + _RST
                    summary   = matched_ev.get("ai_summary", "")
                    print(
                        f"    {sym} [{bar}] {score_col}{_BD}{score:3d}%{_RST}  "
                        f"{job['title'][:45]}  {_DIM}[{job['region']}]{_RST}"
                    )
                    if summary:
                        print(f"         {_DIM}{summary}{_RST}")
            else:
                rdb.save_error(job, "não retornado na avaliação do lote")
                if _VERBOSE:
                    print(f"    {CROSS}  ---  {job['title'][:50]}  {_DIM}[sem avaliação]{_RST}")
            done_count += 1
            if sp: sp.update(f"Avaliando vagas: {done_count}/{total} — lote {lote_num}...")

        time.sleep(0.5)

    if sp: sp.stop()
    stats = rdb.get_stats()
    log_ok(
        f"Avaliação concluída  |  "
        f"avaliadas: {stats.get('evaluated', 0)}  |  "
        f"com match: {_G}{_BD}{stats.get('matched', 0)}{_RST}  |  "
        f"erros: {stats.get('errors', 0)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 7. Exibição dos resultados
# ──────────────────────────────────────────────────────────────────────────────

def print_results(matched_jobs: list[dict]) -> None:
    clr()
    section(f"Resultados — {len(matched_jobs)} vaga(s) com aderência ≥ {MIN_MATCH_SCORE}%")

    if not matched_jobs:
        log_warn("Nenhuma vaga com aderência suficiente.")
        log_info("Dica: tente outros termos de busca ou aumente --max-pages.")
        return

    for i, job in enumerate(matched_jobs, 1):
        score = job.get("score", 0)
        bar   = f"{_G}" + "█" * (score // 10) + f"{_RST}{_DIM}" + "░" * (10 - score // 10) + _RST
        color = _G if score >= 90 else (_Y if score >= 80 else _R)

        print(f"\n  {CHECK}  #{i}  {_BD}{job['title']}{_RST}  —  {job['company']}")
        print(f"       {job.get('location', '')}  [{_CY}{job['region']}{_RST}]")
        print(f"\n       Aderência: [{bar}] {color}{_BD}{score}%{_RST}")
        print(f"       {_DIM}{job.get('ai_summary', '')}{_RST}")

        if job.get("match_reasons"):
            print(f"\n       {_G}Pontos de match:{_RST}")
            for r in job["match_reasons"]:
                print(f"         • {r}")

        if job.get("gap_reasons"):
            print(f"\n       {_Y}Pontos de atenção:{_RST}")
            for g in job["gap_reasons"]:
                print(f"         • {g}")

        print(f"\n       {_B}🔗 {job['link']}{_RST}")

    print(f"\n{_BD}{_CY}{'─' * 60}{_RST}")


# Resultados persistidos exclusivamente no MongoDB (collection jobs).


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Menus interativos
# ──────────────────────────────────────────────────────────────────────────────

def _abort_if_none(value, label: str = "seleção"):
    if value is None:
        raise UserAbort(label)
    return value


def select_sources(defaults: Optional[list[str]] = None) -> list[str]:
    """
    Menu multi-seleção de plataformas agrupadas por região.
    defaults: lista de source_keys a pré-marcar (vinda de um preset ao editar).
    Retorna lista de source_keys selecionados.
    """
    clr()
    section("Onde deseja buscar as vagas?")
    print(f"  {_DIM}ESPAÇO para marcar/desmarcar  |  ENTER para confirmar  |  Ctrl+C para voltar{_RST}")
    if defaults:
        print(f"  {_CY}Pré-selecionado do preset anterior:{_RST} {', '.join(defaults)}\n")
    else:
        print()

    # Defaults padrão quando não vem de edição
    # Migração: presets salvos com "linkedin" passam a usar "linkedin-br"
    _raw_defaults = set(defaults) if defaults else {"indeed-br", "gupy", "remoteok", "linkedin-br"}
    if "linkedin" in _raw_defaults:
        _raw_defaults.discard("linkedin")
        _raw_defaults.add("linkedin-br")
    _defaults: set[str] = _raw_defaults

    # Ícone de status do LinkedIn Feed (sem ANSI — questionary não renderiza)
    li_at_ok = bool(os.environ.get("LINKEDIN_LI_AT","").strip())
    feed_label = (
        "LinkedIn Feed  [li_at ok]"
        if li_at_ok
        else "LinkedIn Feed  [requer LINKEDIN_LI_AT]"
    )

    def _chk(key: str) -> bool:
        if defaults is not None:
            return key in _defaults
        return key in {"indeed-br", "gupy", "linkedin-br", "programathor", "geekHunter", "remoteok"}

    _fonte_choices = [
        # ── Brasil — Geral ──────────────────────────────────────────────────────
        questionary.Choice(title="── Brasil — Geral ───────────", value="__sep_br__",       disabled=""),
        questionary.Choice("Indeed BR       (br.indeed.com)",    value="indeed-br",          checked=_chk("indeed-br")),
        questionary.Choice("InfoJobs        (infojobs.com.br)",  value="infojobs",           checked=_chk("infojobs")),
        questionary.Choice("Catho           (catho.com.br)",     value="catho",              checked=_chk("catho")),
        questionary.Choice("Gupy            (portal.gupy.io)",   value="gupy",               checked=_chk("gupy")),
        questionary.Choice("Vagas.com.br",                       value="vagas",              checked=_chk("vagas")),
        # ── Brasil — Tech ───────────────────────────────────────────────────────
        questionary.Choice(title="── Brasil — Tech ────────────", value="__sep_br_tech__",   disabled=""),
        questionary.Choice("ProgramaThor    (programathor.com.br)", value="programathor",    checked=_chk("programathor")),
        questionary.Choice("GeekHunter      (geekHunter.com.br)",   value="geekHunter",      checked=_chk("geekHunter")),
        questionary.Choice("Revelo          (revelo.com.br)",    value="revelo",             checked=_chk("revelo")),
        questionary.Choice("Impulso         (impulso.network)",  value="impulso",            checked=_chk("impulso")),
        questionary.Choice("Remotar         (remotar.com.br)",   value="remotar",            checked=_chk("remotar")),
        # ── Freelance ───────────────────────────────────────────────────────────
        questionary.Choice(title="── Freelance ─────────────────", value="__sep_free__",     disabled=""),
        questionary.Choice("Upwork          (upwork.com)",        value="upwork",             checked=_chk("upwork")),
        questionary.Choice("Workana         (workana.com)",       value="workana",            checked=_chk("workana")),
        questionary.Choice("99Freelas       (99freelas.com.br)",  value="99freelas",          checked=_chk("99freelas")),
        # ── Internacional ───────────────────────────────────────────────────────
        questionary.Choice(title="── Internacional ────────────", value="__sep_int__",       disabled=""),
        questionary.Choice("Indeed USA      (indeed.com)",       value="indeed-us",          checked=_chk("indeed-us")),
        questionary.Choice("Glassdoor       (glassdoor.com.br)", value="glassdoor",          checked=_chk("glassdoor")),
        questionary.Choice("ZipRecruiter    (ziprecruiter.com)", value="ziprecruiter",       checked=_chk("ziprecruiter")),
        questionary.Choice("Careerjet BR    (careerjet.com.br)", value="careerjet",          checked=_chk("careerjet")),
        questionary.Choice("Jora            (jora.com)",         value="jora",               checked=_chk("jora")),
        questionary.Choice("RemoteOK        (remoteok.com)",     value="remoteok",           checked=_chk("remoteok")),
        questionary.Choice("Himalayas       (himalayas.app)",    value="himalayas",          checked=_chk("himalayas")),
        questionary.Choice("We Work Remotely",                   value="weworkremotely",     checked=_chk("weworkremotely")),
        questionary.Choice("Turing          (turing.com)",       value="turing",             checked=_chk("turing")),
        questionary.Choice("Toptal          (toptal.com)",       value="toptal",             checked=_chk("toptal")),
        # ── LinkedIn ────────────────────────────────────────────────────────────
        questionary.Choice(title="── LinkedIn ─────────────────", value="__sep_lk__",        disabled=""),
        questionary.Choice("LinkedIn Jobs — Brasil  (geoId BR)", value="linkedin-br",        checked=_chk("linkedin-br")   or (defaults is None)),
        questionary.Choice("LinkedIn Jobs — Global  (Remote)",   value="linkedin-intl",      checked=_chk("linkedin-intl")),
        questionary.Choice(feed_label,                            value="linkedin-feed",      checked=_chk("linkedin-feed") or (defaults is None and li_at_ok)),
    ]

    chosen = _abort_if_none(
        questionary.checkbox(
            "Plataformas de busca:",
            choices=_fonte_choices,
            style=Q_STYLE,
        ).ask(),
        "Seleção de fontes",
    )

    # Remove separadores (caso o questionary os retorne)
    selected = [s for s in chosen if not str(s).startswith("__sep")]

    if not selected:
        log_warn("Nenhuma fonte selecionada — usando Indeed BR como padrão.")
        selected = ["indeed-br"]

    log_ok(f"Fontes selecionadas: {_BD}{', '.join(selected)}{_RST}")
    return selected


def select_query(
    suggestions: Optional[dict] = None,
    client: "Groq | None" = None,
    prefs:  Optional[dict] = None,
    sources: Optional[list[str]] = None,
) -> str:
    """
    Menu em dois passos com pré-seleção inteligente:
      1. Seleção da área/stack  — padrão: última usada → IA → primeiro da lista
      2. Seleção de tecnologias — padrão: última seleção para esta stack → IA
      3. IA gera a query otimizada — usuário confirma ou edita

    Persiste stack e techs escolhidas no MongoDB para pré-marcar na próxima vez.
    """
    suggestions = suggestions or {}
    ai_stack    = suggestions.get("stack")
    ai_techs    = set(suggestions.get("technologies", []))

    # ── Carrega última seleção do MongoDB ─────────────────────────────────────
    last_stack: Optional[str] = None
    last_techs_by_stack: dict = {}   # {stack_name: [tech, ...]}
    if _global_rdb:
        try:
            last_stack = _global_rdb.load_setting("last_query_stack")
            last_techs_by_stack = _global_rdb.load_setting("last_query_techs") or {}
        except Exception:
            pass

    # ── 1. Stack / área ───────────────────────────────────────────────────────
    clr()
    section("Qual área você está buscando?")

    # Prioridade para o default: última usada > IA > primeiro da lista
    default_stack = last_stack if last_stack in STACKS else (ai_stack if ai_stack in STACKS else None)

    hint_parts = []
    if last_stack and last_stack in STACKS:
        hint_parts.append(f"última: {_BD}{last_stack}{_RST}")
    if ai_stack and ai_stack != last_stack:
        hint_parts.append(f"IA sugeriu: {_BD}{ai_stack}{_RST}")
    if hint_parts:
        print(f"  {_CY}{' · '.join(hint_parts)}{_RST}  {_DIM}(confirme ou altere){_RST}\n")

    stack_choices = []
    for name in STACKS.keys():
        tags = []
        if name == last_stack:
            tags.append("← última")
        if name == ai_stack and name != last_stack:
            tags.append("← IA")
        title = f"{name}  {' '.join(tags)}" if tags else name
        stack_choices.append(questionary.Choice(title=title, value=name))

    stack_name = _abort_if_none(
        questionary.select(
            "Área / Stack:",
            choices=stack_choices,
            default=default_stack or list(STACKS.keys())[0],
            style=Q_STYLE,
        ).ask(),
        "Seleção de área",
    )

    stack = STACKS[stack_name]

    # ── 2. Tecnologias — pré-marca última seleção desta stack ─────────────────
    clr()
    section(f"Tecnologias — {stack_name.strip()}")

    # Última seleção para ESTA stack específica (prioridade máxima)
    last_techs_this_stack = set(last_techs_by_stack.get(stack_name, []))
    # Fallback: sugestão da IA filtrada para techs válidas desta stack
    fallback_techs = ai_techs & set(stack["techs"])

    # Determina o que pré-marcar
    if last_techs_this_stack:
        precheck = last_techs_this_stack & set(stack["techs"])
        pre_label = f"última seleção ({len(precheck)})"
    elif fallback_techs:
        precheck = fallback_techs
        pre_label = f"IA sugeriu ({len(precheck)})"
    else:
        precheck = set()
        pre_label = None

    if pre_label:
        pre_str = ", ".join(sorted(precheck)) or "nenhuma"
        print(f"  {_CY}Pré-marcado ({pre_label}):{_RST} {_BD}{pre_str}{_RST}")
    print(f"  {_DIM}ESPAÇO para marcar/desmarcar  |  ENTER para confirmar{_RST}\n")

    tech_choices = [
        questionary.Choice(title=t, value=t, checked=(t in precheck))
        for t in stack["techs"]
    ]

    techs = _abort_if_none(
        questionary.checkbox(
            "Tecnologias:",
            choices=tech_choices,
            style=Q_STYLE,
        ).ask(),
        "Seleção de tecnologias",
    )

    # ── Persiste seleção atual para próxima busca ─────────────────────────────
    if _global_rdb:
        try:
            last_techs_by_stack[stack_name] = techs   # atualiza só esta stack
            _global_rdb.save_setting("last_query_stack", stack_name)
            _global_rdb.save_setting("last_query_techs", last_techs_by_stack)
        except Exception:
            pass

    # ── 3. IA gera a query otimizada ─────────────────────────────────────────
    clr()
    section("Gerando query de busca com IA")

    if client and prefs is not None:
        ai_query = generate_ai_query(
            client,
            stack_name=stack_name,
            techs=techs,
            prefs=prefs,
            sources=sources or ["indeed-br"],
        )
    else:
        # Fallback sem IA
        role = stack["role"]
        if not techs:
            ai_query = role
        elif len(techs) <= 4:
            ai_query = f"{' '.join(techs)} {role}"
        else:
            ai_query = f"{' '.join(techs[:4])} {role}"

    print(f"  {_CY}Query sugerida pela IA:{_RST}  {_BD}{_G}{ai_query}{_RST}")
    print(f"  {_DIM}Digite sua própria query abaixo, ou deixe em branco e pressione ENTER para usar a sugestão acima.{_RST}\n")

    edited = questionary.text(
        "Query de busca:",
        style=Q_STYLE,
    ).ask()

    final_query = (edited.strip() if edited else "") or ai_query
    log_ok(f"Query final: {_BD}{final_query}{_RST}")
    return final_query


def select_preferences(
    suggested_english: str = "B1",
    defaults: Optional[dict] = None,
) -> dict:
    """
    Coleta preferências de busca passo a passo, com navegação ← Voltar em cada etapa.
    defaults: dict de preferências anteriores (edição de preset).
    Retorna dict com os valores selecionados.
    Lança UserAbort se o usuário cancelar no primeiro passo.
    """
    _BACK  = "__back__"
    _CANCEL = "__cancel__"
    d = defaults or {}

    # ── Definição dos passos ──────────────────────────────────────────────────
    STEPS = [
        {
            "key":     "location_scope",
            "prompt":  "Onde quer trabalhar?",
            "default": d.get("location_scope", "brasil"),
            "choices": [
                questionary.Choice("🇧🇷  Brasil",                    value="brasil"),
                questionary.Choice("🌎  Internacional (EUA/Europa)", value="internacional"),
                questionary.Choice("🌍  Ambos",                       value="ambos"),
            ],
        },
        {
            "key":     "modality",
            "prompt":  "Modalidade de trabalho:",
            "default": d.get("modality", "remoto"),
            "choices": [
                questionary.Choice("🏠  Remoto",     value="remoto"),
                questionary.Choice("🏢  Presencial", value="presencial"),
                questionary.Choice("🔄  Híbrido",    value="hibrido"),
                questionary.Choice("✅  Todos",       value="todos"),
            ],
        },
        {
            "key":     "contract",
            "prompt":  "Modelo de contratação:",
            "default": d.get("contract", "todos"),
            "choices": [
                questionary.Choice("📄  PJ  (Pessoa Jurídica)",   value="pj"),
                questionary.Choice("🪪  CLT (Carteira assinada)", value="clt"),
                questionary.Choice("🤝  Autônomo / Freelancer",   value="autonomo"),
                questionary.Choice("✅  Todos",                    value="todos"),
            ],
        },
        {
            "key":     "english_level",
            "prompt":  "Seu nível de inglês:",
            "default": d.get("english_level", suggested_english),
            "choices": [
                questionary.Choice(label, value=value)
                for value, label in ENGLISH_LEVELS
            ],
            "hint_fn": lambda step_d: (
                f"{_CY}Preset anterior:{_RST} {_BD}{step_d['default']}{_RST}"
                if defaults else
                f"{_CY}IA inferiu:{_RST} {_BD}{suggested_english}{_RST}"
            ),
        },
        {
            "key":     "recency",
            "prompt":  "Período de publicação das vagas:",
            "default": d.get("recency", "7d"),
            "choices": [
                questionary.Choice(label, value=value)
                for value, label in RECENCY_OPTIONS
            ],
        },
    ]

    results: dict = {}
    i = 0

    while i < len(STEPS):
        step      = STEPS[i]
        is_first  = (i == 0)
        back_label= "← Cancelar (voltar ao menu)" if is_first else "← Voltar"

        clr()
        section(f"Preferências de busca  ({i + 1}/{len(STEPS)})")
        if defaults:
            print(f"  {_CY}Pré-selecionado do preset anterior — confirme ou altere{_RST}\n")

        # Dica extra opcional (ex.: inglês)
        if "hint_fn" in step:
            print(f"  {step['hint_fn'](step)}  {_DIM}(confirme ou altere){_RST}\n")

        choices = list(step["choices"]) + [
            questionary.Choice(back_label, value=_BACK),
        ]

        answer = questionary.select(
            step["prompt"],
            choices=choices,
            default=results.get(step["key"], step["default"]),
            style=Q_STYLE,
        ).ask()

        # Ctrl+C / ESC ou None → trata como voltar
        if answer is None or answer == _BACK:
            if is_first:
                raise UserAbort("Preferências canceladas")
            i -= 1
            continue

        results[step["key"]] = answer
        i += 1

    prefs = {
        "location_scope": results["location_scope"],
        "modality":       results["modality"],
        "contract":       results["contract"],
        "english_level":  results["english_level"],
        "recency":        results["recency"],
    }

    clr()
    section("Preferências confirmadas")
    labels = {
        "location_scope": {"brasil": "🇧🇷 Brasil", "internacional": "🌎 Internacional", "ambos": "🌍 Ambos"},
        "modality":       {"remoto": "🏠 Remoto", "presencial": "🏢 Presencial", "hibrido": "🔄 Híbrido", "todos": "✅ Todos"},
        "contract":       {"pj": "📄 PJ", "clt": "🪪 CLT", "autonomo": "🤝 Autônomo", "todos": "✅ Todos"},
        "recency":        dict(RECENCY_OPTIONS),
    }
    log_ok(f"Localização:  {labels['location_scope'][results['location_scope']]}")
    log_ok(f"Modalidade:   {labels['modality'][results['modality']]}")
    log_ok(f"Contratação:  {labels['contract'][results['contract']]}")
    log_ok(f"Inglês:       {_BD}{results['english_level']}{_RST}")
    log_ok(f"Recência:     {_BD}{labels['recency'].get(results['recency'], results['recency'])}{_RST}")

    return prefs


def _enrich_query(query: str, prefs: dict) -> str:
    """Adiciona termos relevantes à query com base nas preferências."""
    parts = [query]
    if prefs.get("modality") == "remoto":
        parts.append("remote remoto")
    elif prefs.get("modality") == "presencial":
        parts.append("presencial on-site")
    elif prefs.get("modality") == "hibrido":
        parts.append("híbrido hybrid")
    if prefs.get("contract") == "pj":
        parts.append("PJ")
    elif prefs.get("contract") == "clt":
        parts.append("CLT")
    elif prefs.get("contract") == "autonomo":
        parts.append("freelancer autônomo")
    return " ".join(parts)


def _prefs_to_sources(base_sources: list[str], prefs: dict) -> list[str]:
    """
    Filtra fontes com base na localização escolhida.
    Fontes marcadas como 'ambos' passam em qualquer caso.
    """
    scope = prefs.get("location_scope", "ambos")
    if scope == "ambos":
        return base_sources

    result = []
    for sk in base_sources:
        src_region = SOURCES.get(sk, {}).get("region", "ambos")
        if src_region == "ambos" or src_region == scope:
            result.append(sk)

    if not result:
        # Fallback: retorna a primeira fonte disponível para o escopo
        fallback = "indeed-br" if scope == "brasil" else "indeed-us"
        log_warn(f"Nenhuma fonte compatível com '{scope}' — usando {fallback}")
        result = [fallback]

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Preset — menu de seleção inicial
# ──────────────────────────────────────────────────────────────────────────────

def _manage_presets_menu() -> None:
    """
    Tela de gerenciamento de presets: exibe todos com checkbox multi-select
    para deletar um ou vários de uma vez.
    """
    while True:
        clr()
        presets = load_presets()
        if not presets:
            log_warn("Nenhum preset salvo.")
            return

        section(f"Gerenciar presets  —  {len(presets)} salvo(s)")
        print(f"  {_DIM}ESPAÇO para marcar  |  ENTER para confirmar  |  ESC para cancelar{_RST}\n")

        choices = [
            questionary.Choice(
                title=_preset_summary(p),
                value=p["id"],
                checked=False,
            )
            for p in presets
        ]

        to_delete = questionary.checkbox(
            "Selecione os presets para EXCLUIR:",
            choices=choices,
            style=Q_STYLE,
        ).ask()

        # ESC ou nenhum selecionado → volta
        if not to_delete:
            return

        # Confirmação antes de deletar
        n = len(to_delete)
        names = [p["name"] for p in presets if p["id"] in to_delete]
        clr()
        section("Confirmar exclusão")
        for name in names:
            print(f"  {CROSS}  {name}")
        print()

        ok = questionary.confirm(
            f"Excluir {n} preset(s)? Esta ação não pode ser desfeita.",
            default=False,
            style=Q_STYLE,
        ).ask()

        if ok:
            remaining = [p for p in presets if p["id"] not in to_delete]
            if _global_rdb:
                _global_rdb.delete_presets_bulk([p["id"] for p in presets if p["id"] in to_delete])
            log_ok(f"{n} preset(s) excluído(s).")
        else:
            log_info("Exclusão cancelada.")

        # Pergunta se quer continuar gerenciando
        if not remaining if ok else presets:
            return
        again = questionary.confirm("Gerenciar mais presets?", default=False, style=Q_STYLE).ask()
        if not again:
            return


def manage_platform_logins_menu(rdb: "MongoManager") -> None:
    """
    Menu para visualizar, renovar e limpar cookies de autenticação por plataforma.
    """
    while True:
        clr()
        section("Login nas plataformas")
        print(f"  {_DIM}Gerencie os logins salvos por plataforma.{_RST}\n")

        auth_all = rdb.load_all_auth_cookies()

        choices = []
        for key, login_url in LOGIN_URLS.items():
            src         = SOURCES.get(key, {})
            label       = src.get("label", key)
            doc         = auth_all.get(key)
            has_session = _has_browser_session(key)
            if has_session or doc:
                saved_at = (rdb.load_auth_cookies(key) or {}).get("saved_at", "")[:10]
                status   = f"✔ logado  ({saved_at})"
            else:
                status = "○ não logado"
            choices.append(questionary.Choice(
                title=f"{label:<20}  {status}",
                value=key,
            ))
        choices.append(questionary.Choice("← Voltar", value="__back__"))

        chosen = questionary.select(
            "Selecione uma plataforma:",
            choices=choices,
            style=Q_STYLE,
        ).ask()

        if not chosen or chosen == "__back__":
            return

        src    = SOURCES.get(chosen, {})
        label  = src.get("label", chosen)
        logged = bool(auth_all.get(chosen)) or _has_browser_session(chosen)

        clr()
        section(f"Login — {label}")

        sub = []
        if logged:
            doc      = rdb.load_auth_cookies(chosen) or {}
            saved_at = doc.get("saved_at", "")[:16]
            print(f"  {_G}✔ Autenticado em: {saved_at}{_RST}\n")
            sub.append(questionary.Choice("🔄  Renovar login (re-autenticar)", value="login"))
            sub.append(questionary.Choice("🗑  Limpar cookies (deslogar)",      value="clear"))
        else:
            print(f"  {_Y}○ Não autenticado{_RST}\n")
            sub.append(questionary.Choice("🔑  Fazer login agora",              value="login"))
        sub.append(questionary.Choice("← Voltar", value="back"))

        action = questionary.select("Ação:", choices=sub, style=Q_STYLE).ask()

        if not action or action == "back":
            continue

        if action == "clear":
            rdb.db.auth_cookies.delete_one({"_id": chosen})
            # Remove perfil persistente do browser
            profile_dir = BROWSER_PROFILES_DIR / chosen
            if profile_dir.exists():
                import shutil as _shutil
                _shutil.rmtree(profile_dir, ignore_errors=True)
                log_ok(f"Perfil do browser de {label} removido.")
            log_ok(f"Cookies de {label} removidos.")
            input(f"\n  {_DIM}ENTER para continuar...{_RST}")

        elif action == "login":
            try:
                from playwright.sync_api import sync_playwright as _pwl
                with _pwl() as pw:
                    ctx = _launch_persistent(pw, chosen, headless=False)
                    pg  = ctx.new_page()
                    pg.goto(LOGIN_URLS[chosen], timeout=60_000)
                    print(f"\n  {_CY}Navegador aberto — faça login em {label}{_RST}")
                    print(f"  {_DIM}A sessão será salva automaticamente.{_RST}")
                    input(f"  ENTER após completar o login em {label}... ")
                    new_ck = ctx.cookies()
                    storage_st = ctx.storage_state()
                    ctx.close()  # persiste estado no disco
                rdb.save_auth_cookies(chosen, new_ck)
                rdb.save_storage_state(chosen, storage_st)
                log_ok(f"{label}: sessão completa salva (storage_state + {len(new_ck)} cookies)")
            except ImportError:
                log_err("playwright não instalado. Execute: pip install playwright && playwright install chromium")
            except Exception as exc:
                log_err(f"Erro: {exc}")
            input(f"\n  {_DIM}ENTER para continuar...{_RST}")


def view_mapped_fields_menu(rdb: "MongoManager") -> None:
    """Menu para visualizar campos mapeados de cada plataforma."""
    while True:
        clr()
        section("Campos mapeados")

        # Carrega todas as plataformas com campos mapeados
        platforms_with_fields = []
        for platform_key, src in SOURCES.items():
            meta = rdb.load_platform_meta(platform_key)
            profile_fields = meta.get("profile_fields", {})
            if profile_fields:
                platforms_with_fields.append((platform_key, src.get("label", platform_key), len(profile_fields)))

        if not platforms_with_fields:
            print(f"  {_DIM}Nenhum campo mapeado ainda.{_RST}")
            print(f"  {_DIM}Campos são descobertos automaticamente ao atualizar o perfil.{_RST}\n")
            input(f"  {_DIM}ENTER para voltar...{_RST}")
            return

        choices = []
        for platform_key, label, n_sections in platforms_with_fields:
            choices.append(questionary.Choice(
                f"{label:<20}  {n_sections} seção(ões)",
                value=platform_key
            ))
        choices.append(questionary.Choice("← Voltar", value="__back__"))

        chosen = questionary.select("Selecione uma plataforma:", choices=choices, style=Q_STYLE).ask()

        if not chosen or chosen == "__back__":
            return

        # Mostra campos dessa plataforma
        clr()
        src = SOURCES.get(chosen, {})
        label = src.get("label", chosen)
        section(f"Campos — {label}")

        meta = rdb.load_platform_meta(chosen)
        profile_fields = meta.get("profile_fields", {})

        if not profile_fields:
            print(f"  {_DIM}Nenhum campo mapeado para {label}.{_RST}\n")
            input(f"  {_DIM}ENTER para voltar...{_RST}")
            continue

        for sec_key, sec_data in list(profile_fields.items()):
            sec_url = sec_data.get("section_url", "")
            sec_label = sec_url.split("/")[-1] if sec_url else sec_key
            discovered_at = sec_data.get("discovered_at", "")[:10]
            fields = sec_data.get("fields", {})

            print(f"\n  {_BD}{sec_label}{_RST}  ({len(fields)} campos)")
            print(f"  {_DIM}URL: {sec_url}{_RST}")
            print(f"  {_DIM}Descoberto em: {discovered_at}{_RST}")

            for fname, fdata in list(fields.items())[:8]:
                flabel = fdata.get("label", fname)[:45]
                ftype = fdata.get("input_type") or fdata.get("type")
                frequired = "✓ obrigatório" if fdata.get("required") else ""
                print(f"      • {fname:<22} ({ftype:<10}) {flabel:<45} {frequired}")

            if len(fields) > 8:
                print(f"      ... e mais {len(fields) - 8} campo(s)")

        input(f"\n  {_DIM}ENTER para voltar...{_RST}")


def _print_main_header(rdb: "Optional[Any]") -> None:
    """
    Cabeçalho verde do menu principal com status em tempo real de todos os serviços.
    Lê _api_ok diretamente (único ponto de verdade) — sem parâmetros de estado.
    """
    W = 62  # largura do box

    # ── Título ────────────────────────────────────────────────────────────────
    title     = "  JOB HUNTER"
    subtitle  = "Busca inteligente de vagas com IA"
    print(f"\n{_G}{_BD}{'╔' + '═' * W + '╗'}{_RST}")
    print(f"{_G}{_BD}║{_RST}{_G}{_BD}{title.ljust(W)}{_RST}{_G}{_BD}║{_RST}")
    print(f"{_G}║{_RST}  {_DIM}{subtitle.ljust(W - 2)}{_RST}{_G}║{_RST}")
    print(f"{_G}{_BD}{'╚' + '═' * W + '╝'}{_RST}\n")

    # ── Status dos serviços ────────────────────────────────────────────────────
    def dot(ok: bool) -> str:
        return f"{_G}●{_RST}" if ok else f"{_R}●{_RST}"

    # MongoDB — usa rdb já conectado (sem ping extra)
    mongo_ok    = rdb is not None
    # GROQ_API_KEY — checa env
    key_ok      = bool(os.environ.get("GROQ_API_KEY", "").strip())
    # API status — lê global (único ponto de verdade)
    api_ok_now  = get_api_ok()
    # Modelo ativo
    model_label = GROQ_MODELS.get(_ACTIVE_MODEL, {}).get("label", _ACTIVE_MODEL)

    # linha 1: serviços de infra
    mongo_txt = f"{dot(mongo_ok)} MongoDB"
    key_txt   = f"{dot(key_ok)} GROQ_API_KEY"
    api_txt   = f"{dot(api_ok_now)} IA {'online' if api_ok_now else 'offline'}"
    sep       = f"  {_DIM}│{_RST}  "

    print(f"  {mongo_txt}{sep}{key_txt}{sep}{api_txt}")

    # linha 2: modelo + dica de verbose
    model_txt   = f"  {_DIM}Modelo:{_RST} {_BD}{model_label}{_RST}"
    verbose_txt = f"{_G}(verbose){_RST}" if _VERBOSE else f"{_DIM}(discreto){_RST}"
    print(f"{model_txt}   {_DIM}Logs:{_RST} {verbose_txt}")
    print()


def _edit_preset_fields_menu(preset: dict) -> Optional[dict]:
    """
    Edição campo-a-campo de um preset existente.
    O usuário escolhe exatamente qual campo alterar; os demais permanecem intactos.
    Retorna o preset atualizado se quiser usá-lo agora, ou None para apenas fechar.
    """
    import copy

    p      = copy.deepcopy(preset)
    prefs  = p.setdefault("prefs", {})

    # ── Mapeamentos de label ──────────────────────────────────────────────────
    mod_l  = {"remoto": "Remoto", "presencial": "Presencial", "hibrido": "Hibrido", "todos": "Todos"}
    cont_l = {"pj": "PJ", "clt": "CLT", "autonomo": "Autonomo", "todos": "Todos"}
    loc_l  = {"brasil": "Brasil", "internacional": "Internacional", "ambos": "Ambos"}
    rec_l  = dict(RECENCY_OPTIONS)

    def _cur(val: str, mapping: dict) -> str:
        return mapping.get(val, val or "—")

    while True:
        clr()
        section("Editar preset")

        # Mostra resumo atual sem ANSI (questionary não processa cores nos títulos)
        fontes_str = ", ".join(p.get("sources", []))
        if len(fontes_str) > 50:
            fontes_str = fontes_str[:47] + "..."

        choices = [
            questionary.Choice(
                f"  Nome         →  {p['name'][:45]}",         value="name"),
            questionary.Choice(
                f"  Query        →  {p.get('query','')[:45]}",  value="query"),
            questionary.Choice(
                f"  Fontes       →  {fontes_str}",              value="sources"),
            questionary.Choice(
                f"  Localizacao  →  {_cur(prefs.get('location_scope',''), loc_l)}",
                value="location_scope"),
            questionary.Choice(
                f"  Modalidade   →  {_cur(prefs.get('modality',''), mod_l)}",
                value="modality"),
            questionary.Choice(
                f"  Contratacao  →  {_cur(prefs.get('contract',''), cont_l)}",
                value="contract"),
            questionary.Choice(
                f"  Ingles       →  {prefs.get('english_level','—')}",
                value="english_level"),
            questionary.Choice(
                f"  Recencia     →  {_cur(prefs.get('recency',''), rec_l)}",
                value="recency"),
            questionary.Separator(),
            questionary.Choice("✅  Salvar e usar agora",        value="__save_use__"),
            questionary.Choice("💾  Salvar sem iniciar busca",   value="__save_only__"),
            questionary.Choice("← Cancelar (sem salvar)",       value="__cancel__"),
        ]

        action = questionary.select(
            "Qual campo deseja editar?",
            choices=choices,
            style=Q_STYLE,
        ).ask()

        if not action or action == "__cancel__":
            return None

        if action == "__save_use__":
            p["prefs"] = prefs
            save_preset(p)
            return p

        if action == "__save_only__":
            p["prefs"] = prefs
            save_preset(p)
            input(f"\n  {_DIM}ENTER para voltar ao menu...{_RST}")
            return None

        # ── Editores por campo ────────────────────────────────────────────────
        if action == "name":
            clr(); section("Editar nome")
            val = questionary.text(
                "Novo nome:", default=p["name"], style=Q_STYLE
            ).ask()
            if val:
                p["name"] = val.strip()

        elif action == "query":
            clr(); section("Editar query de busca")
            val = questionary.text(
                "Nova query:", default=p.get("query", ""), style=Q_STYLE
            ).ask()
            if val is not None:
                p["query"] = val.strip()

        elif action == "sources":
            try:
                new_sources = select_sources(defaults=p.get("sources"))
                p["sources"] = new_sources
            except UserAbort:
                pass  # usuário voltou — mantém fontes anteriores

        elif action == "location_scope":
            clr(); section("Editar localização")
            val = questionary.select(
                "Onde quer trabalhar?",
                choices=[
                    questionary.Choice("Brasil",                    value="brasil"),
                    questionary.Choice("Internacional (EUA/Europa)", value="internacional"),
                    questionary.Choice("Ambos",                      value="ambos"),
                    questionary.Choice("← Cancelar",                value="__back__"),
                ],
                default=prefs.get("location_scope", "brasil"),
                style=Q_STYLE,
            ).ask()
            if val and val != "__back__":
                prefs["location_scope"] = val

        elif action == "modality":
            clr(); section("Editar modalidade")
            val = questionary.select(
                "Modalidade de trabalho:",
                choices=[
                    questionary.Choice("Remoto",     value="remoto"),
                    questionary.Choice("Presencial", value="presencial"),
                    questionary.Choice("Hibrido",    value="hibrido"),
                    questionary.Choice("Todos",      value="todos"),
                    questionary.Choice("← Cancelar", value="__back__"),
                ],
                default=prefs.get("modality", "remoto"),
                style=Q_STYLE,
            ).ask()
            if val and val != "__back__":
                prefs["modality"] = val

        elif action == "contract":
            clr(); section("Editar contratação")
            val = questionary.select(
                "Modelo de contratação:",
                choices=[
                    questionary.Choice("PJ  (Pessoa Juridica)",  value="pj"),
                    questionary.Choice("CLT (Carteira assinada)", value="clt"),
                    questionary.Choice("Autonomo / Freelancer",   value="autonomo"),
                    questionary.Choice("Todos",                   value="todos"),
                    questionary.Choice("← Cancelar",              value="__back__"),
                ],
                default=prefs.get("contract", "todos"),
                style=Q_STYLE,
            ).ask()
            if val and val != "__back__":
                prefs["contract"] = val

        elif action == "english_level":
            clr(); section("Editar nível de inglês")
            choices_eng = [questionary.Choice(label, value=v) for v, label in ENGLISH_LEVELS]
            choices_eng.append(questionary.Choice("← Cancelar", value="__back__"))
            val = questionary.select(
                "Nível de inglês:",
                choices=choices_eng,
                default=prefs.get("english_level", "B1"),
                style=Q_STYLE,
            ).ask()
            if val and val != "__back__":
                prefs["english_level"] = val

        elif action == "recency":
            clr(); section("Editar recência")
            choices_rec = [questionary.Choice(label, value=v) for v, label in RECENCY_OPTIONS]
            choices_rec.append(questionary.Choice("← Cancelar", value="__back__"))
            val = questionary.select(
                "Período de publicação:",
                choices=choices_rec,
                default=prefs.get("recency", "7d"),
                style=Q_STYLE,
            ).ask()
            if val and val != "__back__":
                prefs["recency"] = val


def select_preset_or_new(
    rdb:            "MongoManager",
    profile:        "Optional[dict]" = None,
    client:         "Optional[Any]"  = None,
    resume_path:    str           = "",
    resume_changed: bool          = False,
) -> "Optional[dict]":
    """
    Menu inicial:
      - Continuar com preset existente
      - Criar nova busca
      - Gerenciar / excluir presets
      - Ver histórico de vagas
    Retorna o preset dict se escolhido, None se nova busca.
    Lê get_api_ok() diretamente (único ponto de verdade) — sem parâmetro api_ready.
    """
    _first_render = True
    while True:
        clr()
        presets = load_presets()   # recarrega a cada iteração (deleções refletem imediatamente)
        counts  = rdb.count_by_status()

        _print_main_header(rdb)

        # Aviso de currículo alterado — exibe apenas uma vez (primeira abertura do menu)
        if _first_render and resume_changed:
            print(
                f"  {_Y}{_BD}⚠  Currículo alterado{_RST}{_Y} — cache de vagas resetado.{_RST}\n"
                f"  {_DIM}Todas as vagas serão rebuscadas nesta sessão.{_RST}\n"
            )
        _first_render = False

        if not get_api_ok():
            print(
                f"  {_Y}{_BD}⚠  API Groq indisponível{_RST}{_Y} — nova busca desabilitada.{_RST}\n"
                f"  {_DIM}Buscas e avaliações exigem a API. Você ainda pode revisar vagas já mapeadas.{_RST}\n"
            )

        # Linha de status do histórico
        hist_label = "  ".join(
            f"{ico} {counts.get(s, 0)} {JOB_STATUS[s][1]}"
            for s, (ico, _) in JOB_STATUS.items()
            if s != "seen"
        )
        if any(counts.values()):
            print(f"  {_DIM}Histórico: {hist_label}{_RST}\n")

        # ── Menu principal hierárquico ─────────────────────────────────────────
        model_short  = _ACTIVE_MODEL.split("-")[0]
        api_flag     = "" if get_api_ok() else "  [sem API]"
        top_choices  = []
        if presets:
            top_choices.append(questionary.Choice(
                f"📋  Presets  ({len(presets)} salvo(s))", value="__presets__",
            ))
        top_choices += [
            questionary.Choice(f"🔍  Busca{api_flag}",             value="__busca__"),
            questionary.Choice("🧑  Perfil & Plataformas",          value="__perfil__"),
            questionary.Choice(f"⚙️   Configuracoes  [{model_short}]", value="__config__"),
            questionary.Choice("🚪  Sair  (ou Ctrl+C)",             value="__exit__"),
        ]

        chosen = questionary.select("Escolha:", choices=top_choices, style=Q_STYLE).ask()

        # ── Sair (menu ou Ctrl+C) ──────────────────────────────────────────────
        if chosen is None or chosen == "__exit__":
            return "__exit__"

        # ── Presets ───────────────────────────────────────────────────────────
        if chosen == "__presets__":
            clr()
            section("Presets salvos")
            if not get_api_ok():
                print(f"  {_Y}⚠  API indisponivel — apenas visualizacao.{_RST}\n")
            preset_choices = [
                questionary.Choice(title=_preset_summary(p), value=p["id"])
                for p in presets[:10]
            ]
            preset_choices.append(questionary.Choice("← Voltar", value="__back__"))
            picked = questionary.select(
                "Selecione um preset:", choices=preset_choices, style=Q_STYLE,
            ).ask()
            if not picked or picked == "__back__":
                continue
            # Cai no bloco de detalhes do preset abaixo
            chosen = picked

        # ── Busca ─────────────────────────────────────────────────────────────
        elif chosen == "__busca__":
            clr()
            section("Busca")
            busca_choices = []
            if get_api_ok():
                busca_choices.append(questionary.Choice("🆕  Nova busca",              value="__new__"))
            else:
                busca_choices.append(questionary.Choice("🔄  Tentar reconectar com IA", value="__reconnect__"))
                busca_choices.append(questionary.Choice(
                    "🆕  Nova busca  (requer API)", value="__new__", disabled="Reconecte primeiro",
                ))
            busca_choices += [
                questionary.Choice("📂  Continuar revisando vagas",  value="__resume__"),
                questionary.Choice("📋  Ver historico de vagas",     value="__history__"),
                questionary.Choice("← Voltar",                       value="__back__"),
            ]
            sub = questionary.select("Busca:", choices=busca_choices, style=Q_STYLE).ask()
            if not sub or sub == "__back__":
                continue
            if sub == "__new__":
                return None
            if sub == "__reconnect__":
                return "__reconnect__"
            if sub == "__resume__":
                resume_review_menu(rdb, profile=profile, client=client, resume_path=resume_path)
            elif sub == "__history__":
                show_history_menu(rdb)
            continue

        # ── Perfil & Plataformas ──────────────────────────────────────────────
        elif chosen == "__perfil__":
            clr()
            section("Perfil & Plataformas")
            sub = questionary.select(
                "Perfil & Plataformas:",
                choices=[
                    questionary.Choice("🔑  Login nas plataformas",            value="logins"),
                    questionary.Choice("← Voltar",                              value="__back__"),
                ],
                style=Q_STYLE,
            ).ask()
            if sub == "logins":
                manage_platform_logins_menu(rdb)
            continue

        # ── Configuracoes ─────────────────────────────────────────────────────
        elif chosen == "__config__":
            clr()
            result = show_settings_menu()
            if result == "__key_changed__":
                return "__reload_client__"
            continue

        # ── Preset selecionado — mostra detalhes ───────────────────────────────
        preset = next((p for p in presets if p["id"] == chosen), None)
        if not preset:
            continue

        clr()
        section("Preset selecionado")
        prefs  = preset.get("prefs", {})
        mod_l  = {"remoto": "🏠 Remoto", "presencial": "🏢 Presencial", "hibrido": "🔄 Híbrido", "todos": "✅ Todos"}
        cont_l = {"pj": "📄 PJ", "clt": "🪪 CLT", "autonomo": "🤝 Autônomo", "todos": "✅ Todos"}
        log_ok(f"Nome:        {_BD}{preset['name']}{_RST}")
        log_ok(f"Query:       {preset.get('query', '')}")
        log_ok(f"Fontes:      {', '.join(preset.get('sources', []))}")
        log_ok(f"Localização: {preset.get('location', '')}")
        log_ok(f"Modalidade:  {mod_l.get(prefs.get('modality',''), '')}")
        log_ok(f"Contrato:    {cont_l.get(prefs.get('contract',''), '')}")
        log_ok(f"Inglês:      {prefs.get('english_level', '')}")
        log_ok(f"Recência:    {prefs.get('recency', 'any')}")
        print()

        action = questionary.select(
            "Usar este preset?",
            choices=[
                questionary.Choice("✅  Usar agora",          value="use"),
                questionary.Choice("✏️   Editar campos",       value="edit"),
                questionary.Choice("🗑   Excluir este preset", value="delete"),
                questionary.Choice("←   Voltar",              value="back"),
            ],
            style=Q_STYLE,
        ).ask()

        if action == "use":
            return preset
        if action == "edit":
            updated = _edit_preset_fields_menu(preset)
            if updated:
                return updated   # usa o preset editado diretamente
            # None → usuário cancelou ou apenas salvou sem usar: volta ao loop
        if action == "delete":
            ok = questionary.confirm(
                f"Excluir '{preset['name']}'?",
                default=False,
                style=Q_STYLE,
            ).ask()
            if ok:
                _delete_preset(chosen)
                log_ok(f"Preset '{preset['name']}' excluído.")
        # "back" ou qualquer outro → volta ao loop


def _delete_preset(pid: str) -> None:
    if _global_rdb:
        _global_rdb.delete_preset_from_db(pid)


# ──────────────────────────────────────────────────────────────────────────────
# Histórico de vagas
# ──────────────────────────────────────────────────────────────────────────────

def show_settings_menu() -> Optional[str]:
    """
    Menu de configurações: troca de modelo Groq, GROQ_API_KEY e outras opções.
    Retorna '__key_changed__' quando o usuário salva uma nova chave de API,
    para que o loop principal reinicialize o cliente Groq.
    """
    while True:
        clr()
        section("Configurações de IA")
        current_info = GROQ_MODELS.get(_ACTIVE_MODEL, {})

        # Mostra chave atual (mascarada)
        current_key = os.environ.get("GROQ_API_KEY", "")
        if current_key:
            masked = current_key[:8] + "..." + current_key[-4:] if len(current_key) > 12 else "***"
            log_ok(f"GROQ_API_KEY:  {_DIM}{masked}{_RST}")
        else:
            log_err("GROQ_API_KEY:  não configurada")

        log_ok(f"Modelo atual:  {_BD}{current_info.get('label', _ACTIVE_MODEL)}{_RST}")
        log_info(f"{_DIM}{current_info.get('desc', '')}{_RST}")
        print()

        action = questionary.select(
            "O que deseja configurar?",
            choices=[
                questionary.Choice("🔑  Configurar GROQ_API_KEY", value="key"),
                questionary.Choice("🤖  Trocar modelo de IA",     value="model"),
                questionary.Choice("📄  Trocar currículo",         value="resume"),
                questionary.Choice("🩺  Testar API agora",        value="healthcheck"),
                questionary.Choice("🗑   Limpar todos os dados",   value="reset"),
                questionary.Choice("← Voltar",                    value="back"),
            ],
            style=Q_STYLE,
        ).ask()

        if not action or action == "back":
            return None

        if action == "model":
            _show_model_selector()

        elif action == "key":
            changed = _configure_groq_key()
            if changed:
                return "__key_changed__"

        elif action == "resume":
            new_resume = _pick_resume_from_folder()
            if new_resume:
                # Persiste como novo currículo padrão
                _save_to_dotenv("RESUME_PATH", new_resume)
                log_ok(f"Currículo atualizado: {_DIM}{new_resume}{_RST}")
                input(f"\n  {_DIM}ENTER para continuar...{_RST}")
                return "__resume_changed__"

        elif action == "reset":
            reset_all_data(keep_config=True)

        elif action == "healthcheck":
            clr()
            section("Health-check da API de IA")
            _hc_key = os.environ.get("GROQ_API_KEY", "")
            if not _hc_key:
                log_err("GROQ_API_KEY não configurada — configure a chave primeiro.")
            else:
                # Reutiliza check_groq_api: atualiza _api_ok + grava timestamp no MongoDB
                try:
                    _hc_client = Groq(api_key=_hc_key)
                    check_groq_api(_hc_client, _global_rdb, quiet=False)
                except Exception as exc:
                    set_api_ok(False)
                    log_err(f"Erro ao testar API: {exc}")
            input(f"\n  {_DIM}ENTER para voltar...{_RST}")


def reset_all_data(keep_config: bool = True) -> None:
    """
    Apaga todos os dados do Job Hunter:
      - MongoDB: collections jobs, queue, seen, decisions, sessions, errors
      - Presets: MongoDB collection 'presets'  (pergunta ao usuário)
      - Config:  variáveis GROQ_API_KEY, MODEL, RESUME_PATH do .env  (pergunta ao usuário)
      keep_config=True preserva API key, modelo e RESUME_PATH no .env.
    """
    clr()
    section("Limpar todos os dados")

    print(f"  {_R}{_BD}ATENÇÃO:{_RST} esta operação é {_BD}irreversível{_RST}.\n")
    print(f"  Será apagado:")
    print(f"    {_DIM}• MongoDB: vagas coletadas, avaliadas, histórico, decisões, sessões{_RST}")
    print(f"    {_DIM}• MongoDB: cache de URLs vistas (coleção seen){_RST}")
    print()
    print(f"  {_DIM}Preservado por padrão: API key, modelo, currículo, presets{_RST}")
    print()

    limpar_presets = questionary.confirm(
        "  Apagar presets salvos também?", default=False, style=Q_STYLE
    ).ask()
    if limpar_presets is None:
        log_warn("Cancelado.")
        return

    limpar_config = False
    if not keep_config:
        limpar_config = questionary.confirm(
            "  Apagar configurações (API key, modelo)?", default=False, style=Q_STYLE
        ).ask() or False

    confirma = questionary.confirm(
        f"\n  {_R}Confirma a limpeza total?{_RST}", default=False, style=Q_STYLE
    ).ask()
    if not confirma:
        log_warn("Cancelado.")
        return

    print()
    sp = Spinner("Limpando dados...").start()

    erros = []

    # ── MongoDB ────────────────────────────────────────────────────────────────
    rdb = _global_rdb
    if rdb:
        colecoes = ["jobs", "queue", "seen", "decisions", "sessions", "errors"]
        for col in colecoes:
            try:
                rdb.db[col].drop()
            except Exception as e:
                erros.append(f"MongoDB.{col}: {e}")

        if limpar_config:
            try:
                rdb.db["meta"].drop()
            except Exception as e:
                erros.append(f"MongoDB.meta: {e}")
        else:
            # Preserva api_key e model, apaga apenas api_status e resume_hash
            try:
                rdb.db["meta"].delete_many({
                    "_id": {"$in": ["api_status", "resume_hash"]}
                })
            except Exception as e:
                erros.append(f"MongoDB.meta (parcial): {e}")

    # ── Presets ────────────────────────────────────────────────────────────────
    if limpar_presets:
        if rdb:
            try:
                rdb.db["presets"].drop()
            except Exception as e:
                erros.append(f"MongoDB.presets: {e}")

    # ── Config ────────────────────────────────────────────────────────────────
    if limpar_config:
        # Remove variáveis de config do .env (preserva MONGO_* e LINKEDIN_*)
        for _key in ["GROQ_API_KEY", "MODEL", "RESUME_PATH"]:
            try:
                _remove_from_dotenv(_key)
            except Exception:
                pass
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("MODEL", None)
        set_api_ok(False)

    sp.stop()

    if erros:
        for e in erros:
            log_warn(f"Erro ao limpar {e}")
    else:
        log_ok("Todos os dados foram apagados com sucesso.")

    if not limpar_config:
        log_info("Configurações (API key, modelo) foram preservadas.")
    if not limpar_presets:
        log_info("Presets foram preservados.")

    input(f"\n  {_DIM}ENTER para continuar...{_RST}")


def _show_model_selector() -> None:
    """Submenu de seleção de modelo Groq."""
    clr()
    section("Selecionar modelo de IA")
    print(f"  {_DIM}Modelos com limite maior são úteis quando o modelo principal atinge rate limit.{_RST}\n")

    choices = []
    for model_id, info in GROQ_MODELS.items():
        marker = "  ← atual" if model_id == _ACTIVE_MODEL else ""
        choices.append(questionary.Choice(
            title=f"{info['label']}{marker}  —  {info['desc']}",
            value=model_id,
        ))
    choices.append(questionary.Choice("← Cancelar", value="__cancel__"))

    chosen = questionary.select(
        "Escolha o modelo:",
        choices=choices,
        style=Q_STYLE,
    ).ask()

    if not chosen or chosen == "__cancel__":
        return

    if chosen == _ACTIVE_MODEL:
        log_info("Modelo não alterado.")
        return

    set_active_model(chosen)
    print(f"  {_DIM}{GROQ_MODELS[chosen]['desc']}{_RST}\n")
    input(f"  {_DIM}ENTER para continuar...{_RST}")


def show_history_menu(rdb: "MongoManager") -> None:
    """Exibe vagas por status (aceitas / recusadas / candidatei)."""
    while True:
        clr()
        section("Histórico de vagas")
        counts = rdb.count_by_status()

        status_choices = [
            questionary.Choice(
                f"{ico}  {label}  ({counts.get(s, 0)})",
                value=s,
            )
            for s, (ico, label) in JOB_STATUS.items()
            if s != "seen"
        ]
        status_choices.append(questionary.Choice("← Voltar", value="__back__"))

        chosen = _abort_if_none(
            questionary.select("Ver vagas por status:", choices=status_choices, style=Q_STYLE).ask()
        )

        if chosen == "__back__":
            return

        jobs = rdb.get_jobs_by_status(chosen)
        ico, label = JOB_STATUS[chosen]

        clr()
        section(f"{ico} {label}  ({len(jobs)} vagas)")
        if not jobs:
            log_warn("Nenhuma vaga nesta categoria ainda.")
        else:
            for i, j in enumerate(jobs, 1):
                score = j.get("score", "?")
                print(
                    f"  {_BD}#{i:02d}{_RST}  {_BD}{j.get('title','')[:50]}{_RST}"
                    f"  —  {j.get('company','')[:30]}"
                )
                print(f"       Score: {score}%  |  {j.get('region','')}  |  {j.get('ts','')[:10]}")
                print(f"       {_B}{j.get('link','')}{_RST}")
                print()

        input(f"  {_DIM}ENTER para voltar...{_RST}")


# ──────────────────────────────────────────────────────────────────────────────
# Retomada de revisão — vagas de sessões anteriores
# ──────────────────────────────────────────────────────────────────────────────

_DECISION_LABEL: dict[str, str] = {
    "accepted": f"{_G}✔ Curtida{_RST}",
    "rejected": f"{_R}✗ Recusada{_RST}",
    "applied":  f"{_CY}✉ Candidatado{_RST}",
    "seen":     f"{_DIM}👁 Vista{_RST}",
}


def _ai_fill_profile_section(
    client:         "Groq",
    form_info:      str,
    profile:        dict,
    platform_label: str,
    section_label:  str,
) -> dict:
    """
    IA analisa uma seção de perfil em uma plataforma e retorna instruções de preenchimento.
    Retorna {"fills": [{name, value}], "missing": ["descrição"]}
    """
    personal = profile.get("personal", {})
    prof     = profile.get("professional", {})
    extra    = profile.get("extra_info", {})

    profile_compact = {
        "name":           personal.get("name"),
        "email":          personal.get("email"),
        "phone":          personal.get("phone"),
        "location":       personal.get("location"),
        "linkedin":       personal.get("linkedin"),
        "github":         personal.get("github"),
        "portfolio":      personal.get("portfolio"),
        "headline":       prof.get("current_role"),
        "objective":      prof.get("objective"),
        "seniority":      prof.get("seniority"),
        "years_exp":      prof.get("experience_years"),
        "top_techs":      profile.get("top_technologies", []),
        "main_stack":     profile.get("main_stack"),
        "english_level":  profile.get("english_level"),
        "languages":      profile.get("languages", []),
        "education":      profile.get("education", []),
        "experience":     profile.get("experience", []),
        "certifications": profile.get("certifications", []),
        "soft_skills":    profile.get("soft_skills", []),
        "highlights":     profile.get("highlights", []),
        **extra,
    }
    profile_str = json.dumps(profile_compact, ensure_ascii=False)[:3000]

    prompt = (
        f"Você está preenchendo a seção '{section_label}' do perfil profissional"
        f" na plataforma {platform_label}.\n\n"
        f"PERFIL DO CANDIDATO:\n{profile_str}\n\n"
        f"CAMPOS DETECTADOS NA PÁGINA (JSON):\n{form_info}\n\n"
        "Para cada campo detectado, determine o valor correto usando os dados do perfil.\n"
        "Contexto: é atualização de PERFIL PROFISSIONAL, não candidatura a vaga.\n"
        "- Campos 'headline' / 'título': cargo atual + principais skills em até 120 chars\n"
        "- Campos 'about' / 'summary' / 'sobre': resumo profissional em 3-5 frases\n"
        "- Campos de skills: use top_techs e soft_skills\n"
        "- Campos de localização: use location do perfil\n"
        "Retorne APENAS JSON válido:\n"
        '{"fills": [{"name": "field_name_or_id", "value": "valor"}], '
        '"missing": ["descrição do campo obrigatório sem dados no perfil"]}\n'
        "Omita campos opcionais sem informação. Responda SOMENTE com JSON."
    )

    try:
        resp = client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=2000,  # Aumentado para evitar truncamento
            messages=[
                {"role": "system", "content": "Responda APENAS com JSON válido, sem markdown."},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        result = json.loads(raw)
        return result

    except json.JSONDecodeError as exc:
        log_err(f"IA retornou JSON inválido (tentando recuperar): {exc}")

        # Tenta fechar o JSON truncado
        try:
            # Se está truncado, tenta fechar
            if '"fills"' in raw and '{"name"' in raw:
                # Procura o último fill completo
                import re
                matches = list(re.finditer(r'\{"name"[^}]*\}', raw))
                if matches:
                    last_valid = matches[-1].end()
                    raw_fixed = raw[:last_valid] + '], "missing": []}'
                    log_info("Tentando parse de JSON parcial...")
                    result = json.loads(raw_fixed)
                    return result
        except Exception:
            pass

        log_err(f"Raw truncado: {raw[:300]}...")
        return {"fills": [], "missing": []}
    except Exception as exc:
        log_err(f"Erro ao analisar resposta da IA: {exc}")
        return {"fills": [], "missing": []}


def update_profile_on_platform(
    platform_key:     str,
    profile:          dict,
    client:           "Any",
    rdb:              "MongoManager",
    auth_cookies_all: dict,
    show_browser:     bool = True,
) -> bool:
    """
    Abre browser, faz login se necessário, navega por cada seção de perfil
    da plataforma e preenche com dados do candidato via IA.
    show_browser=False roda em background (headless).
    Retorna True se ao menos uma seção foi atualizada.
    """
    sections = PROFILE_EDIT_SECTIONS.get(platform_key, [])
    if not sections:
        log_warn(f"Plataforma '{platform_key}' sem seções de perfil mapeadas.")
        return False

    src   = SOURCES.get(platform_key, {})
    label = src.get("label", platform_key)

    clr()
    section(f"Atualizando perfil — {label}")

    # ── Login se necessário ───────────────────────────────────────────────────
    if not auth_cookies_all.get(platform_key) and not _has_browser_session(platform_key) and platform_key in LOGIN_URLS:
        print(f"\n  {_Y}Você não está logado em {label}.{_RST}")
        do_login = questionary.confirm(
            f"Fazer login em {label} agora?", default=True, style=Q_STYLE,
        ).ask()
        if not do_login:
            return False
        try:
            from playwright.sync_api import sync_playwright as _pw2
            with _pw2() as pw:
                ctx = _launch_persistent(pw, platform_key, headless=False)
                pg  = ctx.new_page()
                pg.goto(LOGIN_URLS[platform_key], timeout=60_000)
                print(f"\n  {_CY}Navegador aberto — faça login em {label}{_RST}")
                print(f"  {_DIM}A sessão será salva automaticamente.{_RST}")
                input(f"  Pressione ENTER após completar o login... ")
                new_ck = ctx.cookies()
                storage_st = ctx.storage_state()
                ctx.close()  # persiste estado no disco
            rdb.save_auth_cookies(platform_key, new_ck)
            rdb.save_storage_state(platform_key, storage_st)
            auth_cookies_all[platform_key] = new_ck
            log_ok(f"Login em {label} realizado — sessão completa salva")
        except ImportError:
            log_err("playwright não instalado. Execute: pip install playwright && playwright install chromium")
            return False
        except Exception as exc:
            log_err(f"Erro ao fazer login em {label}: {exc}")
            return False

    # ── Abre browser para atualizar perfil ───────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright as _pw3
    except ImportError:
        log_err("playwright não instalado.")
        return False

    any_success = False
    headless    = not show_browser
    bw_mode     = "visivel" if show_browser else "background (headless)"
    log_info(f"Navegador: {bw_mode}")

    try:
        with _pw3() as pw:
            context = _launch_persistent(pw, platform_key, headless=headless)

            # Injeta localStorage, sessionStorage e cookies salvos do banco
            saved_storage = rdb.load_storage_state(platform_key)
            if saved_storage:
                _inject_storage_state(context, saved_storage)
                log_info(f"Storage state carregado para {label}")

            page    = context.new_page()
            log_info(f"Browser iniciado (perfil persistente) — {len(sections)} seção(ões) para processar")

            for sec_idx, (sec_url, sec_label) in enumerate(sections, 1):
                print(f"\n  {_CY}[{sec_idx}/{len(sections)}] Seção:{_RST} {_BD}{sec_label}{_RST}")

                # ── Resolução de URL dinâmica (__auto__:BASE_URL) ─────────────
                if sec_url.startswith("__auto__:"):
                    base_url = sec_url[len("__auto__:"):]

                    # 1. Tenta carregar URL salva do banco (evita redescobrir)
                    meta      = rdb.load_platform_meta(platform_key)
                    saved_url = meta.get("profile_edit_url", "")
                    if saved_url:
                        log_ok(f"URL do perfil carregada do banco: {saved_url}")
                        sec_url = saved_url
                    else:
                        # 2. Navega para a home logada e procura o link de edição
                        log_info(f"Detectando URL do perfil em {base_url}...")
                        page.goto(base_url, timeout=30_000, wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)

                        discovered = None
                        # Padrões comuns: /users/{id}/edit
                        for link_sel in [
                            "a[href*='/users/'][href*='/edit']",
                            "a[href*='edit'][href*='profile']",
                            "a[href*='/edit'][href*='/user']",
                            "a[href*='settings/profile']",
                            "a[href*='account/edit']",
                        ]:
                            try:
                                el = page.query_selector(link_sel)
                                if el:
                                    href = el.get_attribute("href") or ""
                                    if href:
                                        discovered = href if href.startswith("http") \
                                            else base_url.rstrip("/") + "/" + href.lstrip("/")
                                        break
                            except Exception:
                                pass

                        # Tenta extrair ID do padrão /users/\d+ da URL atual ou de links
                        if not discovered:
                            try:
                                current_url = page.url
                                m = re.search(r"/users/(\d+)", current_url)
                                if not m:
                                    # Procura em qualquer link da página
                                    hrefs = page.evaluate(
                                        "() => Array.from(document.querySelectorAll('a[href]'))"
                                        ".map(a => a.href)"
                                    )
                                    for href in hrefs:
                                        m = re.search(r"/users/(\d+)", href)
                                        if m:
                                            break
                                if m:
                                    uid = m.group(1)
                                    discovered = f"{base_url.rstrip('/')}/users/{uid}/edit"
                            except Exception:
                                pass

                        if discovered:
                            log_ok(f"URL do perfil descoberta: {discovered}")
                            rdb.save_platform_meta(platform_key, {"profile_edit_url": discovered})
                            sec_url = discovered
                        else:
                            log_warn("Não foi possível detectar a URL do perfil automaticamente.")
                            typed = input(
                                f"  Cole a URL de edição do perfil em {label} (ex: /users/12345/edit): "
                            ).strip()
                            if typed:
                                sec_url = typed if typed.startswith("http") \
                                    else base_url.rstrip("/") + "/" + typed.lstrip("/")
                                rdb.save_platform_meta(platform_key, {"profile_edit_url": sec_url})
                            else:
                                log_warn("URL não informada — pulando seção.")
                                continue

                log_info(f"URL: {sec_url}")

                try:
                    log_info("Navegando...")
                    page.goto(sec_url, timeout=35_000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)

                    # Detecta CAPTCHA
                    captcha_sel = (
                        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
                        ".cf-turnstile, #challenge-form"
                    )
                    if page.query_selector(captcha_sel):
                        print(f"\n  {_Y}⚠  CAPTCHA detectado. Resolva no navegador e pressione ENTER.{_RST}")
                        input()
                        page.wait_for_timeout(1500)

                    # Extrai campos COMPLETOS da página (novo mapeamento)
                    log_info("Mapeando campos da página...")
                    profile_fields = _extract_profile_fields_from_page(page)
                    if profile_fields:
                        rdb.save_profile_fields(platform_key, sec_url, profile_fields)
                        log_ok(f"✓ {len(profile_fields)} campo(s) descoberto(s) e salvos para '{sec_label}'")
                        # Exibe resumo dos campos encontrados
                        for fname, fdata in list(profile_fields.items())[:5]:
                            flabel = fdata.get("label", fname)[:40]
                            ftype = fdata.get("type", "?")
                            print(f"      • {fname:<20} ({ftype:<10}) — {flabel}")
                        if len(profile_fields) > 5:
                            print(f"      ... e mais {len(profile_fields) - 5} campo(s)")

                    # Extrai campos
                    log_info("Extraindo campos do formulário...")
                    form_info = _extract_form_info(page)

                    # Se não encontrou campos, tenta clicar em botão "Editar"
                    if form_info == "[]":
                        log_info("Nenhum campo direto — procurando botão Editar...")
                        for esel in [
                            "button:has-text('Editar')", "button:has-text('Edit')",
                            "a:has-text('Editar')",      "a:has-text('Edit')",
                            "[aria-label*='edit' i]",    ".edit-button",
                            "[data-control-name*='edit']",
                        ]:
                            try:
                                eb = page.query_selector(esel)
                                if eb and eb.is_visible():
                                    log_info(f"Clicando em botão de edição: '{esel}'")
                                    eb.click()
                                    page.wait_for_timeout(1500)
                                    form_info = _extract_form_info(page)
                                    if form_info != "[]":
                                        break
                            except Exception:
                                pass

                    if form_info == "[]":
                        log_warn(f"Nenhum campo editável encontrado em '{sec_label}'")
                        continue

                    try:
                        n_fields = len(json.loads(form_info))
                    except Exception:
                        n_fields = "?"
                    log_info(f"Formulário com {n_fields} campo(s) detectado — enviando para IA...")

                    ai_result = _ai_fill_profile_section(client, form_info, profile, label, sec_label)
                    fills   = ai_result.get("fills", [])
                    missing = ai_result.get("missing", [])

                    print(f"\n  {_CY}Resposta da IA:{_RST}")
                    print(f"    {_DIM}Raw: {json.dumps(ai_result)[:200]}{_RST}")
                    log_info(f"IA mapeou {len(fills)} campo(s)  |  {len(missing)} campo(s) em falta")

                    if not fills:
                        log_warn("IA não retornou nenhum campo para preencher")
                        print(f"  {_DIM}form_info recebido: {form_info[:300]}{_RST}")

                    # ── Preenche campos ───────────────────────────────────────
                    filled_count = 0
                    print(f"\n  {_CY}Preenchendo campos...{_RST}")

                    for fill in fills:
                        fname = fill.get("name", "")
                        fval  = fill.get("value", "")

                        if not fname:
                            log_warn(f"Campo sem nome na resposta da IA: {fill}")
                            continue
                        if not fval:
                            log_warn(f"Campo '{fname}' sem valor na resposta da IA")
                            continue

                        # Tenta vários seletores
                        found = False
                        selectors_tried = []

                        for fsel in [
                            f"[name='{fname}']",
                            f"#{fname}",
                            f"[id='{fname}']",
                            f"[aria-label*='{fname}' i]",
                            f"[placeholder*='{fname}' i]",
                            f"input[name*='{fname}']",
                            f"textarea[name*='{fname}']",
                            f"select[name*='{fname}']",
                        ]:
                            selectors_tried.append(fsel)
                            try:
                                fel = page.query_selector(fsel)

                                if not fel:
                                    continue

                                # Scroll até o elemento
                                fel.scroll_into_view_if_needed()
                                page.wait_for_timeout(300)

                                if not fel.is_visible():
                                    continue

                                tag   = fel.evaluate("e => e.tagName.toLowerCase()")
                                ftype = (fel.get_attribute("type") or "").lower()

                                # Preenche baseado no tipo
                                if tag == "select":
                                    try:
                                        fel.select_option(label=str(fval))
                                    except Exception:
                                        try:
                                            fel.select_option(value=str(fval))
                                        except Exception:
                                            log_warn(f"Não conseguiu selecionar valor '{fval}' no select '{fname}'")
                                            continue

                                elif ftype in ("checkbox", "radio"):
                                    should_check = str(fval).lower() in ("true", "yes", "sim", "1", "x")
                                    is_checked = fel.is_checked()
                                    if should_check and not is_checked:
                                        fel.click()
                                    elif not should_check and is_checked:
                                        fel.click()

                                elif tag == "textarea":
                                    fel.click()
                                    fel.triple_click()
                                    fel.fill(str(fval))
                                    fel.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")

                                else:
                                    # Input normal
                                    fel.click()
                                    fel.triple_click()
                                    fel.fill(str(fval))
                                    # Trigga mudanças
                                    fel.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
                                    fel.evaluate("el => el.dispatchEvent(new Event('input', { bubbles: true }))")
                                    page.wait_for_timeout(200)

                                fval_display = str(fval)[:50]
                                print(f"      {_G}✓{_RST} {fname:<20} = {fval_display}")
                                filled_count += 1
                                found = True
                                break

                            except Exception as e:
                                pass  # seletor não funcionou, tenta o próximo

                        if not found:
                            sels_str = " | ".join(selectors_tried[:3])
                            print(f"      {_Y}✗{_RST} {fname:<20} — Tentou: {sels_str}")

                    if filled_count:
                        log_ok(f"\n{filled_count} campo(s) preenchido(s) com sucesso")

                    # ── Campos faltando ───────────────────────────────────────
                    for missing_desc in missing:
                        filled_val = _ask_missing_field(missing_desc, profile, rdb)
                        if not filled_val:
                            continue
                        for fsel in [
                            f"[aria-label*='{missing_desc[:25]}' i]",
                            f"[placeholder*='{missing_desc[:25]}' i]",
                            f"[name*='{missing_desc[:20].lower().replace(' ','_')}']",
                        ]:
                            try:
                                fel = page.query_selector(fsel)
                                if fel and fel.is_visible():
                                    fel.triple_click()
                                    fel.fill(filled_val)
                                    log_info(f"  ✎ {missing_desc[:40]}: {filled_val[:40]}")
                                    break
                            except Exception:
                                pass

                    # ── Salva a seção ─────────────────────────────────────────
                    if filled_count > 0:
                        print(f"\n  {_CY}Salvando seção...{_RST}")
                        log_info("Procurando botão de salvar...")
                        saved = False

                        for bsel in [
                            "button:has-text('Salvar')",
                            "button:has-text('Save')",
                            "button:has-text('Atualizar')",
                            "button:has-text('Update')",
                            "button:has-text('Confirmar')",
                            "button:has-text('Confirm')",
                            "button[type='submit']:visible",
                            "button[aria-label*='save' i]",
                            "button[aria-label*='update' i]",
                            "[role='button']:has-text('Salvar')",
                            "[role='button']:has-text('Save')",
                            "a:has-text('Salvar')",
                            "a:has-text('Save')",
                            ".btn-primary:visible",
                            ".btn-submit:visible",
                        ]:
                            try:
                                btn = page.query_selector(bsel)
                                if btn and btn.is_visible() and btn.is_enabled():
                                    btn_text = btn.inner_text()[:30]
                                    print(f"      Clicando em: {btn_text}")
                                    btn.click()
                                    page.wait_for_timeout(2000)
                                    saved      = True
                                    any_success = True
                                    log_ok(f"Seção '{sec_label}' salva com sucesso!")
                                    break
                            except Exception as e:
                                pass

                        if not saved:
                            print(f"\n  {_Y}⚠  Botão de salvar não encontrado automaticamente.{_RST}")
                            print(f"  {_DIM}Opções:{_RST}")
                            print(f"    1. Procurando submissão automática...")
                            page.wait_for_timeout(3000)

                            # Tenta envio de formulário automático se houver
                            try:
                                forms = page.query_selector_all("form")
                                if forms:
                                    forms[0].evaluate("f => f.submit()")
                                    page.wait_for_timeout(2000)
                                    saved = True
                                    log_ok("Formulário enviado automaticamente")
                            except Exception:
                                pass

                            if not saved:
                                print(f"    2. {_Y}Salve manualmente no navegador e pressione ENTER{_RST}")
                                input()
                                any_success = True   # assume que o usuário salvou

                except Exception as exc:
                    log_err(f"Erro na seção '{sec_label}': {exc}")

            page.wait_for_timeout(1000)

            # Salva o storage_state atualizado (após navegação e possíveis mudanças de estado)
            updated_storage = context.storage_state()
            rdb.save_storage_state(platform_key, updated_storage)
            log_ok(f"Storage state atualizado e salvo para {label}")

            context.close()

    except KeyboardInterrupt:
        log_warn("Atualização interrompida pelo usuário.")
    except Exception as exc:
        log_err(f"Erro ao atualizar perfil em {label}: {exc}")

    return any_success


def profile_update_menu(
    rdb:     "MongoManager",
    profile: "Optional[dict]" = None,
    client:  "Optional[Any]"  = None,
) -> None:
    """
    Menu para selecionar plataformas onde atualizar o perfil profissional.
    Faz login automático se necessário, usa IA para preencher e salvar.
    """
    clr()
    section("Atualizar perfil nas plataformas")

    if not profile:
        log_warn("Perfil não disponível — carregue o currículo primeiro.")
        input(f"\n  {_DIM}ENTER para voltar...{_RST}")
        return
    if not client:
        log_warn("API Groq indisponível — o preenchimento por IA requer a API.")
        input(f"\n  {_DIM}ENTER para voltar...{_RST}")
        return

    print(f"  {_DIM}Selecione as plataformas onde atualizar seu perfil.{_RST}")
    print(f"  {_DIM}Se não estiver logado, o navegador abrirá para login antes.{_RST}\n")

    # Carrega todos os cookies de autenticação salvos
    auth_cookies_all = rdb.load_all_auth_cookies()

    # Monta choices com status de login e número de seções
    choices = []
    for key in PROFILE_EDIT_SECTIONS:
        src     = SOURCES.get(key, {})
        lbl     = src.get("label", key)
        # Verifica login em ambos os locais: cookies no banco E perfil persistente
        logged  = bool(auth_cookies_all.get(key)) or _has_browser_session(key)
        status     = "✔ logado" if logged else "○ não logado"
        n_secs     = len(PROFILE_EDIT_SECTIONS[key])
        choice_lbl = f"{lbl:<20}  {status:<15}  ({n_secs} seção(ões))"
        choices.append(questionary.Choice(title=choice_lbl, value=key, checked=False))

    chosen = questionary.checkbox(
        "Plataformas para atualizar (ESPAÇO = marcar  |  ENTER = confirmar):",
        choices=choices,
        style=Q_STYLE,
    ).ask()

    if not chosen:
        return

    print()
    show_browser = questionary.confirm(
        "Mostrar o navegador durante a atualização?  (Não = roda em background)",
        default=True,
        style=Q_STYLE,
    ).ask()
    if show_browser is None:
        return

    results: dict[str, int] = {"success": 0, "failed": 0}

    for idx, platform_key in enumerate(chosen, 1):
        src   = SOURCES.get(platform_key, {})
        label = src.get("label", platform_key)
        print(f"\n  {_DIM}━━━ Plataforma {idx}/{len(chosen)}: {label} ━━━{_RST}")

        ok = update_profile_on_platform(
            platform_key, profile, client, rdb, auth_cookies_all,
            show_browser=show_browser,
        )
        results["success" if ok else "failed"] += 1

        if idx < len(chosen):
            nxt = questionary.confirm(
                f"Continuar para a próxima plataforma?", default=True, style=Q_STYLE,
            ).ask()
            if not nxt:
                break

    clr()
    section("Resultado da atualização de perfil")
    print()
    if results["success"]:
        log_ok(f"✅  Plataformas atualizadas: {_BD}{results['success']}{_RST}")
        print(f"  {_DIM}Perfis atualizados com sucesso!{_RST}")
    if results["failed"]:
        log_err(f"❌  Falhas:                  {results['failed']}")
        print(f"  {_DIM}Algumas plataformas tiveram problemas.{_RST}")
    print(f"  {_DIM}Navegador fechado automaticamente.{_RST}\n")
    input(f"  {_DIM}ENTER para voltar ao menu...{_RST}")


def resume_review_menu(
    rdb:         "MongoManager",
    profile:     "Optional[dict]"  = None,
    client:      "Optional[Any]"   = None,
    resume_path: str            = "",
) -> None:
    """
    Menu para retomar a revisão de vagas já mapeadas e avaliadas em sessões anteriores.
    Permite escolher uma sessão específica ou revisar todas juntas.
    """
    while True:
        clr()
        section("Continuar revisando vagas")
        sessions = rdb.get_all_sessions_with_jobs()

        if not sessions:
            log_warn("Nenhuma sessão com vagas avaliadas encontrada ainda.")
            log_info("Execute uma busca primeiro para mapear e avaliar vagas.")
            input(f"\n  {_DIM}ENTER para voltar...{_RST}")
            return

        choices = []

        # Opção "todas as sessões" quando houver mais de uma
        total_matched = sum(s["matched"] for s in sessions)
        if len(sessions) > 1 and total_matched > 0:
            choices.append(questionary.Choice(
                f"📋  Todas as sessões  ({total_matched} vagas com match ≥ {MIN_MATCH_SCORE}%)",
                value="__all__",
            ))
            choices.append(questionary.Choice(
                title="─── Sessões individuais ───",
                value="__divider__",
                disabled="",
            ))

        for s in sessions[:20]:          # até 20 sessões mais recentes
            ts    = s["started_at"]
            date  = ts[:10]  if len(ts) >= 10 else s["session_id"]
            hora  = ts[11:16] if len(ts) >= 16 else ""
            query = s["query"][:38] if s["query"] else "?"
            label = (
                f"📅  {date} {hora}  —  {query}"
                f"  ({s['matched']} match / {s['total']} total)"
            )
            choices.append(questionary.Choice(label, value=s["session_id"]))

        choices.append(questionary.Choice("← Voltar", value="__back__"))

        chosen = _abort_if_none(
            questionary.select(
                "Selecione a sessão para revisar:",
                choices=choices,
                style=Q_STYLE,
            ).ask(),
            "Retomada de revisão",
        )

        if chosen in ("__back__", None):
            return
        if chosen == "__divider__":
            continue

        # ── Carrega vagas ─────────────────────────────────────────────────────
        if chosen == "__all__":
            jobs: list[dict] = []
            seen_ids: set[str] = set()
            for s in sessions:
                for job in rdb.get_all_jobs_for_session(s["session_id"]):
                    jid = job.get("job_id", "")
                    if jid not in seen_ids:
                        seen_ids.add(jid)
                        jobs.append(job)
            jobs.sort(key=lambda j: j.get("score", 0), reverse=True)
            sess_label = f"todas ({len(sessions)} sessões)"
        else:
            jobs = rdb.get_all_jobs_for_session(chosen)
            meta = next((s for s in sessions if s["session_id"] == chosen), {})
            sess_label = meta.get("query", chosen)[:45]

        if not jobs:
            log_warn("Nenhuma vaga avaliada encontrada nesta sessão.")
            input(f"  {_DIM}ENTER para voltar...{_RST}")
            continue

        # Anota decisão prévia em cada vaga (usada pelo _print_job_card)
        for job in jobs:
            decision = rdb.get_prior_decision(job.get("link", ""))
            if decision:
                job["prior_decision"] = decision

        section(f"Revisando: {sess_label}  —  {len(jobs)} vagas")
        print(f"  {_DIM}Vagas já decididas aparecem marcadas mas podem ser reavaliadas.{_RST}\n")

        auto_apply_jobs = review_jobs(jobs, rdb)

        # Candidatura automática se o usuário pressionou [P] e as dependências estão disponíveis
        if auto_apply_jobs and profile and client and resume_path:
            auto_apply_session(auto_apply_jobs, profile, client, rdb, resume_path)


# ──────────────────────────────────────────────────────────────────────────────
# Revisão de vagas — navegação por teclado
# ──────────────────────────────────────────────────────────────────────────────

def _read_key() -> str:
    """
    Lê um único caractere sem precisar de Enter (raw terminal).
    Retorna lowercase para letras; 'UP'/'DOWN'/'LEFT'/'RIGHT' para setas; 'ESC' demais escapes.
    """
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":                          # início de sequência de escape
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3, "ESC")
            return "ESC"
        if ch == "\r" or ch == "\n":
            return "ENTER"
        if ch == "\x03":                          # Ctrl+C
            raise KeyboardInterrupt
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _wrap_text(text: str, width: int = 78, indent: str = "  ") -> str:
    """Quebra texto em linhas respeitando a largura do terminal."""
    words   = text.split()
    lines   = []
    current = indent
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current.rstrip())
            current = indent + word + " "
        else:
            current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)


def _print_job_card(
    job:       dict,
    idx:       int,
    total:     int,
    full_desc: bool = False,
    decision:  str  = "",
) -> None:
    """Renderiza o card completo de uma vaga com layout rico."""
    cols      = min(shutil.get_terminal_size((80, 24)).columns, 90)
    divider   = f"{_BD}{_CY}{'─' * cols}{_RST}"
    thin_div  = f"{_DIM}{'╌' * cols}{_RST}"

    score     = job.get("score", 0)
    if score >= 80:
        score_col   = _G
        compat_lbl  = f"{_G}{_BD}Alta compatibilidade{_RST}"
    elif score >= 60:
        score_col   = _Y
        compat_lbl  = f"{_Y}{_BD}Compatibilidade média{_RST}"
    elif score >= 40:
        score_col   = "\033[33m"   # laranja
        compat_lbl  = f"\033[33m{_BD}Baixa compatibilidade{_RST}"
    else:
        score_col   = _R
        compat_lbl  = f"{_R}{_BD}Pouca aderência{_RST}"

    filled = score // 10
    bar    = f"{score_col}{'█' * filled}{_DIM}{'░' * (10 - filled)}{_RST}"

    # ── Barra de progresso da revisão ─────────────────────────────────────────
    prog_pct  = int((idx / total) * 20)
    prog_bar  = f"{_CY}{'▰' * prog_pct}{_DIM}{'▱' * (20 - prog_pct)}{_RST}"

    # ── Status de decisão já tomada (sessão atual) ────────────────────────────
    dec_label = {
        "accepted": f"  {_G}{_BD}✅ Aceita{_RST}",
        "rejected": f"  {_R}{_BD}❌ Recusada{_RST}",
        "applied":  f"  {_B}{_BD}📤 Candidatei{_RST}",
    }.get(decision, "")

    # ── Decisão de sessão anterior (carregada por resume_review_menu) ─────────
    prior = job.get("prior_decision", "")
    prior_label = {
        "accepted": f"  {_G}↩ Antes: Curtida{_RST}",
        "rejected": f"  {_R}↩ Antes: Recusada{_RST}",
        "applied":  f"  {_CY}↩ Antes: Candidatei{_RST}",
    }.get(prior, "")

    print(f"\n{divider}")
    print(
        f"  {_BD}Vaga {idx}/{total}{_RST}  {prog_bar}  "
        f"{score_col}{_BD}{score}%{_RST}  {bar}  {compat_lbl}"
        f"  {_DIM}[{job.get('region','')}]{_RST}"
        f"{dec_label}{prior_label}"
    )
    print(divider)

    # ── Título / empresa / localização / metadados ────────────────────────────
    print(f"\n  {_BD}{job.get('title','Sem título')}{_RST}")

    # Empresa e localização em linha separada
    company_str  = job.get("company", "").strip()
    location_str = job.get("location", "").strip()
    published_str = job.get("published", "").strip()
    meta_parts = [p for p in [company_str, location_str] if p]
    if published_str:
        meta_parts.append(f"{_DIM}{published_str}{_RST}")
    if meta_parts:
        print(f"  {_DIM}{'  •  '.join(meta_parts)}{_RST}")

    # Easy Apply / candidatura externa + contagem de candidatos
    easy_apply  = job.get("easy_apply", False)
    applicants  = (job.get("applicants") or "").strip()
    badge_parts = []
    if easy_apply:
        badge_parts.append(f"{_G}⚡ Candidatura simplificada (Easy Apply){_RST}")
    elif "linkedin.com" in job.get("link", ""):
        badge_parts.append(f"{_DIM}↗  Redireciona para o site da empresa{_RST}")
    if applicants:
        badge_parts.append(f"{_DIM}👥 {applicants}{_RST}")
    if badge_parts:
        print(f"  {'    '.join(badge_parts)}")

    # ── Resumo da IA ───────────────────────────────────────────────────────────
    if job.get("ai_summary"):
        print(f"\n  {_DIM}💬  {job['ai_summary']}{_RST}")

    # ── Pontos fortes ──────────────────────────────────────────────────────────
    if job.get("match_reasons"):
        print(f"\n  {_G}✔  Pontos fortes:{_RST}")
        for r in job["match_reasons"]:
            print(f"     • {r}")

    # ── Gaps ──────────────────────────────────────────────────────────────────
    if job.get("gap_reasons"):
        print(f"\n  {_Y}⚠   Pontos de atenção:{_RST}")
        for g in job["gap_reasons"]:
            print(f"     • {g}")

    # ── Descrição ──────────────────────────────────────────────────────────────
    desc = (job.get("description") or job.get("snippet") or "").strip()
    if desc:
        print(f"\n{thin_div}")
        print(f"  {_DIM}📄  Descrição da vaga:{_RST}")
        if full_desc:
            print(_wrap_text(desc, width=cols - 2))
            print(f"\n  {_DIM}[ pressione {_BD}V{_DIM} para recolher ]{_RST}")
        else:
            # Preview maior: 700 chars, quebra em palavra
            preview_len = 700
            if len(desc) <= preview_len:
                print(_wrap_text(desc, width=cols - 2))
            else:
                preview = desc[:preview_len].rsplit(" ", 1)[0]
                print(_wrap_text(preview + "…", width=cols - 2))
                print(f"\n  {_DIM}[ pressione {_BD}V{_DIM} para ver a descrição completa ({len(desc)} chars) ]{_RST}")

    # ── Link ──────────────────────────────────────────────────────────────────
    print(f"\n  {_B}🔗  {job.get('link','')}{_RST}")


# ──────────────────────────────────────────────────────────────────────────────
# Candidatura automática com IA + Playwright
# ──────────────────────────────────────────────────────────────────────────────

def _extract_form_info(page: "Any") -> str:
    """Extrai campos do formulário visível na página via JS. Retorna JSON string."""
    try:
        return page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll('input,textarea,select,[contenteditable="true"]').forEach(el => {
                if (el.type === 'hidden' || el.style.display === 'none') return;
                const labelEl = (
                    document.querySelector('label[for="' + el.id + '"]') ||
                    el.closest('label')
                );
                const label = (
                    labelEl?.innerText ||
                    el.getAttribute('aria-label') ||
                    el.getAttribute('placeholder') ||
                    el.getAttribute('name') ||
                    el.id || ''
                ).trim().slice(0, 100);
                if (!label && !el.name && !el.id) return;
                const opts = el.tagName === 'SELECT'
                    ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 12)
                    : [];
                fields.push({
                    tag:      el.tagName.toLowerCase(),
                    type:     el.type || 'text',
                    name:     el.name || el.id || '',
                    label:    label,
                    required: el.required || false,
                    options:  opts,
                });
            });
            return JSON.stringify(fields.slice(0, 60));
        }""")
    except Exception:
        return "[]"


def _ai_fill_form(
    client:   "Groq",
    form_info: str,
    profile:  dict,
    job:      dict,
) -> dict:
    """
    IA analisa o formulário e retorna instruções de preenchimento mapeadas ao perfil.
    Retorna: {"fills": [{name, value}], "missing": ["descrição"], "file_upload": bool}
    """
    # Compacta o perfil para caber no prompt
    personal = profile.get("personal", {})
    prof     = profile.get("professional", {})
    tech     = profile.get("technical", {})
    extra    = profile.get("extra_info", {})

    profile_compact = {
        "name":          personal.get("name"),
        "email":         personal.get("email"),
        "phone":         personal.get("phone"),
        "location":      personal.get("location"),
        "linkedin":      personal.get("linkedin"),
        "github":        personal.get("github"),
        "portfolio":     personal.get("portfolio"),
        "seniority":     prof.get("seniority"),
        "years_exp":     prof.get("experience_years"),
        "current_role":  prof.get("current_role"),
        "objective":     prof.get("objective"),
        "top_techs":     profile.get("top_technologies", []),
        "main_stack":    profile.get("main_stack"),
        "english_level": profile.get("english_level"),
        "languages":     profile.get("languages", []),
        "education":     profile.get("education", []),
        "soft_skills":   profile.get("soft_skills", []),
        **extra,
    }
    profile_str = json.dumps(profile_compact, ensure_ascii=False)[:2500]

    prompt = (
        "Você preenche formulários de candidatura de emprego automaticamente.\n\n"
        f"PERFIL DO CANDIDATO:\n{profile_str}\n\n"
        f"VAGA: {job.get('title','')} em {job.get('company','')}\n\n"
        f"CAMPOS DO FORMULÁRIO (JSON):\n{form_info}\n\n"
        "Para cada campo do formulário, use os dados do perfil para determinar o valor.\n"
        "Retorne APENAS JSON válido:\n"
        "{\n"
        '  "fills": [{"name": "field_name_or_id", "value": "valor a preencher"}],\n'
        '  "missing": ["descrição do campo obrigatório sem dados no perfil"],\n'
        '  "file_upload": true|false\n'
        "}\n"
        "Para campos de upload de currículo/CV, use value=\"__RESUME__\".\n"
        "Para selects, use exatamente um dos textos das options listadas.\n"
        "Omita campos opcionais sem informação correspondente no perfil.\n"
        "Responda SOMENTE com JSON."
    )

    try:
        resp = client.chat.completions.create(
            model=_ACTIVE_MODEL,
            max_tokens=1500,  # Aumentado para evitar truncamento
            messages=[
                {"role": "system", "content": "Responda APENAS com JSON válido, sem markdown."},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log_err(f"JSON inválido do formulário (tentando recuperar): {exc}")

        # Tenta fechar o JSON truncado
        try:
            if '"fills"' in raw:
                import re
                # Procura o último field completo
                matches = list(re.finditer(r'\{"name"[^}]*\}', raw))
                if matches:
                    last_valid = matches[-1].end()
                    raw_fixed = raw[:last_valid] + '], "missing": [], "file_upload": false}'
                    result = json.loads(raw_fixed)
                    return result
        except Exception:
            pass

        log_err(f"Raw: {raw[:300]}...")
        return {"fills": [], "missing": [], "file_upload": False}
    except Exception as exc:
        log_err(f"IA não pôde analisar o formulário: {exc}")
        return {"fills": [], "missing": [], "file_upload": False}


def _ask_missing_field(field_desc: str, profile: dict, rdb: "MongoManager") -> str:
    """
    Pede ao usuário no terminal um campo não encontrado no perfil.
    Salva o valor no MongoDB e atualiza o dict profile em memória.
    """
    print(f"\n  {_Y}Campo necessário não encontrado no perfil:{_RST}")
    print(f"  {_BD}{field_desc}{_RST}")
    value = input(f"  → Digite o valor (ENTER para pular): ").strip()
    if value:
        key = re.sub(r"[^a-z0-9_]", "_", field_desc.lower())[:40].strip("_")
        rdb.update_profile_extra(key, value)
        if "extra_info" not in profile:
            profile["extra_info"] = {}
        profile["extra_info"][key] = value
        log_ok(f"Salvo no perfil: {key} = {value}")
    return value


def auto_apply_job(
    job:              dict,
    profile:          dict,
    client:           "Groq",
    rdb:              "MongoManager",
    resume_path:      str,
    auth_cookies_all: dict,
) -> str:
    """
    Abre o browser, navega até a vaga, usa IA para preencher o formulário
    com dados do perfil e submete. Pede no terminal campos em falta.
    Retorna: "success" | "manual_needed" | "failed"
    """
    link  = job.get("link", "")
    title = job.get("title", "vaga")
    comp  = job.get("company", "")

    if not link:
        log_err("Vaga sem link — pulando.")
        return "failed"

    clr()
    section(f"Auto-candidatura  —  {title[:50]}")
    print(f"  {_DIM}Empresa:{_RST} {comp}")
    print(f"  {_DIM}Link:   {_RST} {link}\n")

    try:
        from playwright.sync_api import sync_playwright as _sync_pw
    except ImportError:
        log_err("playwright não instalado.")
        log_info("Execute: pip install playwright && playwright install chromium")
        return "failed"

    status = "failed"

    try:
        with _sync_pw() as pw:
            # Detecta plataforma pelo link para usar o perfil correto
            platform_key_for_job = None
            for _pk, _src in SOURCES.items():
                _domain = _src.get("domain", "")
                if _domain and _domain in link:
                    platform_key_for_job = _pk
                    break
            # Se não detectou, usa "default" como chave genérica
            _profile_key = platform_key_for_job or "default"

            context = _launch_persistent(pw, _profile_key, headless=False)

            # Injeta storage_state salvo (localStorage, sessionStorage, cookies)
            saved_storage = rdb.load_storage_state(_profile_key)
            if saved_storage:
                _inject_storage_state(context, saved_storage)
                log_info(f"Sessão carregada para: {_profile_key}")

            page    = context.new_page()
            log_info(f"Navegando para: {link}")
            try:
                page.goto(link, timeout=45_000, wait_until="domcontentloaded")
            except Exception:
                page.goto(link, timeout=45_000, wait_until="commit")
            page.wait_for_timeout(2500)

            # Tenta detectar e clicar em botão de candidatura rápida
            apply_selectors = [
                "button:has-text('Easy Apply')",
                "button:has-text('Candidatura simplificada')",
                "button:has-text('Quick Apply')",
                "button:has-text('Candidatar-se')",
                "button:has-text('Me candidatar')",
                "button:has-text('Apply Now')",
                "button:has-text('Aplicar agora')",
                ".jobs-apply-button",
                "[data-control-name='jobdetails_topcard_inapply']",
                "a:has-text('Apply')",
            ]
            for sel in apply_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        log_ok(f"Botão de candidatura encontrado — clicando...")
                        btn.click()
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            # Loop de preenchimento (multi-step)
            for step in range(10):
                # Detecta CAPTCHA
                captcha_sel = (
                    "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
                    ".cf-turnstile, #challenge-form, iframe[title*='captcha' i]"
                )
                if page.query_selector(captcha_sel):
                    print(f"\n  {_Y}⚠  CAPTCHA detectado no passo {step+1}.{_RST}")
                    print(f"  {_DIM}Resolva no navegador e pressione ENTER aqui...{_RST}")
                    input()
                    page.wait_for_timeout(1500)

                # Verifica se a página já mostra confirmação de envio
                try:
                    page_text = page.inner_text("body")[:800].lower()
                except Exception:
                    page_text = ""

                success_words = [
                    "thank you", "obrigado", "application submitted",
                    "candidatura enviada", "candidatura recebida",
                    "successfully applied", "you've applied",
                ]
                if any(w in page_text for w in success_words):
                    log_ok("Confirmação de envio detectada na página!")
                    status = "success"
                    break

                # Extrai e preenche formulário
                form_info = _extract_form_info(page)
                if form_info == "[]":
                    # Sem campos visíveis — formulário concluído ou redirecionou
                    if step > 0:
                        status = "success"
                    break

                log_info(f"Passo {step+1}: analisando formulário com IA...")
                ai_result = _ai_fill_form(client, form_info, profile, job)

                # Preenche campos mapeados pela IA
                for fill in ai_result.get("fills", []):
                    fname = fill.get("name", "")
                    fval  = fill.get("value", "")
                    if not fname or not fval:
                        continue

                    if fval == "__RESUME__":
                        for fsel in [
                            f"input[name='{fname}']",
                            "input[type='file'][accept*='pdf']",
                            "input[type='file']",
                        ]:
                            try:
                                fel = page.query_selector(fsel)
                                if fel:
                                    fel.set_input_files(resume_path)
                                    log_ok(f"Currículo enviado via campo '{fname}'")
                                    break
                            except Exception:
                                pass
                        continue

                    for fsel in [
                        f"[name='{fname}']",
                        f"#{fname}",
                        f"[aria-label*='{fname}']",
                        f"[placeholder*='{fname}']",
                    ]:
                        try:
                            fel = page.query_selector(fsel)
                            if not fel or not fel.is_visible():
                                continue
                            tag  = fel.evaluate("e => e.tagName.toLowerCase()")
                            ftype = (fel.get_attribute("type") or "").lower()
                            if tag == "select":
                                try:
                                    fel.select_option(label=fval)
                                except Exception:
                                    fel.select_option(value=fval)
                            elif ftype in ("checkbox", "radio"):
                                if fval.lower() in ("true", "yes", "sim", "1"):
                                    fel.check()
                            else:
                                fel.triple_click()
                                fel.fill(fval)
                            break
                        except Exception:
                            pass

                # Pede ao usuário campos obrigatórios em falta
                for missing_desc in ai_result.get("missing", []):
                    filled_val = _ask_missing_field(missing_desc, profile, rdb)
                    if not filled_val:
                        continue
                    # Tentativa heurística de preencher com o valor informado
                    for fsel in [
                        f"[aria-label*='{missing_desc[:25]}' i]",
                        f"[placeholder*='{missing_desc[:25]}' i]",
                        f"[name*='{missing_desc[:20].lower().replace(' ','_')}']",
                    ]:
                        try:
                            fel = page.query_selector(fsel)
                            if fel and fel.is_visible():
                                fel.triple_click()
                                fel.fill(filled_val)
                                break
                        except Exception:
                            pass

                # Procura botão de avanço / envio
                btn_clicked = False
                submit_words = ["submit","enviar","send","apply","candidatar","finalizar","finish"]
                next_words   = ["next","próximo","continue","continuar","review","avançar"]

                for bsel in [
                    "button[type='submit']:visible",
                    "button:has-text('Enviar'):visible",
                    "button:has-text('Submit'):visible",
                    "button:has-text('Apply'):visible",
                    "button:has-text('Next'):visible",
                    "button:has-text('Próximo'):visible",
                    "button:has-text('Continue'):visible",
                    "button:has-text('Continuar'):visible",
                    "button:has-text('Finalizar'):visible",
                    "button:has-text('Review'):visible",
                ]:
                    try:
                        btn = page.query_selector(bsel)
                        if not btn or not btn.is_visible() or not btn.is_enabled():
                            continue
                        btext = btn.inner_text().lower().strip()
                        is_submit = any(w in btext for w in submit_words)
                        is_next   = any(w in btext for w in next_words)
                        if is_submit or is_next:
                            log_info(f"Clicando: '{btn.inner_text().strip()}'")
                            btn.click()
                            page.wait_for_timeout(2000)
                            btn_clicked = True
                            if is_submit:
                                status = "success"
                            break
                    except Exception:
                        pass

                if not btn_clicked:
                    print(f"\n  {_Y}Não encontrei o botão de envio no passo {step+1}.{_RST}")
                    print(f"  {_DIM}Complete manualmente no navegador.{_RST}")
                    print(f"  {_DIM}[ENTER] Confirmei que enviei   [s+ENTER] Pular esta vaga{_RST}")
                    resp = input("  → ").strip().lower()
                    if resp == "s":
                        status = "manual_needed"
                    else:
                        status = "success"
                    break

                if status == "success":
                    break

            page.wait_for_timeout(1200)
            context.close()

    except KeyboardInterrupt:
        log_warn("Candidatura interrompida pelo usuário.")
        status = "manual_needed"
    except Exception as exc:
        log_err(f"Erro durante candidatura automática: {exc}")
        status = "failed"

    # Persiste resultado
    rdb.save_application_result(job, status)
    if status == "success":
        rdb.record_job_decision(job, "applied")
        log_ok(f"✅  Candidatura registrada como enviada!")
    elif status == "manual_needed":
        log_warn(f"⚠   Candidatura marcada como 'ação manual necessária'.")
    else:
        log_err(f"❌  Candidatura falhou.")

    input(f"\n  {_DIM}ENTER para continuar...{_RST}")
    return status


def auto_apply_session(
    jobs:        list[dict],
    profile:     dict,
    client:      "Groq",
    rdb:         "MongoManager",
    resume_path: str,
) -> None:
    """
    Orquestra candidatura automática para uma lista de vagas marcadas com [P].
    Carrega cookies de auth do MongoDB, confirma com o usuário, processa cada vaga.
    """
    if not jobs:
        return

    clr()
    section(f"Candidatura automática  —  {len(jobs)} vaga(s) na fila")
    print(f"  {_DIM}O navegador abrirá para cada vaga.{_RST}")
    print(f"  {_DIM}Você pode acompanhar e intervir sempre que necessário.{_RST}")
    print(f"  {_DIM}Campos sem informação no perfil serão pedidos aqui no terminal.{_RST}\n")

    for idx, job in enumerate(jobs, 1):
        sc  = job.get("score", 0)
        col = _G if sc >= 90 else (_Y if sc >= 80 else _R)
        print(f"  {col}{_BD}{sc:3d}%{_RST}  {job.get('title','')[:50]}  {_DIM}{job.get('company','')[:30]}{_RST}")
    print()

    confirm = questionary.confirm(
        f"Iniciar candidatura automática para {len(jobs)} vaga(s)?",
        default=True,
        style=Q_STYLE,
    ).ask()

    if not confirm:
        return

    # Carrega todos os cookies de auth salvos no MongoDB
    auth_cookies_all = rdb.load_all_auth_cookies()

    results: dict[str, int] = {"success": 0, "manual_needed": 0, "failed": 0}

    for idx, job in enumerate(jobs, 1):
        print(f"\n  {_DIM}━━━ Vaga {idx}/{len(jobs)} ━━━{_RST}")
        status = auto_apply_job(job, profile, client, rdb, resume_path, auth_cookies_all)
        results[status] = results.get(status, 0) + 1

        if idx < len(jobs):
            clr()
            section("Candidatura automática")
            nxt = questionary.confirm(
                f"Continuar para a próxima vaga? [{idx+1}/{len(jobs)}]",
                default=True,
                style=Q_STYLE,
            ).ask()
            if not nxt:
                break

    clr()
    section("Resultado das candidaturas automáticas")
    if results["success"]:       log_ok(f"✅  Enviadas com sucesso:         {_BD}{results['success']}{_RST}")
    if results["manual_needed"]: log_warn(f"⚠   Ação manual necessária:      {results['manual_needed']}")
    if results["failed"]:        log_err(f"❌  Falhas:                       {results['failed']}")
    input(f"\n  {_DIM}ENTER para continuar...{_RST}")


def _print_shortcut_bar(wrap_hint: str = "") -> None:
    """Imprime a barra de atalhos de teclado na parte inferior.
    wrap_hint: mensagem curta exibida quando a navegação deu a volta (carrossel).
    """
    cols    = min(shutil.get_terminal_size((80, 24)).columns, 90)
    divider = f"{_BD}{_CY}{'─' * cols}{_RST}"
    print(f"\n{divider}")

    def key(k: str, label: str, color: str = _BD) -> str:
        return f"{color}[{k}]{_RST} {label}"

    row1 = "  ".join([
        key("A", "Aceitar",        _G),
        key("R", "Recusar",        _R),
        key("C", "Candidatei",     _B),
        key("P", "Auto-candidatar",_CY),
        key("O", "Abrir browser"),
        key("V", "Ver desc."),
    ])
    # B e N sempre disponíveis — modo carrossel
    row2 = "  ".join([
        key("B", "Anterior"),
        key("N", "Próxima"),
        key("L", "Listar vagas"),
        key("Q", "Sair"),
    ])

    print(f"  {row1}")
    print(f"  {row2}")
    if wrap_hint:
        print(f"  {_DIM}{wrap_hint}{_RST}")
    print(f"{divider}")
    print(f"  {_DIM}Pressione a tecla — sem precisar de Enter{_RST}  ", end="", flush=True)


def review_jobs(matched_jobs: list[dict], rdb: "MongoManager") -> list[dict]:
    """
    Revisão interativa de vagas com navegação por atalhos de teclado.
    Retorna lista de vagas marcadas com [P] para candidatura automática.

    Atalhos:
      A — Aceitar (tenho interesse)
      R — Recusar (não aparece mais em buscas futuras)
      C — Já me candidatei
      P — Auto-candidatar (enfileira para candidatura automática)
      O — Abrir link no browser padrão
      V — Ver descrição completa (toggle)
      N — Próxima vaga (decidir depois)
      B — Vaga anterior
      L — Listar todas as vagas e pular para qualquer uma
      Q — Encerrar revisão
    """
    if not matched_jobs:
        return

    clr()
    n_match = sum(1 for j in matched_jobs if j.get("score", 0) >= MIN_MATCH_SCORE)
    section(f"Revisão de vagas  —  {len(matched_jobs)} avaliadas  ({n_match} com ≥ {MIN_MATCH_SCORE}%)")
    print(f"  {_DIM}Vagas ordenadas por compatibilidade. Cores: {_G}verde ≥80%{_RST}  {_Y}amarelo ≥60%{_RST}  {_R}vermelho <60%{_RST}")
    print(f"  {_DIM}Vagas recusadas não voltarão a aparecer em buscas futuras.{_RST}")
    print(f"  {_DIM}Atalhos: A=aceitar  R=recusar  C=candidatei  O=abrir link  V=ver descrição  N=próxima  Q=sair{_RST}\n")

    decisions:     dict[str, str] = {}   # job_id → "accepted"|"rejected"|"applied"
    history:       list[int]      = []   # pilha de índices para B=voltar
    full_desc_on:  bool           = False  # estado persistente do toggle V
    wrap_hint:     str            = ""     # mensagem de carrossel
    i = 0
    n = len(matched_jobs)

    def _advance(idx: int) -> tuple[int, str]:
        """Avança para a próxima vaga em modo carrossel. Retorna (novo_idx, hint)."""
        nxt = (idx + 1) % n
        hint = f"↺  Voltou ao início — vaga 1 de {n}" if nxt == 0 and idx == n - 1 else ""
        return nxt, hint

    def _go_back(idx: int, hist: list[int]) -> tuple[int, str]:
        """Volta para a vaga anterior (histórico ou última do carrossel)."""
        if hist:
            return hist.pop(), ""
        # sem histórico — vai para a última (carrossel reverso)
        last = n - 1
        hint = f"↺  Voltou ao fim — vaga {n} de {n}" if idx == 0 else ""
        return last, hint

    while True:
        job    = matched_jobs[i]
        job_id = job.get("job_id", str(i))

        # Limpa o terminal — só a vaga atual fica visível
        print("\033[2J\033[H", end="", flush=True)

        _print_job_card(
            job,
            idx=i + 1,
            total=n,
            full_desc=full_desc_on,
            decision=decisions.get(job_id, ""),
        )
        # NÃO reseta full_desc_on aqui — V é toggle persistente

        _print_shortcut_bar(wrap_hint=wrap_hint)
        wrap_hint = ""   # limpa após exibir

        key = _read_key()
        print()   # quebra de linha após a tecla

        # ── Interpretação das teclas ───────────────────────────────────────────
        if key == "q":
            log_info("Revisão encerrada.")
            break

        elif key in ("n", "RIGHT", "ENTER"):
            history.append(i)
            i, wrap_hint = _advance(i)
            full_desc_on = False   # nova vaga começa recolhida

        elif key in ("b", "LEFT"):
            i, wrap_hint = _go_back(i, history)
            full_desc_on = False   # nova vaga começa recolhida

        elif key in ("UP", "DOWN"):
            pass   # teclas de seta não usadas aqui — ignorar silenciosamente

        elif key == "v":
            full_desc_on = not full_desc_on   # toggle: expande / recolhe

        elif key == "o":
            link = job.get("link","")
            if link:
                webbrowser.open(link)
                log_ok(f"Aberto: {link}")
            # Permanece na mesma vaga para decidir depois

        elif key == "l":
            # ── Lista todas as vagas para escolha rápida ─────────────────────
            # questionary NÃO renderiza ANSI — títulos devem ser texto puro
            print("\033[2J\033[H", end="", flush=True)
            section("Todas as vagas com match")
            list_choices = []
            for ji, jj in enumerate(matched_jobs):
                sc    = jj.get("score", 0)
                # Score com símbolo ASCII (sem ANSI)
                if sc >= 90:
                    score_str = f"★ {sc:3d}%"
                elif sc >= 80:
                    score_str = f"◆ {sc:3d}%"
                elif sc >= 60:
                    score_str = f"◇ {sc:3d}%"
                else:
                    score_str = f"· {sc:3d}%"
                d_ico = {
                    "accepted": "✅", "rejected": "❌", "applied": "📤",
                }.get(decisions.get(jj.get("job_id", str(ji)), ""), "  ")
                here  = " ← aqui" if ji == i else ""
                title_txt  = jj.get("title",  "")[:46]
                company_txt = jj.get("company","")[:22]
                list_choices.append(questionary.Choice(
                    title=f"{d_ico} {score_str}  {title_txt}  [{company_txt}]{here}",
                    value=ji,
                ))
            list_choices.append(questionary.Choice(
                title="← Cancelar (permanecer na vaga atual)",
                value=-1,
            ))
            jumped = questionary.select(
                "Ir para qual vaga?",
                choices=list_choices,
                style=Q_STYLE,
            ).ask()
            if jumped is not None and jumped >= 0:
                history.append(i)
                i = jumped
                full_desc_on = False   # nova vaga começa recolhida
            # Se -1 ou None → permanece

        elif key == "a":
            rdb.record_job_decision(job, "accepted")
            decisions[job_id] = "accepted"
            print(f"  {CHECK}  {_G}{_BD}Aceita!{_RST}  Registrado.")
            history.append(i)
            i, wrap_hint = _advance(i)
            full_desc_on = False

        elif key == "r":
            rdb.record_job_decision(job, "rejected")
            decisions[job_id] = "rejected"
            print(f"  {CROSS}  {_R}{_BD}Recusada.{_RST}  Esta vaga não aparecerá mais.")
            history.append(i)
            i, wrap_hint = _advance(i)
            full_desc_on = False

        elif key == "c":
            rdb.record_job_decision(job, "applied")
            decisions[job_id] = "applied"
            print(f"  {CHECK}  {_B}{_BD}Candidatura registrada!{_RST}")
            history.append(i)
            i, wrap_hint = _advance(i)
            full_desc_on = False

        elif key == "p":
            decisions[job_id] = "auto_apply"
            print(f"  {CHECK}  {_CY}{_BD}Adicionada à fila de candidatura automática!{_RST}")
            history.append(i)
            i, wrap_hint = _advance(i)
            full_desc_on = False

        # Qualquer outra tecla → ignora e reexibe

    # ── Resumo final da sessão de revisão ─────────────────────────────────────
    n_a  = sum(1 for v in decisions.values() if v == "accepted")
    n_r  = sum(1 for v in decisions.values() if v == "rejected")
    n_c  = sum(1 for v in decisions.values() if v == "applied")
    n_p  = sum(1 for v in decisions.values() if v == "auto_apply")
    n_sk = len(matched_jobs) - len(decisions)

    clr()
    section("Resumo da revisão")
    if n_a:  log_ok(f"✅  Aceitas:                     {_BD}{n_a}{_RST}")
    if n_r:  log_ok(f"❌  Recusadas:                   {_BD}{n_r}{_RST}")
    if n_c:  log_ok(f"📤  Candidaturas manuais:        {_BD}{n_c}{_RST}")
    if n_p:  log_ok(f"🤖  Auto-candidatura na fila:    {_BD}{n_p}{_RST}")
    if n_sk: log_info(f"→   Sem decisão (pendentes):     {n_sk}")

    # Retorna vagas marcadas para candidatura automática
    auto_apply_jobs = [
        job for job in matched_jobs
        if decisions.get(job.get("job_id", ""), "") == "auto_apply"
    ]
    return auto_apply_jobs


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def _print_header() -> None:
    clr()
    print(f"\n{_BD}{_CY}{'═' * 60}{_RST}")
    print(f"  {_BD}JOB HUNTER{_RST}  {_DIM}— Ctrl+C ou menu Sair para encerrar{_RST}")
    print(f"{_BD}{_CY}{'═' * 60}{_RST}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Busca vagas com base no seu currículo PDF (MongoDB + Groq)."
    )
    parser.add_argument("--resume",   required=True,  help="Caminho para o PDF do currículo")
    parser.add_argument("--query",    default=None,   help="Query de busca (pula o menu interativo)")
    parser.add_argument("--location", default="Remote", help="Localidade. Padrão: Remote")
    parser.add_argument("--source",   choices=SOURCE_CHOICES, default=None,
                        help="Fonte direta (indeed-br | indeed-us | linkedin | all)")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="Páginas por fonte (0 = todas)")
    parser.add_argument("--show-browser", action="store_true",
                        help="Compatibilidade — sem efeito (scraping sem browser)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Modo verbose: exibe todos os logs detalhados")
    args = parser.parse_args()

    # ── Aplica modo verbose ANTES de qualquer saída ────────────────────────────
    set_verbose(args.verbose)

    # Declara globais modificáveis no topo da função (antes de qualquer uso)
    global _global_rdb, _ACTIVE_MODEL

    # ── Tela de inicialização silenciosa ───────────────────────────────────────
    clr()
    print(f"\n  {_BD}{_CY}JOB HUNTER{_RST}  {_DIM}inicializando...{_RST}\n")

    # ── 1. MongoDB primeiro — é a fonte primária de toda a configuração ────────
    try:
        rdb = MongoManager("admin")
        _global_rdb = rdb
    except Exception as e:
        log_err(f"Erro ao conectar com MongoDB: {e}")
        rdb = None
        _global_rdb = None

    # ── 2. Carrega modelo do .env (fonte única) ────────────────────────────────
    saved_model = os.environ.get("MODEL", "").strip()
    if saved_model and saved_model in GROQ_MODELS and saved_model != _ACTIVE_MODEL:
        _ACTIVE_MODEL = saved_model

    # ── 3. Carrega GROQ_API_KEY do .env (fonte única) ──────────────────────────
    api_key = os.environ.get("GROQ_API_KEY", "").strip() or None

    if not api_key:
        print(f"\n  {CROSS} GROQ_API_KEY não configurada.")
        print(f"  {_DIM}Configure em: Configurações → IA  (ou exporte GROQ_API_KEY=gsk_...){_RST}")
        # Não encerra — permite usar o app sem API (revisar vagas salvas)

    # ── 4. Cria cliente Groq ────────────────────────────────────────────────────
    client = None
    if api_key:
        try:
            client = Groq(api_key=api_key)
        except Exception as e:
            log_err(f"Erro ao criar cliente Groq: {e}")

    # ── 5. Valida API — spinner visível, resultado atualizado antes do menu ───
    if client:
        sp_api = Spinner("Verificando API Groq...").start()
        try:
            ok = check_groq_api(client, rdb, quiet=True)   # set_api_ok() chamado internamente
        except Exception:
            set_api_ok(False)
            ok = False
        if ok:
            sp_api.stop(f"IA {_G}online{_RST}")
        else:
            sp_api.fail(f"IA {_R}offline{_RST}  {_DIM}— configure a chave em Configurações → IA{_RST}")
    else:
        print(f"  {_R}●{_RST}  IA offline  {_DIM}— GROQ_API_KEY não configurada{_RST}")

    # ── Lê currículo e extrai perfil — com feedback visual ────────────────────
    sp_cv = Spinner("Lendo currículo...").start()
    try:
        resume_text = extract_resume_text(args.resume, quiet=True)
        resume_hash = hashlib.md5(resume_text.encode()).hexdigest()
    except Exception as e:
        sp_cv.fail(f"Erro ao ler currículo: {e}")
        sys.exit(1)

    # ── Verifica hash do currículo ─────────────────────────────────────────────
    _resume_changed = False
    if rdb:
        try:
            stored_hash = rdb.get_resume_hash()
            if stored_hash and stored_hash != resume_hash:
                _resume_changed = True
                rdb.clear_seen_jobs()
                rdb.set_resume_hash(resume_hash)
            elif not stored_hash:
                rdb.set_resume_hash(resume_hash)
        except Exception:
            pass

    # ── Extrai perfil (pode usar cache do MongoDB — rápido na 2ª vez) ─────────
    sp_cv.update("Extraindo perfil do currículo...")
    profile    = extract_full_profile(client, resume_text, resume_hash, rdb, quiet=True)
    profile_json = (
        json.dumps(_profile_to_ai_summary(profile), ensure_ascii=False)
        if profile else "{}"
    )
    menu_hints = _profile_to_menu_hints(profile) if profile else {}
    cv_name    = Path(args.resume).stem[:40]
    sp_cv.stop(f"Currículo pronto  {_DIM}({cv_name}){_RST}")

    # ── Loop principal ─────────────────────────────────────────────────────────
    while True:
        try:

            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

            # ── Menu: preset / nova busca / histórico / sair ──────────────────
            if args.source and args.query:
                # Modo direto via flags — não exibe menu
                menu_result = None
            else:
                menu_result = select_preset_or_new(
                    rdb,
                    profile=profile,
                    client=client,
                    resume_path=args.resume,
                    resume_changed=_resume_changed,
                )
                _resume_changed = False   # mostra o aviso apenas na primeira abertura

            if menu_result == "__exit__":
                break

            # ── Currículo trocado em Configurações — recarrega tudo ───────────
            if menu_result == "__resume_changed__":
                new_resume = os.environ.get("RESUME_PATH", "").strip() or None
                if new_resume and Path(new_resume).exists():
                    try:
                        args.resume   = new_resume
                        resume_text   = extract_resume_text(new_resume, quiet=True)
                        resume_hash   = hashlib.md5(resume_text.encode()).hexdigest()
                        if rdb:
                            rdb.set_resume_hash(resume_hash)
                        profile       = extract_full_profile(client, resume_text, resume_hash, rdb, quiet=True)
                        profile_json  = json.dumps(_profile_to_ai_summary(profile), ensure_ascii=False) if profile else "{}"
                        menu_hints    = _profile_to_menu_hints(profile) if profile else {}
                        log_ok(f"Currículo recarregado: {_DIM}{new_resume}{_RST}")
                    except Exception as exc:
                        log_err(f"Erro ao recarregar currículo: {exc}")
                continue

            # ── Nova GROQ_API_KEY salva em Configurações — reconstrói cliente ──
            # _api_ok já foi atualizado por _configure_groq_key via set_api_ok(True)
            # Só precisamos reconstruir o objeto Groq com a nova key
            if menu_result == "__reload_client__":
                new_key = os.environ.get("GROQ_API_KEY", "")
                if new_key:
                    try:
                        client = Groq(api_key=new_key)
                        # _api_ok já é True (set por _configure_groq_key) — não re-testa
                        log_ok(f"Cliente Groq atualizado  —  IA {_G}online{_RST}")
                    except Exception as exc:
                        log_err(f"Erro ao reinicializar cliente Groq: {exc}")
                        set_api_ok(False)
                else:
                    log_warn("Nenhuma chave configurada.")
                    set_api_ok(False)
                input(f"\n  {_DIM}ENTER para continuar...{_RST}")
                continue

            # ── Reconexão explícita pedida pelo usuário ────────────────────────
            if menu_result == "__reconnect__":
                log_info("Tentando reconectar com a API Groq...")
                rebuilt = extract_full_profile(client, resume_text, resume_hash, rdb)
                if rebuilt:
                    profile      = rebuilt
                    profile_json = json.dumps(
                        _profile_to_ai_summary(profile), ensure_ascii=False
                    )
                    menu_hints   = _profile_to_menu_hints(profile)
                    # check_groq_api foi chamado internamente → _api_ok atualizado
                    log_ok(f"{_G}{_BD}API recuperada! Todas as funcionalidades disponíveis.{_RST}")
                    input(f"\n  {_DIM}ENTER para continuar...{_RST}")
                else:
                    log_warn("API ainda indisponível. Aguarde o rate limit expirar e tente novamente.")
                    input(f"\n  {_DIM}ENTER para voltar ao menu...{_RST}")
                continue

            # Guarda: API indisponível + tentativa via flags
            if not get_api_ok() and menu_result is None:
                log_warn("API Groq indisponível — não é possível iniciar nova busca agora.")
                log_info("Use '🔄 Tentar reconectar' no menu quando o rate limit expirar.")
                input(f"\n  {_DIM}ENTER para voltar ao menu...{_RST}")
                continue

            # ── Configura a busca ─────────────────────────────────────────────
            # Detecta edição de preset (traz defaults) vs uso direto vs nova busca
            edit_defaults: Optional[dict] = None
            if isinstance(menu_result, dict) and "__edit_from__" in menu_result:
                edit_defaults = menu_result["__edit_from__"]
                menu_result   = None   # trata como nova busca, mas com defaults

            auth_cookies: dict = {}

            if isinstance(menu_result, dict):
                # Preset carregado para usar diretamente
                preset   = menu_result
                query    = preset["query"]
                sources  = preset["sources"]
                prefs    = preset.get("prefs", {})
                location = preset.get("location", args.location)
                log_ok(f"Preset: {_BD}{preset['name']}{_RST}")
            else:
                # Nova busca (ou edição de preset com defaults).
                # Usa hints do perfil já cacheado — sem chamada extra à API.
                ai_hints = menu_hints

                src_defaults  = edit_defaults["sources"] if edit_defaults else None
                pref_defaults = edit_defaults.get("prefs") if edit_defaults else None

                if args.source:
                    # Via flag CLI: aplica filtro de região quando "all"
                    base_sources = (
                        list(SOURCES.keys()) if args.source == "all" else [args.source]
                    )
                    prefs   = select_preferences(
                        suggested_english=ai_hints.get("english_level", "B1"),
                        defaults=pref_defaults,
                    )
                    sources = _prefs_to_sources(base_sources, prefs)
                else:
                    # Seleção interativa: usuário escolhe explicitamente — não filtra depois
                    prefs   = select_preferences(
                        suggested_english=ai_hints.get("english_level", "B1"),
                        defaults=pref_defaults,
                    )
                    sources = select_sources(defaults=src_defaults)
                    auth_cookies = authenticate_sources(sources, rdb)

                if args.query:
                    query = args.query
                else:
                    query = select_query(
                        suggestions=ai_hints,
                        client=client,
                        prefs=prefs,
                        sources=sources,
                    )
                    # Injeta as tecnologias selecionadas em prefs para o avaliador usar
                    if _global_rdb:
                        try:
                            _last_stack = _global_rdb.load_setting("last_query_stack")
                            _last_techs = _global_rdb.load_setting("last_query_techs") or {}
                            if _last_stack and _last_stack in _last_techs:
                                prefs["search_techs"] = _last_techs[_last_stack]
                        except Exception:
                            pass

                location = args.location

                # Salva preset com a query RAW (como o usuário digitou)
                mod_s  = {"remoto": "Remote", "presencial": "Presencial",
                          "hibrido": "Híbrido", "todos": ""}.get(prefs.get("modality",""), "")
                cont_s = {"pj": "PJ", "clt": "CLT", "autonomo": "Autônomo",
                          "todos": ""}.get(prefs.get("contract",""), "")
                name   = " · ".join(p for p in [query[:40], mod_s, cont_s] if p)
                save_preset({
                    "id":         session_id,
                    "name":       name,
                    "created_at": datetime.now().isoformat(),
                    "query":      query,   # query limpa, sem termos injetados
                    "location":   location,
                    "sources":    sources,
                    "prefs":      prefs,
                })

            # Enriquece a query APENAS para o scraping — nunca salva a versão enriquecida
            search_query = _enrich_query(query, prefs)

            log_ok(f"Query:  {_BD}{query}{_RST}")
            if search_query != query:
                log_info(f"Query de busca (enriquecida): {_DIM}{search_query}{_RST}")
            log_ok(f"Fontes: {', '.join(sources)}")

            # ── Sessão MongoDB para esta busca ────────────────────────────────
            # session_rdb é dedicado a esta execução: fila, vagas avaliadas e stats.
            # rdb (admin) mantém o estado global: seen, decisions, resume hash.
            session_rdb = MongoManager(session_id)
            session_rdb.save_run_info({
                "query":      query,          # salva query limpa para histórico
                "location":   location,
                "sources":    sources,
                "prefs":      prefs,
                "max_pages":  args.max_pages,
                "started_at": datetime.now().isoformat(),
            })

            # ── Scraping ──────────────────────────────────────────────────────
            recency       = prefs.get("recency", "7d")
            total_scraped = scrape_sources(
                search_query, location, sources, args.max_pages,
                session_rdb, args.show_browser, recency, auth_cookies,
            )

            # Só volta ao menu automaticamente se não coletou nada E o usuário
            # não interrompeu manualmente (stop manual sempre segue o fluxo normal)
            if total_scraped == 0 and not _scrape_aborted():
                log_warn("Nenhuma vaga nova coletada. Voltando ao menu...")
                input(f"\n  {_DIM}ENTER para continuar...{_RST}")
                continue

            # ── Avaliação ─────────────────────────────────────────────────────
            if not get_api_ok():
                log_err("API Groq inválida — avaliação das vagas cancelada.")
                log_warn("Configure uma GROQ_API_KEY válida em Configurações → IA e tente novamente.")
                input(f"\n  {_DIM}ENTER para voltar ao menu...{_RST}")
                continue
            process_queue(client, profile_json, session_rdb, prefs=prefs, batch_size=10)

            # ── Resultados — todas as vagas avaliadas, ordenadas por score ────
            all_evaluated = session_rdb.get_all_evaluated_jobs()
            matched       = [j for j in all_evaluated if j.get("score", 0) >= MIN_MATCH_SCORE]
            print_results(matched)

            if all_evaluated:
                log_ok(f"Sessão: {_BD}{session_id}{_RST}")

            # ── Revisão interativa — mostra todas, não só ≥80% ───────────────
            auto_apply_jobs: list[dict] = []
            if all_evaluated:
                auto_apply_jobs = review_jobs(all_evaluated, rdb)

            # ── Candidatura automática (vagas marcadas com [P]) ───────────────
            if auto_apply_jobs:
                auto_apply_session(auto_apply_jobs, profile, client, rdb, args.resume)

            # ── Resumo da busca ────────────────────────────────────────────────
            clr()
            section("Busca concluída")
            stats  = session_rdb.get_stats()
            log_ok(f"Avaliadas:       {stats.get('evaluated', 0)}")
            log_ok(f"Com match ≥ {MIN_MATCH_SCORE}%:  {_BD}{stats.get('matched', 0)}{_RST}")
            if stats.get("errors", 0):
                log_warn(f"Erros:           {stats.get('errors', 0)}")

            input(f"\n  {_DIM}ENTER para voltar ao menu...{_RST}")

        except UserAbort:
            # ESC / Ctrl+C dentro de um sub-menu → volta ao menu principal
            log_info("Voltando ao menu principal...")

        except KeyboardInterrupt:
            # Ctrl+C no menu principal ou durante scraping → encerra
            break

        except Exception as exc:
            # Qualquer outro erro → log e continua (nunca sai por erro)
            log_err(f"Erro durante execução: {type(exc).__name__}: {str(exc)[:100]}")
            log_info("Tentando recuperar...")
            import traceback
            log_info(f"Stack: {traceback.format_exc()[:300]}")
            input(f"\n  {_DIM}ENTER para tentar novamente...{_RST}")
            continue

    # ── Saída limpa ────────────────────────────────────────────────────────────
    print(f"\n\n  {CHECK} Até logo!\n")


# ──────────────────────────────────────────────────────────────────────────────
# Pré-verificação antes de executar
# ──────────────────────────────────────────────────────────────────────────────

def _load_dotenv(env_path: str = ".env") -> None:
    """
    Carrega variáveis do arquivo .env para os.environ.

    Prioridade:
      - Chaves gerenciadas pelo app (GROQ_API_KEY, MODEL, RESUME_PATH):
        .env SEMPRE vence — sobrescreve qualquer valor do shell.
      - Demais chaves (MONGO_*, LINKEDIN_*, etc.):
        .env só define se ainda não estiver no ambiente.

    Isso evita que uma GROQ_API_KEY antiga ou inválida no shell
    sobreponha a chave válida salva pelo app no .env.
    """
    # Chaves que o app gerencia — .env é a fonte de verdade
    _APP_KEYS = {"GROQ_API_KEY", "MODEL", "RESUME_PATH"}

    p = Path(env_path)
    if not p.exists():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if not key:
                    continue
                # Chaves gerenciadas: sobrescreve sempre
                # Demais: respeita o valor do shell se já definido
                if key in _APP_KEYS or key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass  # falha silenciosa — não deve impedir a execução

def _save_to_dotenv(key: str, value: str, env_path: str = ".env") -> None:
    """Persiste key=value no .env e aplica ao processo atual."""
    p = Path(env_path)
    lines = []
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}\n")
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.environ[key] = value


def _remove_from_dotenv(key: str, env_path: str = ".env") -> None:
    """Remove uma variável do .env sem apagar o arquivo."""
    p = Path(env_path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.environ.pop(key, None)


def _check_requirements() -> dict:
    """Verifica quais requisitos estão faltando"""
    checks = {
        "docker": False,
        "mongo_running": False,
        "env_exists": False,
        "groq_key": False,
        "venv": False,
        "requirements_installed": False,
    }

    # Verifica venv PRIMEIRO
    venv_path = Path("venv")
    if venv_path.exists():
        checks["venv"] = True
    else:
        # Se não tem venv, requirements não podem estar instalados
        return checks

    # Verifica Docker (usa CLI, não SDK Python)
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=5
        )
        checks["docker"] = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        checks["docker"] = False

    # Verifica requirements instalados (essencial!)
    required_modules = [
        "questionary",
        "playwright",
        "pdfplumber",
        "pymongo",
        "groq",
        "bs4",
        "curl_cffi",
    ]

    missing_modules = []
    for module in required_modules:
        try:
            __import__(module)
        except ImportError:
            missing_modules.append(module)

    if not missing_modules:
        checks["requirements_installed"] = True
    else:
        # Se faltam módulos, tenta instalar agora mesmo
        log_warn(f"Módulos faltando: {', '.join(missing_modules)}")
        return checks

    # Verifica MongoDB rodando
    try:
        from pymongo import MongoClient
        client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
        client.admin.command('ping')
        checks["mongo_running"] = True
        client.close()
    except:
        pass

    # Verifica .env
    if Path(".env").exists():
        checks["env_exists"] = True
        # Verifica GROQ_API_KEY
        with open(".env") as f:
            for line in f:
                if line.startswith("GROQ_API_KEY=") and "sk_" in line:
                    checks["groq_key"] = True
                    break

    return checks


def _configure_groq_key() -> bool:
    """
    Solicita ao usuário a GROQ_API_KEY, valida o formato,
    salva no .env e aplica ao processo atual.
    Retorna True se configurada com sucesso.
    """
    print(f"\n  {_CY}{_BD}Configurar GROQ_API_KEY{_RST}")
    print(f"  {_DIM}Obtenha sua chave em: https://console.groq.com{_RST}\n")

    while True:
        key = input("  Cole a chave (gsk_...): ").strip()

        if not key:
            print(f"  {_Y}Cancelado.{_RST}")
            return False

        if not key.startswith("gsk_") or len(key) < 20:
            print(f"  {_R}Formato inválido — a chave deve começar com 'gsk_' e ter ao menos 20 caracteres.{_RST}")
            retry = input("  Tentar novamente? (s/n): ").strip().lower()
            if retry != "s":
                return False
            continue

        # Testa a chave antes de salvar
        print(f"\n  {_DIM}Testando chave na API Groq...{_RST}", end="", flush=True)
        _key_valid = False
        try:
            from groq import Groq as _Groq
            _test_client = _Groq(api_key=key)
            _test_client.chat.completions.create(
                model=_ACTIVE_MODEL,
                max_tokens=5,
                messages=[{"role": "user", "content": "ping"}],
            )
            print(f"  {_G}OK{_RST}")
            _key_valid = True
        except Exception as _key_exc:
            _err = str(_key_exc)
            _is_auth  = "401" in _err or "invalid_api_key" in _err or "Invalid API Key" in _err
            _is_rate  = "429" in _err or "rate" in _err.lower()
            _is_net   = "connection" in _err.lower() or "timeout" in _err.lower()

            if _is_auth:
                print(f"  {_R}INVÁLIDA{_RST}")
                print(f"  {_R}✗ Chave inválida ou expirada. Verifique em console.groq.com{_RST}")
                retry = input("  Tentar novamente com outra chave? (s/n): ").strip().lower()
                if retry == "s":
                    continue
                return False
            elif _is_rate:
                # Rate limit = chave válida, só throttled
                print(f"  {_G}OK{_RST}  {_DIM}(rate limit — chave válida){_RST}")
                _key_valid = True
            elif _is_net:
                print(f"  {_Y}SEM REDE{_RST}")
                print(f"  {_DIM}(Chave será salva — rede pode ser temporária){_RST}")
                _key_valid = True   # assume válida sem confirmação
            else:
                print(f"  {_Y}AVISO{_RST}  {_DIM}{_err[:80]}{_RST}")
                _key_valid = True   # outros erros: salva mesmo assim

        # ── Aplica ao processo atual imediatamente ────────────────────────────
        os.environ["GROQ_API_KEY"] = key
        set_api_ok(True)   # ← único ponto de verdade: chave já foi validada acima

        # ── Salva/atualiza no .env (fonte única) ─────────────────────────────
        _save_to_dotenv("GROQ_API_KEY", key)

        # ── Atualiza timestamp no MongoDB — próximo startup usa cache ─────────
        if _global_rdb:
            try:
                _global_rdb.set_api_status("ok", "chave configurada manualmente")
            except Exception:
                pass

        print(f"\n  {CHECK} Chave validada e salva! IA agora está {_G}online{_RST}.\n")
        return True


def _show_precheck_menu():
    """Mostra menu de pré-verificação com opções contextuais."""
    print(f"\n{_BD}{_B}{'─' * 80}{_RST}")
    print(f"{_BD}📋  Verificação de Requisitos{_RST}")
    print(f"{_BD}{_B}{'─' * 80}{_RST}\n")

    checks = _check_requirements()

    print(f"  {_G}✓ Docker{_RST}"              if checks["docker"]                else f"  {_R}✗ Docker{_RST}")
    print(f"  {_G}✓ MongoDB rodando{_RST}"      if checks["mongo_running"]         else f"  {_Y}⚠ MongoDB não detectado{_RST}")
    print(f"  {_G}✓ .env configurado{_RST}"     if checks["env_exists"]            else f"  {_Y}⚠ .env ausente{_RST}")
    print(f"  {_G}✓ GROQ_API_KEY{_RST}"         if checks["groq_key"]              else f"  {_R}✗ GROQ_API_KEY não configurada{_RST}")
    print(f"  {_G}✓ Python venv{_RST}"          if checks["venv"]                  else f"  {_R}✗ Python venv{_RST}")
    print(f"  {_G}✓ Dependências Python{_RST}"  if checks["requirements_installed"] else f"  {_R}✗ Dependências{_RST}")

    # Falta venv ou deps → setup obrigatório primeiro
    if not checks["venv"] or not checks["requirements_installed"]:
        print(f"\n  {_R}Faltam dependências críticas!{_RST}")
        print(f"  {_DIM}Vou abrir o setup para instalar...{_RST}\n")
        return "setup"

    # Monta menu dinâmico de acordo com o que está faltando
    options: list[tuple[str, str]] = []   # (label, action)

    if not checks["groq_key"]:
        options.append(("🔑  Inserir GROQ_API_KEY", "set_key"))

    if not checks["docker"] or not checks["mongo_running"] or not checks["env_exists"]:
        options.append(("🔧  Instalar/configurar dependências", "setup"))

    options.append(("▶   Executar mesmo assim", "run"))

    if checks["groq_key"]:
        options.append(("🔄  Trocar GROQ_API_KEY", "set_key"))

    options.append(("✖   Sair", "exit"))

    # Se tudo OK, executa direto
    all_ok = all(checks.values())
    if all_ok:
        print(f"\n  {_G}{_BD}✓ Tudo pronto! Executando...{_RST}\n")
        return "run"

    print(f"\n  {_Y}Alguns itens estão faltando ou podem ser configurados.{_RST}\n")
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}. {label}")

    while True:
        raw = input(f"\n  Escolha (1-{len(options)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            _, action = options[int(raw) - 1]
            break
        print(f"  {_Y}Opção inválida.{_RST}")

    if action == "set_key":
        _configure_groq_key()
        return "precheck"   # volta ao loop de precheck para rever status

    return action

def _pick_resume_from_folder() -> Optional[str]:
    """Pede uma pasta ao usuário e lista os PDFs encontrados para escolher."""
    while True:
        folder = questionary.text(
            "Pasta onde estão seus PDFs (ex: ~/Downloads):",
            style=Q_STYLE,
        ).ask()

        if not folder:
            return None

        folder_path = Path(folder.strip().strip("'\"")).expanduser().resolve()

        if not folder_path.exists() or not folder_path.is_dir():
            log_err(f"Pasta não encontrada: {folder_path}")
            continue

        pdf_files = sorted(folder_path.glob("*.pdf"))

        if not pdf_files:
            log_err(f"Nenhum PDF encontrado em: {folder_path}")
            continue

        choices = [
            questionary.Choice(title=p.name, value=str(p))
            for p in pdf_files
        ]
        choices.append(questionary.Choice(title="← Informar outra pasta", value="__back__"))

        picked = questionary.select(
            f"PDFs em {folder_path.name}/:",
            choices=choices,
            style=Q_STYLE,
        ).ask()

        if picked is None or picked == "__back__":
            continue

        return picked


def _get_resume_path() -> Optional[str]:
    """
    Obtém o caminho do currículo PDF:
      1. Via argumento --resume
      2. Último usado (salvo no MongoDB/config) — usa automaticamente
      3. Primeira vez: usuário informa pasta e escolhe o PDF
    Para trocar o currículo use Configurações → Trocar currículo.
    """
    # 1. Via argumento de linha de comando
    for i, arg in enumerate(sys.argv):
        if arg == "--resume" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]

    # 2. RESUME_PATH do .env (fonte única para config)
    last_resume = os.environ.get("RESUME_PATH", "").strip()

    if last_resume and Path(last_resume).exists():
        return last_resume

    # 3. Primeira vez — pede a pasta
    return _pick_resume_from_folder()

if __name__ == "__main__":
    try:
        # ── Carrega .env antes de qualquer verificação ─────────────────────────
        _load_dotenv()

        # ── Pré-check: loop até o usuário optar por executar ou sair ──────────
        while True:
            action = _show_precheck_menu()

            if action == "exit":
                print(f"\n  {CHECK} Até logo!\n")
                sys.exit(0)

            elif action == "setup":
                if Path("setup.py").exists():
                    print(f"\n  {_CY}Iniciando setup...{_RST}\n")
                    subprocess.run([sys.executable, "setup.py"], check=False)
                    print(f"\n  {_G}Setup concluído! Verificando requisitos novamente...{_RST}\n")
                    time.sleep(1)
                else:
                    log_err("setup.py não encontrado")
                # Volta ao topo do loop para rever o estado
                continue

            elif action in ("precheck", "set_key"):
                # Apenas reexibe o menu atualizado
                continue

            else:
                # action == "run" — sai do loop e executa
                break

        # ── Obtém caminho do currículo ─────────────────────────────────────────
        resume_path = _get_resume_path()
        if not resume_path or not Path(resume_path).exists():
            log_err("Currículo não encontrado")
            sys.exit(1)

        # Salva RESUME_PATH no .env (fonte única)
        _save_to_dotenv("RESUME_PATH", resume_path)

        # Injeta --resume nos argumentos se não estiver lá
        if "--resume" not in sys.argv:
            sys.argv.extend(["--resume", resume_path])

        # Executa o projeto
        main()

    except KeyboardInterrupt:
        print(f"\n\n  {CHECK} Até logo!\n")
