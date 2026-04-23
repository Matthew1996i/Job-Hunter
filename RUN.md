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

| Situação | Comando |
|----------|---------|
| **Primeira vez** | `./run.sh` → escolhe opção 1 |
| **Próximas vezes** | `./run.sh` → automático! |
| **Trocar currículo** | `./run.sh` → escolhe outro |
| **Com currículo específico** | `./run.sh --resume arquivo.pdf` |
| **Windows** | `run.bat` (mesmo fluxo) |

---

**Pronto! Agora é realmente só executar e pronto!** 🚀
