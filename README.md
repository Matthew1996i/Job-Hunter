# 🎯 Job Hunter - Guia de Setup

## 🚀 Início Rápido (Uma linha!)

```bash
python3 setup.py
```

Isso vai:

- ✅ Detectar seu SO (Linux, macOS, Windows)
- ✅ Instalar todas as dependências automaticamente
- ✅ Configurar variáveis de ambiente (.env)
- ✅ Criar virtual environment Python
- ✅ Instalar dependências Python (pip)
- ✅ Subir Docker Compose
- ✅ Executar o projeto

---

## 📋 Opções de Menu

Ao executar `python3 setup.py`, você terá 4 opções:

### **1️⃣ Setup Completo**

Instala TUDO e executa o projeto de uma vez:

```bash
python3 setup.py
> Escolha (1-4): 1
```

### **2️⃣ Apenas Instalar**

Instala dependências sem executar:

```bash
python3 setup.py
> Escolha (1-4): 2
```

Use isso quando tiver atualizado `requirements.txt`

### **3️⃣ Apenas Executar**

Executa o projeto (dependências já instaladas):

```bash
python3 setup.py
> Escolha (1-4): 3
```

### **4️⃣ Apenas Docker**

Sobe o Docker Compose:

```bash
python3 setup.py
> Escolha (1-4): 4
```

---

## 🔧 Pré-requisitos (Automático!)

O script detecta e instala automaticamente:

### **macOS**

```bash
# Se não tiver Homebrew:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# O resto é automático:
# - Docker Desktop (via Homebrew)
# - Python 3 (via Homebrew)
```

### **Linux (Ubuntu/Debian)**

```bash
# Instalar sudo se necessário (geralmente já vem)
# O rest é automático:
# - Docker & Docker Compose
# - Python 3 & pip
# - Todas as dependências do sistema
```

### **Linux (Fedora/CentOS/RHEL)**

Mesmo que Ubuntu, mas com `dnf` ao invés de `apt-get`

### **Linux (Arch)**

Mesmo que Ubuntu, mas com `pacman` ao invés de `apt-get`

### **Windows**

Você terá que instalar manualmente:

