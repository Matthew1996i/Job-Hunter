# Job Hunter

Busca vagas automaticamente em múltiplas plataformas com base no seu currículo PDF, avalia compatibilidade via IA (Groq) e apresenta os resultados num painel interativo de revisão.

---

## Pré-requisitos

| Requisito | Versão mínima | Como obter |
|---|---|---|
| Python | 3.10+ | https://python.org/downloads |
| Docker Desktop | qualquer | https://docker.com/products/docker-desktop |
| Conta Groq | — | https://console.groq.com |

> **Windows:** instale Python e Docker Desktop manualmente antes de continuar.

---

## Instalação

### 1. Clone o repositório

```bash
git clone <url-do-repositorio>
cd "Job Hunter"
```

### 2. Execute o setup automático

```bash
python3 setup.py
```

O script detecta seu sistema operacional e faz tudo automaticamente:

- Cria o virtual environment Python (`venv/`)
- Instala as dependências do `requirements.txt`
- Cria o arquivo `.env` a partir do `.env.example`
- Sobe o MongoDB via Docker Compose
- Valida a conexão com o banco

> Em Linux, pode ser necessário adicionar seu usuário ao grupo docker antes:
> ```bash
> sudo usermod -aG docker $USER && newgrp docker
> ```

### 3. Obtenha uma chave de API do Groq

1. Acesse https://console.groq.com
2. Crie uma conta e gere uma API Key
3. A chave será solicitada na primeira execução, ou adicione manualmente ao `.env`:

```env
GROQ_API_KEY=gsk_...
```

---

## Execução

### Forma padrão

```bash
python3 run.py --resume caminho/para/curriculo.pdf
```

### Modo verbose (logs detalhados)

```bash
python3 run.py --resume caminho/curriculo.pdf -v
```

### Atalhos por SO

| Sistema | Comando |
|---|---|
| Linux / macOS | `./run.sh --resume curriculo.pdf` |
| Windows | `run.bat --resume curriculo.pdf` |
| Qualquer SO | `python3 run.py --resume curriculo.pdf` |

> **Dica:** o caminho do currículo pode ser salvo nas configurações do app — depois da primeira execução, você pode configurar `RESUME_PATH` no `.env` ou via menu de configurações.

---

## Variáveis de ambiente (`.env`)

Copie `.env.example` para `.env` e preencha:

```env
# Obrigatório
GROQ_API_KEY=gsk_...

# Opcional — caminho padrão do currículo
RESUME_PATH=/caminho/curriculo.pdf

# Opcional — MongoDB (padrão: localhost:27017)
MONGO_HOST=localhost
MONGO_PORT=27017
MONGO_DB=job_hunter

# Opcional — LinkedIn Feed (cookie de autenticação)
LINKEDIN_LI_AT=
```

---

## Plataformas de busca disponíveis

| Grupo | Plataformas |
|---|---|
| Brasil — Geral | Indeed BR, InfoJobs, Catho, Gupy, Vagas.com.br |
| Brasil — Tech | ProgramaThor, GeekHunter, Revelo, Impulso, Remotar |
| Freelance | Upwork, Workana, 99Freelas |
| Internacional | Indeed USA, Glassdoor, ZipRecruiter, Careerjet BR, Jora, RemoteOK, Himalayas, We Work Remotely, Turing, Toptal |
| LinkedIn | LinkedIn Brasil *(filtra por geoId BR)*, LinkedIn Global, LinkedIn Jobs, LinkedIn Feed* |

> *LinkedIn Feed requer `LINKEDIN_LI_AT` configurado (cookie de sessão).

---

## Fluxo de uma busca

```
1. Menu principal → Nova busca
2. Preferências   → localização, modalidade, contrato, inglês, recência
3. Fontes         → seleciona plataformas
4. Stack / Área   → ex: Frontend, Backend, Mobile...
5. Tecnologias    → ex: React, TypeScript, Node.js...
6. Query          → IA gera sugestão; edite ou confirme
7. Scraping       → coleta vagas nas plataformas selecionadas
8. Avaliação      → IA analisa compatibilidade currículo × vaga
9. Revisão        → navegue pelos cards e decida cada vaga
```

### Atalhos na revisão de vagas

| Tecla | Ação |
|---|---|
| `A` | Aceitar (tenho interesse) |
| `R` | Recusar (não aparece mais em buscas futuras) |
| `C` | Já me candidatei |
| `P` | Fila de candidatura automática |
| `O` | Abrir vaga no navegador |
| `V` | Expandir / recolher descrição completa |
| `N` / `→` | Próxima vaga |
| `B` / `←` | Vaga anterior |
| `L` | Listar todas as vagas e pular para qualquer uma |
| `Q` | Encerrar revisão |

> A navegação é em carrossel: na última vaga, `N` volta para a primeira.

---

## Presets

Cada busca é salva automaticamente como preset. No menu principal você pode:

- **Usar preset** — repete uma busca anterior com um clique
- **Editar preset** — ajusta qualquer campo antes de usar
- **Excluir preset** — remove buscas antigas

---

## Resetar dados

```bash
# Apaga vagas, fila, histórico e decisões (preserva API key e presets)
./reset.sh

# Apaga tudo, inclusive .env e presets
./reset.sh --tudo
```

---

## Gerenciamento do banco (Docker)

```bash
# Iniciar MongoDB
docker-compose up -d

# Verificar status
docker-compose ps

# Ver logs
docker-compose logs -f mongodb

# Parar
docker-compose down
```

---

## Troubleshooting

### Docker não inicia

```bash
# macOS: abra o Docker.app
# Linux:
sudo systemctl start docker

# Verifique:
docker ps
```

### MongoDB sem conexão

```bash
docker-compose down
docker-compose up -d
docker-compose logs mongodb
```

### Dependências Python faltando

```bash
python3 setup.py   # reinstala tudo
```

### Permissão negada em run.sh

```bash
chmod +x run.sh reset.sh
```

### Reinstalar do zero

```bash
rm -rf venv .env
python3 setup.py
```

---

## Estrutura do projeto

```
Job Hunter/
├── job_hunter.py        # Aplicação principal
├── run.py               # Wrapper de execução (cross-platform)
├── run.sh               # Atalho Linux/macOS
├── run.bat              # Atalho Windows
├── setup.py             # Setup automático
├── reset.sh             # Reset de dados / configuração
├── docker-compose.yml   # MongoDB via Docker
├── requirements.txt     # Dependências Python
├── .env.example         # Template de variáveis
├── .env                 # Suas variáveis (gerado no setup)
└── venv/                # Virtual environment (gerado no setup)
```