1. [Docker Desktop](https://www.docker.com/products/docker-desktop)
2. [Python 3](https://www.python.org/downloads/)

Depois execute `python3 setup.py` novamente

---

## 📝 Variáveis de Ambiente

O script cria `.env` automaticamente com:

```env
GROQ_API_KEY=sk_live_  # ← Preencha com sua chave!
RESUME_PATH=/Documents/...
```

### Obter GROQ_API_KEY:

1. Acesse https://console.groq.com
2. Crie uma conta ou faça login
3. Gere uma nova API Key
4. Cole no setup quando solicitar

---

## 🐳 Docker Compose

O script automáticamente:

- ✅ Valida instalação do Docker
- ✅ Sobe containers (`docker-compose up -d`)
- ✅ Aguarda MongoDB estar pronto
- ✅ Valida conexão com banco

```bash
# Verificar status:
docker-compose ps

# Ver logs:
docker-compose logs -f

# Parar:
docker-compose down
```

---

## 🐍 Python Virtual Environment

Criado em `venv/`:

```bash
# Ativar manualmente (se preciso):
# Linux/macOS:
source venv/bin/activate

# Windows:
venv\Scripts\activate
```

---

## 🎮 Executar Manualmente

Se preferir rodar sem o script:

```bash
# 1. Ativar venv
source venv/bin/activate  # ou venv\Scripts\activate no Windows

# 2. Subir Docker
docker-compose up -d

# 3. Aguardar MongoDB
sleep 5

# 4. Executar
python job_hunter.py --resume seu_curriculo.pdf
```

---

## 🛠️ Troubleshooting

### Docker não inicia

```bash
# Verifique se Docker daemon está rodando:
docker ps

# Se não funcionar, inicie Docker:
# macOS: Abra Docker.app
# Linux: sudo systemctl start docker
# Windows: Abra Docker Desktop
```

### Módulo Python não encontrado

```bash
# Reinstale dependências:
python3 setup.py
> Escolha (1-4): 2
```

### MongoDB não conecta

```bash
# Verifique logs do container:
docker-compose logs mongodb

# Reinicie:
docker-compose down
docker-compose up -d
```

### Permissão negada (Linux)

```bash
# Adicione seu usuário ao grupo docker:
sudo usermod -aG docker $USER
newgrp docker

# Reinicie o terminal
```

---

## 📊 Estrutura após setup

```
Job Hunter/
├── venv/                    # Virtual environment
├── .env                     # Variáveis (criado automaticamente)
├── .env.example             # Template
├── job_hunter.py            # Código principal
├── requirements.txt         # Dependências Python
├── docker-compose.yml       # Config Docker
├── setup.py                 # Este script
└── SETUP.md                 # Este arquivo
```

---

## 🚨 Notas Importantes

1. **GROQ_API_KEY é obrigatória** para usar IA
2. **Docker precisa estar rodando** para usar MongoDB
3. **Python 3.8+** é necessário
4. **Conexão com internet** para baixar dependências primeira vez

---

## 📞 Suporte

Se encontrar problemas:

1. Verifique os logs:

   ```bash
   docker-compose logs
   ```

2. Teste componentes individualmente:

   ```bash
   python3 --version
   docker --version
   docker-compose --version
   ```

3. Reinstale do zero:
   ```bash
   rm -rf venv .env
   python3 setup.py
   ```

---

Pronto! 🚀 Agora é só `python3 setup.py` e está tudo funcionando!

# 🚀 Job Hunter - Como Executar

## ⚡ Forma Mais Rápida

### **Linux/macOS:**

```bash
./run.sh
```

### **Windows:**

```bash
run.bat
```

### **Qualquer SO:**

```bash
python3 run.py
```

---

## 📋 Primeira Execução (Setup Automático)

Na primeira vez, o programa verifica o que falta:

```bash
$ ./run.sh

┌──────────────────────────────────────────────┐
│ 📋  Verificação de Requisitos                │
└──────────────────────────────────────────────┘

  ✓ Docker
  ✗ MongoDB rodando
  ✓ .env configurado
  ✓ GROQ_API_KEY
  ✓ Python venv
  ✓ Dependências Python

  Alguns requisitos estão faltando.

  O que deseja fazer?

  1. Instalar/configurar o que falta automaticamente
  2. Executar mesmo assim (com funcionalidades limitadas)
  3. Sair

  Escolha (1-3): 1
```

Escolha **1** e tudo será instalado automaticamente!

---

## ✅ Execuções Posteriores (Automático!)

```bash
$ ./run.sh

┌──────────────────────────────────────────────┐
│ 📋  Verificação de Requisitos                │
└──────────────────────────────────────────────┘

  ✓ Docker
  ✓ MongoDB rodando
  ✓ .env configurado
  ✓ GROQ_API_KEY
  ✓ Python venv
  ✓ Dependências Python

  ✓ Tudo pronto! Executando projeto...

  Qual currículo deseja usar?

  1. curriculo.pdf ← usado por último
  2. cv_english.pdf
  3. Outro arquivo

  Escolha (1-3): 1

→ Executa com curriculo.pdf
```

Pronto! **Sem mais setup, sem argumentos, tudo automático!**

---

## 🎯 Opções de Execução

### **Executar com currículo diferente:**

```bash
./run.sh --resume outro_curriculo.pdf
```

### **Executar com query customizada:**

```bash
./run.sh --query "React Developer" --location "São Paulo"
```

### **Ver todas as opções:**

```bash
./run.sh --help
```

---

## 🔧 Métodos Alternativos

### **1️⃣ Via Python direto:**

```bash
python3 job_hunter.py
```

### **2️⃣ Via wrapper Python (funciona em qualquer SO):**

```bash
python3 run.py
```

### **3️⃣ Via shell script (Linux/macOS):**

```bash
./run.sh
```

### **4️⃣ Via batch script (Windows):**

```bash
run.bat
```

---

## 📱 Inicialização Automática

Se quer executar sem interação (vai pedir só o currículo na primeira vez):

```bash
# Aceita tudo com defaults
./run.sh

# Ou com currículo já definido
./run.sh --resume curriculo.pdf
```

---

## 🆘 Troubleshooting

### **"python not found"**

```bash
# Use python3 explicitamente:
python3 job_hunter.py

# Ou use o wrapper:
python3 run.py
```

### **"permission denied" no run.sh**

```bash
# Torne executável:
chmod +x run.sh

# Depois execute:
./run.sh
```

### **Docker não inicia**

```bash
# Inicie manualmente:
docker-compose up -d

# Depois execute:
./run.sh
```

---

## 💾 Configuração Persistente

O programa salva automaticamente:

- ✅ Último currículo usado
- ✅ Modelo de IA preferido
- ✅ Preferências de busca

Na próxima execução, usa os mesmos valores!

---

## 🎓 Resumo

| Situação                     | Comando                         |
| ---------------------------- | ------------------------------- |
| **Primeira vez**             | `./run.sh` → escolhe opção 1    |
| **Próximas vezes**           | `./run.sh` → automático!        |
| **Trocar currículo**         | `./run.sh` → escolhe outro      |
| **Com currículo específico** | `./run.sh --resume arquivo.pdf` |
| **Windows**                  | `run.bat` (mesmo fluxo)         |

---

**Pronto! Agora é realmente só executar e pronto!** 🚀
